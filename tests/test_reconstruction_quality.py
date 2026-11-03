"""Quality gates and real SDK wiring; model answers here are explicit simulations.

These tests cannot establish model judgement accuracy. The independent original
and old-generated photograph controls are executed separately with the real API.
"""

import asyncio
import base64
import copy
import io
import json

import httpx
import pytest
from openai import AsyncOpenAI
from PIL import Image

from wardrobe.config import Settings
from wardrobe.providers import (
    ProviderError,
    Providers,
    _reconstruction_region_url,
    _reconstruction_source_detail_url,
    _reference_data_url,
)
from wardrobe.reconstruction_quality import (
    CHECK_NAMES,
    FIDELITY_SCHEMA,
    PLAN_SCHEMA,
    ReconstructionPlan,
    ReconstructionReport,
    assess_fidelity,
    build_report,
    is_accepted,
    is_preview_ready,
    select_attempt,
    validate_planning_diagnostics,
    validate_report,
)
from wardrobe.schemas import unknown_garment


def source_plan(view="back"):
    return {
        "target": "one_garment",
        "source_view": view,
        "view_evidence": "原圖可見後領與背片，前方扣襟不可見。" if view == "back" else "原圖可見前方拉鍊。",
        "garment_type": "shirt",
        "visibility": "partial",
        "primary_front_state": {
            "state": "not_applicable",
            "source_image": "context",
            "evidence": "此純色測試來源沒有可分離的前襟範圍。",
            "box": None,
        },
        "visible_features": [
            {"feature": "pattern", "detail": "紅底黑色格紋，背部與袖子格線間距一致。"},
            {"feature": "silhouette", "detail": "長袖襯衫，後領與圓弧下襬可見。"},
        ],
        "uncertain_areas": [
            {
                "area": "未展示的另一面",
                "detail": "另一面的扣襟或背片沒有原圖依據，不能新創細節。",
                "critical": False,
                "scope": "unshown",
                "scope_evidence": "本次只保留原圖可見面，另一面不會出現在平鋪展示中。",
                "unshown_view": "front" if view == "back" else "back",
            }
        ],
        "detail_regions": [],
    }


def fidelity_answer(view="back", **checks):
    value = {
        "observed_view": view,
        "candidate_front_state": {
            "state": "not_applicable",
            "evidence": "此純色測試候選沒有可分離的前襟範圍。",
            "box": None,
        },
        "checks": {
            name: {"status": "pass", "reason": "可見圖像特徵與原始參考一致。"} for name in CHECK_NAMES
        },
        "feature_checks": [
            {"feature": "pattern", "status": "pass", "reason": "紅黑格線間距及位置相符。"},
            {"feature": "silhouette", "status": "pass", "reason": "背面長袖輪廓及下襬相符。"},
        ],
        "corrections": [],
        "region_boxes": [],
        "preview_observations": {
            "category": {"status": "pass", "reason": "候選仍為同類襯衫。"},
            "coarse_color": {"status": "pass", "reason": "候選保留主色。"},
        },
    }
    value["checks"].update(checks)
    return value


def chat_response(value):
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(value)},
                }
            ]
        },
    )


def encoded_image(color="green"):
    data = io.BytesIO()
    Image.new("RGB", (90, 120), color).save(data, format="PNG")
    return data.getvalue()


def image_response(color="green"):
    return httpx.Response(
        200, json={"created": 1, "data": [{"b64_json": base64.b64encode(encoded_image(color)).decode()}]}
    )


def create_provider(tmp_path, handler, timeout=120):
    settings = Settings(
        data_dir=tmp_path,
        api_key="test-reconstruction-secret",
        request_timeout=timeout,
        reconstruction_timeout=timeout,
    )
    provider = Providers(settings)
    provider._meta = AsyncOpenAI(
        api_key=settings.api_key,
        base_url=settings.meta_url,
        max_retries=0,
        timeout=timeout,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return provider


@pytest.fixture
def references(tmp_path):
    cutout, context = tmp_path / "cutout.png", tmp_path / "context.png"
    cutout.write_bytes(encoded_image("blue"))
    context.write_bytes(encoded_image("red"))
    record = {
        **unknown_garment(),
        "category": "top",
        "subtype": "shirt",
        "colors": ["red", "black"],
        "pattern": "plaid",
    }
    return cutout, context, record


def image_pixels(content):
    result = []
    for block in content:
        if block["type"] == "image_url":
            data = base64.b64decode(block["image_url"]["url"].split(",", 1)[1])
            with Image.open(io.BytesIO(data)) as picture:
                result.append(picture.getpixel((0, 0)))
    return result


async def test_back_only_reference_is_planned_generated_and_audited_using_actual_images(tmp_path, references):
    requests = []
    before = [path.read_bytes() for path in references[:2]]

    def respond(request):
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path.endswith("/images/edits"):
            assert (
                json.loads(
                    body["prompt"]
                    .split("Original-photo evidence plan (JSON data): ")[1]
                    .split("\nUntrusted")[0]
                )["source_view"]
                == "back"
            )
            assert "BACK-SIDE" in body["prompt"]
            assert "front-view flat-lay" not in body["prompt"]
            assert "panel geometry" in body["prompt"]
            return image_response()
        schema = body["response_format"]["json_schema"]
        assert schema["strict"] is True and body["reasoning_effort"] == "medium"
        content = body["messages"][1]["content"]
        if schema["name"] == "reconstruction_plan":
            assert image_pixels(content) == [(255, 0, 0), (0, 0, 255)]
            assert content[1]["text"] == "ORIGINAL_CONTEXT"
            return chat_response(source_plan())
        assert schema["name"] == "reconstruction_fidelity"
        assert image_pixels(content) == [(255, 0, 0), (0, 0, 255), (0, 128, 0)]
        return chat_response(fidelity_answer())

    provider = create_provider(tmp_path, respond)
    destination = tmp_path / "output" / "new.png"
    try:
        report = await provider.reconstruct(*references, destination)
    finally:
        await provider.close()
    assert len(requests) == 3 and is_accepted(report)
    assert report["source_view"] == "back" and report["generated"] is True
    assert report["retried"] is False and len(report["attempts"]) == 1
    assert [path.read_bytes() for path in references[:2]] == before
    with Image.open(destination) as picture:
        assert picture.getpixel((0, 0)) == (0, 128, 0)
    assert not list(destination.parent.glob(".reconstruction-*"))


def test_front_from_back_is_rejected_even_if_model_overall_checks_say_pass():
    plan = source_plan("back")
    attempt = assess_fidelity(plan, fidelity_answer(view="front"), 1)
    assert attempt["checks"]["viewpoint"]["status"] == "fail"
    assert attempt["status"] == "rejected"
    assert not is_accepted(build_report(plan, [attempt]))


def test_design_mismatch_cannot_be_hidden_by_generic_shirt_agreement():
    plan = source_plan()
    answer = fidelity_answer()
    answer["feature_checks"][0].update(status="fail", reason="原圖背片為紅黑格紋，生成圖格線間距被改動。")
    attempt = assess_fidelity(plan, answer, 1)
    assert attempt["checks"]["visible_details"]["status"] == "fail"
    assert attempt["status"] == "rejected"
    assert "格線間距" in " ".join(attempt["issues"])


@pytest.mark.parametrize("field", CHECK_NAMES)
@pytest.mark.parametrize("status", ["fail", "uncertain"])
def test_every_fidelity_dimension_is_required_to_accept(field, status):
    plan = source_plan()
    answer = fidelity_answer(**{field: {"status": status, "reason": "圖像證據不足或不符。"}})
    report = build_report(plan, [assess_fidelity(plan, answer, 1)])
    assert not is_accepted(report)
    assert report["status"] == ("rejected" if status == "fail" else "needs_review")


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "invented"])
def test_fidelity_must_check_exactly_the_observed_source_features(mutation):
    answer = fidelity_answer()
    if mutation == "missing":
        answer["feature_checks"].pop()
    elif mutation == "duplicate":
        answer["feature_checks"].append(copy.deepcopy(answer["feature_checks"][0]))
    else:
        answer["feature_checks"][0]["feature"] = "closures"
    with pytest.raises(ValueError, match="every visible source feature"):
        assess_fidelity(source_plan(), answer, 1)


@pytest.mark.parametrize(
    "change",
    [
        {"visibility": "fragment"},
        {"target": "multiple_garments"},
        {"target": "no_garment"},
        {"garment_type": None},
        {"visible_features": []},
    ],
)
async def test_unsupported_plan_does_not_invent_an_entire_garment(tmp_path, references, change):
    plan = source_plan()
    plan.update(change)
    if change.get("source_view") in ("side", "unclear"):
        plan["uncertain_areas"][0].update(
            scope="unclear", scope_evidence="來源視角不明，不能確定未知面是否會展示。", unshown_view=None
        )
    requests = []

    def respond(request):
        requests.append(request.url.path)
        return chat_response(plan)

    provider = create_provider(tmp_path, respond)
    destination = tmp_path / "existing.png"
    destination.write_bytes(b"previous accepted display")
    try:
        report = await provider.reconstruct(*references, destination)
    finally:
        await provider.close()
    assert requests == ["/v1/chat/completions"]
    assert report["generated"] is False and report["checks"] is None and report["issues"]
    assert not is_accepted(report)
    assert destination.read_bytes() == b"previous accepted display"


@pytest.mark.parametrize("second_passes", [False, True])
async def test_concrete_design_feedback_can_retry_once_and_retains_improved_candidate(
    tmp_path, references, second_passes
):
    edits, audits = [], []

    def respond(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/images/edits"):
            edits.append(body)
            return image_response("green" if len(edits) == 1 else "red")
        if body["response_format"]["json_schema"]["name"] == "reconstruction_plan":
            return chat_response(source_plan())
        audits.append(body)
        answer = fidelity_answer()
        if len(audits) == 1 or not second_passes:
            answer["feature_checks"][0].update(status="fail", reason="格紋被重畫成寬色帶，與原圖不同。")
            answer["corrections"] = [
                {
                    "check": "visible_details",
                    "instruction": "Restore the narrow red-and-black plaid spacing visible around the backpack.",
                }
            ]
        return chat_response(answer)

    provider = create_provider(tmp_path, respond)
    destination = tmp_path / "candidate.png"
    try:
        report = await provider.reconstruct(*references, destination)
    finally:
        await provider.close()
    assert len(edits) == len(audits) == 2 and report["retried"] is True
    assert all("Restore the narrow" not in edit["prompt"] for edit in edits)
    targets = json.loads(
        edits[1]["prompt"]
        .split("Typed repair targets, if this is a retry (JSON data): ")[1]
        .split("\nReference manifest")[0]
    )
    assert targets["features"] == ["pattern"]
    assert "Restore the narrow" in report["attempts"][0]["corrective_feedback"][0]
    assert report["attempts"][0]["status"] == "rejected"
    assert is_accepted(report) is second_passes
    assert report["status"] == ("accepted" if second_passes else "rejected")
    assert report["selected_attempt"] == (2 if second_passes else 1)
    assert len(edits[0]["images"]) == 2 and len(edits[1]["images"]) == 3
    assert "SYNTHETIC_EDIT_BASE" in edits[1]["prompt"]
    with Image.open(
        io.BytesIO(base64.b64decode(edits[1]["images"][-1]["image_url"].split(",", 1)[1]))
    ) as base:
        assert base.getpixel((0, 0)) == (0, 128, 0)
    with Image.open(destination) as picture:
        assert picture.getpixel((0, 0)) == ((255, 0, 0) if second_passes else (0, 128, 0))


@pytest.mark.parametrize(
    "failure",
    [
        "uncertain",
        "view-flip",
        "no-feedback",
        "unrelated-feedback",
        "no-budget",
        "model-error",
        "missing-feature",
    ],
)
async def test_failed_or_uncertain_candidate_is_retained_with_only_supported_bounded_retry(
    tmp_path, references, failure
):
    edits = []

    def respond(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/images/edits"):
            edits.append(body)
            return image_response()
        if body["response_format"]["json_schema"]["name"] == "reconstruction_plan":
            return chat_response(source_plan())
        if failure == "model-error":
            return httpx.Response(
                401, json={"error": {"message": "test-reconstruction-secret private-payload"}}
            )
        answer = fidelity_answer()
        if failure == "view-flip":
            answer["observed_view"] = "front"
        elif failure == "missing-feature":
            answer["feature_checks"].pop()
        else:
            answer["checks"]["cut_structure"] = {
                "status": "uncertain" if failure == "uncertain" else "fail",
                "reason": "原圖色塊接縫與候選不符，或無法辨認。",
            }
            if failure in ("uncertain", "no-budget"):
                answer["corrections"] = [
                    {"check": "cut_structure", "instruction": "Restore source panel boundaries."}
                ]
                if failure == "no-budget":
                    answer["feature_checks"][1].update(status="fail", reason="Visible outline changed.")
            elif failure == "unrelated-feedback":
                answer["corrections"] = [
                    {"check": "complete_outline", "instruction": "Arbitrary unsupported instruction."}
                ]
        return chat_response(answer)

    provider = create_provider(tmp_path, respond, timeout=2 if failure == "no-budget" else 120)
    destination = tmp_path / "candidate.png"
    try:
        report = await provider.reconstruct(*references, destination)
    finally:
        await provider.close()
    # A confirmed front/back flip has a deterministic target even without a
    # free-text command. Generic design failures lack a specific source target.
    assert len(edits) == (2 if failure == "view-flip" else 1)
    assert report["retried"] is (failure == "view-flip")
    assert not is_accepted(report) and report["generated"] is True
    assert "test-reconstruction-secret" not in json.dumps(report) and "private-payload" not in json.dumps(
        report
    )
    with Image.open(destination) as picture:
        picture.verify()


@pytest.mark.parametrize("cancel", [False, True])
async def test_cancel_cleans_staging_but_audit_deadline_retains_candidate_for_review(
    tmp_path, references, cancel
):
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def respond(request):
        if request.url.path.endswith("/images/edits"):
            return image_response()
        name = json.loads(request.content)["response_format"]["json_schema"]["name"]
        if name == "reconstruction_plan":
            return chat_response(source_plan())
        entered.set()
        try:
            await asyncio.sleep(30)
        finally:
            stopped.set()

    provider = create_provider(tmp_path, respond, timeout=2 if cancel else 0.15)
    destination = tmp_path / "existing.png"
    destination.write_bytes(b"prior accepted display")
    try:
        task = asyncio.create_task(provider.reconstruct(*references, destination))
        await asyncio.wait_for(entered.wait(), 1)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            report = await task
            assert report["status"] == "needs_review" and report["generated"] is True
            assert "timed out" in " ".join(report["issues"])
        assert stopped.is_set()
    finally:
        await provider.close()
    if cancel:
        assert destination.read_bytes() == b"prior accepted display"
    else:
        with Image.open(destination) as picture:
            assert picture.getpixel((0, 0)) == (0, 128, 0)
    assert not list(tmp_path.glob(".reconstruction-*"))


async def test_public_negative_control_audit_does_not_generate_or_modify_any_image(tmp_path, references):
    candidate = tmp_path / "old-front.png"
    candidate.write_bytes(encoded_image())
    before = [path.read_bytes() for path in (*references[:2], candidate)]
    requests = []

    def respond(request):
        requests.append(request.url.path)
        assert request.url.path == "/v1/chat/completions"
        return chat_response(fidelity_answer(view="front"))

    provider = create_provider(tmp_path, respond)
    try:
        report = await provider.inspect_reconstruction(source_plan(), references[0], references[1], candidate)
    finally:
        await provider.close()
    assert requests == ["/v1/chat/completions"] and report["status"] == "rejected"
    assert [path.read_bytes() for path in (*references[:2], candidate)] == before


@pytest.mark.parametrize(
    "change",
    [
        "empty",
        "forged-accepted",
        "hidden-detail-failure",
        "wrong-view",
        "wrong-final",
        "wrong-plan",
        "extra-attempt",
    ],
)
def test_manager_cannot_accept_malformed_or_inconsistent_quality_reports(change):
    plan = source_plan()
    report = build_report(plan, [assess_fidelity(plan, fidelity_answer(), 1)])
    if change == "empty":
        report = {"status": "accepted"}
    elif change == "forged-accepted":
        report["checks"]["color_pattern"]["status"] = "fail"
    elif change == "hidden-detail-failure":
        report["attempts"][0]["feature_checks"][0]["status"] = "fail"
    elif change == "wrong-view":
        report["attempts"][0]["observed_view"] = "front"
    elif change == "wrong-final":
        report["status"] = "needs_review"
    elif change == "wrong-plan":
        report["plan"]["source_view"] = "front"
    else:
        report["attempts"] *= 3
    assert not is_accepted(report)
    with pytest.raises(ValueError):
        validate_report(report)


def regional_fixture(feature="sleeves", support="clear"):
    plan = source_plan("front")
    plan["detail_regions"] = [
        {
            "name": "target-construction",
            "feature": feature,
            "source_image": "context",
            "box": [0.15, 0.15, 0.7, 0.8],
            "support": support,
            "detail": "袖口可見簡單平直開口。"
            if support == "clear"
            else "坐姿重疊使腰線與下襬輪廓無法確定。",
        }
    ]
    answer = fidelity_answer("front")
    answer["region_boxes"] = [{"name": "target-construction", "box": [0.2, 0.1, 0.8, 0.8]}]
    checks = [
        {
            "name": "target-construction",
            "feature": feature,
            "source_observation": "原圖開口為平直邊緣，沒有可見彈性縮口或花邊。",
            "candidate_observation": "候選袖口有明顯鬆緊抽皺與花邊。",
            "status": "fail",
            "reason": "可見平直袖口被改成抽皺花邊袖口。",
            "correction": "保留原圖的平直袖口，不加抽皺與花邊。",
        }
    ]
    return plan, answer, checks


def test_new_api_plan_requires_regions_while_legacy_stored_plan_remains_readable():
    legacy = source_plan()
    legacy.pop("detail_regions")
    assert ReconstructionPlan.model_validate(legacy).detail_regions == []
    assert "detail_regions" in PLAN_SCHEMA["required"]
    assert "region_boxes" in FIDELITY_SCHEMA["required"]


@pytest.mark.parametrize("box", [[0.5, 0, 0.4, 1], [0, 0, 0, 1], [-0.1, 0, 1, 1], [0, 0, 1, float("nan")]])
def test_region_boxes_are_bounded_finite_nonempty(box):
    plan, _, _ = regional_fixture()
    plan["detail_regions"][0]["box"] = box
    with pytest.raises(ValueError):
        ReconstructionPlan.model_validate(plan)


def test_region_coordinates_use_the_exact_exif_normalized_reference_and_leave_source_unchanged(tmp_path):
    original = tmp_path / "rotated.png"
    picture = Image.new("RGB", (80, 40), "blue")
    picture.paste("red", (0, 0, 40, 20))
    exif = picture.getexif()
    exif[274] = 6
    picture.save(original, exif=exif)
    before = original.read_bytes()
    reference = _reference_data_url(original)
    detail = _reconstruction_region_url(reference, [0.55, 0.05, 0.95, 0.45])
    with Image.open(io.BytesIO(base64.b64decode(detail.split(",", 1)[1]))) as cropped:
        assert cropped.size == (18, 36)
        assert cropped.getextrema() == ((255, 255), (0, 0), (0, 0))
    native_detail = _reconstruction_source_detail_url(original, [0.55, 0.05, 0.95, 0.45])
    assert native_detail == detail
    assert original.read_bytes() == before


def test_visible_cuff_redesign_overrides_all_global_passes_with_separate_observations():
    plan, answer, checks = regional_fixture()
    attempt = assess_fidelity(plan, answer, 1, checks)
    assert attempt["status"] == "rejected"
    assert attempt["checks"]["cut_structure"]["status"] == "fail"
    assert attempt["checks"]["visible_details"]["status"] == "fail"
    assert (
        attempt["regional_checks"][0]["source_observation"]
        != attempt["regional_checks"][0]["candidate_observation"]
    )
    assert attempt["corrective_feedback"] == [checks[0]["correction"]]
    assert not is_accepted(build_report(plan, [attempt]))


@pytest.mark.parametrize("feature", ["panel_geometry", "silhouette", "hem"])
@pytest.mark.parametrize("model_status", ["pass", "fail"])
def test_ambiguous_source_geometry_cannot_be_promoted_to_agreement_or_definite_mismatch(
    feature, model_status
):
    plan, answer, checks = regional_fixture(feature, "ambiguous")
    checks[0]["status"] = model_status
    attempt = assess_fidelity(plan, answer, 1, checks)
    assert attempt["status"] == "needs_review"
    assert attempt["regional_checks"][0]["status"] == "uncertain"
    assert "坐姿重疊" in attempt["regional_checks"][0]["reason"]
    assert attempt["regional_checks"][0]["correction"] is None
    assert attempt["corrective_feedback"] == []
    assert not is_accepted(build_report(plan, [attempt]))


def test_partial_source_with_clear_regional_agreement_can_still_be_accepted():
    plan, answer, checks = regional_fixture()
    checks[0].update(
        status="pass",
        candidate_observation="候選袖口保留同樣平直開口。",
        reason="可見開口一致。",
        correction=None,
    )
    report = build_report(plan, [assess_fidelity(plan, answer, 1, checks)])
    assert plan["visibility"] == "partial" and is_accepted(report)
    assert report["regional_checks"] == report["attempts"][0]["regional_checks"]


@pytest.mark.parametrize("mutation", ["omitted", "duplicate", "wrong-feature", "wrong-name"])
def test_regional_coverage_is_exact_and_cannot_disappear_behind_global_passes(mutation):
    plan, answer, checks = regional_fixture()
    if mutation == "omitted":
        checks = []
    elif mutation == "duplicate":
        checks *= 2
    elif mutation == "wrong-feature":
        checks[0]["feature"] = "pattern"
    else:
        checks[0]["name"] = "unrelated-object"
    with pytest.raises(ValueError, match="every source detail region"):
        assess_fidelity(plan, answer, 1, checks)


@pytest.mark.parametrize("mutation", ["hidden-regional-fail", "ambiguous-promoted", "removed-regions"])
def test_report_validator_rechecks_regional_evidence_not_only_top_level_status(mutation):
    plan, answer, checks = regional_fixture()
    checks[0].update(status="pass", correction=None)
    report = build_report(plan, [assess_fidelity(plan, answer, 1, checks)])
    if mutation == "ambiguous-promoted":
        report["plan"]["detail_regions"][0]["support"] = "ambiguous"
    elif mutation == "hidden-regional-fail":
        report["regional_checks"][0]["status"] = "fail"
        report["attempts"][0]["regional_checks"][0]["status"] = "fail"
    else:
        report["regional_checks"] = []
        report["attempts"][0]["regional_checks"] = []
    assert not is_accepted(report)
    with pytest.raises(ValueError):
        validate_report(report)


async def test_one_regional_call_sends_all_actual_original_candidate_crop_pairs(tmp_path, references):
    plan, answer, checks = regional_fixture()
    for index in (2, 3):
        plan["detail_regions"].append({**plan["detail_regions"][0], "name": f"region-{index}"})
        answer["region_boxes"].append({**answer["region_boxes"][0], "name": f"region-{index}"})
        checks.append({**checks[0], "name": f"region-{index}"})
    # This test isolates one paired audit. A short overall budget prevents a
    # second edit; raw correction prose is not required for a clear target.
    for check in checks:
        check["correction"] = None
    calls = []

    def respond(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/images/edits"):
            calls.append("edit")
            assert len(body["images"]) == 5
            assert body["prompt"].count('"role": "ORIGINAL_DETAIL"') == 3
            for detail in body["images"][2:]:
                with Image.open(io.BytesIO(base64.b64decode(detail["image_url"].split(",", 1)[1]))) as crop:
                    assert crop.getpixel((0, 0)) == (255, 0, 0)
            return image_response()
        name = body["response_format"]["json_schema"]["name"]
        calls.append(name)
        if name == "reconstruction_plan":
            return chat_response(plan)
        if name == "reconstruction_source_details":
            return chat_response(
                {"detail_regions": plan["detail_regions"], "visible_features": [], "additional_regions": []}
            )
        if name == "reconstruction_fidelity":
            return chat_response(answer)
        assert name == "reconstruction_regional_fidelity"
        content = body["messages"][1]["content"]
        assert image_pixels(content) == [(255, 0, 0), (0, 128, 0)] * 3
        text = " ".join(block.get("text", "") for block in content)
        assert "ORIGINAL_REGION: target-construction" in text
        assert "CANDIDATE_REGION: target-construction" in text
        assert "TARGET_CUTOUT" not in text
        assert "source_observation" in body["messages"][0]["content"]
        assert "candidate_observation" in body["messages"][0]["content"]
        return chat_response({"regional_checks": checks})

    provider = create_provider(tmp_path, respond, timeout=2)
    candidate = tmp_path / "candidate.png"
    before = [path.read_bytes() for path in references[:2]]
    try:
        report = await provider.reconstruct(*references, candidate)
    finally:
        await provider.close()
    assert calls == [
        "reconstruction_plan",
        "reconstruction_source_details",
        "edit",
        "reconstruction_fidelity",
        "reconstruction_regional_fidelity",
    ]
    assert report["status"] == "rejected" and len(report["regional_checks"]) == 3
    assert candidate.is_file() and [path.read_bytes() for path in references[:2]] == before


async def test_unlocatable_candidate_region_is_uncertain_without_guessing_a_crop(tmp_path, references):
    plan, answer, _ = regional_fixture()
    answer["region_boxes"][0]["box"] = None
    candidate = tmp_path / "candidate.png"
    candidate.write_bytes(encoded_image())
    calls = []

    def respond(request):
        calls.append(json.loads(request.content)["response_format"]["json_schema"]["name"])
        return chat_response(answer)

    provider = create_provider(tmp_path, respond)
    try:
        report = await provider.inspect_reconstruction(plan, *references[:2], candidate)
    finally:
        await provider.close()
    assert calls == ["reconstruction_fidelity"]
    assert report["status"] == "needs_review"
    assert report["regional_checks"][0]["status"] == "uncertain"


async def test_regional_timeout_retains_generated_candidate_and_cancels_pending_request(tmp_path, references):
    plan, answer, _ = regional_fixture()
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def respond(request):
        if request.url.path.endswith("/images/edits"):
            return image_response()
        name = json.loads(request.content)["response_format"]["json_schema"]["name"]
        if name == "reconstruction_plan":
            return chat_response(plan)
        if name == "reconstruction_source_details":
            return chat_response(
                {"detail_regions": plan["detail_regions"], "visible_features": [], "additional_regions": []}
            )
        if name == "reconstruction_fidelity":
            return chat_response(answer)
        entered.set()
        try:
            await asyncio.sleep(30)
        finally:
            stopped.set()

    provider = create_provider(tmp_path, respond, timeout=0.25)
    candidate = tmp_path / "candidate.png"
    try:
        report = await asyncio.wait_for(provider.reconstruct(*references, candidate), 1)
    finally:
        await provider.close()
    assert entered.is_set() and stopped.is_set()
    assert report["status"] == "needs_review" and report["generated"] is True
    assert is_preview_ready(report), "A timed-out regional audit must retain completed global preview checks"
    assert report["regional_checks"][0]["status"] == "uncertain"
    assert "timed out" in " ".join(report["issues"])
    with Image.open(candidate) as picture:
        assert picture.getpixel((0, 0)) == (0, 128, 0)
    assert not list(tmp_path.glob(".reconstruction-*"))


def test_generation_detail_is_cropped_before_scene_downsampling_without_inventing_pixels(tmp_path):
    source = tmp_path / "high-resolution.png"
    original = Image.new("RGB", (4000, 2000), "red")
    original.paste("blue", (2000, 0, 4000, 2000))
    original.save(source)
    box = [0.1, 0.1, 0.3, 0.3]
    native = _reconstruction_source_detail_url(source, box)
    thumbnail = _reconstruction_region_url(_reference_data_url(source), box)
    with Image.open(io.BytesIO(base64.b64decode(native.split(",", 1)[1]))) as detail:
        with Image.open(io.BytesIO(base64.b64decode(thumbnail.split(",", 1)[1]))) as reduced:
            assert detail.width > reduced.width * 2
        assert detail.getextrema() == ((255, 255), (0, 0), (0, 0))
        assert detail.width <= 900 and detail.height <= 900


@pytest.mark.parametrize("second", ["improves", "new-fail", "new-unknown", "same", "missing-feature"])
def test_best_candidate_requires_actual_improvement_without_regressing_any_observed_check(second):
    plan = source_plan()
    first = fidelity_answer()
    first["feature_checks"][0]["status"] = "fail"
    first_attempt = assess_fidelity(plan, first, 1)
    next_answer = fidelity_answer()
    if second == "new-fail":
        next_answer["checks"]["viewpoint"] = {"status": "fail", "reason": "Changed view."}
    elif second == "new-unknown":
        next_answer["feature_checks"][1]["status"] = "uncertain"
    elif second == "same":
        next_answer = copy.deepcopy(first)
    second_attempt = assess_fidelity(plan, next_answer, 2)
    if second == "missing-feature":
        second_attempt["feature_checks"].pop()
        with pytest.raises(ValueError, match="coverage must remain identical"):
            select_attempt([first_attempt, second_attempt])
        return
    selected = select_attempt([first_attempt, second_attempt])
    assert selected == (2 if second == "improves" else 1)
    report = build_report(plan, [first_attempt, second_attempt], selected_attempt=selected)
    assert [entry["number"] for entry in report["attempts"]] == [1, 2]
    assert report["checks"] == report["attempts"][selected - 1]["checks"]
    assert is_accepted(report) is (second == "improves")


def test_selected_report_cannot_borrow_passes_from_another_attempt_and_legacy_means_final():
    plan = source_plan()
    answer = fidelity_answer()
    first = assess_fidelity(plan, answer, 1)
    answer["checks"]["cut_structure"] = {"status": "fail", "reason": "Sleeve changed."}
    second = assess_fidelity(plan, answer, 2)
    report = build_report(plan, [first, second], selected_attempt=1)
    assert is_accepted(report)
    wrong = copy.deepcopy(report)
    wrong["selected_attempt"] = 2
    assert not is_accepted(wrong)
    legacy = build_report(plan, [first, second])
    legacy.pop("selected_attempt")
    assert validate_report(legacy)["status"] == "rejected"
    for number in (0, 3):
        with pytest.raises(ValueError):
            build_report(plan, [first, second], selected_attempt=number)


@pytest.mark.parametrize("failure", ["timeout", "api-error", "malformed-image"])
async def test_failed_revision_keeps_first_candidate_and_safe_original_verdict(tmp_path, references, failure):
    calls = 0
    pending_stopped = asyncio.Event()

    async def respond(request):
        nonlocal calls
        if request.url.path.endswith("/images/edits"):
            calls += 1
            if calls == 1:
                return image_response("green")
            if failure == "timeout":
                try:
                    raise TimeoutError("test-reconstruction-secret")
                finally:
                    pending_stopped.set()
            if failure == "api-error":
                return httpx.Response(401, json={"error": {"message": "test-reconstruction-secret"}})
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(b"broken").decode()}]})
        if json.loads(request.content)["response_format"]["json_schema"]["name"] == "reconstruction_plan":
            return chat_response(source_plan())
        answer = fidelity_answer()
        answer["feature_checks"][0].update(status="fail", reason="格紋改成寬條紋。")
        answer["corrections"] = [{"check": "visible_details", "instruction": "Restore original plaid."}]
        return chat_response(answer)

    provider = create_provider(tmp_path, respond)
    candidate = tmp_path / "candidate.png"
    try:
        report = await provider.reconstruct(*references, candidate)
    finally:
        await provider.close()
    assert calls == 2 and report["selected_attempt"] == 1 and len(report["attempts"]) == 1
    assert report["status"] == "rejected" and report["revision_error"]
    assert "test-reconstruction-secret" not in json.dumps(report)
    with Image.open(candidate) as image:
        assert image.getpixel((0, 0)) == (0, 128, 0)
    assert not list(tmp_path.glob(".reconstruction-*"))


async def test_source_only_detail_review_is_unprimed_can_relocate_roi_and_preserves_visible_side(
    tmp_path, references
):
    plan, answer, checks = regional_fixture("pattern", "ambiguous")
    poison = "UNSUPPORTED_FIRST_PLAN_GUESS"
    plan["detail_regions"][0].update(box=[0.05, 0.1, 0.3, 0.7], detail=poison)
    plan["visible_features"][0]["detail"] = poison
    original_context = Image.new("RGB", (90, 120), "red")
    original_context.paste("blue", (45, 0, 90, 120))
    original_context.save(references[1])
    refined = {
        **plan["detail_regions"][0],
        "box": [0.65, 0.1, 0.9, 0.7],
        "support": "clear",
        "detail": "原圖較清楚的另一側布片呈藍色，邊緣平直。",
    }
    side_constraint = {"feature": "other", "detail": "原圖畫面右側下襬可見小標，須保留原側位置。"}
    answer["feature_checks"].append({"feature": "other", "status": "pass", "reason": "右側小標位置保留。"})
    checks[0].update(status="pass", reason="局部實際可見細節相符。", correction=None)
    calls = []

    def respond(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/images/edits"):
            calls.append("edit")
            assert poison not in body["prompt"]
            assert refined["detail"] in body["prompt"] and side_constraint["detail"] in body["prompt"]
            with Image.open(
                io.BytesIO(base64.b64decode(body["images"][-1]["image_url"].split(",", 1)[1]))
            ) as detail:
                assert detail.getextrema() == ((0, 0), (0, 0), (255, 255))
            return image_response()
        name = body["response_format"]["json_schema"]["name"]
        calls.append(name)
        if name == "reconstruction_plan":
            return chat_response(plan)
        if name == "reconstruction_source_details":
            content = body["messages"][1]["content"]
            assert poison not in json.dumps(content)
            assert "GENERATED_CANDIDATE" not in json.dumps(content)
            assert image_pixels(content) == [(255, 0, 0), (0, 0, 255)] + [(255, 0, 0)] * 4
            return chat_response(
                {
                    "detail_regions": [refined],
                    "visible_features": [side_constraint],
                    "additional_regions": [],
                }
            )
        if name == "reconstruction_fidelity":
            assert "原圖畫面右側下襬" in body["messages"][1]["content"][0]["text"]
            return chat_response(answer)
        assert name == "reconstruction_regional_fidelity"
        assert image_pixels(body["messages"][1]["content"]) == [(0, 0, 255), (0, 128, 0)]
        return chat_response({"regional_checks": checks})

    provider = create_provider(tmp_path, respond)
    try:
        report = await provider.reconstruct(*references, tmp_path / "candidate.png")
    finally:
        await provider.close()
    assert calls == [
        "reconstruction_plan",
        "reconstruction_source_details",
        "edit",
        "reconstruction_fidelity",
        "reconstruction_regional_fidelity",
    ]
    assert report["plan"]["detail_regions"][0] == refined
    assert report["plan"]["visible_features"][-1] == side_constraint
    assert report["plan"]["initial_source_plan"] == plan
    assert report["plan"]["original_ambiguous_regions"] == plan["detail_regions"]
    assert report["regional_checks"][0]["status"] == "pass"
    assert report["feature_checks"][0]["status"] == "pass"
    assert report["status"] == "needs_review" and not is_accepted(report)


@pytest.mark.parametrize("bad", ["missing", "duplicate", "wrong-feature", "cutout-over-context"])
async def test_source_detail_review_cannot_drop_evidence_or_replace_context_with_mask(
    tmp_path, references, bad
):
    plan, _, _ = regional_fixture()
    reviewed = copy.deepcopy(plan["detail_regions"])
    if bad == "missing":
        reviewed = []
    elif bad == "duplicate":
        reviewed *= 2
    elif bad == "wrong-feature":
        reviewed[0]["feature"] = "graphics"
    else:
        reviewed[0]["source_image"] = "cutout"
    calls = []

    def respond(request):
        name = json.loads(request.content)["response_format"]["json_schema"]["name"]
        calls.append(name)
        return chat_response(
            plan
            if name == "reconstruction_plan"
            else {
                "detail_regions": reviewed,
                "visible_features": [],
                "additional_regions": [],
            }
        )

    provider = create_provider(tmp_path, respond)
    candidate = tmp_path / "candidate.png"
    try:
        with pytest.raises(ProviderError) as caught:
            await provider.reconstruct(*references, candidate)
    finally:
        await provider.close()
    assert calls == ["reconstruction_plan", "reconstruction_source_details", "reconstruction_source_details"]
    assert caught.value.diagnostics[-1]["issues"][0]["code"] == (
        "reference" if bad == "cutout-over-context" else "duplicate" if bad == "duplicate" else "coverage"
    )
    assert not candidate.exists()


@pytest.mark.parametrize("regional_status", ["pass", "fail"])
def test_original_unknown_scope_survives_refined_roi_without_hiding_visible_failure(regional_status):
    initial, answer, checks = regional_fixture("panel_geometry", "ambiguous")
    plan = copy.deepcopy(initial)
    plan["initial_source_plan"] = copy.deepcopy(initial)
    plan["original_ambiguous_regions"] = copy.deepcopy(initial["detail_regions"])
    plan["detail_regions"][0].update(
        support="clear", box=[0.3, 0.2, 0.45, 0.4], detail="更小範圍只有可見肩縫。"
    )
    checks[0]["status"] = regional_status
    if regional_status == "pass":
        checks[0].update(reason="可見肩縫一致。", correction=None)
    attempt = assess_fidelity(plan, answer, 1, checks)
    report = build_report(plan, [attempt])
    assert report["regional_checks"][0]["status"] == regional_status
    assert all(check["status"] == "pass" for check in report["feature_checks"])
    assert report["status"] == ("rejected" if regional_status == "fail" else "needs_review")
    assert not is_accepted(report)
    assert report["plan"]["original_ambiguous_regions"] == initial["detail_regions"]
    tampered = copy.deepcopy(report)
    tampered["plan"]["original_ambiguous_regions"] = []
    with pytest.raises(ValueError, match="erase"):
        validate_report(tampered)
    if regional_status == "pass":
        forged = copy.deepcopy(report)
        forged["status"] = forged["attempts"][0]["status"] = "accepted"
        for name in ("visible_details", "cut_structure"):
            forged["checks"][name]["status"] = "pass"
            forged["attempts"][0]["checks"][name]["status"] = "pass"
        assert not is_accepted(forged)


def test_clear_and_legacy_source_plans_are_still_eligible_for_acceptance():
    plan, answer, checks = regional_fixture()
    checks[0].update(status="pass", reason="可見袖口一致。", correction=None)
    legacy = build_report(plan, [assess_fidelity(plan, answer, 1, checks)])
    assert is_accepted(legacy)
    new = {
        **copy.deepcopy(plan),
        "initial_source_plan": copy.deepcopy(plan),
        "original_ambiguous_regions": [],
    }
    assert is_accepted(build_report(new, [assess_fidelity(new, answer, 1, checks)]))
    for key in ("initial_source_plan", "original_ambiguous_regions", "planning_attempts"):
        assert key not in PLAN_SCHEMA["properties"]


async def test_source_format_repairs_are_bounded_unprimed_and_record_both_stages(tmp_path, references):
    plan, _, _ = regional_fixture()
    requests = []
    secret = "test-reconstruction-secret"
    observations = [
        {"feature": "other", "detail": "畫面左側有小標。"},
        {"feature": "other", "detail": "畫面右側有拉片。"},
    ]

    def respond(request):
        body = json.loads(request.content)
        name = body["response_format"]["json_schema"]["name"]
        requests.append(body)
        count = sum(row["response_format"]["json_schema"]["name"] == name for row in requests)
        if count == 2:
            assert secret not in json.dumps(body)
            assert "Preserve uncertainty" in body["messages"][1]["content"][-1]["text"]
        if name == "reconstruction_plan":
            if count == 1:
                data = chat_response(plan).json()
                data["choices"][0]["message"]["content"] = '{"unclosed":"' + secret
                return httpx.Response(200, json=data)
            return chat_response(plan)
        assert name == "reconstruction_source_details"
        assert "exactly once" in body["messages"][0]["content"]
        return chat_response(
            {
                "detail_regions": plan["detail_regions"],
                "visible_features": observations
                if count == 1
                else [{"feature": "other", "detail": "畫面左側有小標；畫面右側有拉片。"}],
                "additional_regions": [],
            }
        )

    provider = create_provider(tmp_path, respond)
    try:
        result = await provider.plan_reconstruction(*references)
    finally:
        await provider.close()
    diagnostics = validate_planning_diagnostics(result["planning_attempts"])
    assert len(requests) == 4
    assert [(entry["stage"], entry["status"]) for entry in diagnostics] == [
        ("source_plan", "invalid"),
        ("source_plan", "valid"),
        ("source_refinement", "invalid"),
        ("source_refinement", "valid"),
    ]
    assert secret not in json.dumps(diagnostics)
    assert diagnostics[0]["issues"] == [{"code": "json", "path": ["response"]}]
    assert diagnostics[2]["issues"][0]["code"] == "duplicate"
    assert result["visible_features"][-1]["detail"] == "畫面左側有小標；畫面右側有拉片。"
    assert diagnostics[-1]["source_details"]["visible_features"] == result["visible_features"][-1:]


@pytest.mark.parametrize("bad", ["missing", "duplicate", "incomplete"])
async def test_plan_shape_retry_exhaustion_keeps_safe_stage_diagnostics(tmp_path, references, bad):
    calls = []

    def respond(request):
        calls.append(request)
        value = source_plan()
        if bad == "missing":
            value.pop("detail_regions")
        elif bad == "duplicate":
            value["visible_features"].append(value["visible_features"][0])
        response = chat_response(value).json()
        if bad == "incomplete":
            response["choices"][0]["finish_reason"] = "length"
        return httpx.Response(200, json=response)

    provider = create_provider(tmp_path, respond)
    try:
        with pytest.raises(ProviderError, match="failed structural validation twice in a row") as caught:
            await provider.reconstruct(*references, tmp_path / "candidate.png")
    finally:
        await provider.close()
    assert len(calls) == 2 and not (tmp_path / "candidate.png").exists()
    entries = validate_planning_diagnostics(caught.value.diagnostics)
    assert [entry["status"] for entry in entries] == ["invalid", "invalid"]
    assert (
        entries[-1]["issues"][0]["code"]
        == {"missing": "missing", "duplicate": "duplicate", "incomplete": "incomplete"}[bad]
    )
    assert "test-reconstruction-secret" not in json.dumps(entries)


@pytest.mark.parametrize("failure", ["403", "429", "timeout", "refusal", "unexpected"])
async def test_source_provider_errors_are_not_retried_or_echoed(tmp_path, references, failure):
    calls = []

    def respond(request):
        calls.append(request)
        if failure in ("403", "429"):
            return httpx.Response(int(failure), json={"error": "test-reconstruction-secret"})
        if failure == "timeout":
            raise httpx.ReadTimeout("test-reconstruction-secret", request=request)
        if failure == "unexpected":
            raise RuntimeError("test-reconstruction-secret")
        response = chat_response(source_plan()).json()
        response["choices"][0]["message"]["refusal"] = "test-reconstruction-secret"
        return httpx.Response(200, json=response)

    provider = create_provider(tmp_path, respond)
    try:
        with pytest.raises(ProviderError, match="source planning did not complete") as caught:
            await provider.plan_reconstruction(*references)
    finally:
        await provider.close()
    assert len(calls) == 1
    entries = validate_planning_diagnostics(caught.value.diagnostics)
    assert entries[0]["status"] == "error"
    assert "test-reconstruction-secret" not in str(caught.value) + json.dumps(entries)


async def test_total_deadline_cancels_structural_retry_without_a_late_image(tmp_path, references):
    calls = []
    stopped = asyncio.Event()

    async def respond(request):
        calls.append(request)
        if len(calls) == 1:
            malformed = source_plan()
            malformed.pop("detail_regions")
            return chat_response(malformed)
        try:
            await asyncio.sleep(30)
        finally:
            stopped.set()

    provider = create_provider(tmp_path, respond, timeout=0.15)
    try:
        with pytest.raises(ProviderError, match="timed out") as caught:
            await asyncio.wait_for(provider.reconstruct(*references, tmp_path / "candidate.png"), 1)
    finally:
        await provider.close()
    assert stopped.is_set() and len(calls) == 2
    assert len(validate_planning_diagnostics(caught.value.diagnostics)) == 1
    assert not (tmp_path / "candidate.png").exists()


def displayed_uncertainty(scope="displayed"):
    return {
        "area": "袖口收口",
        "detail": "彎曲雙手遮住袖口，不能確認直筒或羅紋收口。",
        "critical": False,
        "scope": scope,
        "scope_evidence": "完整展示會露出袖口；手部遮擋不表示袖口在衣物背面。",
        "unshown_view": None,
    }


@pytest.mark.parametrize("scope", ["displayed", "unclear", None])
@pytest.mark.parametrize("visible_failure", [False, True])
def test_unresolved_displayed_details_block_acceptance_even_with_clear_regions(scope, visible_failure):
    plan, answer, checks = regional_fixture()
    area = displayed_uncertainty()
    if scope is None:
        for field in ("scope", "scope_evidence", "unshown_view"):
            area.pop(field)
    else:
        area["scope"] = scope
    plan["uncertain_areas"] = [area]
    plan["initial_source_plan"] = copy.deepcopy(plan)
    plan["original_ambiguous_regions"] = []
    checks[0].update(status="pass", reason="獨立清楚的區域相符。", correction=None)
    if visible_failure:
        answer["feature_checks"][0].update(status="fail", reason="可見格紋被改為圓點。")
    attempt = assess_fidelity(plan, answer, 1, checks)
    report = build_report(plan, [attempt])
    assert not is_accepted(report)
    assert report["status"] == ("rejected" if visible_failure else "needs_review")
    assert report["regional_checks"][0]["status"] == "pass"
    assert report["feature_checks"] == answer["feature_checks"]
    assert report["checks"]["visible_details"]["status"] == ("fail" if visible_failure else "uncertain")
    if visible_failure:
        assert "圓點" in report["checks"]["visible_details"]["reason"]


@pytest.mark.parametrize("view", ["front", "back"])
def test_only_evidenced_opposite_face_uncertainty_can_be_excluded(view):
    plan = source_plan(view)
    assert is_accepted(build_report(plan, [assess_fidelity(plan, fidelity_answer(view), 1)]))
    plan["uncertain_areas"][0]["unshown_view"] = view
    with pytest.raises(ValueError, match="opposite"):
        ReconstructionPlan.model_validate(plan)
    plan["uncertain_areas"][0]["unshown_view"] = "back" if view == "front" else "front"
    plan["uncertain_areas"][0]["scope_evidence"] = " "
    with pytest.raises(ValueError):
        ReconstructionPlan.model_validate(plan)


@pytest.mark.parametrize("mutation", ["drop", "relabel"])
def test_refinement_cannot_remove_or_relabel_original_uncertain_display_scope(mutation):
    initial = source_plan("front")
    initial["uncertain_areas"] = [displayed_uncertainty()]
    refined = {**copy.deepcopy(initial), "initial_source_plan": copy.deepcopy(initial)}
    if mutation == "drop":
        refined["uncertain_areas"] = []
    else:
        refined["uncertain_areas"][0].update(scope="unshown", unshown_view="back")
    with pytest.raises(ValueError, match="original uncertain areas"):
        ReconstructionPlan.model_validate(refined)


def test_legacy_accepted_scope_is_downgraded_without_inventing_observations_or_reselecting_image():
    plan = source_plan()
    plan["initial_source_plan"] = copy.deepcopy(plan)
    passed = assess_fidelity(plan, fidelity_answer(), 1)
    failed_answer = fidelity_answer(color_pattern={"status": "fail", "reason": "可見顏色錯誤。"})
    failed = assess_fidelity(plan, failed_answer, 2)
    legacy = build_report(plan, [passed, failed], selected_attempt=1)
    legacy.pop("preview")
    for attempt in legacy["attempts"]:
        attempt.pop("preview")
    for evidence in (legacy["plan"], legacy["plan"]["initial_source_plan"]):
        for field in ("scope", "scope_evidence", "unshown_view"):
            evidence["uncertain_areas"][0].pop(field)
    original = copy.deepcopy(legacy)
    with pytest.raises(ValueError, match="unscoped"):
        ReconstructionReport.model_validate(legacy)
    upgraded = validate_report(legacy)
    assert legacy == original
    assert upgraded["generated"] and upgraded["selected_attempt"] == 1
    assert upgraded["status"] == "needs_review" and not is_accepted(legacy)
    assert [entry["status"] for entry in upgraded["attempts"]] == ["needs_review", "rejected"]
    assert upgraded["attempts"][1]["checks"]["color_pattern"] == failed["checks"]["color_pattern"]
    for field in ("scope", "scope_evidence", "unshown_view"):
        assert upgraded["plan"]["uncertain_areas"][0][field] is None
    assert upgraded["feature_checks"] == original["feature_checks"]
    assert upgraded["regional_checks"] == original["regional_checks"]
    assert validate_report(upgraded) == upgraded
    tampered = copy.deepcopy(legacy)
    tampered["attempts"][0]["feature_checks"][0]["status"] = "fail"
    with pytest.raises(ValueError, match="source details"):
        validate_report(tampered)


@pytest.mark.parametrize("missing", ["scope", "scope_evidence", "unshown_view"])
async def test_new_source_response_requires_scoped_unknowns_with_one_bounded_repair(
    tmp_path, references, missing
):
    calls = []
    complete = source_plan()
    complete["uncertain_areas"] = [displayed_uncertainty()]

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        assert "NOT whether the source photograph exposes it" in body["messages"][0]["content"]
        value = copy.deepcopy(complete)
        if len(calls) == 1:
            value["uncertain_areas"][0].pop(missing)
        return chat_response(value)

    provider = create_provider(tmp_path, respond)
    try:
        plan = await provider.plan_reconstruction(*references)
    finally:
        await provider.close()
    assert len(calls) == 2
    assert [entry["status"] for entry in plan["planning_attempts"]] == ["invalid", "valid"]
    assert plan["uncertain_areas"] == complete["uncertain_areas"]
    assert plan["planning_attempts"][0]["issues"][0]["path"][:2] == ["uncertain_areas", 0]
    schema = PLAN_SCHEMA["$defs"]["UncertainArea"]
    assert {"scope", "scope_evidence", "unshown_view"}.issubset(schema["required"])
    assert schema["properties"]["scope"]["enum"] == ["displayed", "unshown", "unclear"]


async def test_noncritical_display_uncertainty_preserves_generated_candidate_for_review(tmp_path, references):
    calls = []
    plan = source_plan()
    plan["uncertain_areas"] = [displayed_uncertainty()]

    def respond(request):
        calls.append(request.url.path)
        body = json.loads(request.content)
        if request.url.path.endswith("/images/edits"):
            return image_response()
        name = body["response_format"]["json_schema"]["name"]
        return chat_response(plan if name == "reconstruction_plan" else fidelity_answer())

    provider = create_provider(tmp_path, respond)
    destination = tmp_path / "candidate.png"
    try:
        report = await provider.reconstruct(*references, destination)
    finally:
        await provider.close()
    assert report["status"] == "needs_review" and report["generated"]
    assert calls == ["/v1/chat/completions", "/v1/images/edits", "/v1/chat/completions"]
    with Image.open(destination) as candidate:
        assert candidate.getpixel((0, 0)) == (0, 128, 0)
    assert report["plan"]["initial_source_plan"]["uncertain_areas"] == plan["uncertain_areas"]
