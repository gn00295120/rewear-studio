"""Preview suitability is independently checked, never a same-item claim.

All vision/image answers below are explicit simulations of the real SDK boundary.
The fixed photo cohort is evaluated separately by the actual model.
"""

import copy
import json

import pytest
from PIL import Image

from wardrobe.providers import ProviderError
from wardrobe.reconstruction_quality import (
    PREVIEW_CHECK_NAMES,
    PREVIEW_NOTICE,
    assess_fidelity,
    build_report,
    is_accepted,
    is_preview_ready,
    select_attempt,
    validate_report,
)
from test_reconstruction import (
    ReconstructionProviders,
    quality_report,
    seed_item,
    start,
    studio,
    unchanged_evidence,
    wait_item,
)
from test_reconstruction_quality import (
    chat_response,
    create_provider,
    displayed_uncertainty,
    encoded_image,
    fidelity_answer,
    image_response,
    source_plan,
)
from wardrobe.schemas import unknown_garment


@pytest.fixture
def references(tmp_path):
    cutout, context = tmp_path / "cutout.png", tmp_path / "context.png"
    cutout.write_bytes(encoded_image("blue"))
    context.write_bytes(encoded_image("red"))
    record = {**unknown_garment(), "category": "top", "subtype": "shirt", "colors": ["red"]}
    return cutout, context, record


@pytest.mark.parametrize("view", ["front", "back", "side", "unclear"])
async def test_recognizable_occluded_or_side_source_generates_complete_preview_without_claiming_fidelity(
    tmp_path, references, view
):
    plan = source_plan("front")
    plan["source_view"] = view
    plan["uncertain_areas"] = [
        {
            **displayed_uncertainty(),
            "area": "Hem out of frame or tucked into pants",
            "detail": "Only the upper half of a recognizable top is visible; the length and hem are not fully exposed and need to be inferred.",
            "critical": True,
        }
    ]
    displayed = "back" if view == "back" else "front"
    calls = []
    before = [path.read_bytes() for path in references[:2]]

    def respond(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/images/edits"):
            calls.append("image")
            assert len(body["images"]) == 2
            assert f"Requested preview display_view: {displayed}" in body["prompt"]
            assert "extend a tucked or occluded hem" in body["prompt"]
            return image_response()
        name = body["response_format"]["json_schema"]["name"]
        calls.append(name)
        return chat_response(plan if name == "reconstruction_plan" else fidelity_answer(displayed))

    provider = create_provider(tmp_path, respond)
    destination = tmp_path / "preview.png"
    try:
        report = await provider.reconstruct(*references, destination)
    finally:
        await provider.close()
    assert calls == ["reconstruction_plan", "image", "reconstruction_fidelity"]
    assert is_preview_ready(report) and not is_accepted(report)
    assert report["generated"] and report["status"] == "needs_review"
    assert report["source_view"] == report["plan"]["initial_source_plan"]["source_view"] == view
    assert report["preview"]["display_view"] == displayed
    assert report["preview"]["view_inferred"] == (view in ("side", "unclear"))
    assert report["preview"]["inferred_areas"][0]["detail"] == plan["uncertain_areas"][0]["detail"]
    assert report["preview"]["notice"] == PREVIEW_NOTICE
    assert [path.read_bytes() for path in references[:2]] == before
    with Image.open(destination) as preview:
        assert preview.getpixel((0, 0)) == (0, 128, 0)


def test_complete_preview_keeps_specific_pattern_and_cuff_mismatch_warnings():
    plan = source_plan()
    answer = fidelity_answer()
    answer["feature_checks"][0].update(
        status="fail", reason="The original plaid pattern was redrawn as polka dots."
    )
    answer["checks"]["cut_structure"].update(
        status="fail", reason="The original straight cuffs were given ruffle edges."
    )
    report = build_report(plan, [assess_fidelity(plan, answer, 1)])
    assert report["status"] == "rejected" and is_preview_ready(report) and not is_accepted(report)
    assert "The original plaid pattern was redrawn as polka dots." in report["preview"]["mismatch_warnings"]
    assert "The original straight cuffs were given ruffle edges." in report["preview"]["mismatch_warnings"]
    assert report["feature_checks"][0]["status"] == "fail"
    forged = copy.deepcopy(report)
    forged["preview"]["mismatch_warnings"] = []
    forged["attempts"][0]["preview"]["mismatch_warnings"] = []
    with pytest.raises(ValueError, match="every known fidelity mismatch"):
        validate_report(forged)


@pytest.mark.parametrize("check", PREVIEW_CHECK_NAMES)
@pytest.mark.parametrize("status", ["fail", "uncertain"])
def test_all_five_preview_checks_are_required_and_unknown_is_not_ready(check, status):
    plan = source_plan()
    answer = fidelity_answer()
    target = answer["preview_observations"] if check in ("category", "coarse_color") else answer["checks"]
    target[check].update(
        status=status, reason="Visible image does not match or cannot be determined for this check."
    )
    report = build_report(plan, [assess_fidelity(plan, answer, 1)])
    assert not is_preview_ready(report)
    assert report["preview"]["status"] == ("rejected" if status == "fail" else "needs_review")
    assert not is_accepted(report)
    forged = copy.deepcopy(report)
    for preview in (forged["preview"], forged["attempts"][0]["preview"]):
        preview["status"] = "ready"
    with pytest.raises(ValueError):
        validate_report(forged)


def test_legacy_fidelity_pass_without_preview_observation_is_not_promoted_to_preview_ready():
    plan = source_plan()
    answer = fidelity_answer()
    answer.pop("preview_observations")
    report = build_report(plan, [assess_fidelity(plan, answer, 1)])
    assert is_accepted(report) and not is_preview_ready(report)
    assert report["preview"]["status"] == "unverified" and report["preview"]["checks"] is None
    legacy = copy.deepcopy(report)
    legacy.pop("preview")
    legacy["attempts"][0].pop("preview")
    assert is_accepted(legacy) and not is_preview_ready(legacy)


def test_preview_selection_requires_improvement_without_regressing_visible_details():
    plan = source_plan()
    first_answer = fidelity_answer(
        color_pattern={"status": "fail", "reason": "Color or pattern does not match."}
    )
    first_answer["preview_observations"]["coarse_color"].update(
        status="fail", reason="Dominant color changed from red to blue."
    )
    second_answer = fidelity_answer(
        color_pattern={"status": "fail", "reason": "Pattern still does not match."}
    )
    first = assess_fidelity(plan, first_answer, 1)
    second = assess_fidelity(plan, second_answer, 2)
    assert select_attempt([first, second]) == 2
    report = build_report(plan, [first, second], selected_attempt=2)
    assert is_preview_ready(report) and report["status"] == "rejected"
    # Improving the coarse colour cannot compensate for a newly lost clear source detail.
    second_answer["feature_checks"][1].update(
        status="fail", reason="New version changed long sleeves to short sleeves."
    )
    regressed = assess_fidelity(plan, second_answer, 2)
    assert select_attempt([first, regressed]) == 1


@pytest.mark.parametrize("repaired", [False, True])
async def test_audit_shape_retry_is_bounded_and_failed_audit_retains_valid_candidate(
    tmp_path, references, repaired
):
    calls = []

    def respond(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/images/edits"):
            calls.append("image")
            return image_response()
        name = body["response_format"]["json_schema"]["name"]
        calls.append(name)
        if name == "reconstruction_plan":
            return chat_response(source_plan())
        answer = fidelity_answer()
        if not repaired or calls.count(name) == 1:
            answer.pop("preview_observations")
        return chat_response(answer)

    provider = create_provider(tmp_path, respond)
    destination = tmp_path / "preview.png"
    try:
        report = await provider.reconstruct(*references, destination)
    finally:
        await provider.close()
    assert calls.count("image") == 1 and calls.count("reconstruction_fidelity") == 2
    assert report["generated"] and destination.is_file()
    assert is_preview_ready(report) is repaired
    if not repaired:
        assert report["status"] == "needs_review" and report["preview"]["status"] == "unverified"


def test_api_persists_preview_with_rejected_fidelity_and_keeps_it_across_failed_retry(tmp_path):
    provider = ReconstructionProviders(["rejected", "failure", "insufficient_source"])
    with studio(tmp_path, provider) as (client, app, _):
        item, paths = seed_item(app, status="needs_confirmation")
        originals = {name: path.read_bytes() for name, path in paths.items()}
        start(client, item["id"])
        first = wait_item(client, item["id"], "rejected")["reconstruction"]
        assert first["image_url"] is None and is_preview_ready(first["preview_validation"])
        assert first["preview_url"] == first["candidate_url"]
        assert first["preview_validation"]["preview"]["mismatch_warnings"]
        rendered = client.get(first["preview_url"]).content
        start(client, item["id"])
        second = wait_item(client, item["id"], "failed")["reconstruction"]
        assert second["preview_url"] == first["preview_url"]
        assert second["preview_validation"] == first["preview_validation"]
        start(client, item["id"])
        final = wait_item(client, item["id"], "needs_review")
        assert final["reconstruction"]["preview_url"] == first["preview_url"]
        assert final["reconstruction"]["preview_validation"] == first["preview_validation"]
        assert client.get(first["preview_url"]).content == rendered
        assert {name: path.read_bytes() for name, path in paths.items()} == originals
        unchanged_evidence(item, final)


@pytest.mark.parametrize(
    "exception", [ProviderError("Verification format incomplete"), TimeoutError(), RuntimeError("private")]
)
def test_manager_retains_valid_late_image_on_audit_error_but_never_certifies_it(tmp_path, exception):
    class LateErrorProvider(ReconstructionProviders):
        async def reconstruct(self, cutout_path, context_path, record, destination):
            Image.new("RGB", (64, 80), "green").save(destination, "PNG")
            raise exception

    with studio(tmp_path, LateErrorProvider()) as (client, app, _):
        item, _ = seed_item(app)
        start(client, item["id"])
        final = wait_item(client, item["id"], "needs_review")
        state = final["reconstruction"]
        assert state["candidate_url"] and client.get(state["candidate_url"]).status_code == 200
        assert state["preview_url"] is None and state["image_url"] is None
        assert not is_preview_ready(state["candidate_validation"])
        assert "private" not in json.dumps(state)
        unchanged_evidence(item, final)


def test_cancelling_retry_preserves_previous_ready_preview_with_its_own_validation(tmp_path):
    provider = ReconstructionProviders(["rejected", "block"])
    with studio(tmp_path, provider) as (client, app, _):
        item, _ = seed_item(app)
        start(client, item["id"])
        first = wait_item(client, item["id"], "rejected")["reconstruction"]
        start(client, item["id"])
        client.post(f"/api/items/{item['id']}/reconstruction/cancel")
        cancelled = wait_item(client, item["id"], "cancelled")["reconstruction"]
        assert cancelled["preview_url"] == first["preview_url"]
        assert cancelled["preview_validation"] == first["preview_validation"]
        assert client.get(first["preview_url"]).status_code == 200


def test_preview_follows_selected_image_and_cannot_borrow_another_attempt_checks():
    first = quality_report("rejected")
    second = copy.deepcopy(quality_report("accepted")["attempts"][0])
    second["number"] = 2
    report = build_report(first["plan"], [first["attempts"][0], second], selected_attempt=1)
    assert report["preview"] == first["preview"] and is_preview_ready(report)
    report["preview"] = second["preview"]
    with pytest.raises(ValueError, match="selected"):
        validate_report(report)
