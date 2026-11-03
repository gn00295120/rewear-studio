"""Preview readiness is separate from strict same-garment fidelity."""

import copy
import json
import subprocess

import pytest

from wardrobe.reconstruction_quality import assess_fidelity, build_report, is_accepted, is_preview_ready
from wardrobe.schemas import unknown_garment

from test_reconstruction_ui_contract import NODE, NODE_PROBE, ROOT, fixture_report

pytestmark = pytest.mark.skipif(NODE is None, reason="The frontend contract needs a Node runtime")


def preview_report(view="front", *, tucked=False, mismatch=False):
    base = fixture_report()
    plan = base["plan"]
    plan["source_view"] = view
    if tucked:
        plan["uncertain_areas"] = [
            {
                "area": "下襬與衣長",
                "detail": "原圖下襬紮入褲頭，完整衣長與下襬由 AI 推測補畫。",
                "critical": True,
                "scope": "displayed",
                "scope_evidence": "原照片可見上衣被褲腰遮住，沒有完整下襬。",
                "unshown_view": None,
            }
        ]
    answer = {
        "observed_view": "back" if view == "back" else "front",
        "checks": base["checks"],
        "feature_checks": base["feature_checks"],
        "corrections": [],
        "region_boxes": [],
        "preview_observations": {
            name: {"status": "pass", "reason": "明確介面假資料：基本展示條件通過。"}
            for name in ("category", "coarse_color")
        },
    }
    if mismatch:
        plan["visible_features"].append({"feature": "graphics", "detail": "原圖胸前有小字樣。"})
        answer["feature_checks"].append(
            {"feature": "graphics", "status": "fail", "reason": "胸前字樣的位置與原圖不同，不能視為同款。"}
        )
    return build_report(plan, [assess_fidelity(plan, answer, 1)])


def fixture_item(item_id, report=None):
    item = {
        "id": item_id,
        "job_id": "fixture-job",
        "created_at": "2026-10-22T08:00:00Z",
        "record": {
            **unknown_garment(),
            "category": "top",
            "subtype": f"明確測試 · {item_id}",
            "colors": ["blue"],
        },
        "quality": {
            "status": "needs_confirmation",
            "visibility": "partial",
            "reasons": ["測試衣物部分被遮擋。"],
        },
        "reviewed": False,
        "provenance": "explicit UI fixture",
        "frame": None,
        "image_url": "/media/fixture-job/context.jpg",
        "original_url": "/media/fixture-job/source.jpg",
        "source_url": "/media/fixture-job/source.jpg",
        "cutout_url": "/media/fixture-job/cutout.jpg",
        "discovery": None,
        "reconstruction": None,
    }
    if report:
        strict = is_accepted(report)
        ready = is_preview_ready(report)
        image = "/media/fixture-job/reconstructions/preview.png"
        item["reconstruction"] = {
            "id": "fixture-attempt",
            "status": "completed" if strict else report["status"],
            "image_url": image if strict else None,
            "image_validation": copy.deepcopy(report) if strict else None,
            "preview_url": image if ready else None,
            "preview_validation": copy.deepcopy(report) if ready else None,
            "candidate_url": image,
            "candidate_validation": copy.deepcopy(report),
            "validation": copy.deepcopy(report),
        }
    return item


def shopping_result():
    offer = {
        "title": "明確介面假資料：藍色上衣",
        "url": "https://www.nike.com/tw/t/explicit-fixture",
        "merchant": "www.nike.com",
        "match": "similar",
        "reason": "只比較原圖可見款式，不是同款證明。",
        "verification": {
            "status": "verified",
            "page_url": "https://www.nike.com/tw/t/explicit-fixture",
            "page_checked_at": "2026-10-22T08:00:00Z",
            "market": "in_scope",
            "market_evidence": "明確測試資料：頁面列出台灣地區。",
            "availability": "unknown",
            "comparison": {
                key: "match"
                for key in (
                    "category",
                    "primary_color",
                    "pattern",
                    "sleeve_length",
                    "neckline",
                    "design_details",
                )
            },
            "image_url": "/media/fixture-job/shopping/attempt/1.png",
            "product_code": None,
        },
    }
    return {
        "market": "TW",
        "identity": {
            "status": "unknown",
            "brand": None,
            "evidence_type": "none",
            "visible_text": None,
            "evidence": "原圖沒有可辨認的品牌標記。",
        },
        "source_urls": ["/media/fixture-job/source.jpg"],
        "search_links": [{"label": "搜尋購買管道", "url": "https://www.google.com/search?q=blue+shirt"}],
        "shopping": {
            "version": 2,
            "status": "searched",
            "offers": [offer],
            "sources": [],
            "checked_candidates": [],
            "searched_at": "2026-10-22T08:00:00Z",
        },
    }


def cases():
    rows = [
        ("side-inferred-front", preview_report("side"), True),
        ("tucked-inferred-hem", preview_report(tucked=True), True),
        ("known-logo-mismatch", preview_report(mismatch=True), True),
        ("back-keeps-back", preview_report("back"), True),
    ]
    good = preview_report("side", tucked=True, mismatch=True)
    for name, mutation in [
        ("missing-preview-proof", lambda r: r.update(preview=None)),
        ("missing-single-check", lambda r: r["preview"]["checks"].pop("single_garment")),
        ("uncertain-category", lambda r: r["preview"]["checks"]["category"].update(status="uncertain")),
        ("incomplete-outline", lambda r: r["preview"]["checks"]["complete_outline"].update(status="fail")),
        ("changed-attempt-binding", lambda r: r["attempts"][0]["preview"].update(notice="另一張預覽")),
        (
            "hidden-inferences",
            lambda r: (
                r["preview"].update(inferred_areas=[]),
                r["attempts"][0]["preview"].update(inferred_areas=[]),
            ),
        ),
        (
            "hidden-known-mismatch",
            lambda r: (
                r["preview"].update(mismatch_warnings=[]),
                r["attempts"][0]["preview"].update(mismatch_warnings=[]),
            ),
        ),
        ("missing-generated-image", lambda r: r.update(generated=False)),
        ("fragment-cannot-invent-target", lambda r: r["plan"].update(visibility="fragment")),
        ("missing-attempt", lambda r: r.update(attempts=[])),
    ]:
        report = copy.deepcopy(good)
        mutation(report)
        rows.append((name, report, False))
    return rows


CASES = cases()
PROBE = NODE_PROBE.replace(
    "image_validation: input, validation: input,",
    "image_validation: null, validation: input, preview_url: '/media/fixture/preview.png', preview_validation: input,",
).replace(
    "accepted: acceptedReconstructionReport(input),",
    "ready: readyPreviewReport(input), accepted: acceptedReconstructionReport(input),",
)


@pytest.mark.parametrize(("name", "report", "expected"), CASES, ids=[row[0] for row in CASES])
def test_preview_readiness_matches_backend_without_claiming_strict_fidelity(name, report, expected):
    output = subprocess.run(
        [NODE, "-e", PROBE],
        cwd=ROOT,
        input=json.dumps(report, ensure_ascii=False),
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    actual = json.loads(output.stdout)
    assert actual["ready"] is expected and is_preview_ready(report) is expected, name
    assert actual["verifiedLabel"] is False
    assert actual["cardImage"].endswith("/preview.png" if expected else "/context.jpg")
    assert actual["defaultMedia"] == ("preview" if expected else "context")
    assert {"context", "source", "cutout", "candidate"}.issubset(entry["key"] for entry in actual["media"])
    if name in {"side-inferred-front", "tucked-inferred-hem", "known-logo-mismatch"}:
        assert actual["accepted"] is False and not is_accepted(report)
