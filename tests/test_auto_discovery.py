"""Upload option and lifecycle integration; provider fixtures do not measure matching accuracy."""

import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from wardrobe.app import create_app
from wardrobe.config import Settings
from wardrobe.store import Store
from test_discovery import DiscoveryProviders
from test_lifestyle_api import photo


class OutfitDiscovery(DiscoveryProviders):
    async def segment(self, path, concept, video=False):
        with Image.open(path) as image:
            width, height = image.size
        return [
            {
                "kind": "box",
                "object_id": "shirt",
                "frame": None,
                "left": 1,
                "top": 1,
                "right": width - 1,
                "bottom": height - 1,
            }
        ]


def wait_job(client, ident):
    for _ in range(300):
        job = client.get(f"/api/jobs/{ident}").json()
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.01)
    raise AssertionError("Analysis did not finish")


def wait_discovery(client, ident):
    for _ in range(300):
        item = client.get(f"/api/items/{ident}").json()
        state = item.get("discovery") or {}
        if state.get("status") not in {None, "queued", "running"}:
            return item
        time.sleep(0.01)
    raise AssertionError("Discovery did not finish")


def upload(client, batch=False, **data):
    files = (
        [("files", ("outfit.jpg", photo(), "image/jpeg"))]
        if batch
        else {"file": ("outfit.jpg", photo(), "image/jpeg")}
    )
    return client.post(
        "/api/batches" if batch else "/api/jobs",
        files=files,
        data={"mode": "outfit", "provider": "meta", "reconstruct": "false", **data},
    )


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("discover", [None, "false", "true"])
def test_upload_opt_in_searches_original_photo_and_preserves_analysis(tmp_path, batch, discover):
    provider = OutfitDiscovery()
    app = create_app(Settings(data_dir=tmp_path, api_key="test-only"), provider)
    with TestClient(app) as client:
        data = {"discovery_market": "global"}
        if discover is not None:
            data["discover"] = discover
        response = upload(client, batch, **data)
        assert response.status_code == 202, response.text
        pending = response.json()["jobs"][0] if batch else response.json()
        job = wait_job(client, pending["id"])
        assert job["status"] == "completed" and len(job["items"]) == 1
        assert app.state.store.job(job["id"])["options"]["discover"] is (discover == "true")
        item = job["items"][0]
        if discover == "true":
            done = wait_discovery(client, item["id"])
            assert done["discovery"]["status"] == "completed"
            assert done["record"] == item["record"] and done["reviewed"] is False
            assert provider.shopping_calls[0]["market"] == "global"
            assert provider.identify_calls[0]["cutout"].name.startswith("crop-")
            assert "reconstructions" not in provider.identify_calls[0]["cutout"].parts
            assert (
                len(provider.identify_calls)
                == len(provider.shopping_calls)
                == len(provider.verify_calls)
                == 1
            )
        else:
            assert not item.get("discovery") and not provider.identify_calls
        assert item.get("reconstruction") is None


@pytest.mark.parametrize("batch", [False, True])
def test_auto_discovery_rejects_wrong_mode_and_invalid_market_without_jobs(tmp_path, batch):
    app = create_app(Settings(data_dir=tmp_path, api_key="test-only"), OutfitDiscovery())
    with TestClient(app) as client:
        assert upload(client, batch, mode="single", discover="true").status_code == 422
        assert upload(client, batch, discover="true", discovery_market="invalid").status_code == 422
        assert app.state.store.jobs() == []


def test_batch_discovery_replay_market_binding_and_retry(tmp_path):
    provider = OutfitDiscovery()
    app = create_app(Settings(data_dir=tmp_path, api_key="test-only"), provider)
    with TestClient(app) as client:
        options = {"client_id": "search-once", "discover": "true", "discovery_market": "TW"}
        first = upload(client, True, **options)
        assert first.status_code == 202
        batch = first.json()
        job = wait_job(client, batch["jobs"][0]["id"])
        wait_discovery(client, job["items"][0]["id"])
        assert upload(client, True, **options).json()["id"] == batch["id"]
        assert upload(client, True, **{**options, "discover": "false"}).status_code == 409
        assert upload(client, True, **{**options, "discovery_market": "global"}).status_code == 409
        assert len(provider.shopping_calls) == 1
        app.state.store.update_job(job["id"], status="partial", warnings=["Fixture retry trigger"])
        retried = client.post(f"/api/batches/{batch['id']}/retry")
        assert retried.status_code == 202
        new = wait_job(client, retried.json()["jobs"][0]["id"])
        wait_discovery(client, new["items"][0]["id"])
        assert app.state.store.job(new["id"])["options"] == app.state.store.job(job["id"])["options"]
        assert len(provider.shopping_calls) == 2


@pytest.mark.parametrize("retained_key", ["preview_url", "candidate_url"])
def test_restart_cleanup_preserves_a_committed_preview_or_candidate(tmp_path, retained_key):
    store = Store(tmp_path)
    job, attempt = uuid4().hex, uuid4().hex
    store.create_job(id=job, status="completed")
    directory = tmp_path / "jobs" / job / "reconstructions"
    directory.mkdir(parents=True)
    image = directory / f"{attempt}.png"
    Image.new("RGB", (30, 40), "blue").save(image)
    original = image.read_bytes()
    state = {"id": attempt, "status": "running", retained_key: f"/media/{job}/reconstructions/{attempt}.png"}
    item = store.add_item(job, reconstruction=state)
    store.recover()
    assert image.read_bytes() == original
    assert store.item(item["id"])["reconstruction"]["status"] == "failed"


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("link_root", [False, True])
def test_retry_does_not_import_a_source_through_a_replaced_directory(tmp_path, batch, link_root):
    data = tmp_path / "catalogue"
    app = create_app(Settings(data_dir=data, api_key="test-only"), OutfitDiscovery())
    with TestClient(app) as client:
        response = upload(client, batch, discover="false")
        pending = response.json()["jobs"][0] if batch else response.json()
        job = wait_job(client, pending["id"])
        app.state.store.update_job(job["id"], status="failed")
        before = app.state.store.jobs()
        directory = data / "jobs" if link_root else data / "jobs" / job["id"]
        external = tmp_path / "external"
        directory.rename(external)
        directory.symlink_to(external, target_is_directory=True)
        route = f"/api/batches/{response.json()['id']}/retry" if batch else f"/api/jobs/{job['id']}/retry"
        assert client.post(route).status_code == 409
        assert app.state.store.jobs() == before
