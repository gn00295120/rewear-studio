"""Import the built wheel outside the source tree and check its offline API/assets."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


CHECK = r"""
import csv, io, json
from pathlib import Path
from fastapi.testclient import TestClient
from PIL import Image
import wardrobe.app
from wardrobe.identity_schemas import normalize_identity
from wardrobe.product_evidence import ProductFetchError
from wardrobe.reconstruction_sources import ReferenceManifest, ReconstructionSourceBundle
from wardrobe.schemas import ReconstructionRequest

assert Path(wardrobe.app.__file__).resolve().is_relative_to(Path.cwd())
app = wardrobe.app.app
with TestClient(app) as client:
    health = client.get('/api/health').json()
    assert health['discovery']['available'] is False
    assert health['discovery']['markets'] == ['TW', 'global']
    assert health['discovery']['image_search']['enabled'] is False
    assert health['discovery']['image_search']['configured'] is False
    assert health['discovery']['image_search']['provider'] is None
    assert health['reconstruction']['available'] is False
    refreshed = client.get('/api/health?refresh=true')
    assert refreshed.status_code == 200 and refreshed.json()['local']['available'] is False
    assert client.get('/').status_code == 200
    script = client.get('/static/app.js').text
    assert '品牌與購買管道' in script and 'AI 重建圖' in script
    assert 'AI 預覽' in script and 'search_attempts' in script
    assert 'data-reconstruction-reference' in script and 'reference-history' in script
    assert ReconstructionRequest(reference_item_ids=['a' * 32]).reference_item_ids == ['a' * 32]
    assert '查找購買選項' in client.get('/').text
    assert '改用 Meta 雲端分析' in client.get('/').text
    buffer = io.BytesIO()
    Image.new('RGB', (32, 32), 'white').save(buffer, 'PNG')
    offline = client.post('/api/jobs', files={'file': ('shirt.png', buffer.getvalue(), 'image/png')},
                          data={'provider': 'local', 'mode': 'single'})
    assert offline.status_code == 503 and 'Ollama' in offline.json()['detail']
    assert client.get('/api/jobs').json()['jobs'] == []
    assert not list((Path.cwd() / 'data/jobs').iterdir())
    job = client.post('/api/demo').json()
    items = client.get('/api/items').json()['items']
    assert len(items) == 3
    item = items[0]
    assert client.get(f"/api/items/{item['id']}").status_code == 200
    assert client.get(item['image_url']).status_code == 200
    assert client.post(f"/api/items/{item['id']}/discovery", json={'market': 'TW'}).status_code == 503
    assert client.post(f"/api/items/{item['id']}/reconstruction").status_code == 503
    assert client.post(f"/api/items/{item['id']}/discovery/cancel").status_code == 200
    assert client.get('/api/batches').json() == {'batches': []}
    assert client.get('/api/duplicates').status_code == 200
    exported = client.get('/api/export?format=json').json()
    assert len(exported['items']) == 3 and exported['groups'] == []
    rows = list(csv.DictReader(io.StringIO(client.get('/api/export?format=csv').content.decode('utf-8-sig'))))
    assert len(rows) == 3
    assert {'brand_candidate', 'brand_evidence_status', 'shopping_urls', 'shopping_market',
            'shopping_verification_version', 'shopping_exclusions',
            'reconstruction_is_synthetic', 'reconstruction_preview_url',
            'reconstruction_preview_status', 'reconstruction_preview_inferred_areas',
            'reconstruction_preview_warnings'} <= rows[0].keys()
    assert all(row['brand_candidate'] == row['shopping_urls'] == '' for row in rows)
    paths = client.get('/api/openapi.json').json()['paths']
    assert '/api/items/{item_id}/discovery' in paths
    assert '/api/items/{item_id}/discovery/cancel' in paths
from wardrobe.hosted import create_hosted_app
from wardrobe.config import Settings
from wardrobe.backup import backup_data, restore_data

origin = 'https://wardrobe.example'
hosted_dir = Path.cwd() / 'hosted-data'
hosted = create_hosted_app(Settings(), origin=origin, hosted_dir=hosted_dir)
headers = {'Origin': origin, 'X-Rewear-Request': '1'}
credentials = {'username': 'wheel-account', 'password': 'Wheel offline password 123'}
with TestClient(hosted, base_url=origin) as client:
    assert client.get('/login').status_code == 200
    assert client.get('/static/auth.js').status_code == 200
    assert client.get('/api/items').status_code == 401
    registered = client.post('/auth/register', headers=headers, json=credentials)
    assert registered.status_code == 200
    assert '__Host-rewear_session' in client.cookies
    session = client.get('/auth/session').json()
    headers['X-CSRF-Token'] = session['csrf_token']
    sample = client.post('/api/demo', headers=headers)
    assert sample.status_code == 200
    hosted_items = client.get('/api/items').json()['items']
    assert len(hosted_items) == 3
    assert client.get(hosted_items[0]['image_url']).status_code == 200
    assert client.get('/api/storage').json()['usage']['jobs'] == 1
    assert len(client.get('/api/export?format=json').json()['items']) == 3
archive, restored_dir = Path.cwd() / 'hosted.zip', Path.cwd() / 'restored-hosted'
backup_data(hosted_dir, archive, mode='hosted')
restore_data(archive, restored_dir)
restored = create_hosted_app(Settings(), origin=origin, hosted_dir=restored_dir)
with TestClient(restored, base_url=origin) as client:
    assert client.post('/auth/login', headers={'Origin': origin, 'X-Rewear-Request': '1'},
                       json=credentials).status_code == 200
    restored_items = client.get('/api/items').json()['items']
    assert [item['id'] for item in restored_items] == [item['id'] for item in hosted_items]
    assert client.get(restored_items[0]['image_url']).status_code == 200
print(json.dumps({'status': 'passed', 'module': wardrobe.app.__file__, 'inference': 'none',
                  'checks': ['isolated wheel import', 'packaged discovery and reconstruction UI',
                             'fresh unavailable health and explicit provider controls',
                             'offline upload rejected without jobs or media',
                             'discovery health and unavailable API', 'offline sample and media',
                             'supplemental cancel endpoint', 'JSON/CSV export', 'OpenAPI endpoints',
                             'batch and duplicate endpoints', 'packaged hosted login and account isolation',
                             'packaged storage and authenticated sample media/export',
                             'packaged multiview source schema and optional-reference UI',
                             'offline hosted backup/restore and real password login']}))
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    wheel = args.wheel.resolve()
    with tempfile.TemporaryDirectory(prefix="rewear-wheel-") as directory:
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(directory)
        environment = {
            **os.environ,
            "REWEAR_DATA_DIR": str(Path(directory) / "data"),
            "MODEL_API_KEY": " ",
            "OLLAMA_URL": "http://127.0.0.1:9",
            "REWEAR_IMAGE_SEARCH": "disabled",
        }
        for key in ("REWEAR_VISION_API_KEY", "REWEAR_VISION_PROJECT"):
            environment.pop(key, None)
        environment.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, "-c", CHECK],
            cwd=directory,
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(
                    {"status": "failed", "returncode": result.returncode, "stderr": result.stderr[-8000:]},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            raise SystemExit(result.stderr[-8000:])
        report = json.loads(result.stdout)
    report.update(temporary_directory_removed=True, wheel=wheel.name)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
