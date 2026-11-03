"""Primary front presentation survives extra views and independent audit.

Model answers are explicit simulations of the observed failure mode. These
checks establish deterministic enforcement, not an improvement in image quality.
"""

import copy
import json
from typing import get_args

import pytest
from pydantic import ValidationError

from wardrobe.providers import ProviderError
from wardrobe.reconstruction_quality import (
    FIDELITY_SCHEMA,
    PLAN_SCHEMA,
    Feature,
    FrontStateObservation,
    PreviewObservations,
    ReconstructionAttempt,
    ReconstructionPlan,
    _preview_assessment,
    assess_fidelity,
    build_report,
    front_state_comparison,
    is_accepted,
    is_preview_ready,
    reconstruction_repair_targets,
    select_attempt,
    validate_report,
)

from test_reconstruction_quality import (
    chat_response,
    create_provider,
    encoded_image,
    fidelity_answer,
    image_response,
    regional_fixture,
    source_plan,
)
from test_reconstruction_sources import bundle_fixture, filled_manifest, garment, matching_region_checks


KNOWN = ("open", "partly_open", "closed", "overlapping")


def observation(state="open"):
    return {
        "state": state,
        "evidence": {
            "open": "兩片前身邊緣從胸前至下襬明顯分離，中央有連續開口。",
            "partly_open": "上半前身分開，下半前片邊緣相接。",
            "closed": "兩片前身沿軀幹靠攏，僅領口留有小開口。",
            "overlapping": "一片前身跨過另一片，中央呈明顯交疊。",
            "unknown": "遮擋使前片相對關係無法確認。",
            "not_applicable": "這件套頭衣物沒有可分離前襟。",
        }[state],
        "box": [0.25, 0.2, 0.75, 0.9] if state in KNOWN else None,
    }


def primary_plan(state="open", *, closures=True):
    plan = source_plan("front")
    plan["primary_front_state"] = {**observation(state), "source_image": "context"}
    if closures:
        plan["visible_features"].append({"feature": "closures", "detail": "主照白色鈕扣門襟，兩片前身敞開。"})
    return plan


def candidate_answer(plan, state="open"):
    answer = fidelity_answer("front")
    answer["candidate_front_state"] = observation(state) if state is not None else None
    answer["feature_checks"] = [
        {"feature": feature["feature"], "status": "pass", "reason": "模擬模型錯誤地聲稱所有可見細節相符。"}
        for feature in plan["visible_features"]
    ]
    return answer


@pytest.mark.parametrize("source", KNOWN)
@pytest.mark.parametrize("candidate", KNOWN)
def test_independent_definite_state_mismatch_cannot_hide_behind_global_or_closure_pass(source, candidate):
    plan = primary_plan(source)
    answer = candidate_answer(plan, candidate)
    attempt = assess_fidelity(plan, answer, 1)
    expected = "pass" if source == candidate else "fail"
    assert attempt["checks"]["cut_structure"]["status"] == expected
    assert attempt["checks"]["visible_details"]["status"] == expected
    assert attempt["feature_checks"][-1]["status"] == expected
    assert answer["checks"]["cut_structure"]["status"] == "pass"  # caller's raw answer is retained
    report = build_report(plan, [attempt])
    assert is_accepted(report) is (source == candidate)
    assert report["candidate_front_state"] == observation(candidate)
    if source != candidate:
        assert "closures" in reconstruction_repair_targets(plan, attempt)["features"]
        compared = front_state_comparison(
            ReconstructionPlan.model_validate(plan),
            FrontStateObservation.model_validate(observation(candidate)),
        )
        assert compared.reason in report["preview"]["mismatch_warnings"]
        assert is_preview_ready(report)  # a fidelity failure cannot become an exact-item claim


@pytest.mark.parametrize("candidate", [None, "unknown", "not_applicable"])
def test_known_primary_missing_or_unknown_candidate_never_defaults_to_pass(candidate):
    plan = primary_plan()
    attempt = assess_fidelity(plan, candidate_answer(plan, candidate), 1)
    assert attempt["status"] == "needs_review"
    assert attempt["checks"]["cut_structure"]["status"] == "uncertain"
    assert attempt["feature_checks"][-1]["status"] == "uncertain"
    assert not any(reconstruction_repair_targets(plan, attempt).values())
    assert not is_accepted(build_report(plan, [attempt]))


def test_unknown_primary_is_not_changed_by_a_confident_candidate():
    plan = primary_plan("unknown")
    attempt = assess_fidelity(plan, candidate_answer(plan, "closed"), 1)
    assert attempt["status"] == "needs_review"
    assert attempt["checks"]["visible_details"]["status"] == "uncertain"
    assert not any(reconstruction_repair_targets(plan, attempt).values())


@pytest.mark.parametrize("candidate", [None, "unknown", "not_applicable"])
def test_not_applicable_pullover_adds_no_front_state_obstacle(candidate):
    plan = primary_plan("not_applicable", closures=False)
    plan["garment_type"] = "T-shirt"
    report = build_report(plan, [assess_fidelity(plan, candidate_answer(plan, candidate), 1)])
    assert is_accepted(report) and is_preview_ready(report)


def test_only_both_absent_are_legacy_and_a_one_sided_observation_stays_unverified():
    plan = primary_plan()
    del plan["primary_front_state"]
    answer = candidate_answer(plan, None)
    del answer["candidate_front_state"]
    legacy = build_report(plan, [assess_fidelity(plan, answer, 1)])
    assert is_accepted(legacy)
    legacy["plan"].pop("primary_front_state")
    legacy["attempts"][0].pop("candidate_front_state")
    legacy.pop("candidate_front_state")
    assert is_accepted(legacy)
    half = assess_fidelity(plan, candidate_answer(plan, "closed"), 1)
    assert half["status"] == "needs_review"
    assert half["checks"]["cut_structure"]["status"] == "uncertain"
    assert not is_accepted(build_report(plan, [half]))


@pytest.mark.parametrize("candidate", ["open", "closed", "unknown"])
def test_front_observation_never_downgrades_other_failures_and_keeps_all_warnings(candidate):
    plan = primary_plan()
    answer = candidate_answer(plan, candidate)
    for name in ("visible_details", "cut_structure"):
        answer["checks"][name] = {"status": "fail", "reason": "原有袖口構造錯誤。"}
    answer["feature_checks"][-1].update(status="fail", reason="原有鈕扣構造錯誤。")
    attempt = assess_fidelity(plan, answer, 1)
    assert attempt["checks"]["cut_structure"] == {"status": "fail", "reason": "原有袖口構造錯誤。"}
    assert attempt["feature_checks"][-1]["reason"] == "原有鈕扣構造錯誤。"
    assert attempt["status"] == "rejected"
    report = build_report(plan, [attempt])
    warnings = report["preview"]["mismatch_warnings"]
    assert "原有袖口構造錯誤。" in warnings and "原有鈕扣構造錯誤。" in warnings
    if candidate == "closed":
        assert any("未保留主照展示狀態" in warning for warning in warnings)


def test_typed_state_failure_is_actionable_without_an_optional_closures_feature_or_roi():
    plan = primary_plan(closures=False)
    attempt = assess_fidelity(plan, candidate_answer(plan, "overlapping"), 1)
    targets = reconstruction_repair_targets(plan, attempt)
    assert targets["features"] == ["closures"] and targets["regions"] == []
    assert set(targets["checks"]) == {"visible_details", "cut_structure"}


def test_new_front_state_regression_cannot_hide_behind_an_existing_global_failure():
    plan = primary_plan(closures=False)
    first = candidate_answer(plan, "open")
    first["feature_checks"][0].update(status="fail", reason="格紋錯誤。")
    first["checks"]["cut_structure"] = {"status": "fail", "reason": "原有袖口錯誤。"}
    second = candidate_answer(plan, "closed")
    second["checks"]["cut_structure"] = {"status": "fail", "reason": "原有袖口錯誤。"}
    attempts = [assess_fidelity(plan, first, 1), assess_fidelity(plan, second, 2)]
    assert attempts[1]["feature_checks"][0]["status"] == "pass"
    assert select_attempt(attempts, plan=plan) == 1
    report = build_report(plan, attempts, selected_attempt=1)
    assert report["candidate_front_state"]["state"] == "open"


@pytest.mark.parametrize("field", ["primary_front_state", "candidate_front_state"])
def test_definite_states_require_real_located_evidence(field):
    plan = primary_plan()
    if field == "primary_front_state":
        plan[field]["box"] = None
        with pytest.raises(ValidationError, match="located image observation"):
            ReconstructionPlan.model_validate(plan)
    else:
        bad = observation("closed")
        bad["box"] = None
        with pytest.raises(ValidationError, match="located image observation"):
            FrontStateObservation.model_validate(bad)


@pytest.mark.parametrize("change", ["state", "source", "evidence", "missing"])
def test_refinement_cannot_replace_or_drop_primary_state(change):
    initial = primary_plan()
    final = {**copy.deepcopy(initial), "initial_source_plan": copy.deepcopy(initial)}
    if change == "state":
        final["primary_front_state"]["state"] = "closed"
    elif change == "source":
        final["primary_front_state"]["source_image"] = "cutout"
    elif change == "evidence":
        final["primary_front_state"]["evidence"] = "補照只有領口敞開。"
    else:
        final["primary_front_state"] = None
    with pytest.raises(ValidationError, match="cannot change the primary"):
        ReconstructionPlan.model_validate(final)


def test_primary_state_is_bound_to_actual_primary_not_extra_or_mask(tmp_path):
    bundle = bundle_fixture(tmp_path)
    plan = {**primary_plan(), "reference_manifest": filled_manifest(bundle)}
    for source in ("cutout", "extra_1"):
        plan["primary_front_state"]["source_image"] = source
        with pytest.raises(ValidationError):
            ReconstructionPlan.model_validate(plan)


@pytest.mark.parametrize(
    "forgery", ["candidate", "closure-pass", "check-pass", "selected", "warning", "half-legacy"]
)
def test_stored_report_cannot_hide_state_mismatch_or_borrow_another_candidate(forgery):
    plan = primary_plan()
    report = build_report(plan, [assess_fidelity(plan, candidate_answer(plan, "closed"), 1)])
    if forgery == "candidate":
        report = build_report(plan, [assess_fidelity(plan, candidate_answer(plan, "open"), 1)])
        report["candidate_front_state"] = observation("closed")
        report["attempts"][0]["candidate_front_state"] = observation("closed")
    elif forgery == "closure-pass":
        report["feature_checks"][-1]["status"] = "pass"
        report["attempts"][0]["feature_checks"][-1]["status"] = "pass"
    elif forgery == "check-pass":
        report["checks"]["cut_structure"]["status"] = "pass"
        report["attempts"][0]["checks"]["cut_structure"]["status"] = "pass"
    elif forgery == "selected":
        report["candidate_front_state"] = observation("open")
    elif forgery == "warning":
        report["preview"]["mismatch_warnings"] = []
        report["attempts"][0]["preview"]["mismatch_warnings"] = []
    else:
        report = build_report(plan, [assess_fidelity(plan, candidate_answer(plan, "open"), 1)])
        report["plan"]["primary_front_state"] = None
    assert not is_accepted(report)
    with pytest.raises(ValueError):
        validate_report(report)


def test_new_wire_schema_requires_nonnullable_state_objects_while_stored_legacy_is_readable():
    assert "primary_front_state" in PLAN_SCHEMA["required"]
    assert PLAN_SCHEMA["properties"]["primary_front_state"] == {"$ref": "#/$defs/PrimaryFrontState"}
    assert "candidate_front_state" in FIDELITY_SCHEMA["required"]
    assert FIDELITY_SCHEMA["properties"]["candidate_front_state"] == {"$ref": "#/$defs/FrontStateObservation"}
    assert set(PLAN_SCHEMA["$defs"]["PrimaryFrontState"]["required"]) == {
        "state",
        "source_image",
        "box",
        "evidence",
    }


async def test_extra_view_refinement_cannot_overwrite_primary_and_existing_loop_repairs_state(tmp_path):
    bundle = bundle_fixture(tmp_path)
    plan, _, checks = regional_fixture("pattern")
    primary = primary_plan()
    plan["primary_front_state"] = primary["primary_front_state"]
    plan["visible_features"].append(primary["visible_features"][-1])
    plan["detail_regions"][0]["source_image"] = "extra_1"
    bad_extra_description = "補照門襟僅最上方一至兩顆解開，下方前片靠攏。"
    checks = matching_region_checks(checks)
    calls, edits = [], []

    def respond(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/images/edits"):
            calls.append("edit")
            edits.append(body)
            text = body["prompt"]
            assert bad_extra_description not in text
            assert "immutable display state" in text
            assert '"primary_front_state": {"state": "open"' in text
            if len(edits) == 2:
                targets = json.loads(
                    text.split("Typed repair targets, if this is a retry (JSON data): ")[1].split(
                        "\nReference manifest"
                    )[0]
                )
                assert "closures" in targets["features"]
                assert "hidden fasteners" in text
            return image_response("green" if len(edits) == 1 else "blue")
        name = body["response_format"]["json_schema"]["name"]
        calls.append(name)
        if name == "reconstruction_plan":
            assert "primary_front_state" in body["response_format"]["json_schema"]["schema"]["required"]
            return chat_response(plan)
        if name == "reconstruction_source_details":
            return chat_response(
                {
                    "detail_regions": plan["detail_regions"],
                    "additional_regions": [],
                    "visible_features": [{"feature": "closures", "detail": bad_extra_description}],
                }
            )
        if name == "reconstruction_fidelity":
            answer = candidate_answer(plan, "closed" if len(edits) == 1 else "open")
            answer["region_boxes"] = [
                {"name": plan["detail_regions"][0]["name"], "box": [0.2, 0.2, 0.8, 0.8]}
            ]
            return chat_response(answer)
        assert name == "reconstruction_regional_fidelity"
        return chat_response({"regional_checks": checks})

    provider = create_provider(tmp_path, respond)
    try:
        report = await provider.reconstruct(
            bundle.paths["cutout"],
            bundle.paths["context"],
            garment(),
            tmp_path / "candidate.png",
            source_bundle=bundle,
        )
    finally:
        await provider.close()
    assert len(calls) == 8 and len(edits) == 2  # no additional planning/audit phase
    assert (
        report["plan"]["primary_front_state"]
        == report["plan"]["initial_source_plan"]["primary_front_state"]
        == primary["primary_front_state"]
    )
    assert report["plan"]["visible_features"][-1] == primary["visible_features"][-1]
    assert bad_extra_description in json.dumps(report["plan"]["planning_attempts"], ensure_ascii=False)
    assert report["attempts"][0]["status"] == "rejected"
    assert report["selected_attempt"] == 2 and report["candidate_front_state"]["state"] == "open"
    assert is_accepted(report)


@pytest.mark.parametrize("defect", ["missing", "null", "wrong-primary", "unlocated"])
async def test_invalid_fresh_primary_answer_has_only_bounded_shape_retry_and_no_generation(tmp_path, defect):
    bundle = bundle_fixture(tmp_path)
    plan = primary_plan()
    if defect == "missing":
        del plan["primary_front_state"]
    elif defect == "null":
        plan["primary_front_state"] = None
    elif defect == "wrong-primary":
        plan["primary_front_state"]["source_image"] = "cutout"
    else:
        plan["primary_front_state"]["box"] = None
    calls = []

    def respond(request):
        calls.append(request)
        assert request.url.path.endswith("/chat/completions")
        return chat_response(plan)

    provider = create_provider(tmp_path, respond)
    try:
        with pytest.raises(ProviderError):
            await provider.reconstruct(
                bundle.paths["cutout"],
                bundle.paths["context"],
                garment(),
                tmp_path / "candidate.png",
                source_bundle=bundle,
            )
    finally:
        await provider.close()
    assert len(calls) == 2 and not (tmp_path / "candidate.png").exists()


async def test_missing_fresh_candidate_observation_retains_existing_image_as_unverified(tmp_path):
    bundle = bundle_fixture(tmp_path)
    plan = {**primary_plan(), "reference_manifest": filled_manifest(bundle)}
    candidate = tmp_path / "candidate.png"
    before = encoded_image("green")
    candidate.write_bytes(before)
    answer = candidate_answer(plan)
    del answer["candidate_front_state"]
    calls = []

    def respond(request):
        calls.append(request)
        assert request.url.path.endswith("/chat/completions")
        return chat_response(answer)

    provider = create_provider(tmp_path, respond)
    try:
        report = await provider.inspect_reconstruction(
            plan, bundle.paths["cutout"], bundle.paths["context"], candidate, source_bundle=bundle
        )
    finally:
        await provider.close()
    assert len(calls) == 2 and candidate.read_bytes() == before
    assert report["status"] == "needs_review" and report["candidate_front_state"] is None
    assert not is_accepted(report)


def test_maximum_distinct_failures_keep_front_warning_without_invalidating_candidate_report():
    plan = primary_plan()
    plan["visible_features"] = [{"feature": key, "detail": f"原圖 {key} 依據。"} for key in get_args(Feature)]
    region = regional_fixture("pattern")[0]["detail_regions"][0]
    plan["detail_regions"] = [{**region, "name": f"region-{number}"} for number in range(5)]
    answer = candidate_answer(plan, "closed")
    for name, check in answer["checks"].items():
        check.update(status="fail", reason=f"整體 {name} 錯誤。")
    for feature in answer["feature_checks"]:
        feature.update(status="fail", reason=f"特徵 {feature['feature']} 錯誤。")
    regional = [
        {
            "name": region["name"],
            "feature": region["feature"],
            "source_observation": "原圖格線清楚。",
            "candidate_observation": "候選格線不同。",
            "status": "fail",
            "reason": f"局部 {region['name']} 錯誤。",
            "correction": None,
        }
        for region in plan["detail_regions"]
    ]
    attempt = assess_fidelity(plan, answer, 1, regional)
    # A stored report may preserve distinct global reasons at equal severity
    # rather than replacing them with the regional reason. Both are evidence.
    attempt["checks"] = copy.deepcopy(answer["checks"])
    parsed = ReconstructionAttempt.model_validate(attempt)
    parsed.preview = _preview_assessment(
        ReconstructionPlan.model_validate(plan),
        parsed,
        PreviewObservations.model_validate(answer["preview_observations"]),
    )
    report = build_report(plan, [parsed.model_dump()])
    warnings = report["preview"]["mismatch_warnings"]
    assert len(warnings) == 25
    assert "未保留主照展示狀態" in warnings[-1]
    assert report["generated"] and report["status"] == "rejected"
