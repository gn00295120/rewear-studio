"""CSV preserves the distinction between a usable preview and faithful reconstruction."""

import csv
import io
import json
from copy import deepcopy
from pathlib import Path

import pytest

from test_reconstruction import quality_report, seed_item, studio


@pytest.mark.parametrize("valid", [True, False])
def test_csv_preview_never_promotes_known_fidelity_failure(tmp_path, valid):
    report = quality_report("rejected")
    assert report["preview"]["status"] == "ready"
    if not valid:
        report = deepcopy(report)
        report["preview"]["checks"]["complete_outline"]["status"] = "fail"
    with studio(tmp_path) as (client, app, _):
        item, _ = seed_item(
            app,
            reconstruction={
                "status": "rejected",
                "validation": report,
                "preview_url": "/media/example/preview.png",
                "preview_validation": report,
                "is_synthetic": True,
            },
        )
        response = client.get(f"/api/export?format=csv&job_id={item['job_id']}")
        row = next(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
        assert row["reconstruction_status"] == "rejected"
        assert row["reconstruction_url"] == ""
        if valid:
            assert row["reconstruction_preview_url"] == "/media/example/preview.png"
            assert row["reconstruction_preview_status"] == "ready"
            assert "袖口橫條" in row["reconstruction_preview_warnings"]
        else:
            assert row["reconstruction_preview_url"] == ""
            assert row["reconstruction_preview_status"] == ""


def test_csv_legacy_acceptance_is_revalidated_without_mutating_stored_evidence(tmp_path):
    fixture = Path(__file__).parent / "fixtures/reconstruction_runner_legacy.json"
    report = json.loads(fixture.read_text())["report"]
    assert report["status"] == "accepted"
    state = {
        "status": "completed",
        "image_url": "/media/example/image.png",
        "candidate_url": "/media/example/image.png",
        **{key: deepcopy(report) for key in ("validation", "image_validation", "candidate_validation")},
    }
    with studio(tmp_path) as (client, app, _):
        item, _ = seed_item(app, reconstruction=state)
        response = client.get(f"/api/export?format=csv&job_id={item['job_id']}")
        row = next(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
        for field in (
            "reconstruction_quality",
            "reconstruction_image_quality",
            "reconstruction_candidate_quality",
        ):
            assert row[field] == "needs_review"
        assert row["reconstruction_issues"]
        assert app.state.store.item(item["id"])["reconstruction"] == state
