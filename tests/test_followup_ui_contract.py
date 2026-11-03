"""Self-contained UI contracts for analysis versus follow-up completion."""

import json
import subprocess

import pytest

from test_preview_ui_contract import fixture_item, preview_report, shopping_result
from test_reconstruction_ui_contract import NODE, ROOT

pytestmark = pytest.mark.skipif(NODE is None, reason="The frontend contract needs Node")

PROBE = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('wardrobe/static/app.js', 'utf8');
const boundary = source.indexOf('  document.addEventListener("click", event => {');
if (boundary < 0) throw new Error('Update the UI event-binding boundary in this harness.');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const context = { URL, window: { location: { origin: 'http://127.0.0.1:18500' } }, input };
vm.runInNewContext(source.slice(0, boundary) + `
  state.items = input.items;
  globalThis.result = jobFollowupSummary({ id: 'fixture-job', status: 'completed', item_count: 2 });
})();`, context, { timeout: 2000 });
process.stdout.write(JSON.stringify(context.result));
"""


def summary(items):
    process = subprocess.run(
        [NODE, "-e", PROBE],
        input=json.dumps({"items": items}),
        text=True,
        capture_output=True,
        cwd=ROOT,
        check=True,
    )
    result = json.loads(process.stdout)
    return result, " ".join(line["text"] for line in result["lines"])


def test_completed_analysis_does_not_hide_queued_or_running_followups():
    items = [fixture_item("a"), fixture_item("b")]
    for item, status in zip(items, ("running", "queued"), strict=True):
        item["reconstruction"] = {"status": status}
        item["discovery"] = {"status": status}
    result, text = summary(items)
    assert result["active"] is True
    assert "1 generating · 1 waiting" in text
    assert "1 searching · 1 waiting" in text


def test_ready_preview_with_fidelity_mismatch_is_not_called_failed_or_compared():
    item = fixture_item("a", preview_report(mismatch=True))
    result, text = summary([item])
    assert result["active"] is False
    assert "1 generated (with inferences) · 1 with known differences" in text
    assert "compared" not in text and "failed" not in text


def test_preview_summary_uses_its_bound_report_not_unrelated_latest_attempt():
    item = fixture_item("a", preview_report("side"))
    item["reconstruction"]["validation"] = preview_report(mismatch=True)
    _, text = summary([item])
    assert "1 to review" in text
    assert "with known differences" not in text


def test_legacy_preview_without_bound_proof_does_not_claim_preview_or_fidelity():
    item = fixture_item("a", preview_report(mismatch=True))
    item["reconstruction"]["preview_validation"] = None
    _, text = summary([item])
    assert "1 results to review" in text
    assert "generated" not in text and "with known differences" not in text


@pytest.mark.parametrize("status", ["running", "failed", "cancelled"])
def test_retained_preview_is_not_misreported_as_latest_success(status):
    item = fixture_item("a", preview_report("side"))
    item["reconstruction"]["status"] = status
    item["discovery"] = {"status": status, "result": shopping_result()}
    result, text = summary([item])
    assert result["active"] is (status == "running")
    assert "previous previews preserved" in text
    assert "previous results preserved" in text
    assert "generated" not in text


@pytest.mark.parametrize("version, expected", [(1, 0), (2, 1)])
def test_only_current_verified_shopping_contract_counts_offers(version, expected):
    item = fixture_item("a")
    result = shopping_result()
    result["shopping"]["version"] = version
    item["discovery"] = {"status": "completed", "result": result}
    _, text = summary([item])
    assert ("1 products verified" in text) is bool(expected)
    assert ("no verified products yet" in text) is not bool(expected)


def test_no_tasks_and_unrelated_jobs_do_not_invent_followup_work():
    item = fixture_item("a")
    other = fixture_item("b")
    other.update(job_id="another-job", reconstruction={"status": "running"})
    result, text = summary([item, other])
    assert result == {"active": False, "lines": [], "count": 1}
    assert text == ""
