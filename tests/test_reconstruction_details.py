"""Bounded source-detail evidence and correction transport, using simulated SDK answers.

These tests establish data flow and fail-closed rules, not model visual accuracy.
The fixed photographic cohort is measured separately with actual inference.
"""

import base64
import copy
import io
import json

import pytest
from PIL import Image

from wardrobe.providers import ProviderError
from wardrobe.reconstruction_quality import (
    SOURCE_DETAIL_SCHEMA,
    ReconstructionPlan,
    SourceDetailAnswer,
    SourceEvidencePlan,
    assess_fidelity,
    is_accepted,
    is_preview_ready,
)
from wardrobe.schemas import unknown_garment
from test_reconstruction_quality import (
    chat_response,
    create_provider,
    encoded_image,
    fidelity_answer,
    image_response,
    source_plan,
)


def detail_plan():
    plan = source_plan("front")
    plan["visible_features"].append({"feature": "hem", "detail": "First pass has not noticed a hem stripe."})
    plan["detail_regions"] = [
        {
            "name": name,
            "feature": feature,
            "source_image": "context",
            "box": [0.1, 0.05 + index * 0.2, 0.9, 0.25 + index * 0.2],
            "support": "clear",
            "detail": "A source-only observable design relationship.",
        }
        for index, (name, feature) in enumerate(
            (("upper-shape", "collar"), ("visible-cuff", "sleeves"), ("fabric-repeat", "pattern"))
        )
    ]
    extras = [
        {
            "name": "body-border",
            "feature": "hem",
            "source_image": "context",
            "box": [0.1, 0.75, 0.9, 0.98],
            "support": "clear",
            "detail": "A white stripe crosses the yellow BODY hem, separate from the sleeve cuff.",
        },
        {
            "name": "side-attachment",
            "feature": "other",
            "source_image": "context",
            "box": [0.68, 0.59, 0.96, 0.82],
            "support": "clear",
            "detail": "A blue vertical piece and a separate pale tab attach at the viewer-right side.",
        },
    ]
    return plan, extras


def references(tmp_path):
    cutout, context = tmp_path / "cutout.png", tmp_path / "original.png"
    cutout.write_bytes(encoded_image("red"))
    original = Image.new("RGB", (120, 240), "green")
    original.paste("purple", (0, 70, 120, 150))
    original.paste("yellow", (0, 150, 120, 240))
    original.paste("white", (15, 210, 110, 214))
    original.paste("blue", (90, 158, 94, 190))
    original.paste("white", (94, 185, 104, 195))
    original.save(context)
    return cutout, context, {**unknown_garment(), "category": "top", "colors": ["yellow"]}


def decode(block):
    return Image.open(io.BytesIO(base64.b64decode(block["image_url"]["url"].split(",", 1)[1])))


async def test_source_survey_adds_real_regions_and_repairs_visible_failure_despite_original_unknown(tmp_path):
    plan, extras = detail_plan()
    # The original ambiguity remains binding after a clearer crop is selected.
    plan["detail_regions"][0]["support"] = "ambiguous"
    refined = copy.deepcopy(plan["detail_regions"])
    refined[0]["support"] = "clear"
    plan["uncertain_areas"].append(
        {
            "area": "covered-cuff",
            "detail": "The other cuff is covered; its construction remains unknown.",
            "critical": False,
            "scope": "displayed",
            "scope_evidence": "A complete front display will expose both cuffs.",
            "unshown_view": None,
        }
    )
    refs = references(tmp_path)
    before = [path.read_bytes() for path in refs[:2]]
    regions = refined + extras
    calls, edits = [], []
    correction = "Restore the observed white BODY-HEM stripe; do not change the matching sleeve cuff."

    def respond(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/images/edits"):
            calls.append("edit")
            edits.append(body)
            assert len(body["images"]) == (7 if len(edits) == 1 else 8)
            assert body["prompt"].count('"role": "ORIGINAL_DETAIL"') == 5
            assert extras[0]["detail"] in body["prompt"]
            assert "First pass has not noticed" not in body["prompt"]
            if len(edits) == 2:
                assert correction not in body["prompt"] and "SYNTHETIC_EDIT_BASE" in body["prompt"]
                targets = json.loads(
                    body["prompt"]
                    .split("Typed repair targets, if this is a retry (JSON data): ")[1]
                    .split("\nReference manifest")[0]
                )
                assert targets["regions"] == [3]
            return image_response("green" if len(edits) == 1 else "blue")
        name = body["response_format"]["json_schema"]["name"]
        calls.append(name)
        content = body["messages"][1]["content"]
        if name == "reconstruction_plan":
            return chat_response(plan)
        if name == "reconstruction_source_details":
            assert "GENERATED_CANDIDATE" not in json.dumps(content)
            assert len([block for block in content if block["type"] == "image_url"]) == 8
            surveys = [
                (block, content[index + 1])
                for index, block in enumerate(content)
                if block.get("text", "").startswith("SOURCE_BOUNDARY_SURVEY:")
            ]
            assert len(surveys) == 3
            for (_, image), colour in zip(surveys, ((0, 128, 0), (128, 0, 128), (255, 255, 0))):
                with decode(image) as crop:
                    assert max(crop.size) <= 720
                    assert crop.getpixel((crop.width // 2, crop.height // 2)) == colour
            with decode(surveys[-1][1]) as crop:
                # The real white hem stripe survives, not synthetic inference.
                assert (255, 255, 255) in [colour for _, colour in crop.getcolors()]
            return chat_response(
                {"detail_regions": refined, "visible_features": [], "additional_regions": extras}
            )
        if name == "reconstruction_fidelity":
            answer = fidelity_answer("front")
            answer["feature_checks"].append(
                {"feature": "hem", "status": "pass", "reason": "Global audit overlooked the local mismatch."}
            )
            answer["region_boxes"] = [{"name": entry["name"], "box": entry["box"]} for entry in regions]
            return chat_response(answer)
        assert name == "reconstruction_regional_fidelity"
        assert len([block for block in content if block["type"] == "image_url"]) == 10
        checks = [
            {
                "name": region["name"],
                "feature": region["feature"],
                "source_observation": region["detail"],
                "candidate_observation": "Corresponding candidate detail separately inspected.",
                "status": "fail" if len(edits) == 1 and region["name"] == "body-border" else "pass",
                "reason": "The visible hem stripe is absent."
                if len(edits) == 1
                else "Visible detail retained.",
                "correction": correction if len(edits) == 1 and region["name"] == "body-border" else None,
            }
            for region in regions
        ]
        return chat_response({"regional_checks": checks})

    provider = create_provider(tmp_path, respond)
    destination = tmp_path / "candidate.png"
    try:
        report = await provider.reconstruct(*refs, destination)
    finally:
        await provider.close()
    assert calls.count("reconstruction_source_details") == 1
    assert calls.count("edit") == 2
    assert report["attempts"][0]["status"] == "rejected"
    assert report["attempts"][0]["corrective_feedback"] == [correction]
    assert report["selected_attempt"] == 2 and report["status"] == "needs_review"
    assert is_preview_ready(report) and not is_accepted(report)
    assert report["plan"]["initial_source_plan"] == plan
    assert report["plan"]["uncertain_areas"] == plan["uncertain_areas"]
    assert len(report["regional_checks"]) == 5
    assert [path.read_bytes() for path in refs[:2]] == before
    with Image.open(destination) as selected:
        assert selected.getpixel((0, 0)) == (0, 0, 255)
    assert not list(tmp_path.glob(".reconstruction-*"))


@pytest.mark.parametrize("invalid", ["name-collision", "wrong-source", "too-many", "missing-field"])
async def test_additional_regions_are_bounded_source_backed_and_cannot_replace_original_coverage(
    tmp_path, invalid
):
    plan, extras = detail_plan()
    response = {
        "detail_regions": plan["detail_regions"],
        "visible_features": [],
        "additional_regions": extras,
    }
    if invalid == "name-collision":
        extras[0]["name"] = plan["detail_regions"][0]["name"]
    elif invalid == "wrong-source":
        extras[0]["source_image"] = "cutout"
    elif invalid == "too-many":
        extras.append({**extras[0], "name": "third-extra"})
    else:
        response.pop("additional_regions")
    calls = []

    def respond(request):
        name = json.loads(request.content)["response_format"]["json_schema"]["name"]
        calls.append(name)
        return chat_response(plan if name == "reconstruction_plan" else response)

    provider = create_provider(tmp_path, respond)
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.plan_reconstruction(*references(tmp_path))
    finally:
        await provider.close()
    assert calls == ["reconstruction_plan", "reconstruction_source_details", "reconstruction_source_details"]
    assert caught.value.diagnostics[-1]["stage"] == "source_refinement"
    assert caught.value.diagnostics[-1]["status"] == "invalid"


def test_initial_three_final_five_and_legacy_source_details_remain_explicit():
    plan, extras = detail_plan()
    assert (
        SourceDetailAnswer.model_validate({"detail_regions": plan["detail_regions"]}).additional_regions == []
    )
    assert "additional_regions" in SOURCE_DETAIL_SCHEMA["required"]
    final = {**plan, "detail_regions": plan["detail_regions"] + extras}
    assert len(ReconstructionPlan.model_validate(final).detail_regions) == 5
    with pytest.raises(ValueError):
        SourceEvidencePlan.model_validate(final)
    with pytest.raises(ValueError):
        ReconstructionPlan.model_validate(
            {**final, "detail_regions": final["detail_regions"] + [{**extras[0], "name": "sixth"}]}
        )


def test_specific_paired_pixel_repairs_take_priority_over_generic_global_feedback():
    plan, extras = detail_plan()
    plan["detail_regions"] = extras[:1]
    answer = fidelity_answer("front")
    answer["feature_checks"].append({"feature": "hem", "status": "pass", "reason": "Global missed detail."})
    answer["corrections"] = [
        {"check": "visible_details", "instruction": f"Generic global repair {index}."} for index in range(7)
    ]
    check = {
        "name": extras[0]["name"],
        "feature": "hem",
        "source_observation": "Two distinct visible borders.",
        "candidate_observation": "One border is missing.",
        "status": "fail",
        "reason": "Visible border lost.",
        "correction": "Restore the missing body border using the original edge pixels.",
    }
    attempt = assess_fidelity(plan, answer, 1, [check])
    assert attempt["status"] == "rejected"
    assert len(attempt["corrective_feedback"]) == 7
    assert attempt["corrective_feedback"][0] == check["correction"]
