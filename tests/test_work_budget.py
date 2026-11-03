"""Hosted limits span isolated wardrobes, including queued work and batches."""

import asyncio
import os
import subprocess
import sys

import httpx
import pytest

from wardrobe.app import create_app
from wardrobe.config import Settings
from wardrobe.work_budget import WorkBudget, WorkCapacityError
from test_app import FakeProviders, image_bytes


class HeldProvider(FakeProviders):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def inspect(self, path, provider, *, context_path=None):
        self.started.set()
        await self.release.wait()
        return await super().inspect(path, provider, context_path=context_path)


def photo():
    return {"file": ("shirt.jpg", image_bytes(), "image/jpeg")}


@pytest.mark.asyncio
async def test_global_admission_and_execution_are_shared_between_wardrobes(tmp_path):
    budget = WorkBudget(max_pending=2, per_kind=1)
    providers = [HeldProvider(), HeldProvider()]
    apps = [
        create_app(Settings(data_dir=tmp_path / str(i), api_key=None), p, work_budget=budget)
        for i, p in enumerate(providers)
    ]
    async with apps[0].router.lifespan_context(apps[0]), apps[1].router.lifespan_context(apps[1]):
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=apps[0]), base_url="http://testserver") as a,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=apps[1]), base_url="http://testserver") as b,
        ):
            first = await a.post("/api/jobs", files=photo())
            assert first.status_code == 202
            await asyncio.wait_for(providers[0].started.wait(), 3)
            second = await b.post("/api/jobs", files=photo())
            assert second.status_code == 202
            assert not providers[1].started.is_set()
            denied = await b.post("/api/jobs", files=photo())
            assert denied.status_code == 429
            assert len(apps[1].state.store.jobs()) == 1
            assert len(list((tmp_path / "1" / "jobs").iterdir())) == 1

            second_task = apps[1].state.manager.tasks[second.json()["id"]]
            cancelled = await b.post(f"/api/jobs/{second.json()['id']}/cancel")
            assert cancelled.status_code == 200
            await asyncio.gather(second_task, return_exceptions=True)
            assert budget.pending == 1
            assert not providers[1].started.is_set()
            assert apps[0].state.store.job(first.json()["id"])["status"] == "running"
    assert budget.pending == 0


@pytest.mark.asyncio
async def test_whole_batch_is_rejected_without_persistence_when_global_slots_are_short(tmp_path):
    budget = WorkBudget(max_pending=2)
    blocker = budget.create_task(asyncio.Event().wait(), name="other-wardrobe-work")
    app = create_app(Settings(data_dir=tmp_path, api_key=None), FakeProviders(), work_budget=budget)
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                result = await client.post(
                    "/api/batches",
                    data={"mode": "single", "provider": "local"},
                    files=[("files", (f"shirt-{i}.jpg", image_bytes(), "image/jpeg")) for i in range(2)],
                )
                assert result.status_code == 429
                assert app.state.store.jobs() == app.state.store.batches() == []
                assert list((tmp_path / "jobs").iterdir()) == []
    finally:
        blocker.cancel()
        await asyncio.gather(blocker, return_exceptions=True)
    assert budget.pending == 0


@pytest.mark.asyncio
async def test_cancel_before_start_and_failure_both_return_global_capacity():
    budget = WorkBudget(max_pending=1)
    entered = asyncio.Event()

    async def work():
        entered.set()
        raise ValueError("synthetic failure")

    task = budget.create_task(work(), name="cancel-before-start")
    with pytest.raises(WorkCapacityError):
        budget.create_task(work(), name="rejected")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not entered.is_set()
    replacement = budget.create_task(work(), name="replacement")
    with pytest.raises(ValueError):
        await replacement
    assert entered.is_set() and budget.has_capacity()


def test_importing_app_factory_does_not_open_or_create_local_storage(tmp_path):
    target = tmp_path / "must-not-exist"
    result = subprocess.run(
        [sys.executable, "-c", "from wardrobe.app import create_app; assert callable(create_app)"],
        env={**os.environ, "REWEAR_DATA_DIR": str(target)},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert not target.exists()


def test_app_factory_does_not_initialize_database_during_offline_lock(tmp_path):
    import fcntl

    directory = tmp_path / "wardrobe"
    directory.mkdir()
    with (directory / ".server.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="backup is already running"):
            create_app(Settings(data_dir=directory), FakeProviders())
        assert not (directory / "wardrobe.sqlite3").exists()
        assert not (directory / "jobs").exists()


def test_app_factory_rejects_symlink_lock_without_writing_target(tmp_path):
    target = tmp_path / "outside"
    target.write_text("preserve")
    directory = tmp_path / "wardrobe"
    directory.mkdir()
    (directory / ".server.lock").symlink_to(target)
    with pytest.raises(RuntimeError, match="lock"):
        create_app(Settings(data_dir=directory), FakeProviders())
    assert target.read_text() == "preserve"
    assert not (directory / "wardrobe.sqlite3").exists()
