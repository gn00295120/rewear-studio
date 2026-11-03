import asyncio
import csv
import io
import os
import sqlite3
import time

import pytest
import httpx
from fastapi.testclient import TestClient
from PIL import Image
from starlette.datastructures import UploadFile

from wardrobe.app import create_app
from wardrobe.config import Settings
from wardrobe.providers import ProviderError
from wardrobe.store import Store


RECORD = {
    "category": "top",
    "subtype": "shirt",
    "brand": None,
    "size_label": None,
    "colors": ["blue"],
    "pattern": None,
    "material_guess": None,
    "condition": "good",
    "defects": [],
    "confidence": 0.86,
    "needs_review": False,
}


class FakeProviders:
    def __init__(self):
        self.delay = 0
        self.failure = None
        self.records = []
        self.calls = 0
        self.generation_calls = 0
        self.record = dict(RECORD)
        self.target = "one_garment"
        self.visibility = "complete"
        self.contexts = []

    async def health(self):
        return {
            "local": {"available": True, "model": "test-model", "error": None},
            "meta": {"configured": True},
        }

    async def extract(self, path, provider):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.failure:
            raise ProviderError(self.failure)
        return dict(self.record)

    async def inspect(self, path, provider, *, context_path=None):
        self.contexts.append(context_path)
        return {
            "garment": await self.extract(path, provider),
            "target": self.target,
            "visibility": self.visibility,
            "reason": None
            if self.target == "one_garment" and self.visibility == "complete"
            else "Synthetic evidence for this test",
        }

    async def segment(self, path, concept, video=False):
        return self.records

    async def generate(self, prompt, destination):
        self.generation_calls += 1
        Image.new("RGB", (80, 90), "blue").save(destination)

    async def close(self):
        pass


def image_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (80, 90), "blue").save(buffer, format="JPEG")
    return buffer.getvalue()


def upload(client, **data):
    return client.post("/api/jobs", files={"file": ("shirt.jpg", image_bytes(), "image/jpeg")}, data=data)


def wait_job(client, job_id, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = client.get(f"/api/jobs/{job_id}")
        assert result.status_code == 200
        job = result.json()
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job did not finish: {job}")


@pytest.fixture
def setup(tmp_path):
    provider = FakeProviders()
    settings = Settings(data_dir=tmp_path, api_key="test-key-never-expose")
    app = create_app(settings, providers=provider)
    with TestClient(app) as client:
        yield client, app, provider


def test_image_to_persistent_record_and_review(setup):
    client, app, provider = setup
    response = upload(client)
    assert response.status_code == 202
    job = wait_job(client, response.json()["id"])
    assert job["status"] == "completed"
    assert job["progress"] == 100
    assert job["item_count"] == 1
    assert job["quality_version"] == 3
    assert provider.calls == 1
    item = job["items"][0]
    assert item["record"] == RECORD
    assert item["quality"]["version"] == 3 and item["quality"]["model_target"] == "one_garment"
    assert client.get(item["image_url"]).headers["content-type"] == "image/jpeg"
    assert client.get("/api/items?review=pending").json()["items"][0]["id"] == item["id"]
    update = client.patch(
        f"/api/items/{item['id']}",
        json={"record": {"brand": "TestBrand", "size_label": "3Y"}, "reviewed": True},
    )
    assert update.status_code == 200
    assert update.json()["reviewed"] is True
    assert update.json()["model_record"]["brand"] is None
    assert (
        client.get("/api/items?q=TestBrand&review=reviewed&category=top").json()["items"][0]["record"][
            "brand"
        ]
        == "TestBrand"
    )
    assert client.get("/api/items?review=pending").json()["items"] == []
    assert Store(app.state.settings.data_dir).items()[0]["reviewed"] is True
    invalid = client.patch(f"/api/items/{item['id']}", json={"record": {"confidence": 1.2}})
    assert invalid.status_code == 422


def test_demo_is_historical_idempotent_and_has_no_inference(setup):
    client, app, provider = setup
    first = client.post("/api/demo").json()
    second = client.post("/api/demo").json()
    assert first["id"] == second["id"]
    assert first["is_demo"] is True
    assert "no new model analysis" in first["warnings"][0]
    assert len(client.get("/api/items").json()["items"]) == 3
    assert provider.calls == 0


def test_export_escapes_formula_cells_and_preserves_json(setup):
    client, _, _ = setup
    job_id = client.post("/api/demo").json()["id"]
    item = client.get("/api/items").json()["items"][0]
    malicious = '=HYPERLINK("https://example.invalid")'
    client.patch(f"/api/items/{item['id']}", json={"record": {"brand": malicious}})
    export = client.get(f"/api/export?format=csv&job_id={job_id}")
    assert export.status_code == 200
    rows = list(csv.DictReader(io.StringIO(export.content.decode("utf-8-sig"))))
    assert any(row["brand"] == "'" + malicious for row in rows)
    exported = client.get("/api/export?format=json").json()
    assert len(exported["items"]) == 3
    assert any(row["record"]["brand"] == malicious for row in exported["items"])
    assert client.get("/api/export?format=xlsx").status_code == 422


def test_rejects_invalid_uploads_options_and_cross_origin(setup):
    client, _, _ = setup
    assert client.post("/api/jobs", files={"file": ("x.html", b"<html>", "text/html")}).status_code == 415
    assert client.post("/api/jobs", files={"file": ("x.jpg", b"", "image/jpeg")}).status_code == 422
    assert upload(client, provider="fake").status_code == 422
    assert upload(client, concept="   ").status_code == 422
    assert client.post("/api/demo", headers={"Origin": "https://example.invalid"}).status_code == 403
    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 400
    invalid = client.post("/api/jobs", files={"file": ("x.jpg", b"not-a-jpeg", "image/jpeg")})
    assert wait_job(client, invalid.json()["id"])["status"] == "failed"


def test_partial_records_and_retry(setup):
    client, _, provider = setup
    provider.failure = "Model temporarily unavailable."
    old = wait_job(client, upload(client).json()["id"])
    assert old["status"] == "partial"
    assert old["items"][0]["record"]["needs_review"] is True
    assert old["items"][0]["error"] == provider.failure
    provider.failure = None
    retried = client.post(f"/api/jobs/{old['id']}/retry")
    assert retried.status_code == 202
    new = wait_job(client, retried.json()["id"])
    assert new["status"] == "completed"
    assert new["id"] != old["id"]
    assert client.get(f"/api/jobs/{old['id']}").json()["status"] == "partial"


def test_cancel_active_and_queued_jobs(setup):
    client, _, provider = setup
    provider.delay = 3
    first = upload(client).json()
    second = upload(client).json()
    assert client.delete(f"/api/jobs/{first['id']}").status_code == 409
    assert client.post(f"/api/jobs/{second['id']}/cancel").status_code == 200
    assert wait_job(client, second["id"])["status"] == "cancelled"
    assert client.post(f"/api/jobs/{first['id']}/cancel").status_code == 200
    assert wait_job(client, first["id"])["status"] == "cancelled"
    assert client.post(f"/api/jobs/{first['id']}/cancel").status_code == 409


def test_delete_retains_recovery_data_and_restricts_media(setup):
    client, app, _ = setup
    job = client.post("/api/demo").json()
    item = client.get("/api/items").json()["items"][0]
    assert client.delete(f"/api/items/{item['id']}").status_code == 204
    assert client.get(f"/api/jobs/{job['id']}").json()["item_count"] == 2
    directory = app.state.settings.data_dir / "jobs" / job["id"]
    secret = app.state.settings.data_dir / "secret.jpg"
    secret.write_text("not public")
    (directory / "link.jpg").symlink_to(secret)
    assert client.get(f"/media/{job['id']}/link.jpg").status_code == 404
    assert client.get(f"/media/{job['id']}/..%2F..%2Fwardrobe.sqlite3").status_code == 404
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 204
    assert client.get(f"/api/jobs/{job['id']}").status_code == 404
    assert client.get(item["image_url"]).status_code == 404
    assert list((app.state.settings.data_dir / ".trash").iterdir())
    with sqlite3.connect(app.state.store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] >= 2


def test_restart_recovers_interrupted_jobs(tmp_path):
    store = Store(tmp_path)
    job = store.create_job(status="running")
    with TestClient(create_app(Settings(data_dir=tmp_path), FakeProviders())) as client:
        result = client.get(f"/api/jobs/{job['id']}").json()
        assert result["status"] == "failed"
        assert "interrupted" in result["error"]


def test_size_limit_and_missing_meta(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_key=None, max_upload_mb=1), FakeProviders())
    with TestClient(app) as client:
        assert upload(client, mode="rack").status_code == 503
        assert upload(client, provider="meta").status_code == 503
        large = client.post("/api/jobs", files={"file": ("x.jpg", b"x" * (1024 * 1024 + 1))})
        assert large.status_code == 413
        too_large = client.post("/api/jobs", content=b"x" * (3 * 1024 * 1024))
        assert too_large.status_code == 413
        assert app.state.store.jobs() == []


def test_queue_limit_and_health_redacts_key(tmp_path):
    provider = FakeProviders()
    provider.delay = 3
    app = create_app(Settings(data_dir=tmp_path, api_key="not-for-clients", max_queue=1), provider)
    with TestClient(app) as client:
        upload(client)
        assert upload(client).status_code == 429
        health = client.get("/api/health")
        assert health.json()["local"]["available"] is True
        assert health.json()["model_usage"]["estimated_usd"] == 0
        assert health.json()["reconstruction"]["default_mode"] == "preview"
        assert health.json()["reconstruction"]["preview_candidates"] == 3
        assert health.json()["reconstruction"]["preview_standard_estimate_usd"] == 0.03
        assert "not-for-clients" not in health.text
        assert health.headers["cache-control"] == "no-store"


def test_rack_crops_and_empty_detection(setup):
    client, _, provider = setup
    provider.records = [
        {"kind": "box", "object_id": "one", "frame": None, "left": 0, "top": 0, "right": 35, "bottom": 70},
        {"kind": "box", "object_id": "two", "frame": None, "left": 40, "top": 0, "right": 80, "bottom": 80},
    ]
    job = wait_job(client, upload(client, mode="rack").json()["id"])
    assert job["status"] == "completed"
    assert job["item_count"] == 2
    assert client.get(job["overlay_url"]).status_code == 200
    provider.records = []
    empty = wait_job(client, upload(client, mode="rack").json()["id"])
    assert empty["status"] == "completed"
    assert empty["item_count"] == 0
    assert empty["warnings"]


def test_generation_enters_same_pipeline(setup):
    client, _, provider = setup
    response = client.post("/api/generate", json={"prompt": "One blue shirt on a plain white background"})
    assert response.status_code == 202
    job = wait_job(client, response.json()["id"])
    assert job["status"] == "completed"
    assert job["media_type"] == "generated"
    assert client.get(job["source_url"]).status_code == 200
    assert job["item_count"] == 1


def test_fragments_keep_context_and_model_evidence_without_cataloguing_guesses(setup):
    client, app, provider = setup
    provider.visibility = "fragment"
    provider.records = [
        {"kind": "box", "object_id": "one", "frame": None, "left": 12, "top": 10, "right": 55, "bottom": 70}
    ]
    job = wait_job(client, upload(client, mode="rack").json()["id"])
    assert job["status"] == "completed"
    assert job["item_count"] == job["retake_count"] == 1
    assert job["usable_count"] == 0
    assert "retake" in job["stage"] and any("retake" in warning.lower() for warning in job["warnings"])
    item = job["items"][0]
    assert provider.contexts[0] is not None
    assert item["record"]["category"] == "other" and item["record"]["subtype"] is None
    assert item["model_record"] == RECORD
    assert item["quality"]["status"] == "needs_retake"
    assert item["quality"]["version"] == job["quality_version"] == 3
    assert item["quality"]["model_target"] == "one_garment"
    assert item["image_url"] != item["cutout_url"]
    for field in ("image_url", "cutout_url", "source_url"):
        assert client.get(item[field]).status_code == 200
    assert client.get("/api/items?quality=ready").json()["items"] == []
    assert len(client.get("/api/items?quality=needs_retake").json()["items"]) == 1
    rows = list(csv.DictReader(io.StringIO(client.get("/api/export?format=csv").content.decode("utf-8-sig"))))
    assert rows[0]["quality_status"] == "needs_retake" and rows[0]["subtype"] == ""
    # A deliberate human correction overrides catalogue eligibility, not evidence.
    changed = client.patch(
        f"/api/items/{item['id']}", json={"record": {"category": "top", "subtype": "shirt"}, "reviewed": True}
    ).json()
    assert changed["quality"] == item["quality"] and changed["model_record"] == RECORD
    assert len(client.get("/api/items?quality=ready").json()["items"]) == 1
    updated = client.get(f"/api/jobs/{job['id']}").json()
    assert updated["usable_count"] == 1 and updated["retake_count"] == 0
    assert updated["stage"] == "Analysis complete" and not updated["warnings"]
    with app.state.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE entity_id=?", (item["id"],)).fetchone()[0] == 1
    # Revoke the override and delete the remaining fragment; no stale retake count.
    client.patch(f"/api/items/{item['id']}", json={"reviewed": False})
    assert client.get(f"/api/jobs/{job['id']}").json()["retake_count"] == 1
    client.delete(f"/api/items/{item['id']}")
    empty = client.get(f"/api/jobs/{job['id']}").json()
    assert empty["retake_count"] == 0 and not empty["warnings"]
    assert empty["stage"] == "Results removed"


@pytest.mark.parametrize(
    "target,garment",
    [
        pytest.param("no_garment", {"category": "other", "subtype": None}, id="high-confidence-cup"),
        pytest.param("multiple_garments", {"category": "bottom", "subtype": "jeans"}, id="two-garments"),
        pytest.param("uncertain", {"category": "top", "subtype": "shirt"}, id="uncertain-target"),
    ],
)
def test_complete_non_single_target_keeps_evidence_and_export_counts_through_override(setup, target, garment):
    client, _, provider = setup
    provider.target = target
    provider.record.update(**garment, confidence=0.99)
    job = wait_job(client, upload(client).json()["id"])
    assert job["status"] == "completed" and job["quality_version"] == 3
    assert job["item_count"] == job["retake_count"] == 1
    assert job["usable_count"] == job["unassessed_count"] == 0
    item = job["items"][0]
    assert item["record"]["category"] == "other" and item["record"]["subtype"] is None
    assert item["model_record"] == provider.record
    assert item["quality"]["model_target"] == target
    assert item["quality"]["visibility"] == item["quality"]["model_visibility"] == "complete"
    assert item["quality"]["status"] == "needs_retake"
    assert client.get("/api/items?quality=ready").json()["items"] == []
    assert client.get("/api/items?quality=needs_retake").json()["items"] == [item]
    exported = client.get(f"/api/export?format=json&job_id={job['id']}").json()["items"]
    assert exported == [item]
    rows = list(csv.DictReader(io.StringIO(client.get("/api/export?format=csv").content.decode("utf-8-sig"))))
    assert len(rows) == 1 and rows[0]["quality_status"] == "needs_retake"
    assert rows[0]["category"] == "other" and rows[0]["subtype"] == ""
    assert rows[0]["visibility"] == "complete" and rows[0]["quality_reasons"]
    changed = client.patch(f"/api/items/{item['id']}", json={"reviewed": True}).json()
    assert changed["quality"] == item["quality"] and changed["model_record"] == item["model_record"]
    reviewed = client.get(f"/api/jobs/{job['id']}").json()
    assert reviewed["usable_count"] == 1 and reviewed["retake_count"] == 0
    assert reviewed["stage"] == "Analysis complete" and reviewed["warnings"] == []
    client.patch(f"/api/items/{item['id']}", json={"reviewed": False})
    revoked = client.get(f"/api/jobs/{job['id']}").json()
    assert revoked["usable_count"] == 0 and revoked["retake_count"] == 1
    assert "retake" in revoked["stage"] and revoked["warnings"]
    assert client.get(f"/api/export?format=json&job_id={job['id']}").json()["items"] == [item]


def test_deleting_last_ready_current_version_item_clears_summary_and_exports(setup):
    client, _, _ = setup
    job = wait_job(client, upload(client).json()["id"])
    assert job["quality_version"] == 3 and job["warnings"] == []
    assert client.delete(f"/api/items/{job['items'][0]['id']}").status_code == 204
    empty = client.get(f"/api/jobs/{job['id']}").json()
    listed = next(row for row in client.get("/api/jobs").json()["jobs"] if row["id"] == job["id"])
    for summary in (empty, listed):
        assert summary["stage"] == "Results removed" and summary["warnings"] == []
        assert summary["item_count"] == summary["usable_count"] == summary["retake_count"] == 0
        assert summary["unassessed_count"] == 0
    assert client.get(f"/api/export?format=json&job_id={job['id']}").json()["items"] == []
    exported = client.get(f"/api/export?format=csv&job_id={job['id']}")
    assert list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig")))) == []


def test_retry_preserves_generated_source(setup):
    client, _, provider = setup
    provider.failure = "Temporarily unable to extract fields"
    response = client.post("/api/generate", json={"prompt": "A blue shirt on a white background"})
    old = wait_job(client, response.json()["id"])
    assert old["status"] == "partial"
    source = client.get(old["source_url"]).content
    provider.failure = None
    retry = client.post(f"/api/jobs/{old['id']}/retry")
    new = wait_job(client, retry.json()["id"])
    assert new["status"] == "completed"
    assert client.get(new["source_url"]).content == source
    assert provider.generation_calls == 1


def test_restart_preserves_partial_status(tmp_path):
    store = Store(tmp_path)
    job = store.create_job(status="running")
    store.add_item(job["id"], record=RECORD)
    with TestClient(create_app(Settings(data_dir=tmp_path), FakeProviders())) as client:
        result = client.get(f"/api/jobs/{job['id']}").json()
        assert result["status"] == "partial"
        assert result["item_count"] == 1


async def test_concurrent_uploads_respect_queue_limit(tmp_path, monkeypatch):
    provider = FakeProviders()
    provider.delay = 30
    app = create_app(Settings(data_dir=tmp_path, max_queue=1), provider)
    image = Image.frombytes("RGB", (1200, 1200), os.urandom(1200 * 1200 * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=96)
    gate = asyncio.Event()
    seen = set()
    read = UploadFile.read

    async def synchronized_read(self, size=-1):
        if id(self) not in seen:
            seen.add(id(self))
            if len(seen) == 2:
                gate.set()
            await asyncio.wait_for(gate.wait(), 5)
        return await read(self, size)

    monkeypatch.setattr(UploadFile, "read", synchronized_read)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver"
        ) as client:
            responses = await asyncio.gather(
                *[
                    client.post("/api/jobs", files={"file": ("large.jpg", buffer.getvalue(), "image/jpeg")})
                    for _ in range(2)
                ]
            )
            assert sorted(response.status_code for response in responses) == [202, 429]
            assert len(app.state.store.jobs()) == 1


async def test_chunked_upload_limit_is_413(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, max_upload_mb=1), FakeProviders())

    async def chunks():
        yield b'--testboundary\r\nContent-Disposition: form-data; name="file"; filename="large.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'
        for _ in range(3):
            yield b"x" * (1024 * 1024)
        yield b"\r\n--testboundary--\r\n"

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/api/jobs",
                content=chunks(),
                headers={"Content-Type": "multipart/form-data; boundary=testboundary"},
            )
            assert response.status_code == 413
            assert (
                "upload" in response.json()["detail"].lower()
                or "size limit" in response.json()["detail"].lower()
            )
            assert app.state.store.jobs() == []


async def test_cancel_while_upload_closes_cannot_restart_job(tmp_path, monkeypatch):
    provider = FakeProviders()
    provider.delay = 30
    app = create_app(Settings(data_dir=tmp_path), provider)
    closing, release = asyncio.Event(), asyncio.Event()
    close = UploadFile.close
    seen = set()

    async def blocked_close(self):
        if id(self) not in seen:
            seen.add(id(self))
            closing.set()
            await asyncio.wait_for(release.wait(), 5)
        return await close(self)

    monkeypatch.setattr(UploadFile, "close", blocked_close)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver"
        ) as client:
            uploading = asyncio.create_task(
                client.post("/api/jobs", files={"file": ("shirt.jpg", image_bytes(), "image/jpeg")})
            )
            await asyncio.wait_for(closing.wait(), 5)
            job = app.state.store.jobs()[0]
            await client.post(f"/api/jobs/{job['id']}/cancel")
            release.set()
            assert (await uploading).status_code == 202
            for _ in range(100):
                if app.state.store.job(job["id"])["status"] == "cancelled":
                    break
                await asyncio.sleep(0.01)
            assert app.state.store.job(job["id"])["status"] == "cancelled"
            assert app.state.store.items(job["id"]) == []
