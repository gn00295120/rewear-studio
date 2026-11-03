"""Readiness prevents doomed uploads without silently changing data destinations."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from test_app import FakeProviders, image_bytes, wait_job
from wardrobe.app import create_app
from wardrobe.config import Settings


class ReadinessProviders(FakeProviders):
    available = False
    health_calls = 0
    health_error = False

    async def health(self):
        self.health_calls += 1
        if self.health_error:
            raise RuntimeError("private provider connection detail")
        return {"local": {"available": self.available}, "meta": {"configured": True}}


def submit(client, route, provider="local", mode="single", **data):
    if route == "/api/generate":
        return client.post(route, json={"prompt": "A blue shirt", "provider": provider, "mode": mode})
    key = "files" if route == "/api/batches" else "file"
    return client.post(
        route,
        files=[(key, ("shirt.jpg", image_bytes(), "image/jpeg"))],
        data={"provider": provider, "mode": mode, **data},
    )


@pytest.mark.parametrize("route", ["/api/jobs", "/api/batches", "/api/generate"])
@pytest.mark.parametrize("health_error", [False, True])
def test_unavailable_local_never_creates_work_or_spends_cloud_calls(tmp_path, route, health_error):
    provider = ReadinessProviders()
    provider.health_error = health_error
    app = create_app(Settings(data_dir=tmp_path, api_key="test-only"), provider)
    with TestClient(app) as client:
        response = submit(client, route)
        assert response.status_code == 503
        assert "will not auto-switch" in response.json()["detail"]
        assert "private provider" not in response.text
        assert provider.calls == provider.generation_calls == 0
        assert app.state.store.jobs() == app.state.store.batches() == []
        assert list((tmp_path / "jobs").iterdir()) == []


@pytest.mark.parametrize("route", ["/api/jobs", "/api/batches", "/api/generate"])
def test_explicit_meta_selection_works_without_querying_offline_local(tmp_path, route):
    provider = ReadinessProviders()
    app = create_app(Settings(data_dir=tmp_path, api_key="test-only"), provider)
    with TestClient(app) as client:
        response = submit(client, route, provider="meta")
        assert response.status_code == 202, response.text
        pending = response.json()["jobs"][0] if route == "/api/batches" else response.json()
        job = wait_job(client, pending["id"])
        assert job["status"] == "completed"
        assert job["provider"] == "meta"
        assert provider.health_calls == 0


def test_health_refresh_and_upload_do_not_reuse_stale_model_availability(tmp_path):
    provider = ReadinessProviders()
    app = create_app(Settings(data_dir=tmp_path, api_key="test-only"), provider)
    with TestClient(app) as client:
        assert client.get("/api/health").json()["local"]["available"] is False
        provider.available = True
        assert client.get("/api/health").json()["local"]["available"] is False
        assert client.get("/api/health?refresh=true").json()["local"]["available"] is True
        provider.available = False
        assert submit(client, "/api/jobs").status_code == 503
        provider.available = True
        result = submit(client, "/api/jobs")
        assert result.status_code == 202
        assert wait_job(client, result.json()["id"])["status"] == "completed"
        assert len(app.state.store.jobs()) == 1


@pytest.mark.parametrize("batch", [False, True])
def test_retry_rechecks_provider_and_preserves_original_when_offline(tmp_path, batch):
    provider = ReadinessProviders()
    provider.available = True
    app = create_app(Settings(data_dir=tmp_path, api_key="test-only"), provider)
    with TestClient(app) as client:
        route = "/api/batches" if batch else "/api/jobs"
        response = submit(client, route)
        pending = response.json()["jobs"][0] if batch else response.json()
        done = wait_job(client, pending["id"])
        app.state.store.update_job(done["id"], status="failed")
        before = app.state.store.jobs()
        directories = sorted(p.name for p in (tmp_path / "jobs").iterdir())
        provider.available = False
        assert client.post(f"{route}/{response.json()['id']}/retry").status_code == 503
        assert app.state.store.jobs() == before
        assert sorted(p.name for p in (tmp_path / "jobs").iterdir()) == directories
        assert provider.calls == 1


def test_idempotent_batch_replay_remains_available_when_model_goes_offline(tmp_path):
    provider = ReadinessProviders()
    provider.available = True
    app = create_app(Settings(data_dir=tmp_path, api_key="test-only"), provider)
    with TestClient(app) as client:
        first = submit(client, "/api/batches", client_id="same-local-upload")
        wait_job(client, first.json()["jobs"][0]["id"])
        calls = provider.health_calls
        provider.available = False
        repeated = submit(client, "/api/batches", client_id="same-local-upload")
        assert repeated.status_code == 202
        assert repeated.json()["id"] == first.json()["id"]
        assert provider.health_calls == calls
        assert len(app.state.store.jobs()) == 1


@pytest.mark.parametrize("cached_image", [True, False])
def test_local_retry_of_generated_image_only_needs_meta_when_image_is_missing(tmp_path, cached_image):
    provider = ReadinessProviders()
    provider.available = True
    app = create_app(Settings(data_dir=tmp_path, api_key=None), provider)
    with TestClient(app) as client:
        directory = tmp_path / "jobs" / "saved-generated"
        directory.mkdir()
        if cached_image:
            (directory / "generated.jpg").write_bytes(image_bytes())
        old = app.state.store.create_job(
            id="saved-generated",
            status="failed",
            filename="Previously generated.jpg",
            media_type="generated",
            provider="local",
            mode="single",
            concept="garment",
            input_name="generated.jpg" if cached_image else None,
            options={"prompt": "A blue shirt"},
        )
        response = client.post(f"/api/jobs/{old['id']}/retry")
        assert response.status_code == (202 if cached_image else 503)
        if cached_image:
            done = wait_job(client, response.json()["id"])
            assert done["status"] == "completed"
            assert (tmp_path / "jobs" / done["id"] / "generated.jpg").read_bytes() == image_bytes()
            assert provider.calls == 1
        else:
            assert len(app.state.store.jobs()) == 1
            assert len(list((tmp_path / "jobs").iterdir())) == 1
            assert provider.calls == 0
        assert provider.generation_calls == 0
        assert app.state.store.job(old["id"]) == old


def test_retry_cannot_resurrect_generated_job_deleted_during_health_check(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class WaitingProvider(ReadinessProviders):
        async def health(self):
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return {"local": {"available": True}, "meta": {"configured": True}}

    provider = WaitingProvider()
    app = create_app(Settings(data_dir=tmp_path, api_key="test-only"), provider)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as executor:
        directory = tmp_path / "jobs" / "deleted-during-health"
        directory.mkdir()
        (directory / "generated.jpg").write_bytes(image_bytes())
        app.state.store.create_job(
            id=directory.name,
            status="failed",
            filename="Generated.jpg",
            media_type="generated",
            provider="local",
            mode="single",
            input_name="generated.jpg",
            options={"prompt": "A blue shirt"},
        )
        pending = executor.submit(client.post, f"/api/jobs/{directory.name}/retry")
        try:
            assert entered.wait(2)
            assert client.delete(f"/api/jobs/{directory.name}").status_code == 204
        finally:
            release.set()
        assert pending.result(timeout=3).status_code == 404
        assert app.state.store.jobs() == []
        assert list((tmp_path / "jobs").iterdir()) == []
        assert provider.calls == provider.generation_calls == 0


def test_readiness_deadline_includes_waiting_for_other_health_requests(tmp_path):
    class SlowProvider(ReadinessProviders):
        cancelled = 0

        async def health(self):
            try:
                await asyncio.sleep(3)
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
            return {"local": {"available": False}, "meta": {"configured": True}}

    provider = SlowProvider()
    app = create_app(Settings(data_dir=tmp_path, api_key="test-only"), provider)

    async def concurrent_uploads():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver"
        ) as client:
            responses = await asyncio.gather(
                *[
                    client.post(
                        "/api/jobs",
                        files={"file": ("shirt.jpg", image_bytes(), "image/jpeg")},
                        data={"provider": "local", "mode": "single"},
                    )
                    for _ in range(2)
                ]
            )
            assert [response.status_code for response in responses] == [503, 503]

    asyncio.run(concurrent_uploads())
    # The second three-second request must be cancelled at the shared five-
    # second admission deadline, instead of waiting six seconds to finish.
    assert provider.cancelled == 1
    assert app.state.store.jobs() == []
