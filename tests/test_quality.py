import pytest
from pydantic import ValidationError

from wardrobe.quality import apply_inspection, result_counts, summarize_results, unassessed_quality
from wardrobe.schemas import unknown_garment


def assessment(visibility="complete", target="one_garment", **garment):
    return {
        "garment": {
            **unknown_garment(),
            "category": "accessory",
            "subtype": "scarf",
            "confidence": 0.91,
            **garment,
        },
        "target": target,
        "visibility": visibility,
        "reason": "Visible clothing fragment",
    }


@pytest.mark.parametrize("visibility", ["partial", "fragment", "unclear"])
def test_incomplete_high_confidence_guesses_are_not_catalogue_facts(visibility):
    record, original, quality = apply_inspection(assessment(visibility))
    assert record == unknown_garment()
    assert original["subtype"] == "scarf" and original["confidence"] == 0.91
    assert quality["status"] == "needs_retake" and quality["reasons"]
    assert quality["version"] == 3 and quality["model_target"] == "one_garment"


@pytest.mark.parametrize(
    "target,garment",
    [
        pytest.param("no_garment", {"category": "other", "subtype": None}, id="high-confidence-cup"),
        pytest.param("multiple_garments", {"category": "bottom", "subtype": "jeans"}, id="two-garments"),
        pytest.param("uncertain", {"category": "top", "subtype": "shirt"}, id="uncertain-target"),
    ],
)
def test_complete_outline_cannot_make_an_invalid_target_catalogue_ready(target, garment):
    # Reproduce observed bad decisions with synthetic responses, not model accuracy claims.
    given = assessment(target=target, confidence=0.99, needs_review=False, **garment)
    record, original, quality = apply_inspection(given, {"mask_fill": 0.95, "frame_edges": []})
    assert record == unknown_garment()
    assert original["category"] == garment["category"] and original["subtype"] == garment["subtype"]
    assert original["confidence"] == 0.99
    assert quality["version"] == 3 and quality["model_target"] == target
    assert quality["status"] == "needs_retake" and quality["reasons"]
    assert quality["visibility"] == quality["model_visibility"] == "complete"
    assert quality["model_reason"] == given["reason"]


@pytest.mark.parametrize(
    "garment",
    [
        pytest.param({"category": "other", "subtype": None}, id="legitimate-other-garment"),
        pytest.param({"material_guess": None, "brand": None, "size_label": None}, id="unknown-fabric"),
        pytest.param({"confidence": 0.3, "needs_review": False}, id="low-confidence"),
    ],
)
def test_single_complete_garment_is_eligible_despite_uncertain_attributes(garment):
    record, original, quality = apply_inspection(assessment(**garment))
    assert quality["status"] == "ready" and quality["reasons"] == []
    assert quality["model_target"] == "one_garment" and quality["visibility"] == "complete"
    assert record == original
    for name, value in garment.items():
        if name != "needs_review":
            assert record[name] == value
    assert record["needs_review"] is True


def test_complete_narrow_garment_is_allowed_but_sparse_clipped_mask_is_not():
    metrics = {"mask_fill": 0.2, "frame_edges": []}
    record, _, quality = apply_inspection(assessment(), metrics)
    assert record["subtype"] == "scarf" and quality["status"] == "ready"
    metrics["frame_edges"] = ["bottom"]
    record, original, quality = apply_inspection(assessment(), metrics)
    assert record == unknown_garment() and original["subtype"] == "scarf"
    assert quality["visibility"] == "partial" and quality["model_visibility"] == "complete"


def test_quality_assessment_must_be_explicit_and_valid():
    with pytest.raises(ValidationError):
        apply_inspection({"garment": unknown_garment()})
    with pytest.raises(ValidationError):
        apply_inspection(assessment("invented"))


@pytest.mark.parametrize("target", [None, True, "single", {"one_garment": True}])
def test_quality_rejects_invalid_target_without_guessing(target):
    with pytest.raises(ValidationError):
        apply_inspection(assessment(target=target))


@pytest.mark.parametrize("missing", ["target", "reason"])
def test_quality_requires_target_and_reason_fields(missing):
    given = assessment()
    given.pop(missing)
    with pytest.raises(ValidationError):
        apply_inspection(given)


def test_counts_distinguish_retakes_errors_and_manual_override():
    assert result_counts(
        [
            {"quality": {"status": "needs_retake"}},
            {"quality": unassessed_quality()},
            {"quality": {"status": "needs_retake"}, "reviewed": True},
            {},  # Compatible historical demo.
        ]
    ) == {
        "item_count": 4,
        "usable_count": 2,
        "retake_count": 1,
        "confirmation_count": 0,
        "unassessed_count": 1,
    }


def test_version_two_empty_job_clears_summary_even_without_stale_quality_warnings():
    summary = summarize_results(
        {"status": "completed", "stage": "分析完成", "quality_version": 2, "warnings": []}, []
    )
    assert summary["stage"] == "Results removed" and summary["warnings"] == []
    assert {key: summary[key] for key in result_counts([])} == result_counts([])


def test_legacy_chinese_quality_notices_are_replaced_not_duplicated():
    job = {
        "status": "completed",
        "stage": "分析完成",
        "warnings": ["1 個區域尚未完成重新判讀，請重試或人工確認。", "unrelated notice"],
    }
    summary = summarize_results(job, [{"quality": unassessed_quality()}])
    assert summary["warnings"] == [
        "unrelated notice",
        "1 regions still unassessed. Please retry or confirm manually.",
    ]


def test_zero_detections_without_assessment_keep_the_detection_summary():
    job = {
        "status": "completed",
        "stage": "未找到符合提示的衣物",
        "quality_version": None,
        "warnings": ["沒有找到符合提示的衣物。"],
    }
    summary = summarize_results(job, [])
    assert summary["stage"] == job["stage"] and summary["warnings"] == job["warnings"]


def test_unassessed_quality_does_not_invent_target_evidence():
    quality = unassessed_quality({"mask_fill": 0.8})
    assert quality["version"] == 3 and quality["status"] == "unassessed"
    assert quality["model_target"] is None and quality["model_visibility"] is None
    assert quality["metrics"] == {"mask_fill": 0.8}
