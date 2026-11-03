"""Isolated reconstruction lifecycle tests; fake providers do not measure image quality."""

import asyncio
import csv
import io
import sqlite3
import threading
import time
from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from wardrobe.app import create_app
from wardrobe.config import Settings
from wardrobe.providers import ProviderError
from wardrobe.schemas import unknown_garment
from wardrobe.reconstruction import QUALITY_CHECKS
from wardrobe.reconstruction_quality import assess_fidelity, build_report


RECORD = {
    **unknown_garment(),
    "category": "top",
    "subtype": "synthetic shirt",
    "colors": ["blue"],
    "condition": "good",
    "confidence": 0.86,
    "needs_review": False,
}


def quality_report(status="accepted"):
    """Explicit fake audit; lifecycle tests never measure visual quality."""
    plan = {
        "target": "one_garment",
        "source_view": "front",
        "view_evidence": "Front closure visible.",
        "garment_type": "shirt",
        "visibility": "partial",
        "visible_features": [{"feature": "colour", "detail": "Blue fabric."}],
        "uncertain_areas": [],
    }
    answer = {
        "observed_view": "front",
        "checks": {name: {"status": "pass", "reason": "Synthetic test evidence"} for name in QUALITY_CHECKS},
        "feature_checks": [{"feature": "colour", "status": "pass", "reason": "Blue fabric agrees."}],
        "corrections": [],
        "preview_observations": {
            "category": {"status": "pass", "reason": "Synthetic same category."},
            "coarse_color": {"status": "pass", "reason": "Synthetic same main colour."},
        },
    }
    if status == "rejected":
        answer["checks"]["visible_details"] = {"status": "fail", "reason": "新增原圖沒有的袖口橫條。"}
    elif status == "needs_review":
        answer["checks"]["cut_structure"] = {"status": "uncertain", "reason": "下襬沒有足夠證據。"}
    elif status == "insufficient_source":
        plan["uncertain_areas"] = [
            {"area": "structure", "detail": "領口與下襬都未入鏡，無法確認版型。", "critical": True}
        ]
        return build_report(plan, [])
    return build_report(plan, [assess_fidelity(plan, answer, 1)])


class InspectionProviders:
    def __init__(self, visibility="partial"):
        self.visibility = visibility

    async def health(self):
        return {"local": {"available": True}, "meta": {"configured": True}}

    async def segment(self, path, concept, video=False):
        assert path.is_file() and not video
        with Image.open(path) as image:
            width, height = image.size
        return [
            {
                "kind": "box",
                "object_id": "synthetic-shirt",
                "frame": None,
                "left": 5,
                "top": 5,
                "right": width - 5,
                "bottom": height - 5,
            }
        ]

    async def inspect(self, path, provider, *, context_path=None, wearing=False):
        assert path.is_file()
        if context_path is not None:
            assert context_path.is_file()
        visibility = self.visibility if wearing else "complete"
        return {
            "garment": deepcopy(RECORD),
            "target": "one_garment",
            "visibility": visibility,
            "reason": "Synthetic partial visibility" if visibility != "complete" else None,
        }

    async def close(self):
        pass


class ReconstructionProviders(InspectionProviders):
    def __init__(self, outcomes=(), **kwargs):
        super().__init__(**kwargs)
        self.outcomes = deque(outcomes)
        self.calls = []
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.release = threading.Event()
        self.cleanup_finished = threading.Event()
        self.cleanup_parent_existed = None

    async def reconstruct(self, cutout_path, context_path, record, destination):
        assert cutout_path.is_file()
        if context_path is not None:
            assert context_path.is_file()
        assert not destination.exists(), "Each reconstruction attempt must receive a new output path"
        self.calls.append(
            {
                "cutout": Path(cutout_path),
                "context": Path(context_path) if context_path is not None else None,
                "record": deepcopy(record),
                "destination": Path(destination),
            }
        )
        outcome = self.outcomes.popleft() if self.outcomes else "success"
        self.started.set()
        if outcome == "hold":
            while not self.release.is_set():
                await asyncio.sleep(0.01)
        if outcome in {"block", "delayed_cancel"}:
            destination.write_bytes(b"synthetic incomplete image")
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                if outcome == "delayed_cancel":
                    # A provider may finish local cleanup after receiving cancellation.
                    await asyncio.sleep(0.08)
                    self.cleanup_parent_existed = destination.parent.is_dir()
                    if self.cleanup_parent_existed:
                        destination.write_bytes(b"last synthetic cleanup write")
                self.cleanup_finished.set()
                raise
        if outcome == "failure":
            destination.write_bytes(b"synthetic incomplete image")
            raise ProviderError("Synthetic reconstruction failure")
        if outcome == "unexpected_failure":
            destination.write_bytes(b"synthetic incomplete image")
            raise RuntimeError("synthetic-private-key-and-internal-path")
        if outcome == "invalid":
            destination.write_bytes(b"not a PNG")
            return
        if outcome == "insufficient_source":
            return quality_report("insufficient_source")
        Image.new("RGB", (64, 80), (30, 90, 170)).save(destination, format="PNG")
        if outcome == "unvalidated":
            return None
        report = quality_report(
            {"uncertain": "needs_review", "inconsistent": "rejected"}.get(outcome, outcome)
            if outcome in {"rejected", "uncertain", "inconsistent"}
            else "accepted"
        )
        if outcome == "inconsistent":
            report["status"] = "accepted"
        return report


class AffordablePreviewProviders(ReconstructionProviders):
    def __init__(self):
        super().__init__()
        self.preview_calls = []

    async def reconstruct_preview(self, cutout, context, record, destinations, *, source_bundle=None):
        assert source_bundle is None
        self.preview_calls.append(
            {
                "cutout": Path(cutout),
                "context": Path(context),
                "record": deepcopy(record),
                "destinations": list(map(Path, destinations)),
            }
        )
        for index, destination in enumerate(destinations, 1):
            Image.new("RGB", (64, 80), (index * 40, 90, 170)).save(destination, format="PNG")
        return {"reference_manifest": None, "candidate_count": len(destinations)}


@contextmanager
def studio(directory, provider=None, *, api_key="synthetic-key-no-model-calls"):
    provider = provider if provider is not None else ReconstructionProviders()
    app = create_app(Settings(data_dir=directory, api_key=api_key), providers=provider)
    with TestClient(app) as client:
        yield client, app, provider


def seed_item(app, *, status="ready", reviewed=False, record=None, reconstruction=None, job=None):
    store, data_dir = app.state.store, app.state.settings.data_dir
    if job is None:
        ident = uuid4().hex
        job = store.create_job(
            id=ident,
            status="completed",
            mode="outfit",
            input_name="source.jpg",
            source_url=f"/media/{ident}/source.jpg",
        )
    directory = data_dir / "jobs" / job["id"]
    directory.mkdir(parents=True, exist_ok=True)
    paths = {}
    for filename, size in (("source.jpg", (120, 160)), ("cutout.png", (64, 80)), ("context.jpg", (90, 110))):
        path = directory / filename
        if not path.exists():
            Image.new("RGB", size, (30, 90, 170)).save(path)
        paths[filename] = path
    visible = {**deepcopy(RECORD), **(record or {})}
    item = store.add_item(
        job["id"],
        record=visible,
        model_record=deepcopy(RECORD),
        reviewed=reviewed,
        quality={"version": 3, "status": status, "visibility": "complete", "model_target": "one_garment"},
        image_url=f"/media/{job['id']}/context.jpg",
        cutout_url=f"/media/{job['id']}/cutout.png",
        source_url=f"/media/{job['id']}/source.jpg",
        original_url=f"/media/{job['id']}/source.jpg",
        visual_signature={"version": "synthetic-test-evidence"},
        reconstruction=deepcopy(reconstruction),
    )
    return item, paths


def image_bytes():
    output = io.BytesIO()
    Image.new("RGB", (120, 160), (30, 90, 170)).save(output, format="JPEG")
    return output.getvalue()


def wait_item(client, item_id, status=None, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/items/{item_id}")
        assert response.status_code == 200
        item = response.json()
        reconstruction = item.get("reconstruction")
        if reconstruction and (
            reconstruction["status"] == status
            if status
            else reconstruction["status"] not in {"queued", "running"}
        ):
            return item
        time.sleep(0.01)
    pytest.fail(f"Reconstruction did not reach the expected status: {item}")


def wait_job(client, job_id, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.01)
    pytest.fail(f"Inspection did not finish: {job}")


def unchanged_evidence(before, after):
    assert {key: value for key, value in before.items() if key != "reconstruction"} == {
        key: value for key, value in after.items() if key != "reconstruction"
    }


def start(client, item_id):
    response = client.post(f"/api/items/{item_id}/reconstruction")
    assert response.status_code == 202, response.text
    return response.json()


def test_default_affordable_preview_creates_three_selectable_candidates(tmp_path):
    provider = AffordablePreviewProviders()
    with studio(tmp_path, provider) as (client, app, _):
        item, paths = seed_item(app, status="needs_confirmation")
        original = {name: path.read_bytes() for name, path in paths.items()}
        queued = start(client, item["id"])
        assert queued["reconstruction"]["mode"] == "preview"
        completed = wait_item(client, item["id"], "needs_review")
        unchanged_evidence(item, completed)
        state = completed["reconstruction"]
        assert state["validation"] == state["candidate_validation"]
        assert state["validation"]["version"] == 2
        assert state["validation"]["mode"] == "preview"
        assert state["selected_candidate_id"] == "1"
        assert [entry["id"] for entry in state["preview_candidates"]] == ["1", "2", "3"]
        assert state["candidate_url"] == state["preview_candidates"][0]["variant_url"]
        assert not state.get("image_url") and not state.get("preview_url")
        assert provider.calls == [] and len(provider.preview_calls) == 1
        assert len(provider.preview_calls[0]["destinations"]) == 3
        for entry in state["preview_candidates"]:
            response = client.get(entry["variant_url"])
            assert response.status_code == 200 and response.content.startswith(b"\x89PNG")
        selected = client.post(f"/api/items/{item['id']}/reconstruction/select", json={"candidate_id": "2"})
        assert selected.status_code == 200
        selected_state = selected.json()["reconstruction"]
        assert selected_state["selected_candidate_id"] == "2"
        assert selected_state["candidate_url"] == selected_state["preview_candidates"][1]["variant_url"]
        assert (
            client.post(
                f"/api/items/{item['id']}/reconstruction/select", json={"candidate_id": "4"}
            ).status_code
            == 422
        )
        assert {name: path.read_bytes() for name, path in paths.items()} == original


def test_verified_reconstruction_remains_an_explicit_opt_in(tmp_path):
    provider = AffordablePreviewProviders()
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        response = client.post(
            f"/api/items/{item['id']}/reconstruction",
            json={"reference_item_ids": [], "mode": "verified"},
        )
        assert response.status_code == 202
        result = wait_item(client, item["id"], "completed")
        assert result["reconstruction"]["mode"] == "verified"
        assert len(provider.calls) == 1 and provider.preview_calls == []


def test_success_keeps_source_evidence_and_exports_synthetic_result_separately(tmp_path):
    with studio(tmp_path) as (client, app, provider):
        item, paths = seed_item(app, status="needs_confirmation")
        source_bytes = {name: path.read_bytes() for name, path in paths.items()}
        start(client, item["id"])
        completed = wait_item(client, item["id"], "completed")
        unchanged_evidence(item, completed)
        reconstruction = completed["reconstruction"]
        assert reconstruction["is_synthetic"] is True
        assert reconstruction["note"] and reconstruction["model"] and reconstruction["id"]
        assert reconstruction["created_at"] and reconstruction["updated_at"]
        assert reconstruction["error"] is None
        assert reconstruction["validation"]["status"] == "accepted"
        assert reconstruction["image_validation"] == reconstruction["validation"]
        assert reconstruction["image_url"] not in {
            item[name] for name in ("image_url", "cutout_url", "source_url", "original_url")
        }
        response = client.get(reconstruction["image_url"])
        assert response.status_code == 200
        with Image.open(io.BytesIO(response.content)) as generated:
            assert generated.format == "PNG" and generated.size == (64, 80)
        assert {name: path.read_bytes() for name, path in paths.items()} == source_bytes
        assert len(provider.calls) == 1 and provider.calls[0]["record"] == item["record"]
        assert provider.calls[0]["cutout"] == paths["cutout.png"]
        assert provider.calls[0]["context"] in {paths["source.jpg"], paths["context.jpg"]}
        assert app.state.store.item(item["id"]) == completed
        exported = client.get(f"/api/export?format=json&job_id={item['job_id']}").json()
        assert exported["items"] == [completed]
        rows = list(
            csv.DictReader(io.StringIO(client.get("/api/export?format=csv").content.decode("utf-8-sig")))
        )
        assert (
            rows[0]["category"] == item["record"]["category"]
            and rows[0]["quality_status"] == "needs_confirmation"
        )
        assert rows[0]["reconstruction_status"] == "completed"
        assert rows[0]["reconstruction_url"] == reconstruction["image_url"]
        assert rows[0]["reconstruction_is_synthetic"] == "True" and rows[0]["reconstruction_note"]


def test_active_resubmission_is_idempotent(tmp_path):
    provider = ReconstructionProviders(["block"])
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        first = start(client, item["id"])
        assert provider.started.wait(5)
        again = start(client, item["id"])
        assert first["reconstruction"]["id"] == again["reconstruction"]["id"]
        assert len(provider.calls) == 1
        unchanged_evidence(item, again)
        response = client.post(f"/api/items/{item['id']}/reconstruction/cancel")
        assert response.status_code == 200
        cancelled = wait_item(client, item["id"], "cancelled")
        unchanged_evidence(item, cancelled)
        assert provider.cancelled.wait(3) and not provider.calls[0]["destination"].exists()


@pytest.mark.parametrize("outcome", ["failure", "invalid", "unexpected_failure"])
def test_failed_regeneration_keeps_previous_image_then_retries_to_a_new_file(tmp_path, outcome):
    provider = ReconstructionProviders(["success", outcome, "success"])
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        start(client, item["id"])
        first = wait_item(client, item["id"], "completed")
        previous = first["reconstruction"]
        previous_bytes = client.get(previous["image_url"]).content
        retry = start(client, item["id"])
        assert retry["reconstruction"]["id"] != previous["id"]
        assert retry["reconstruction"]["image_url"] == previous["image_url"]
        failed = wait_item(client, item["id"], "failed")
        unchanged_evidence(item, failed)
        assert failed["reconstruction"]["image_url"] == previous["image_url"]
        assert failed["reconstruction"]["error"]
        assert "synthetic-private-key" not in failed["reconstruction"]["error"]
        assert not provider.calls[1]["destination"].exists()
        assert client.get(previous["image_url"]).content == previous_bytes
        start(client, item["id"])
        completed = wait_item(client, item["id"], "completed")
        assert completed["reconstruction"]["image_url"] != previous["image_url"]
        assert len({call["destination"] for call in provider.calls}) == 3
        assert client.get(previous["image_url"]).content == previous_bytes
        unchanged_evidence(item, completed)


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "foreign_job",
        "outside_symlink",
        "synthetic_reference",
        "synthetic_symlink",
        "foreign_context",
    ],
)
def test_reference_validation_prevents_generation_from_missing_or_foreign_sources(tmp_path, kind):
    with studio(tmp_path) as (client, app, provider):
        item, paths = seed_item(app)
        if kind == "missing":
            paths["cutout.png"].unlink()
        elif kind in {"foreign_job", "foreign_context"}:
            other, _ = seed_item(app)
            changes = {"cutout_url" if kind == "foreign_job" else "image_url": other["cutout_url"]}
            item = app.state.store.update_item(item["id"], **changes)
        elif kind == "outside_symlink":
            outside = tmp_path / "private.png"
            paths["cutout.png"].rename(outside)
            paths["cutout.png"].symlink_to(outside)
        else:
            directory = paths["cutout.png"].parent / "reconstructions"
            directory.mkdir()
            paths["cutout.png"].rename(directory / "old.png")
            if kind == "synthetic_symlink":
                paths["cutout.png"].symlink_to(directory / "old.png")
            else:
                item = app.state.store.update_item(
                    item["id"], cutout_url=f"/media/{item['job_id']}/reconstructions/old.png"
                )
        response = client.post(f"/api/items/{item['id']}/reconstruction")
        assert response.status_code == 409
        assert provider.calls == []
        assert app.state.store.item(item["id"]) == item


def test_missing_originals_do_not_fall_back_to_previous_synthetic_image(tmp_path):
    with studio(tmp_path) as (client, app, provider):
        item, paths = seed_item(app)
        start(client, item["id"])
        completed = wait_item(client, item["id"], "completed")
        old_image = client.get(completed["reconstruction"]["image_url"]).content
        for path in paths.values():
            path.unlink()
        response = client.post(f"/api/items/{item['id']}/reconstruction")
        assert response.status_code == 409 and len(provider.calls) == 1
        assert app.state.store.item(item["id"]) == completed
        assert client.get(completed["reconstruction"]["image_url"]).content == old_image


def test_cancelling_queued_item_never_calls_provider_and_keeps_other_work_running(tmp_path):
    provider = ReconstructionProviders(["block", "success"])
    with studio(tmp_path, provider) as (client, app, _):
        first, _ = seed_item(app)
        second, _ = seed_item(app)
        start(client, first["id"])
        assert provider.started.wait(5)
        queued = start(client, second["id"])
        assert queued["reconstruction"]["status"] == "queued" and len(provider.calls) == 1
        assert client.post(f"/api/items/{second['id']}/reconstruction/cancel").status_code == 200
        cancelled = wait_item(client, second["id"], "cancelled")
        unchanged_evidence(second, cancelled)
        assert client.get(f"/api/items/{first['id']}").json()["reconstruction"]["status"] == "running"
        assert len(provider.calls) == 1
        assert client.post(f"/api/items/{first['id']}/reconstruction/cancel").status_code == 200
        assert provider.cancelled.wait(3)
        assert not provider.calls[0]["destination"].exists()


def test_reconstruction_queue_limit_and_single_provider_concurrency(tmp_path):
    provider = ReconstructionProviders(["block"])
    with studio(tmp_path, provider) as (client, app, _):
        first, _ = seed_item(app)
        job = app.state.store.job(first["job_id"])
        all_items = [first] + [seed_item(app, job=job)[0] for _ in range(64)]
        start(client, first["id"])
        assert provider.started.wait(5)
        for item in all_items[1:64]:
            assert start(client, item["id"])["reconstruction"]["status"] == "queued"
        assert client.post(f"/api/items/{all_items[64]['id']}/reconstruction").status_code == 429
        assert app.state.store.item(all_items[64]["id"]).get("reconstruction") is None
        assert len(provider.calls) == 1
        assert client.post(f"/api/jobs/{job['id']}/cancel").status_code == 200
        assert all(
            app.state.store.item(item["id"])["reconstruction"]["status"] == "cancelled"
            for item in all_items[:64]
        )
        assert len(provider.calls) == 1


@pytest.mark.parametrize("target", ["item", "job"])
def test_delete_waits_for_provider_cleanup_and_prevents_late_output(tmp_path, target):
    provider = ReconstructionProviders(["delayed_cancel"])
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        start(client, item["id"])
        assert provider.started.wait(5)
        route = f"/api/items/{item['id']}" if target == "item" else f"/api/jobs/{item['job_id']}"
        response = client.delete(route)
        assert response.status_code == 204
        assert provider.cancelled.is_set() and provider.cleanup_finished.is_set()
        assert provider.cleanup_parent_existed is True
        assert app.state.store.item(item["id"]) is None
        assert client.get(f"/api/items/{item['id']}").status_code == 404
        assert not provider.calls[0]["destination"].exists()
        assert not list(tmp_path.rglob(provider.calls[0]["destination"].name))
        assert not app.state.reconstruction.active_for_job(item["job_id"])


def test_finished_analysis_job_cancel_stops_its_reconstruction_only(tmp_path):
    provider = ReconstructionProviders(["block"])
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        start(client, item["id"])
        assert provider.started.wait(5)
        response = client.post(f"/api/jobs/{item['job_id']}/cancel")
        assert response.status_code == 200 and response.json()["status"] == "completed"
        cancelled = wait_item(client, item["id"], "cancelled")
        unchanged_evidence(item, cancelled)
        assert provider.cancelled.is_set() and not provider.calls[0]["destination"].exists()


def test_batch_cancel_stops_running_and_queued_reconstructions(tmp_path):
    provider = ReconstructionProviders(["block", "success"])
    with studio(tmp_path, provider) as (client, app, _):
        batch = app.state.store.create_batch(
            [
                {"status": "completed", "mode": "outfit", "input_name": "source.jpg"},
                {"status": "completed", "mode": "outfit", "input_name": "source.jpg"},
            ],
            file_count=2,
            provider="local",
            mode="outfit",
            concept="garment",
        )
        items = [seed_item(app, job=job)[0] for job in batch["jobs"]]
        for item in items:
            start(client, item["id"])
        assert provider.started.wait(5)
        assert client.post(f"/api/batches/{batch['id']}/cancel").status_code == 200
        for item in items:
            cancelled = wait_item(client, item["id"], "cancelled")
            unchanged_evidence(item, cancelled)
        assert len(provider.calls) == 1 and provider.cancelled.is_set()
        assert not provider.calls[0]["destination"].exists()


def test_finishing_attempt_does_not_overwrite_concurrent_human_edits(tmp_path):
    provider = ReconstructionProviders(["hold"])
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        start(client, item["id"])
        assert provider.started.wait(5)
        edited = client.patch(
            f"/api/items/{item['id']}", json={"record": {"brand": "Human confirmed brand"}, "reviewed": True}
        ).json()
        provider.release.set()
        completed = wait_item(client, item["id"], "completed")
        unchanged_evidence(edited, completed)
        assert completed["record"]["brand"] == "Human confirmed brand" and completed["reviewed"] is True
        assert provider.calls[0]["record"] == item["record"]


def test_stale_attempt_cancellation_cannot_replace_a_newer_saved_state(tmp_path):
    provider = ReconstructionProviders(["block"])
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        first = start(client, item["id"])
        assert provider.started.wait(5)
        newer = {**first["reconstruction"], "id": uuid4().hex, "status": "failed", "error": "newer state"}
        saved = app.state.store.update_reconstruction(item["id"], newer)
        client.post(f"/api/items/{item['id']}/reconstruction/cancel")
        assert app.state.store.item(item["id"]) == saved
        assert not provider.calls[0]["destination"].exists()


@pytest.mark.parametrize("reason", ["no_key", "no_provider"])
def test_unavailable_reconstruction_never_calls_a_model(tmp_path, reason):
    provider = ReconstructionProviders() if reason == "no_key" else InspectionProviders()
    api_key = None if reason == "no_key" else "synthetic-key"
    with studio(tmp_path, provider, api_key=api_key) as (client, app, _):
        item, _ = seed_item(app)
        health = client.get("/api/health").json()
        assert health["reconstruction"]["available"] is False
        assert health["reconstruction"]["model"]
        assert client.post(f"/api/items/{item['id']}/reconstruction").status_code == 503
        client.portal.call(app.state.reconstruction.enqueue_automatic, item)
        assert app.state.store.item(item["id"]) == item
        assert getattr(provider, "calls", []) == []


@pytest.mark.parametrize(
    "status,reviewed,category,expected",
    [
        ("needs_retake", False, "top", 422),
        ("unassessed", False, "top", 422),
        ("ready", False, "other", 422),
        ("needs_retake", True, "other", 422),
        ("needs_retake", True, "top", 202),
    ],
)
def test_generation_eligibility_requires_known_clothing_or_explicit_review(
    tmp_path, status, reviewed, category, expected
):
    with studio(tmp_path) as (client, app, provider):
        item, _ = seed_item(app, status=status, reviewed=reviewed, record={"category": category})
        response = client.post(f"/api/items/{item['id']}/reconstruction")
        assert response.status_code == expected
        if expected == 202:
            unchanged_evidence(item, wait_item(client, item["id"], "completed"))
        else:
            assert provider.calls == [] and app.state.store.item(item["id"]) == item


@pytest.mark.parametrize("status", ["queued", "running"])
def test_restart_marks_interrupted_attempt_failed_without_resending_or_losing_prior_image(tmp_path, status):
    provider = ReconstructionProviders()
    app = create_app(Settings(data_dir=tmp_path, api_key="synthetic-key"), providers=provider)
    item, paths = seed_item(app)
    output = paths["cutout.png"].parent / "reconstructions"
    output.mkdir()
    previous = output / f"{uuid4().hex}.png"
    Image.new("RGB", (32, 48), "blue").save(previous)
    previous_bytes = previous.read_bytes()
    attempt_id = uuid4().hex
    incomplete = output / f"{attempt_id}.png"
    incomplete.write_bytes(b"interrupted provider output")
    item = app.state.store.update_reconstruction(
        item["id"],
        {
            "id": attempt_id,
            "status": status,
            "image_url": f"/media/{item['job_id']}/reconstructions/{previous.name}",
            "error": None,
            "is_synthetic": True,
            "model": "synthetic-test-model",
            "note": "Synthetic fixture",
        },
    )
    with TestClient(app) as client:
        recovered = client.get(f"/api/items/{item['id']}").json()
        unchanged_evidence(item, recovered)
        assert recovered["reconstruction"]["id"] == attempt_id
        assert recovered["reconstruction"]["status"] == "failed"
        assert recovered["reconstruction"]["error"]
        assert recovered["reconstruction"]["image_url"] == item["reconstruction"]["image_url"]
        assert client.get(recovered["reconstruction"]["image_url"]).content == previous_bytes
        assert provider.calls == [] and app.state.reconstruction.tasks == {}
        assert not incomplete.exists()
        start(client, item["id"])
        completed = wait_item(client, item["id"], "completed")
        unchanged_evidence(item, completed)
        assert completed["reconstruction"]["id"] != attempt_id and len(provider.calls) == 1
        assert previous.read_bytes() == previous_bytes


@pytest.mark.parametrize(
    "mode,visibility,reconstruct,expected_count,quality_status",
    [
        ("outfit", "complete", None, 1, "ready"),
        ("outfit", "partial", None, 1, "needs_confirmation"),
        ("outfit", "partial", "false", 0, "needs_confirmation"),
        ("single", "complete", None, 0, "ready"),
        ("rack", "complete", None, 0, "ready"),
    ],
)
def test_upload_auto_generation_defaults_on_for_eligible_outfits_only(
    tmp_path, mode, visibility, reconstruct, expected_count, quality_status
):
    provider = ReconstructionProviders(visibility=visibility)
    with studio(tmp_path, provider) as (client, app, _):
        data = {"mode": mode}
        if reconstruct is not None:
            data["reconstruct"] = reconstruct
        response = client.post(
            "/api/jobs", files={"file": ("outfit.jpg", image_bytes(), "image/jpeg")}, data=data
        )
        assert response.status_code == 202, response.text
        job = wait_job(client, response.json()["id"])
        assert job["status"] == "completed" and len(job["items"]) == 1
        assert app.state.store.job(job["id"])["options"]["reconstruct"] is (reconstruct != "false")
        item = job["items"][0]
        assert item["quality"]["status"] == quality_status
        if expected_count:
            completed = wait_item(client, item["id"], "completed")
            unchanged_evidence(item, completed)
            assert completed["reconstruction"]["is_synthetic"] is True
            # Automatic generation does not convert partially observed clothing into a confirmed record.
            assert completed["reviewed"] is False
        else:
            assert item.get("reconstruction") is None
        assert len(provider.calls) == expected_count


def test_fragment_stays_unusable_until_human_corrects_and_confirms_record(tmp_path):
    provider = ReconstructionProviders(visibility="fragment")
    with studio(tmp_path, provider) as (client, _, _):
        response = client.post(
            "/api/jobs", files={"file": ("outfit.jpg", image_bytes(), "image/jpeg")}, data={"mode": "outfit"}
        )
        assert response.status_code == 202
        job = wait_job(client, response.json()["id"])
        item = job["items"][0]
        assert item["quality"]["status"] == "needs_retake" and item["record"]["category"] == "other"
        assert item["model_record"]["category"] == "top"
        assert item.get("reconstruction") is None and provider.calls == []
        assert client.post(f"/api/items/{item['id']}/reconstruction").status_code == 422
        edited = client.patch(
            f"/api/items/{item['id']}", json={"record": {"category": "top"}, "reviewed": True}
        )
        assert edited.status_code == 200
        start(client, item["id"])
        completed = wait_item(client, item["id"], "completed")
        unchanged_evidence(edited.json(), completed)
        assert completed["quality"] == item["quality"] and completed["model_record"] == item["model_record"]


def test_outfit_analysis_remains_available_without_reconstruction_provider(tmp_path):
    with studio(tmp_path, InspectionProviders()) as (client, _, _):
        response = client.post(
            "/api/jobs", files={"file": ("outfit.jpg", image_bytes(), "image/jpeg")}, data={"mode": "outfit"}
        )
        assert response.status_code == 202
        job = wait_job(client, response.json()["id"])
        assert job["status"] == "completed" and job["confirmation_count"] == 1
        assert job["items"][0].get("reconstruction") is None


@pytest.mark.parametrize("reconstruct", [None, "false"])
def test_batch_reconstruction_option_is_persisted_and_bound_to_replay_key(tmp_path, reconstruct):
    with studio(tmp_path) as (client, app, provider):
        data = {"mode": "outfit", "client_id": "reconstruction-option-test"}
        if reconstruct is not None:
            data["reconstruct"] = reconstruct
        files = [("files", (name, image_bytes(), "image/jpeg")) for name in ("a.jpg", "b.jpg")]
        response = client.post("/api/batches", files=files, data=data)
        assert response.status_code == 202, response.text
        batch = response.json()
        for pending in batch["jobs"]:
            job = wait_job(client, pending["id"])
            assert job["status"] == "completed" and len(job["items"]) == 1
            assert app.state.store.job(job["id"])["options"]["reconstruct"] is (reconstruct != "false")
            if reconstruct != "false":
                wait_item(client, job["items"][0]["id"], "completed")
            else:
                assert job["items"][0].get("reconstruction") is None
        assert len(provider.calls) == (2 if reconstruct != "false" else 0)
        replay = client.post("/api/batches", files=files, data={**data, "reconstruct": reconstruct or "true"})
        assert replay.status_code == 202 and replay.json()["id"] == batch["id"]
        conflict = client.post(
            "/api/batches",
            files=files,
            data={**data, "reconstruct": "true" if reconstruct == "false" else "false"},
        )
        assert conflict.status_code == 409
        assert len(app.state.store.batches()) == 1 and len(app.state.store.jobs()) == 2
        assert len(list((tmp_path / "jobs").iterdir())) == 2
        assert len(provider.calls) == (2 if reconstruct != "false" else 0)


def test_inactive_cancellation_is_idempotent_and_missing_items_are_not_created(tmp_path):
    with studio(tmp_path) as (client, app, provider):
        item, _ = seed_item(app)
        response = client.post(f"/api/items/{item['id']}/reconstruction/cancel")
        assert response.status_code == 200 and response.json() == item
        start(client, item["id"])
        completed = wait_item(client, item["id"], "completed")
        response = client.post(f"/api/items/{item['id']}/reconstruction/cancel")
        assert response.status_code == 200 and response.json() == completed
        for suffix in ("", "/cancel"):
            assert client.post(f"/api/items/absent/reconstruction{suffix}").status_code == 404
        assert client.get("/api/items/absent").status_code == 404
        assert len(provider.calls) == 1 and len(app.state.store.items()) == 1


@pytest.mark.parametrize("target", ["item", "job"])
def test_deletion_rejects_a_new_attempt_queued_during_cancellation_cleanup(tmp_path, target):
    class ConcurrentSubmission(ReconstructionProviders):
        retry = None

        async def reconstruct(self, *args):
            try:
                return await super().reconstruct(*args)
            except asyncio.CancelledError:
                if self.retry is not None:
                    callback, self.retry = self.retry, None
                    # A queued request can enqueue after the old task completes,
                    # before the DELETE coroutine resumes from awaiting it.
                    asyncio.get_running_loop().call_soon(callback)
                raise

    provider = ConcurrentSubmission(["block", "hold"])
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        submitted = []

        def resubmit():
            try:
                submitted.append(app.state.reconstruction.enqueue(item["id"]))
            except ValueError as error:
                submitted.append(getattr(error, "status_code", None))

        provider.retry = resubmit
        start(client, item["id"])
        assert provider.started.wait(5)
        route = f"/api/items/{item['id']}" if target == "item" else f"/api/jobs/{item['job_id']}"
        assert client.delete(route).status_code == 204
        assert len(submitted) == 1 and submitted[0] in (404, 409)
        assert len(provider.calls) == 1
        assert app.state.store.item(item["id"]) is None
        assert item["id"] not in app.state.reconstruction.tasks


@pytest.mark.parametrize("alias", ["job", "output", "file"])
def test_recovery_does_not_unlink_files_reached_through_symlinks(tmp_path, alias):
    provider = ReconstructionProviders()
    app = create_app(Settings(data_dir=tmp_path / "catalogue", api_key="synthetic-key"), providers=provider)
    item, paths = seed_item(app)
    directory = paths["cutout.png"].parent
    output = directory / "reconstructions"
    output.mkdir()
    attempt_id = uuid4().hex
    unfinished = output / f"{attempt_id}.png"
    unfinished.write_bytes(b"outside recovery authority")
    if alias == "job":
        target = tmp_path / "outside-job"
        directory.rename(target)
        directory.symlink_to(target, target_is_directory=True)
        protected = target / "reconstructions" / unfinished.name
    elif alias == "output":
        target = tmp_path / "outside-output"
        output.rename(target)
        output.symlink_to(target, target_is_directory=True)
        protected = target / unfinished.name
    else:
        protected = tmp_path / "outside-file.png"
        unfinished.rename(protected)
        unfinished.symlink_to(protected)
    app.state.store.update_reconstruction(
        item["id"], {"id": attempt_id, "status": "running", "image_url": None}
    )
    with TestClient(app) as client:
        recovered = client.get(f"/api/items/{item['id']}").json()
        assert recovered["reconstruction"]["status"] == "failed"
        unchanged_evidence(item, recovered)
        assert protected.read_bytes() == b"outside recovery authority"
        assert provider.calls == []


def test_recovery_keeps_an_already_referenced_image_even_with_active_status(tmp_path):
    provider = ReconstructionProviders()
    app = create_app(Settings(data_dir=tmp_path, api_key="synthetic-key"), providers=provider)
    item, paths = seed_item(app)
    output = paths["cutout.png"].parent / "reconstructions"
    output.mkdir()
    attempt_id = uuid4().hex
    committed = output / f"{attempt_id}.png"
    Image.new("RGB", (32, 48), "blue").save(committed)
    before = committed.read_bytes()
    image_url = f"/media/{item['job_id']}/reconstructions/{committed.name}"
    app.state.store.update_reconstruction(
        item["id"], {"id": attempt_id, "status": "running", "image_url": image_url}
    )
    with TestClient(app) as client:
        recovered = client.get(f"/api/items/{item['id']}").json()
        assert recovered["reconstruction"]["status"] == "failed"
        assert recovered["reconstruction"]["image_url"] == image_url
        assert committed.read_bytes() == before and provider.calls == []


def test_queued_reconstruction_rechecks_source_before_calling_provider(tmp_path):
    provider = ReconstructionProviders(["block", "success"])
    with studio(tmp_path, provider) as (client, app, _):
        first, _ = seed_item(app)
        second, paths = seed_item(app)
        start(client, first["id"])
        assert provider.started.wait(5)
        start(client, second["id"])
        paths["cutout.png"].unlink()
        assert client.post(f"/api/items/{first['id']}/reconstruction/cancel").status_code == 200
        failed = wait_item(client, second["id"], "failed")
        unchanged_evidence(second, failed)
        assert failed["reconstruction"]["image_url"] is None and failed["reconstruction"]["error"]
        assert len(provider.calls) == 1


@pytest.mark.parametrize("target", ["item", "job"])
def test_failed_deletion_restores_media_and_releases_reconstruction_guard(tmp_path, target):
    provider = ReconstructionProviders(["block", "success"])
    with studio(tmp_path, provider) as (client, app, _):
        item, paths = seed_item(app)
        original = {name: path.read_bytes() for name, path in paths.items()}
        with app.state.store.connect() as db:
            db.execute(
                "CREATE TRIGGER reconstruction_test_delete_failure BEFORE DELETE ON items "
                "BEGIN SELECT RAISE(ABORT, 'synthetic delete failure'); END"
            )
        start(client, item["id"])
        assert provider.started.wait(5)
        route = f"/api/items/{item['id']}" if target == "item" else f"/api/jobs/{item['job_id']}"
        with pytest.raises(sqlite3.IntegrityError, match="synthetic delete failure"):
            client.delete(route)
        assert app.state.store.job(item["job_id"]) is not None
        restored = app.state.store.item(item["id"])
        unchanged_evidence(item, restored)
        assert restored["reconstruction"]["status"] == "cancelled"
        assert {name: path.read_bytes() for name, path in paths.items()} == original
        assert not provider.calls[0]["destination"].exists()
        with app.state.store.connect() as db:
            db.execute("DROP TRIGGER reconstruction_test_delete_failure")
        start(client, item["id"])
        completed = wait_item(client, item["id"], "completed")
        unchanged_evidence(item, completed)
        assert len(provider.calls) == 2


@pytest.mark.parametrize(
    "outcome,status",
    [
        ("rejected", "rejected"),
        ("uncertain", "needs_review"),
        ("unvalidated", "needs_review"),
        ("inconsistent", "needs_review"),
    ],
)
def test_decodable_candidate_never_becomes_primary_without_fidelity_pass(tmp_path, outcome, status):
    with studio(tmp_path, ReconstructionProviders([outcome])) as (client, app, _):
        item, paths = seed_item(app)
        original = {name: path.read_bytes() for name, path in paths.items()}
        start(client, item["id"])
        result = wait_item(client, item["id"], status)
        reconstruction = result["reconstruction"]
        assert reconstruction["image_url"] is None
        assert reconstruction["image_validation"] is None
        assert reconstruction["validation"]["status"] == status
        assert reconstruction["validation"]["issues"]
        assert client.get(reconstruction["candidate_url"]).status_code == 200
        exported = client.get(f"/api/export?format=csv&job_id={item['job_id']}")
        row = next(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
        assert row["reconstruction_url"] == ""
        assert row["reconstruction_candidate_url"] == reconstruction["candidate_url"]
        assert row["reconstruction_quality"] == status and row["reconstruction_issues"]
        assert {name: path.read_bytes() for name, path in paths.items()} == original
        unchanged_evidence(item, result)


def test_rejected_retry_keeps_previous_verified_image_and_separate_candidate(tmp_path):
    with studio(tmp_path, ReconstructionProviders(["success", "rejected"])) as (client, app, _):
        item, _ = seed_item(app)
        start(client, item["id"])
        first = wait_item(client, item["id"], "completed")["reconstruction"]
        start(client, item["id"])
        second = wait_item(client, item["id"], "rejected")["reconstruction"]
        assert second["image_url"] == first["image_url"]
        assert second["image_validation"] == first["image_validation"]
        assert second["validation"]["status"] == "rejected"
        assert second["candidate_url"] != second["image_url"]
        assert client.get(second["candidate_url"]).status_code == 200


def test_insufficient_source_is_quality_outcome_without_fake_generated_file(tmp_path):
    with studio(tmp_path, ReconstructionProviders(["insufficient_source"])) as (client, app, provider):
        item, _ = seed_item(app)
        start(client, item["id"])
        result = wait_item(client, item["id"], "needs_review")["reconstruction"]
        assert result["image_url"] is None and result["candidate_url"] is None
        assert result["validation"]["generated"] is False
        assert result["validation"]["issues"]
        assert result["error"] is None
        assert not provider.calls[0]["destination"].exists()


def test_legacy_unverified_image_is_kept_for_comparison_without_primary_promotion(tmp_path):
    with studio(tmp_path, ReconstructionProviders(["insufficient_source"])) as (client, app, _):
        item, paths = seed_item(app)
        legacy = paths["cutout.png"].parent / "reconstructions" / "legacy.png"
        legacy.parent.mkdir()
        Image.new("RGB", (64, 80), "red").save(legacy)
        url = f"/media/{item['job_id']}/reconstructions/legacy.png"
        app.state.store.update_reconstruction(item["id"], {"status": "completed", "image_url": url})
        start(client, item["id"])
        result = wait_item(client, item["id"], "needs_review")["reconstruction"]
        assert result["image_url"] is None
        assert result["candidate_url"] == url and client.get(url).status_code == 200
