"""Read-only browser verification of real quality-assessed catalogue results."""

import argparse
import csv
import io
import json
import sys
from pathlib import Path
from urllib.parse import urljoin

import httpx
from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--output", type=Path, default=Path("out/verification/quality-live-browser"))
    parser.add_argument(
        "--policy-dir", type=Path, default=Path.home() / ".agents/skills/webapp-testing/scripts"
    )
    args = parser.parse_args()
    sys.path.insert(0, str(args.policy_dir))
    from network_policy import LocalNetworkPolicy, install_network_policy

    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    items = httpx.get(args.url + "/api/items").json()["items"]
    retakes = [i for i in items if i.get("quality", {}).get("status") == "needs_retake" and not i["reviewed"]]
    ready = [i for i in items if i.get("quality", {}).get("status") == "ready" or i["reviewed"]]
    assert retakes and ready, "This check requires both real incomplete and complete garments."
    rows = list(
        csv.DictReader(
            io.StringIO(httpx.get(args.url + "/api/export?format=csv").content.decode("utf-8-sig"))
        )
    )
    assert sum(row["quality_status"] == "needs_retake" for row in rows) == len(retakes)
    assert all(
        row["category"] == "other" and not row["subtype"]
        for row in rows
        if row["quality_status"] == "needs_retake"
    )
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block")
        install_network_policy(context, LocalNetworkPolicy.for_http(args.url))
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(args.url, wait_until="networkidle")
        expect(page.locator("#usable-count")).to_have_text(str(len(ready)))
        expect(page.locator("#attention-count")).to_have_text(str(len(retakes)))
        expect(page.locator(".result-group.usable .garment-card")).to_have_count(len(ready))
        expect(page.locator(".result-group.retake .garment-card")).to_have_count(len(retakes))
        assert all(
            title in {"衣物未完整入鏡", "需補拍的衣物區域"}
            for title in page.locator(".result-group.retake .card-title").all_text_contents()
        )
        # Scroll real lazy-loaded cards into view and verify their decoded pixels
        # before capturing a full-page screenshot.
        for img in page.locator(".garment-card img").all():
            img.scroll_into_view_if_needed()
            assert img.evaluate("img => img.decode().then(() => img.naturalWidth > 0)")
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=output / "desktop.png", full_page=True, animations="disabled")
        item = retakes[0]
        page.locator(f'[data-item-id="{item["id"]}"]').click()
        expect(page.locator(".quality-assessment")).to_contain_text("需要補拍完整衣物")
        for key, field in (("context", "image_url"), ("source", "source_url"), ("cutout", "cutout_url")):
            page.locator(f'[data-item-media-key="{key}"]').click()
            expect(page.locator("#item-media-content img")).to_have_attribute(
                "src", urljoin(args.url, item[field])
            )
            assert page.locator("#item-media-content img").evaluate(
                "img => img.decode().then(() => img.naturalWidth > 0)"
            )
            assert page.locator("#item-dialog").evaluate("dialog => dialog.scrollTop") == 0
        page.locator('[data-item-media-key="context"]').click()
        page.screenshot(path=output / "retake-editor.png", animations="disabled")
        page.keyboard.press("Escape")
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator('[data-review="attention"]').click()
        expect(page.locator(".result-group.usable")).to_have_count(0)
        expect(page.locator(".result-group.retake .garment-card")).to_have_count(len(retakes))
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.locator(f'[data-item-id="{item["id"]}"]').click()
        expect(page.locator("#item-dialog")).to_be_visible()
        assert page.locator("#item-dialog").evaluate("e => e.scrollWidth <= e.clientWidth + 1")
        page.screenshot(path=output / "mobile-retake.png", animations="disabled")
        assert not errors, errors
        context.close()
        browser.close()
    report = {
        "status": "passed",
        "real_results": len(items),
        "usable": len(ready),
        "needs_retake": len(retakes),
        "checks": [
            "real catalogue grouping",
            "neutral retake names",
            "source/context/cutout tabs",
            "CSV suppresses uncertain guesses",
            "390px editor without overflow",
        ],
        "page_errors": errors,
        "mutations": "none",
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
