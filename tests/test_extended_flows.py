"""Isolated workflow tests with scripted fakes; these do not measure model accuracy."""

import asyncio
import csv
import io
import json
import threading
import time
from collections import Counter, deque
from contextlib import contextmanager
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from wardrobe.app import create_app
from wardrobe.config import Settings
from wardrobe.providers import ProviderError
from wardrobe.schemas import unknown_garment
from wardrobe.store import Store


BLOCK_UNTIL_CANCELLED = object()
RECORD = {
    "category": "top",
    "subtype": "synthetic shirt",
    "brand": None,
    "size_label": None,
    "colors": ["blue"],
    "pattern": None,
    "material_guess": None,
    "condition": "good",
    "defects": [],
    "confidence": 0.91,
    "needs_review": False,
}


def inspection(visibility="complete", target="one_garment", **garment):
    return {
        "garment": {**deepcopy(RECORD), **garment},
        "target": target,
        "visibility": visibility,
        "reason": None
        if visibility == "complete" and target == "one_garment"
        else "Synthetic evidence for this test",
    }


class ScriptedProviders:
    """Never contacts a model: each inspection consumes one explicit test outcome."""

    def __init__(self, outcomes=(), *, detections=3):
        self.outcomes = deque(outcomes)
        self.detections = detections
        self.inspections = []
        self.segment_calls = 0
        self.segment_error = None
        self.block_started = threading.Event()
        self.block_cancelled = threading.Event()

    async def health(self):
        return {"local": {"available": True}, "meta": {"configured": True}}

    async def segment(self, path, concept, video=False):
        assert path.is_file() and not video
        self.segment_calls += 1
        if self.segment_error is not None:
            raise self.segment_error
        return [
            {
                "kind": "box",
                "object_id": f"garment-{index + 1}",
                "frame": None,
                "left": 8 + index * 55,
                "top": 12,
                "right": 53 + index * 55,
                "bottom": 150,
            }
            for index in range(self.detections)
        ]

    async def inspect(self, path, provider, *, context_path=None):
        assert path.is_file()
        assert context_path is not None and context_path.is_file()
        self.inspections.append((path, provider, context_path))
        assert self.outcomes, "The pipeline made an unexpected extra inspection call"
        outcome = self.outcomes.popleft()
        if outcome is BLOCK_UNTIL_CANCELLED:
            self.block_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.block_cancelled.set()
                raise
        if isinstance(outcome, Exception):
            raise outcome
        return deepcopy(outcome)

    async def close(self):
        pass


@contextmanager
def studio(directory, provider, **settings):
    app = create_app(
        Settings(data_dir=directory, api_key="synthetic-test-key-no-network", **settings),
        providers=provider,
    )
    with TestClient(app) as client:
        yield client, app


def upload_rack(client):
    image = io.BytesIO()
    Image.new("RGB", (320, 180), "blue").save(image, format="JPEG")
    response = client.post(
        "/api/jobs",
        files={"file": ("synthetic-rack.jpg", image.getvalue(), "image/jpeg")},
        data={"provider": "local", "mode": "rack"},
    )
    assert response.status_code == 202
    return response.json()["id"]


def finished_job(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.01)
    pytest.fail(f"Synthetic job did not reach a terminal state: {job}")


def assert_catalogue_consistent(client, job_id, expected_statuses):
    """Compare observable API/export surfaces against explicit expected item statuses."""
    detail = client.get(f"/api/jobs/{job_id}").json()
    expected_counts = Counter(expected_statuses.values())
    listed = next(job for job in client.get("/api/jobs").json()["jobs"] if job["id"] == job_id)
    for field, count in {
        "item_count": len(expected_statuses),
        "usable_count": expected_counts["ready"],
        "retake_count": expected_counts["needs_retake"],
        "unassessed_count": expected_counts["unassessed"],
    }.items():
        assert detail[field] == listed[field] == count
    assert detail["stage"] == listed["stage"]
    assert detail["warnings"] == listed["warnings"]
    for status in ("ready", "needs_retake", "unassessed"):
        filtered = client.get("/api/items", params={"job_id": job_id, "quality": status}).json()["items"]
        assert {item["id"] for item in filtered} == {
            item_id for item_id, expected in expected_statuses.items() if expected == status
        }
    csv_response = client.get("/api/export", params={"format": "csv", "job_id": job_id})
    assert csv_response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(csv_response.content.decode("utf-8-sig"))))
    assert {row["id"]: row["quality_status"] for row in rows} == expected_statuses
    exported = client.get("/api/export", params={"format": "json", "job_id": job_id}).json()["items"]
    assert {item["id"]: item for item in exported} == {item["id"]: item for item in detail["items"]}
    assert {item["id"] for item in exported} == set(expected_statuses)
    return detail, {row["id"]: row for row in rows}


def test_one_job_keeps_ready_fragments_and_provider_failures_distinct(tmp_path):
    fragment = inspection("fragment", category="accessory", subtype="synthetic scarf guess", confidence=0.99)
    provider = ScriptedProviders([inspection(), fragment, ProviderError("Synthetic inspection outage")])
    with studio(tmp_path, provider) as (client, _):
        job_id = upload_rack(client)
        job = finished_job(client, job_id)
        assert job["status"] == "partial"
        assert len(provider.inspections) == 3
        ready, retake, failed = job["items"]
        assert ready["record"] == RECORD
        assert retake["record"] == failed["record"] == unknown_garment()
        assert retake["model_record"]["subtype"] == "synthetic scarf guess"
        assert retake["quality"]["model_visibility"] == "fragment"
        assert failed["error"] == "Synthetic inspection outage"
        _, rows = assert_catalogue_consistent(
            client, job_id, {ready["id"]: "ready", retake["id"]: "needs_retake", failed["id"]: "unassessed"}
        )
        assert rows[retake["id"]]["subtype"] == rows[failed["id"]]["subtype"] == ""
        for item in job["items"]:
            assert client.get(item["image_url"]).status_code == 200


def test_cancellation_after_one_saved_item_preserves_it_and_stops_later_inspections(tmp_path):
    provider = ScriptedProviders([inspection(), BLOCK_UNTIL_CANCELLED, inspection()])
    with studio(tmp_path, provider) as (client, app):
        job_id = upload_rack(client)
        assert provider.block_started.wait(timeout=5), "Second inspection was never reached"
        before = client.get(f"/api/jobs/{job_id}").json()
        assert before["item_count"] == 1 and before["status"] == "running"
        assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
        cancelled = finished_job(client, job_id)
        assert cancelled["status"] == "cancelled"
        assert provider.block_cancelled.wait(timeout=2)
        assert cancelled["items"] == before["items"]
        assert len(provider.inspections) == 2 and len(provider.outcomes) == 1
        assert Store(app.state.settings.data_dir).items(job_id) == before["items"]
        assert_catalogue_consistent(client, job_id, {before["items"][0]["id"]: "ready"})
        assert not app.state.manager.tasks
        assert len(provider.inspections) == 2


@pytest.mark.parametrize("max_items", [1, 2])
def test_item_limit_caps_actual_inspection_calls_and_marks_truncation(tmp_path, max_items):
    provider = ScriptedProviders([inspection() for _ in range(5)], detections=5)
    with studio(tmp_path, provider, max_items=max_items) as (client, app):
        job_id = upload_rack(client)
        job = finished_job(client, job_id)
        assert job["status"] == "partial"
        assert len(provider.inspections) == len(job["items"]) == max_items
        assert len(provider.outcomes) == 5 - max_items
        assert any(f"maximum of {max_items} garments" in warning for warning in job["warnings"])
        segments = json.loads((app.state.settings.data_dir / "jobs" / job_id / "segments.json").read_text())
        assert segments["record_count"] == 5
        assert_catalogue_consistent(client, job_id, {item["id"]: "ready" for item in job["items"]})


def test_zero_detections_produce_empty_exports_without_inspection_or_invented_records(tmp_path):
    provider = ScriptedProviders(detections=0)
    with studio(tmp_path, provider) as (client, _):
        job_id = upload_rack(client)
        job = finished_job(client, job_id)
        assert job["status"] == "completed"
        assert job["stage"] == "No garments matching the prompt were found"
        assert provider.segment_calls == 1 and provider.inspections == []
        assert any("No garments matching" in warning for warning in job["warnings"])
        assert client.get(job["preview_url"]).status_code == 200
        assert_catalogue_consistent(client, job_id, {})


def test_failed_segmentation_can_retry_without_changing_the_original_job_or_source(tmp_path):
    provider = ScriptedProviders([inspection(), inspection()], detections=2)
    provider.segment_error = ProviderError("Synthetic segmentation outage")
    with studio(tmp_path, provider) as (client, _):
        old_id = upload_rack(client)
        old = finished_job(client, old_id)
        assert old["status"] == "failed" and old["error"] == "Synthetic segmentation outage"
        assert provider.inspections == []
        source = client.get(old["source_url"]).content
        assert_catalogue_consistent(client, old_id, {})
        provider.segment_error = None
        retried = client.post(f"/api/jobs/{old_id}/retry")
        assert retried.status_code == 202 and retried.json()["id"] != old_id
        new = finished_job(client, retried.json()["id"])
        assert new["status"] == "completed"
        assert provider.segment_calls == 2 and len(provider.inspections) == 2
        assert client.get(new["source_url"]).content == source
        assert client.get(f"/api/jobs/{old_id}").json() == old
        assert_catalogue_consistent(client, new["id"], {item["id"]: "ready" for item in new["items"]})


def test_human_override_revoke_and_delete_keep_exports_counts_and_evidence_consistent(tmp_path):
    provider = ScriptedProviders([inspection(), inspection("partial")], detections=2)
    with studio(tmp_path, provider) as (client, _):
        job_id = upload_rack(client)
        ready, retake = finished_job(client, job_id)["items"]
        expected = {ready["id"]: "ready", retake["id"]: "needs_retake"}
        assert_catalogue_consistent(client, job_id, expected)
        changed = client.patch(
            f"/api/items/{retake['id']}",
            json={"record": {"category": "dress", "subtype": "Human confirmed dress"}, "reviewed": True},
        )
        assert changed.status_code == 200
        assert changed.json()["quality"] == retake["quality"]
        assert changed.json()["model_record"] == retake["model_record"]
        expected[retake["id"]] = "ready"
        reviewed, rows = assert_catalogue_consistent(client, job_id, expected)
        assert reviewed["stage"] == "Analysis complete" and reviewed["warnings"] == []
        assert rows[retake["id"]]["category"] == "dress"
        assert rows[retake["id"]]["reviewed"] == "True"
        assert rows[retake["id"]]["visibility"] == "partial"
        assert rows[retake["id"]]["quality_reasons"]
        revoked = client.patch(f"/api/items/{retake['id']}", json={"reviewed": False})
        assert revoked.status_code == 200
        assert revoked.json()["quality"] == retake["quality"]
        assert revoked.json()["model_record"] == retake["model_record"]
        expected[retake["id"]] = "needs_retake"
        pending, rows = assert_catalogue_consistent(client, job_id, expected)
        assert "1 regions need retake" in pending["stage"]
        assert rows[retake["id"]]["reviewed"] == "False"
        assert rows[retake["id"]]["category"] == "dress"  # Keep the user's edit when approval is revoked.
        assert client.delete(f"/api/items/{retake['id']}").status_code == 204
        expected.pop(retake["id"])
        remaining, _ = assert_catalogue_consistent(client, job_id, expected)
        assert remaining["stage"] == "Analysis complete" and remaining["warnings"] == []
        assert remaining["items"] == [ready]


@pytest.mark.parametrize("missing", ["target", "visibility", "reason"])
def test_malformed_inspection_keeps_prior_success_and_saves_an_unassessed_crop(tmp_path, missing):
    malformed_inspection = inspection()
    malformed_inspection.pop(missing)
    provider = ScriptedProviders([inspection(), malformed_inspection], detections=2)
    with studio(tmp_path, provider) as (client, _):
        job_id = upload_rack(client)
        job = finished_job(client, job_id)
        assert job["status"] == "partial" and len(provider.inspections) == 2
        ready, malformed = job["items"]
        assert malformed["record"] == unknown_garment()
        assert malformed["quality"]["status"] == "unassessed"
        assert malformed["quality"]["version"] == 3 and malformed["quality"]["model_target"] is None
        assert malformed["error"] and "incomplete fields" in malformed["error"]
        assert client.get(malformed["image_url"]).status_code == 200
        assert_catalogue_consistent(client, job_id, {ready["id"]: "ready", malformed["id"]: "unassessed"})
