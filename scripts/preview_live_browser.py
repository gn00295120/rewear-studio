"""Read-only UI verification of the predeclared ten real preview observations."""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wardrobe.reconstruction_quality import is_accepted, is_preview_ready  # noqa: E402

POLICY = Path.home() / ".agents/skills/webapp-testing/scripts"
sys.path.insert(0, str(POLICY))
from network_policy import LocalNetworkPolicy, install_network_policy  # noqa: E402


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def offers_for(item):
    shopping = ((item.get("discovery") or {}).get("result") or {}).get("shopping") or {}
    return shopping.get("offers", []) if shopping.get("version") == 2 else []


def visual_review_metadata(path, cases):
    audit = json.loads(path.read_text())
    indexed = {case["case"]: case for case in audit["cases"]}
    assert set(indexed) == {case["case"] for case in cases}, "Visual audit must cover the same fixed ten."
    counts = {
        status: sum(case["visible_fidelity"] == status for case in indexed.values())
        for status in ("pass", "fail", "uncertain")
    }
    assert sum(counts.values()) == len(cases)
    return {
        "source": str(path),
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "reviewer": audit["reviewer"],
        "disclosure": audit["disclosure"],
        "visible_fidelity": {
            "passed": counts["pass"],
            "failed": counts["fail"],
            "uncertain": counts["uncertain"],
            "denominator": len(cases),
        },
        "conclusion": audit["conclusion"],
        "separate_from": "Coarse preview readiness and strict model acceptance; this script checks UI presentation, not physical garment identity.",
    }, {name: case["visible_fidelity"] for name, case in indexed.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--visual-review", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    address = urlsplit(args.url)
    assert address.hostname in {"127.0.0.1", "localhost"} and address.scheme == "http"
    assert address.port not in {8765, 8774, 8775}, "Use only the new dedicated results server."
    base = args.url.rstrip("/")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(args.manifest.read_text())
    cases = manifest["cases"]
    assert len(cases) == 10 and len({case["item_id"] for case in cases}) == 10
    visual_review, fidelity_by_case = visual_review_metadata(
        args.visual_review or args.manifest.parent / "independent-visual-review.json", cases
    )

    def get_items():
        response = httpx.get(base + "/api/items", timeout=20)
        response.raise_for_status()
        return response.json()["items"]

    before = get_items()
    static_hashes = {}
    for asset in ("app.js", "styles.css"):
        response = httpx.get(base + "/static/" + asset, timeout=20)
        response.raise_for_status()
        static_hashes[asset] = hashlib.sha256(response.content).hexdigest()
    by_id = {item["id"]: item for item in before}
    assert all(case["item_id"] in by_id for case in cases), "The fixed ten observations are required."
    # Separately identified shopping results may have no verified offers; they
    # still need search telemetry and candidate-reason checks. They never change
    # the fixed preview denominator and is not substituted for a failed preview.
    extra = [
        {"case": "supplemental-shopping-" + item["id"], "item_id": item["id"]}
        for item in before
        if isinstance(((item.get("discovery") or {}).get("result") or {}).get("shopping"), dict)
        and item["id"] not in {case["item_id"] for case in cases}
    ]
    ready_count = sum(
        bool((by_id[case["item_id"]].get("reconstruction") or {}).get("preview_url"))
        and is_preview_ready(by_id[case["item_id"]]["reconstruction"].get("preview_validation"))
        for case in cases
    )
    report = {
        "status": "running",
        "scope": "Read-only real saved results in the actual app; no model calls, no fixture substitution",
        "url": base,
        "manifest": str(args.manifest),
        "manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        "fixed_preview_observations": len(cases),
        "ready_previews": ready_count,
        "coarse_preview_only_target_met": ready_count >= manifest["target"]["preview_ready"],
        "coarse_preview_definition": manifest["definition"],
        "source_fidelity_visual_review": visual_review,
        "supplemental_shopping_items": len(extra),
        "before_items_sha256": digest(before),
        "served_static_sha256": static_hashes,
        "scenarios": [],
        "page_errors": [],
        "blocked_mutations": [],
        "viewports": [1440, 390],
    }

    def write_report():
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(service_workers="block")
            install_network_policy(context, LocalNetworkPolicy.for_http(base))

            def read_only(route):
                if route.request.method not in {"GET", "HEAD", "OPTIONS"}:
                    report["blocked_mutations"].append(
                        {"method": route.request.method, "url": route.request.url}
                    )
                    route.abort("blockedbyclient")
                else:
                    route.fallback()

            context.route("**/*", read_only)
            page = context.new_page()
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
            for width, height in ((1440, 1050), (390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                page.goto(base, wait_until="networkidle")
                for case in cases + extra:
                    item = by_id[case["item_id"]]
                    saved = item.get("reconstruction") or {}
                    strict = bool(saved.get("image_url")) and is_accepted(saved.get("image_validation"))
                    preview = bool(saved.get("preview_url")) and is_preview_ready(
                        saved.get("preview_validation")
                    )
                    expected_media = "reconstruction" if strict else "preview" if preview else "context"
                    expected_url = (
                        saved["image_url"]
                        if strict
                        else saved["preview_url"]
                        if preview
                        else item["image_url"]
                    )
                    result = {
                        "case": case["case"],
                        "item_id": item["id"],
                        "viewport": width,
                        "preview_ready": preview,
                        "strict_accepted": strict,
                        "source_fidelity_visual": fidelity_by_case.get(case["case"], "not_in_preview_cohort"),
                        "default_media": expected_media,
                        "status": "running",
                    }
                    report["scenarios"].append(result)
                    try:
                        card = page.locator(f'.garment-card:has([data-item-id="{item["id"]}"])')
                        expect(card).to_be_visible()
                        card.scroll_into_view_if_needed()
                        expect(card.locator(".garment-image img")).to_have_attribute(
                            "src", urljoin(base, expected_url)
                        )
                        assert card.locator(".garment-image img").evaluate(
                            "img => img.decode().then(() => img.naturalWidth > 0)"
                        )
                        if strict or preview:
                            expect(card.locator(".card-reconstruction")).to_have_text(
                                "AI 展示圖 · 已比對" if strict else "AI 預覽・含推測"
                            )
                        expect(card.locator(".card-shopping")).to_contain_text("購買選項")
                        expect(card.locator("[data-open-discovery]")).to_be_visible()
                        page.locator(f'[data-item-id="{item["id"]}"]').click()
                        expect(page.locator("#item-media-region")).to_have_attribute(
                            "data-selected", expected_media
                        )
                        assert page.locator("#item-dialog").evaluate(
                            "el => el.scrollWidth <= el.clientWidth + 1"
                        )
                        if strict or preview:
                            expect(page.locator("#item-media-content figure")).to_have_count(2)
                            source = page.locator("#item-media-content figure").first
                            expect(source).to_contain_text("原圖 · 可見依據")
                            source_url = source.locator("img").get_attribute("src")
                            assert "/reconstructions/" not in source_url and "/shopping/" not in source_url
                        for image in page.locator("#item-media-content img").all():
                            assert image.evaluate("img => img.decode().then(() => img.naturalWidth > 0)")
                        if preview:
                            proof = saved["preview_validation"]["preview"]
                            panel = page.locator("#item-reconstruction")
                            expect(panel).to_contain_text(proof["notice"])
                            for area in proof["inferred_areas"]:
                                expect(panel.locator(".preview-inferences").first).to_contain_text(
                                    area["detail"]
                                )
                            for warning in proof["mismatch_warnings"]:
                                expect(panel.locator(".preview-mismatches")).to_contain_text(warning)
                            result["inferred_areas"] = len(proof["inferred_areas"])
                            result["visible_mismatch_warnings"] = len(proof["mismatch_warnings"])
                        page.screenshot(
                            path=output / f"{case['case']}-{width}-preview.png", animations="disabled"
                        )
                        if saved:
                            evidence = (
                                page.locator(".preview-inferences").first
                                if preview
                                else page.locator("#item-reconstruction")
                            )
                            evidence.evaluate(
                                "el => el.scrollIntoView({block: 'start', behavior: 'instant'})"
                            )
                            page.screenshot(
                                path=output / f"{case['case']}-{width}-inferences.png", animations="disabled"
                            )
                            if preview and saved["preview_validation"]["preview"]["mismatch_warnings"]:
                                page.locator(".preview-mismatches").evaluate(
                                    "el => el.scrollIntoView({block: 'start', behavior: 'instant'})"
                                )
                                page.screenshot(
                                    path=output / f"{case['case']}-{width}-mismatches.png",
                                    animations="disabled",
                                )

                        # Inspect the purchasing controls without pressing the
                        # start/retry button or following any external link.
                        purchase = page.locator("#item-discovery")
                        purchase.scroll_into_view_if_needed()
                        expect(purchase).to_contain_text("品牌與購買管道")
                        result["shopping_status"] = (item.get("discovery") or {}).get("status", "not_started")
                        offers = offers_for(item)
                        result["verified_offers"] = len(offers)
                        expect(page.locator(".shopping-offers > li")).to_have_count(len(offers))
                        shopping = ((item.get("discovery") or {}).get("result") or {}).get("shopping") or {}
                        attempts = shopping.get("search_attempts", [])
                        result["search_attempts"] = len(attempts)
                        if attempts:
                            expect(page.locator(".shopping-attempts li")).to_have_count(len(attempts))
                            for attempt in attempts:
                                expect(page.locator(".shopping-attempts")).to_contain_text(attempt["query"])
                        checked = [
                            candidate
                            for candidate in shopping.get("checked_candidates", [])
                            if candidate.get("status") in {"rejected", "unverified"}
                        ]
                        result["unaccepted_candidates"] = len(checked)
                        if shopping.get("version") == 2 and checked and not offers:
                            assert page.locator(".shopping-checks").evaluate("el => el.open")
                            for candidate in checked:
                                expect(page.locator(".shopping-checks")).to_contain_text(candidate["title"])
                                for reason in candidate.get("reasons", []):
                                    expect(page.locator(".shopping-checks")).to_contain_text(reason)
                        for offer, listing in zip(offers, page.locator(".shopping-offers > li").all()):
                            expect(listing.locator(".offer-match")).to_have_text(
                                "型號候選" if offer["match"] == "model_candidate" else "相似款"
                            )
                            expect(listing).to_contain_text("不代表同款或真偽驗證")
                            image = listing.locator(".offer-product-image")
                            assert urlsplit(image.get_attribute("src")).netloc == address.netloc
                            assert image.evaluate("img => img.decode().then(() => img.naturalWidth > 0)")
                            link = listing.locator("a").first
                            assert link.get_attribute("target") == "_blank"
                            assert link.get_attribute("rel") == "noopener noreferrer"
                        for image in page.locator(".discovery-image-evidence img").all():
                            source_url = image.get_attribute("src")
                            assert "/reconstructions/" not in source_url and "/shopping/" not in source_url
                        if item.get("discovery"):
                            page.screenshot(
                                path=output / f"{case['case']}-{width}-shopping.png", animations="disabled"
                            )
                        page.keyboard.press("Escape")
                        expect(page.locator("#item-dialog")).not_to_be_visible()
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                        result["status"] = "passed"
                    except Exception as error:
                        result.update(status="failed", error=str(error))
                        page.screenshot(path=output / f"{case['case']}-{width}-failed.png")
                        raise
                    finally:
                        write_report()
            assert not report["page_errors"], report["page_errors"]
            assert not report["blocked_mutations"], report["blocked_mutations"]
            context.close()
            browser.close()
        after = get_items()
        report["after_items_sha256"] = digest(after)
        assert report["after_items_sha256"] == report["before_items_sha256"], "Saved results changed."
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        write_report()
    print(
        json.dumps({"status": report["status"], "scenarios": len(report["scenarios"]), "output": str(output)})
    )


if __name__ == "__main__":
    main()
