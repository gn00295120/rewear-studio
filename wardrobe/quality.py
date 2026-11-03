"""Keep visible clothing fragments out of the usable wardrobe catalogue."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .schemas import Garment, unknown_garment

QUALITY_VERSION = 3


class Inspection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    garment: Garment
    target: Literal["one_garment", "multiple_garments", "no_garment", "uncertain"]
    visibility: Literal["complete", "partial", "fragment", "unclear"]
    reason: str | None = Field(max_length=1000)


RETAKE_REASONS = {
    "partial": "Garment is partially occluded or cropped. Please retake with the full garment visible, including cuffs and hem.",
    "fragment": "Only a fragment of fabric is visible — cannot reliably identify the garment. Please photograph this item separately.",
    "unclear": "Cannot confirm a single garment from the image. Please use a well-lit, focused photo of the complete garment.",
}

TARGET_REASONS = {
    "multiple_garments": "Multiple garments detected but no single target specified. Use multi-item mode, or photograph one item at a time.",
    "no_garment": "No identifiable garment in the image. Please upload a garment photo with the full item clearly visible.",
    "uncertain": "Cannot confirm the garment target in the image. Please use a well-lit, focused photo of a single garment.",
}


def apply_inspection(
    assessment: dict, metrics: dict | None = None, *, wearing: bool = False
) -> tuple[dict, dict, dict]:
    """Return display record, original model record and independently saved quality.

    A confidence score is not a visibility assessment. Narrow complete garments
    are valid; geometry can only contradict 'complete' when a sparse mask also
    reaches the original frame boundary. Human review remains a separate field.
    """
    inspected = Inspection.model_validate(assessment)
    original = inspected.garment.model_dump()
    visibility = inspected.visibility
    metrics = dict(metrics or {})
    fill = metrics.get("mask_fill")
    sparse_edge = (
        isinstance(fill, (int, float))
        and not isinstance(fill, bool)
        and 0 <= fill < 0.35
        and bool(metrics.get("frame_edges"))
    )
    edge_fragment = visibility == "complete" and sparse_edge
    if edge_fragment:
        visibility = "partial"
    eligible = inspected.target == "one_garment" and visibility == "complete"
    confirmation = (
        wearing
        and inspected.target == "one_garment"
        and inspected.visibility == "partial"
        and original["category"] != "other"
        and original["confidence"] >= 0.5
        and not sparse_edge
    )
    reasons = (
        [TARGET_REASONS[inspected.target]]
        if inspected.target != "one_garment"
        else []
        if visibility == "complete"
        else [RETAKE_REASONS[visibility]]
    )
    if confirmation:
        reasons = [
            "Garment only partially visible due to pose or occlusion. Visible attributes preserved — please confirm garment type and details manually before adding to wardrobe. Retake for hidden parts."
        ]
    elif wearing and inspected.target == "one_garment" and visibility == "partial":
        reasons = [
            "Garment visibility or identification confidence is insufficient in this wearing photo. Please retake with the full garment clearly visible."
        ]
    if edge_fragment or (wearing and sparse_edge and visibility == "partial"):
        reasons.append(
            "Segmentation region touches image edge with less than 35% visible fabric. Kept for retake."
        )
    quality = {
        "version": QUALITY_VERSION,
        "status": "ready" if eligible else "needs_confirmation" if confirmation else "needs_retake",
        "model_target": inspected.target,
        "visibility": visibility,
        "model_visibility": inspected.visibility,
        "model_reason": inspected.reason,
        "reasons": reasons,
        "metrics": metrics,
        **({"capture_mode": "outfit"} if wearing else {}),
    }
    # Worn partials show visible attributes only in the explicit confirmation
    # queue. Other unusable guesses stay in model_record for inspection only.
    displayed = dict(original) if eligible or confirmation else unknown_garment()
    if confirmation:
        displayed["needs_review"] = True
    return displayed, original, quality


def unassessed_quality(metrics: dict | None = None, *, wearing: bool = False) -> dict:
    return {
        "version": QUALITY_VERSION,
        "model_target": None,
        "status": "unassessed",
        "visibility": "unclear",
        "model_visibility": None,
        "model_reason": None,
        "reasons": ["Image analysis could not be completed. Please retry or confirm manually."],
        "metrics": dict(metrics or {}),
        **({"capture_mode": "outfit"} if wearing else {}),
    }


def result_status(item: dict) -> str:
    if item.get("reviewed"):
        return "ready"
    return (item.get("quality") or {}).get("status", "ready")


def result_counts(items: list[dict]) -> dict:
    return {
        "item_count": len(items),
        "usable_count": sum(result_status(item) == "ready" for item in items),
        "retake_count": sum(result_status(item) == "needs_retake" for item in items),
        "confirmation_count": sum(result_status(item) == "needs_confirmation" for item in items),
        "unassessed_count": sum(result_status(item) == "unassessed" for item in items),
    }


# Markers of notices this module generates; the Chinese ones identify jobs saved before the English UI.
GENERATED_WARNING_MARKERS = (
    "not included in wearable items",
    "regions still unassessed",
    "未列入可整理衣物",
    "個區域尚未完成重新判讀",
)


def summarize_results(job: dict, items: list[dict]) -> dict:
    """Derive current counts and quality notices after edits, deletions or repair."""
    counts = result_counts(items)
    original_warnings = job.get("warnings", [])
    warnings = [
        warning
        for warning in original_warnings
        if not any(marker in warning for marker in GENERATED_WARNING_MARKERS)
    ]
    assessed = (
        job.get("quality_version") in (1, 2, 3)
        or any(item.get("quality") for item in items)
        or warnings != original_warnings
    )
    if counts["retake_count"]:
        warnings.append(
            f"{counts['retake_count']} regions could not be confirmed as clear, complete garments — marked for retake, not included in wearable items."
        )
    if counts["unassessed_count"]:
        warnings.append(
            f"{counts['unassessed_count']} regions still unassessed. Please retry or confirm manually."
        )
    if counts["confirmation_count"]:
        warnings.append(
            f"{counts['confirmation_count']} worn garments pending manual confirmation — visible attributes only, not included in wearable items until confirmed."
        )
    stage = job["stage"]
    if assessed and job["status"] == "completed":
        details = []
        if counts["retake_count"]:
            details.append(f"{counts['retake_count']} regions need retake")
        if counts["confirmation_count"]:
            details.append(f"{counts['confirmation_count']} worn garments pending")
        if counts["unassessed_count"]:
            details.append(f"{counts['unassessed_count']} regions unassessed")
        stage = "Analysis complete" + (" — " + ", ".join(details) if details else "")
        if not items:
            stage = "Results removed"
    return {**job, **counts, "stage": stage, "warnings": warnings}
