"""Frontend parity for original front-opening evidence; explicit offline fixtures."""

import copy
import json
import subprocess

import pytest

from wardrobe.reconstruction_quality import assess_fidelity, build_report, is_accepted, is_preview_ready

from test_multiview_ui_contract import source_manifest
from test_reconstruction_ui_contract import NODE, NODE_PROBE, ROOT, fixture_report

pytestmark = pytest.mark.skipif(NODE is None, reason="Node is required for actual frontend helper checks")

FRONT_REASON = "主照前襟為「敞開分離」，候選為「靠攏閉合」，未保留主照展示狀態。"
OTHER_REASON = "明確測試：另有可見領口差異，必須保留這項原有失敗。"


def observation(state, primary=False):
    if state is None:
        return None
    value = {
        "state": state,
        "evidence": "明確介面測試：獨立觀察的前襟展示狀態。",
        "box": None if state in ("unknown", "not_applicable") else [0.2, 0.1, 0.8, 0.9],
    }
    if primary:
        value["source_image"] = "context"
    return value


def primary_report(source="open", candidate="open", *, initial=True, prior_fail=False):
    base = fixture_report()
    plan = base["plan"]
    plan["primary_front_state"] = observation(source, primary=True)
    plan["visible_features"].append({"feature": "closures", "detail": "明確測試：保留主照前襟展示。"})
    if initial:
        plan["initial_source_plan"] = copy.deepcopy(plan)
        plan["original_ambiguous_regions"] = []
    answer = {
        "observed_view": "front",
        "candidate_front_state": observation(candidate),
        "checks": base["checks"],
        "feature_checks": [
            *base["feature_checks"],
            {"feature": "closures", "status": "pass", "reason": "明確測試：前襟細節觀察。"},
        ],
        "corrections": [],
        "region_boxes": [],
        "preview_observations": {
            name: {"status": "pass", "reason": "明確介面測試：基本展示條件通過。"}
            for name in ("category", "coarse_color")
        },
    }
    if prior_fail:
        for name in ("visible_details", "cut_structure"):
            answer["checks"][name] = {"status": "fail", "reason": OTHER_REASON}
        answer["feature_checks"][-1].update(status="fail", reason=OTHER_REASON)
    return build_report(plan, [assess_fidelity(plan, answer, 1)])


def set_candidate(report, state):
    value = observation(state)
    report["candidate_front_state"] = copy.deepcopy(value)
    report["attempts"][0]["candidate_front_state"] = value


def valid_cases():
    rows = [
        ("open-preserved", primary_report(), True, True),
        ("partly-open-preserved", primary_report("partly_open", "partly_open"), True, True),
        ("closed-preserved", primary_report("closed", "closed"), True, True),
        ("overlapping-preserved", primary_report("overlapping", "overlapping"), True, True),
        ("closed-mismatch-preview-retained", primary_report(candidate="closed"), False, True),
        ("overlap-mismatch-preview-retained", primary_report(candidate="overlapping"), False, True),
        ("candidate-missing-unverified", primary_report(candidate=None), False, True),
        ("candidate-unknown-unverified", primary_report(candidate="unknown"), False, True),
        ("candidate-na-unverified", primary_report(candidate="not_applicable"), False, True),
        ("source-unknown-unverified", primary_report(source="unknown"), False, True),
        ("source-missing-candidate-present-unverified", primary_report(None, "open"), False, True),
        ("na-candidate-missing-compatible", primary_report("not_applicable", None), True, True),
        ("na-candidate-unknown-compatible", primary_report("not_applicable", "unknown"), True, True),
        ("legacy-null-no-new-claim", primary_report(None, None), True, True),
        (
            "other-failures-retain-front-warning",
            primary_report(candidate="closed", prior_fail=True),
            False,
            True,
        ),
    ]
    legacy = primary_report(None, None)
    legacy["plan"].pop("primary_front_state")
    legacy["plan"]["initial_source_plan"].pop("primary_front_state")
    legacy.pop("candidate_front_state")
    legacy["attempts"][0].pop("candidate_front_state")
    rows.append(("legacy-missing-no-new-claim", legacy, True, True))
    cutout = primary_report()
    cutout["plan"]["primary_front_state"]["source_image"] = "cutout"
    cutout["plan"]["initial_source_plan"]["primary_front_state"]["source_image"] = "cutout"
    rows.append(("cutout-primary-compatible", cutout, True, True))
    multi = primary_report()
    multi["plan"]["reference_manifest"] = source_manifest()
    rows.append(("multiview-primary-context-retained", multi, True, True))
    second = primary_report(candidate="closed")["attempts"][0]
    second["number"] = 2
    selected_first = build_report(
        primary_report()["plan"], [primary_report()["attempts"][0], second], selected_attempt=1
    )
    rows.append(("selected-first-not-last-observation", selected_first, True, True))
    return rows


def invalid_cases():
    rows = []
    for name, mutate in [
        ("forged-allpass-closed", lambda r: set_candidate(r, "closed")),
        ("forged-allpass-overlap", lambda r: set_candidate(r, "overlapping")),
        ("forged-allpass-partly-open", lambda r: set_candidate(r, "partly_open")),
        ("forged-allpass-candidate-missing", lambda r: set_candidate(r, None)),
        ("forged-allpass-candidate-unknown", lambda r: set_candidate(r, "unknown")),
        ("forged-allpass-candidate-na", lambda r: set_candidate(r, "not_applicable")),
        ("initial-state-cannot-change", lambda r: r["plan"]["primary_front_state"].update(state="closed")),
        (
            "initial-evidence-cannot-change",
            lambda r: r["plan"]["primary_front_state"].update(evidence="後續替換主照觀察。"),
        ),
        ("initial-state-cannot-be-erased", lambda r: r["plan"].update(primary_front_state=None)),
        (
            "legacy-initial-cannot-gain-made-up-state",
            lambda r: r["plan"]["initial_source_plan"].update(primary_front_state=None),
        ),
        (
            "extra-original-cannot-be-primary",
            lambda r: r["plan"]["primary_front_state"].update(source_image="extra_1"),
        ),
        (
            "selected-observation-cannot-change",
            lambda r: r["candidate_front_state"].update(evidence="另一張候選觀察。"),
        ),
        ("selected-observation-cannot-disappear", lambda r: r.update(candidate_front_state=None)),
        ("selected-attempt-cannot-be-missing", lambda r: r.update(selected_attempt=2)),
        ("candidate-must-have-box", lambda r: r["candidate_front_state"].update(box=None)),
        (
            "candidate-box-must-be-normalized",
            lambda r: r["candidate_front_state"].update(box=[0.1, 0.2, 1.2, 0.9]),
        ),
        (
            "candidate-box-cannot-be-empty",
            lambda r: r["candidate_front_state"].update(box=[0.2, 0.1, 0.2, 0.8]),
        ),
        ("candidate-cannot-name-source", lambda r: r["candidate_front_state"].update(source_image="extra_1")),
        ("source-state-cannot-be-string", lambda r: r["plan"].update(primary_front_state="open")),
    ]:
        report = primary_report()
        mutate(report)
        rows.append((name, report, False, False))
    for name, mutate in [
        ("primary-cannot-use-extra-source", lambda s: s.update(source_image="extra_1")),
        ("primary-must-match-manifest", lambda s: s.update(source_image="cutout")),
        ("primary-evidence-required", lambda s: s.update(evidence=" ")),
        ("primary-evidence-bounded", lambda s: s.update(evidence="字" * 601)),
        ("primary-box-required", lambda s: s.update(box=None)),
        ("primary-box-key-required", lambda s: s.pop("box")),
        ("primary-box-numeric", lambda s: s.update(box=["0.2", 0.1, 0.8, 0.9])),
    ]:
        report = primary_report()
        report["plan"]["reference_manifest"] = source_manifest()
        mutate(report["plan"]["primary_front_state"])
        report["plan"]["initial_source_plan"]["primary_front_state"] = copy.deepcopy(
            report["plan"]["primary_front_state"]
        )
        rows.append((name, report, False, False))
    omitted = primary_report(candidate="closed")
    for container in (omitted, omitted["attempts"][0]):
        container["preview"]["mismatch_warnings"] = []
    rows.append(("preview-cannot-hide-front-warning", omitted, False, False))
    omitted_other = primary_report(candidate="closed", prior_fail=True)
    for container in (omitted_other, omitted_other["attempts"][0]):
        container["preview"]["mismatch_warnings"] = [OTHER_REASON]
    rows.append(("other-failures-cannot-hide-front-warning", omitted_other, False, False))
    bad_closure = primary_report(candidate="closed")
    for container in (bad_closure, bad_closure["attempts"][0]):
        container["feature_checks"][-1].update(status="pass")
    rows.append(("closures-cannot-pass-front-mismatch", bad_closure, False, False))
    bad_structure = primary_report(candidate="closed")
    for container in (bad_structure, bad_structure["attempts"][0]):
        container["checks"]["cut_structure"].update(status="uncertain")
    rows.append(("structure-cannot-downgrade-front-mismatch", bad_structure, False, False))
    initial_unknown = primary_report()
    for plan in (initial_unknown["plan"], initial_unknown["plan"]["initial_source_plan"]):
        plan["primary_front_state"] = observation("unknown", primary=True)
    rows.append(("source-unknown-cannot-pass", initial_unknown, False, False))
    source_missing = primary_report()
    for plan in (source_missing["plan"], source_missing["plan"]["initial_source_plan"]):
        plan["primary_front_state"] = None
    rows.append(("source-missing-candidate-present-cannot-pass", source_missing, False, False))
    return rows


def cases():
    return valid_cases() + invalid_cases()


PROBE = NODE_PROBE.replace(
    "accepted: acceptedReconstructionReport(input),",
    "accepted: acceptedReconstructionReport(input), preview: readyPreviewReport(input),",
)


def ui_result(report):
    probe = PROBE
    manifest = report.get("plan", {}).get("reference_manifest")
    if manifest:
        target = manifest["target_item_id"]
        job = next(source["job_id"] for source in manifest["sources"] if source["id"] == "cutout")
        probe = probe.replace(
            "id: 'explicit-ui-fixture', job_id: 'fixture-job'", f"id: '{target}', job_id: '{job}'"
        )
    response = subprocess.run(
        [NODE, "-e", probe],
        cwd=ROOT,
        input=json.dumps(report, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=True,
        timeout=5,
    )
    return json.loads(response.stdout)


@pytest.mark.parametrize("name,report,strict,preview", cases(), ids=[row[0] for row in cases()])
def test_front_state_ui_backend_parity(name, report, strict, preview):
    actual = ui_result(report)
    assert actual["accepted"] == is_accepted(report) == strict, name
    assert actual["preview"] == is_preview_ready(report) == preview, name
    assert actual["verifiedLabel"] is strict, name
    assert {"context", "source", "cutout", "candidate"}.issubset(entry["key"] for entry in actual["media"])
    if not strict:
        assert not actual["image"] and actual["defaultMedia"] == "context"
    if name == "other-failures-retain-front-warning":
        assert report["preview"]["mismatch_warnings"] == [OTHER_REASON, FRONT_REASON]
