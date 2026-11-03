"""Independent export regression: verdicts stay bound to the image they checked."""

import csv
import io
from copy import deepcopy

import pytest
from PIL import Image

from wardrobe.providers import ProviderError
from wardrobe.reconstruction_quality import build_report, validate_planning_diagnostics

from test_reconstruction import (
    ReconstructionProviders,
    quality_report,
    seed_item,
    start,
    studio,
    unchanged_evidence,
    wait_item,
)


def csv_row(client, job_id):
    response = client.get(f"/api/export?format=csv&job_id={job_id}")
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert len(rows) == 1
    return rows[0]


def test_export_retains_image_candidate_and_attempt_verdicts_across_retries(tmp_path):
    provider = ReconstructionProviders(["success", "rejected", "insufficient_source"])
    with studio(tmp_path, provider) as (client, app, _):
        item, paths = seed_item(app)
        original = {name: path.read_bytes() for name, path in paths.items()}

        start(client, item["id"])
        first = wait_item(client, item["id"], "completed")["reconstruction"]
        row = csv_row(client, item["job_id"])
        assert row["reconstruction_quality"] == row["reconstruction_image_quality"] == "accepted"
        assert row["reconstruction_candidate_quality"] == ""
        accepted_bytes = client.get(first["image_url"]).content

        start(client, item["id"])
        second = wait_item(client, item["id"], "rejected")["reconstruction"]
        row = csv_row(client, item["job_id"])
        assert row["reconstruction_url"] == first["image_url"]
        assert row["reconstruction_image_quality"] == "accepted"
        assert row["reconstruction_image_issues"] == ""
        assert row["reconstruction_candidate_url"] == second["candidate_url"]
        assert row["reconstruction_candidate_quality"] == row["reconstruction_quality"] == "rejected"
        assert row["reconstruction_candidate_issues"] == "; ".join(second["candidate_validation"]["issues"])
        rejected_bytes = client.get(second["candidate_url"]).content

        start(client, item["id"])
        final = wait_item(client, item["id"], "needs_review")
        reconstruction = final["reconstruction"]
        assert reconstruction["validation"]["generated"] is False
        assert reconstruction["image_url"] == first["image_url"]
        assert reconstruction["candidate_url"] == second["candidate_url"]
        row = csv_row(client, item["job_id"])
        assert row["reconstruction_quality"] == "needs_review"
        assert row["reconstruction_issues"] == "; ".join(reconstruction["validation"]["issues"])
        assert row["reconstruction_image_quality"] == "accepted"
        assert row["reconstruction_image_issues"] == ""
        assert row["reconstruction_candidate_quality"] == "rejected"
        assert row["reconstruction_candidate_issues"] == "; ".join(second["candidate_validation"]["issues"])
        assert client.get(first["image_url"]).content == accepted_bytes
        assert client.get(second["candidate_url"]).content == rejected_bytes
        assert {name: path.read_bytes() for name, path in paths.items()} == original
        assert len(app.state.store.items(item["job_id"])) == 1
        unchanged_evidence(item, final)


def test_export_marks_retained_legacy_images_without_validation_unverified(tmp_path):
    with studio(tmp_path) as (client, app, _):
        item, paths = seed_item(app)
        directory = paths["source.jpg"].parent / "reconstructions"
        directory.mkdir()
        (directory / "legacy.png").write_bytes(paths["cutout.png"].read_bytes())
        url = f"/media/{item['job_id']}/reconstructions/legacy.png"
        app.state.store.update_reconstruction(item["id"], {"status": "completed", "image_url": url})
        row = csv_row(client, item["job_id"])
        assert row["reconstruction_url"] == url
        assert row["reconstruction_image_quality"] == "unverified"
        assert row["reconstruction_image_issues"] == ""
        assert row["reconstruction_candidate_quality"] == ""


def test_api_and_csv_follow_selected_candidate_when_a_later_revision_regresses(tmp_path):
    first = quality_report("needs_review")
    second = deepcopy(quality_report("rejected")["attempts"][0])
    second["number"] = 2
    report = build_report(first["plan"], [first["attempts"][0], second], selected_attempt=1)

    class SelectedCandidateProvider(ReconstructionProviders):
        async def reconstruct(self, cutout_path, context_path, record, destination):
            Image.new("RGB", (64, 80), "blue").save(destination, "PNG")
            return report

    with studio(tmp_path, SelectedCandidateProvider()) as (client, app, _):
        item, paths = seed_item(app)
        original_bytes = {name: path.read_bytes() for name, path in paths.items()}
        start(client, item["id"])
        final = wait_item(client, item["id"], "needs_review")
        saved = final["reconstruction"]
        assert saved["image_url"] is None
        assert saved["candidate_validation"]["selected_attempt"] == 1
        assert saved["candidate_validation"]["attempts"][1]["status"] == "rejected"
        assert saved["candidate_validation"]["checks"] == first["checks"]
        assert client.get(saved["candidate_url"]).status_code == 200
        row = csv_row(client, item["job_id"])
        assert row["reconstruction_candidate_quality"] == "needs_review"
        assert row["reconstruction_candidate_issues"] == "; ".join(first["issues"])
        assert {name: path.read_bytes() for name, path in paths.items()} == original_bytes
        unchanged_evidence(item, final)


@pytest.mark.parametrize("unsafe", [False, True])
def test_failed_source_planning_keeps_safe_diagnostics_and_prior_image(tmp_path, unsafe):
    diagnostics = [
        {
            "stage": "source_plan",
            "attempt": number,
            "status": "invalid",
            "issues": [{"code": "duplicate", "path": ["visible_features"]}],
        }
        for number in (1, 2)
    ]
    if unsafe:
        diagnostics[0]["raw_exception"] = "synthetic-private-key"

    class DiagnosticProvider(ReconstructionProviders):
        def __init__(self):
            super().__init__()
            self.next_error = False

        async def reconstruct(self, *args, **kwargs):
            if self.next_error:
                self.next_error = False
                error = ProviderError("Source planning format is incomplete; please retry.")
                error.diagnostics = deepcopy(diagnostics)
                raise error
            return await super().reconstruct(*args, **kwargs)

    provider = DiagnosticProvider()
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        start(client, item["id"])
        first = wait_item(client, item["id"], "completed")["reconstruction"]
        original_image = client.get(first["image_url"]).content
        provider.next_error = True
        start(client, item["id"])
        failed_item = wait_item(client, item["id"], "failed")
        failed = failed_item["reconstruction"]
        assert failed["planning_diagnostics"] == (
            [] if unsafe else validate_planning_diagnostics(diagnostics)
        )
        assert failed["image_url"] == first["image_url"]
        assert failed["image_validation"] == first["image_validation"]
        assert client.get(first["image_url"]).content == original_image
        assert "synthetic-private-key" not in client.get("/api/export?format=json").text
        unchanged_evidence(item, failed_item)
        queued = start(client, item["id"])
        assert queued["reconstruction"]["planning_diagnostics"] == []
        completed = wait_item(client, item["id"], "completed")
        assert completed["reconstruction"]["planning_diagnostics"] == []
        unchanged_evidence(item, completed)
