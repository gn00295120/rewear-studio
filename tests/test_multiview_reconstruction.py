"""Confirmed-original provenance/lifecycle tests, not model-quality measurements."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
from uuid import uuid4

from PIL import Image
import pytest
from fastapi.testclient import TestClient

from test_reconstruction import InspectionProviders, quality_report, seed_item, studio, wait_item
from wardrobe.config import Settings
from wardrobe.hosted import create_hosted_app


class MultiProviders(InspectionProviders):
    def __init__(self, *, hold=False, late_change=None, invalid_manifest=False):
        super().__init__()
        self.hold = hold
        self.late_change = late_change
        self.invalid_manifest = invalid_manifest
        self.calls = []
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.cleaned = threading.Event()
        self.release = threading.Event()

    async def reconstruct(self, cutout, context, record, destination, *, source_bundle=None):
        assert source_bundle is not None
        self.calls.append(source_bundle)
        self.started.set()
        if self.hold:
            try:
                while not self.release.is_set():
                    await asyncio.sleep(0.005)
            except asyncio.CancelledError:
                self.cancelled.set()
                await asyncio.sleep(0.08)
                assert destination.parent.is_dir()
                self.cleaned.set()
                raise
        Image.new("RGB", (64, 80), "white").save(destination)
        report = quality_report()
        manifest = source_bundle.manifest.model_dump()
        for source in manifest["sources"]:
            source["reference_sha256"] = hashlib.sha256(
                source_bundle.paths[source["id"]].read_bytes()
            ).hexdigest()
        if self.invalid_manifest:
            manifest["group_id"] = uuid4().hex
        report["plan"]["reference_manifest"] = manifest
        if self.late_change:
            self.late_change()
        return report


def group_sources(client, app, count=3):
    entries, paths = [], []
    for index in range(count):
        item, media = seed_item(app)
        # Different originals, so this does not pass by reusing duplicate pixels.
        Image.new("RGB", (90, 110), (30 + index * 30, 90, 170)).save(media["context.jpg"])
        entries.append(item)
        paths.append(media)
    response = client.post(
        "/api/garment-groups",
        json={"item_ids": [item["id"] for item in entries], "primary_item_id": entries[0]["id"]},
    )
    assert response.status_code == 201, response.text
    return entries, paths, response.json()


def generate(client, entries, refs=None):
    return client.post(
        f"/api/items/{entries[0]['id']}/reconstruction",
        json={"reference_item_ids": refs if refs is not None else [entries[1]["id"]]},
    )


def test_confirmed_multi_originals_bind_output_and_export_to_source_snapshots(tmp_path):
    with studio(tmp_path, MultiProviders()) as (client, app, provider):
        entries, paths, group = group_sources(client, app)
        response = generate(client, entries, [entries[2]["id"], entries[1]["id"]])
        assert response.status_code == 202, response.text
        assert response.json()["reconstruction"]["reference_manifest"]["group_id"] == group["id"]
        item = wait_item(client, entries[0]["id"], "completed")
        state = item["reconstruction"]
        manifest = state["image_validation"]["plan"]["reference_manifest"]
        assert manifest == state["image_reference_manifest"] == state["reference_manifest"]
        assert manifest["target_item_id"] == entries[0]["id"]
        assert manifest["primary_source"] == "context"
        assert [source["id"] for source in manifest["sources"]] == ["cutout", "context", "extra_1", "extra_2"]
        assert all(source["reference_sha256"] for source in manifest["sources"])
        expected = {item["id"]: media for item, media in zip(entries, paths)}
        for source in manifest["sources"]:
            media = expected[source["item_id"]]
            path = media["cutout.png" if source["id"] == "cutout" else "context.jpg"]
            assert source["file_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert item["record"] == entries[0]["record"]
        exported = client.get("/api/export").json()
        assert str(tmp_path) not in json.dumps(exported)
        saved = next(entry for entry in exported["items"] if entry["id"] == item["id"])
        assert saved["reconstruction"]["image_reference_manifest"] == manifest
        assert len(provider.calls) == 1


@pytest.mark.parametrize(
    "kind",
    [
        "self",
        "duplicate",
        "too_many",
        "not_in_group",
        "missing",
        "generated_extra",
        "generated_primary",
        "copied_pixels",
        "synthetic_path",
    ],
)
def test_invalid_sources_do_not_enqueue_or_invoke_models(tmp_path, kind):
    with studio(tmp_path, MultiProviders()) as (client, app, provider):
        entries, paths, _ = group_sources(client, app)
        refs = [entries[1]["id"]]
        if kind == "self":
            refs = [entries[0]["id"]]
        elif kind == "duplicate":
            refs *= 2
        elif kind == "too_many":
            refs = [entry["id"] for entry in entries]
        elif kind == "not_in_group":
            other, _ = seed_item(app)
            refs = [other["id"]]
        elif kind == "missing":
            refs = [uuid4().hex]
        elif kind.startswith("generated"):
            entry = entries[0 if kind == "generated_primary" else 1]
            app.state.store.update_job(entry["job_id"], media_type="generated")
        elif kind == "copied_pixels":
            paths[1]["context.jpg"].write_bytes(paths[0]["context.jpg"].read_bytes())
        elif kind == "synthetic_path":
            forged = paths[1]["context.jpg"].parent / "reconstructions" / "candidate.png"
            forged.parent.mkdir()
            Image.new("RGB", (40, 50), "white").save(forged)
            app.state.store.update_item(
                entries[1]["id"], image_url=f"/media/{entries[1]['job_id']}/reconstructions/candidate.png"
            )
        response = generate(client, entries, refs)
        assert response.status_code in {404, 409, 422}, response.text
        assert not provider.calls
        assert app.state.store.item(entries[0]["id"])["reconstruction"] is None


@pytest.mark.parametrize(
    "operation", ["source_item", "source_job", "unmerge", "merge", "unselected_group_member"]
)
def test_source_and_membership_changes_await_dependent_cancellation(tmp_path, operation):
    with studio(tmp_path, MultiProviders(hold=True)) as (client, app, provider):
        entries, _, group = group_sources(client, app)
        assert generate(client, entries).status_code == 202
        assert provider.started.wait(2)
        if operation == "source_item":
            response = client.delete(f"/api/items/{entries[1]['id']}")
        elif operation == "source_job":
            response = client.delete(f"/api/jobs/{entries[1]['job_id']}")
        elif operation == "unselected_group_member":
            response = client.delete(f"/api/items/{entries[2]['id']}")
        elif operation == "unmerge":
            response = client.delete(f"/api/garment-groups/{group['id']}")
        else:
            other, _ = seed_item(app)
            response = client.post(
                "/api/garment-groups",
                json={"item_ids": [entries[1]["id"], other["id"]], "primary_item_id": other["id"]},
            )
        assert response.status_code in {201, 204}, response.text
        assert provider.cancelled.is_set() and provider.cleaned.is_set()
        state = wait_item(client, entries[0]["id"], "cancelled")["reconstruction"]
        assert state["image_url"] is None and state["candidate_url"] is None
        assert not list((tmp_path / "jobs" / entries[0]["job_id"] / "reconstructions").glob("*.png"))


def test_busy_request_never_silently_ignores_a_different_reference_selection(tmp_path):
    with studio(tmp_path, MultiProviders(hold=True)) as (client, app, provider):
        entries, _, _ = group_sources(client, app)
        response = generate(client, entries)
        assert response.status_code == 202 and provider.started.wait(2)
        repeated = generate(client, entries)
        assert repeated.status_code == 202
        assert repeated.json()["reconstruction"]["id"] == response.json()["reconstruction"]["id"]
        assert generate(client, entries, []).status_code == 409
        assert generate(client, entries, [entries[2]["id"]]).status_code == 409
        client.post(f"/api/items/{entries[0]['id']}/reconstruction/cancel")
        assert len(provider.calls) == 1


def test_group_change_blocks_new_work_until_cancel_cleanup_finishes(tmp_path):
    with studio(tmp_path, MultiProviders(hold=True)) as (client, app, provider):
        entries, _, group = group_sources(client, app)
        assert generate(client, entries).status_code == 202 and provider.started.wait(2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            removal = pool.submit(client.delete, f"/api/garment-groups/{group['id']}")
            assert provider.cancelled.wait(2)
            assert generate(client, [entries[2], entries[1]]).status_code == 409
            assert removal.result(timeout=2).status_code == 204
        assert len(provider.calls) == 1


@pytest.mark.parametrize("change", ["pixels", "group"])
def test_changed_queued_source_never_reaches_provider(tmp_path, change):
    with studio(tmp_path, MultiProviders()) as (client, app, provider):
        entries, paths, group = group_sources(client, app)
        manager = app.state.reconstruction
        client.portal.call(manager.semaphore.acquire)
        try:
            assert generate(client, entries).status_code == 202
            if change == "pixels":
                Image.new("RGB", (90, 110), "red").save(paths[1]["context.jpg"])
            else:
                app.state.store.unmerge(group["id"])
        finally:
            client.portal.call(manager.semaphore.release)
        state = wait_item(client, entries[0]["id"], "cancelled")["reconstruction"]
        assert "Reference photo" in state["error"]
        assert not provider.calls


@pytest.mark.parametrize("change", ["pixels", "group"])
def test_changed_source_cannot_publish_a_late_valid_candidate(tmp_path, change):
    with studio(tmp_path, MultiProviders()) as (client, app, provider):
        entries, paths, group = group_sources(client, app)
        provider.late_change = (
            (lambda: Image.new("RGB", (90, 110), "red").save(paths[1]["context.jpg"]))
            if change == "pixels"
            else lambda: app.state.store.unmerge(group["id"])
        )
        assert generate(client, entries).status_code == 202
        state = wait_item(client, entries[0]["id"], "cancelled")["reconstruction"]
        assert state["candidate_url"] is None and state["image_url"] is None
        assert not list((tmp_path / "jobs" / entries[0]["job_id"] / "reconstructions").glob("*.png"))


def test_report_for_another_bundle_is_unverified_and_old_image_keeps_its_own_sources(tmp_path):
    with studio(tmp_path, MultiProviders()) as (client, app, provider):
        entries, _, _ = group_sources(client, app)
        assert generate(client, entries).status_code == 202
        first = wait_item(client, entries[0]["id"], "completed")["reconstruction"]
        provider.invalid_manifest = True
        assert generate(client, entries, [entries[2]["id"]]).status_code == 202
        second = wait_item(client, entries[0]["id"], "needs_review")["reconstruction"]
        assert second["image_url"] == first["image_url"]
        assert second["image_validation"] == first["image_validation"]
        assert second["image_reference_manifest"] == first["image_reference_manifest"]
        assert second["candidate_url"] != first["image_url"]
        assert second["candidate_reference_manifest"]["sources"][-1]["item_id"] == entries[2]["id"]
        assert second["candidate_validation"]["status"] == "needs_review"


def test_invalid_merge_does_not_cancel_an_existing_generation(tmp_path):
    with studio(tmp_path, MultiProviders(hold=True)) as (client, app, provider):
        entries, _, _ = group_sources(client, app)
        assert generate(client, entries).status_code == 202 and provider.started.wait(2)
        response = client.post(
            "/api/garment-groups",
            json={"item_ids": [entries[0]["id"], uuid4().hex], "primary_item_id": entries[0]["id"]},
        )
        assert response.status_code == 404 and not provider.cancelled.is_set()
        provider.release.set()
        wait_item(client, entries[0]["id"], "completed")


def test_hosted_reference_ids_cannot_read_or_invoke_another_accounts_sources(tmp_path):
    from test_hosted import ORIGIN, headers, register, seed

    providers = []

    def factory(settings):
        provider = MultiProviders()
        providers.append(provider)
        return provider

    app = create_hosted_app(
        Settings(data_dir=tmp_path / "unused-local", api_key="fixture-no-model-calls"),
        origin=ORIGIN,
        hosted_dir=tmp_path / "hosted",
        providers_factory=factory,
    )
    with TestClient(app, base_url=ORIGIN) as client:
        alice, bob = register(client, "alice"), register(client, "bob")
        _, _, primary = seed(app, client, alice, color="blue")
        _, _, own_extra = seed(app, client, alice, color="white")
        _, _, foreign = seed(app, client, bob, color="green")
        response = client.post(
            "/api/garment-groups",
            json={"item_ids": [primary["id"], own_extra["id"]], "primary_item_id": primary["id"]},
            headers=headers(alice),
        )
        assert response.status_code == 201
        for target, references in (
            (primary["id"], [foreign["id"]]),
            (primary["id"], [own_extra["id"], foreign["id"]]),
            (foreign["id"], [primary["id"]]),
        ):
            response = client.post(
                f"/api/items/{target}/reconstruction",
                json={"reference_item_ids": references},
                headers=headers(alice),
            )
            assert response.status_code == 404, response.text
            assert "green" not in response.text and foreign["job_id"] not in response.text
        assert not any(provider.calls for provider in providers)
        assert (
            client.get(f"/api/items/{foreign['id']}", headers=headers(bob)).json().get("reconstruction")
            is None
        )


def test_completed_picture_with_a_deleted_reference_can_be_backed_up_and_restored(tmp_path):
    from wardrobe.backup import backup_data, restore_data
    from wardrobe.store import Store

    directory = tmp_path / "catalogue"
    with studio(directory, MultiProviders()) as (client, app, _):
        entries, _, _ = group_sources(client, app)
        assert generate(client, entries).status_code == 202
        item = wait_item(client, entries[0]["id"], "completed")
        assert client.delete(f"/api/jobs/{entries[1]['job_id']}").status_code == 204
    archive, restored = tmp_path / "backup.zip", tmp_path / "restored"
    backup_data(directory, archive, mode="local")
    restore_data(archive, restored)
    saved = Store(restored).item(item["id"])
    assert saved["reconstruction"]["image_validation"] == item["reconstruction"]["image_validation"]
    assert (
        saved["reconstruction"]["image_reference_manifest"]
        == item["reconstruction"]["image_reference_manifest"]
    )
    assert not (restored / "jobs" / entries[1]["job_id"]).exists()
    relative = saved["reconstruction"]["image_url"].removeprefix("/media/")
    assert (restored / "jobs" / relative).is_file()


@pytest.mark.parametrize("damage", ["existing_reference_file", "generated_output", "malformed_history"])
def test_backup_still_rejects_missing_live_media_or_invalid_reference_history(tmp_path, damage):
    from wardrobe.backup import BackupError, backup_data

    directory = tmp_path / "catalogue"
    with studio(directory, MultiProviders()) as (client, app, _):
        entries, paths, _ = group_sources(client, app)
        assert generate(client, entries).status_code == 202
        item = wait_item(client, entries[0]["id"], "completed")
        if damage == "existing_reference_file":
            paths[1]["context.jpg"].unlink()
        elif damage == "generated_output":
            relative = item["reconstruction"]["image_url"].removeprefix("/media/")
            (directory / "jobs" / relative).unlink()
        else:
            assert client.delete(f"/api/jobs/{entries[1]['job_id']}").status_code == 204
            manifest = item["reconstruction"]["image_reference_manifest"]
            manifest["sources"][-1]["media_url"] = "/media/" + uuid4().hex + "/outside.jpg"
            app.state.store.update_reconstruction(item["id"], {"image_reference_manifest": manifest})
    with pytest.raises(BackupError):
        backup_data(directory, tmp_path / "invalid.zip", mode="local")
    assert not (tmp_path / "invalid.zip").exists()
