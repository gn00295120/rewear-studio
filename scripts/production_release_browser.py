"""Final GET-only smoke of the preserved production catalogue."""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from playwright.sync_api import expect, sync_playwright

POLICY = Path.home() / ".agents/skills/webapp-testing/scripts"
sys.path.insert(0, str(POLICY))
from network_policy import LocalNetworkPolicy, install_network_policy  # noqa: E402


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = args.url.rstrip("/")
    address = urlsplit(base)
    assert address.scheme == "http" and address.hostname == "127.0.0.1" and address.port == 8765
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "status": "running",
        "url": base,
        "scope": "GET-only production smoke; no upload, generation, shopping, edits or database writes",
        "page_errors": [],
        "console_errors": [],
        "blocked_mutations": [],
        "outside_requests": [],
        "failed_requests": [],
        "viewports": [],
    }

    def get(path):
        response = httpx.get(base + path, timeout=20)
        response.raise_for_status()
        return response

    def snapshot():
        return {path: get(path).json() for path in ("/api/items", "/api/jobs")}

    def write_report():
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    before = snapshot()
    report["before_api_sha256"] = {path: digest(body) for path, body in before.items()}
    report["before_combined_sha256"] = digest(before)
    items, jobs = before["/api/items"]["items"], before["/api/jobs"]["jobs"]
    assert len(items) == 14 and len(jobs) == 6
    assert not any(job["status"] in {"queued", "running"} for job in jobs)
    report["counts"] = {"items": len(items), "jobs": len(jobs)}
    report["served_sha256"] = {
        path: hashlib.sha256(get(path).content).hexdigest()
        for path in ("/", "/static/app.js", "/static/styles.css")
    }
    by_job = {job["id"]: job for job in jobs}
    item = next(
        item
        for item in items
        if by_job[item["job_id"]]["media_type"] == "image"
        and item.get("source_url")
        and item.get("cutout_url")
    )
    report["detail_item_id"] = item["id"]
    report["synthetic_comparison_available"] = any(item.get("reconstruction") for item in items)
    report["detail_scope"] = (
        "Actual original context, full source and segmentation; preserved data has no AI reconstruction to compare."
    )
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(service_workers="block")
            install_network_policy(context, LocalNetworkPolicy.for_http(base))

            def read_only(route):
                request = route.request
                if request.method != "GET":
                    report["blocked_mutations"].append({"method": request.method, "url": request.url})
                    route.abort("blockedbyclient")
                elif urlsplit(request.url).netloc != address.netloc:
                    report["outside_requests"].append(request.url)
                    route.abort("blockedbyclient")
                else:
                    route.fallback()

            context.route("**/*", read_only)
            page = context.new_page()
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
            page.on(
                "console",
                lambda message: (
                    report["console_errors"].append(message.text) if message.type == "error" else None
                ),
            )
            page.on(
                "requestfailed",
                lambda request: report["failed_requests"].append(
                    {"url": request.url, "failure": request.failure}
                ),
            )
            for width, height in ((1440, 1050), (390, 844)):
                page.set_viewport_size({"width": width, "height": height})
                response = page.goto(base, wait_until="networkidle")
                assert response.status == 200
                expect(page.locator("#total-count")).to_have_text("14")
                cards = page.locator(".garment-card")
                expect(cards).to_have_count(14)
                images = cards.locator(".garment-image img")
                expect(images).to_have_count(14)
                for image in images.all():
                    image.scroll_into_view_if_needed()
                    assert image.evaluate("img => img.decode().then(() => img.naturalWidth > 0)")
                    assert urlsplit(image.get_attribute("src")).netloc == address.netloc
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                page.evaluate("scrollTo({top: 0, behavior: 'instant'})")
                page.screenshot(path=output / f"home-{width}.png", animations="disabled")
                cards.first.scroll_into_view_if_needed()
                page.screenshot(path=output / f"catalogue-{width}.png", animations="disabled")

                page.locator("label.outfit-mode").click()
                expect(page.locator('input[name="mode"][value="outfit"]')).to_be_checked()
                expect(page.locator("#reconstruction-option")).to_be_visible()
                expect(page.locator("#upload-discovery-option")).to_be_visible()
                for selector in ("#upload-reconstruct", "#upload-discover"):
                    expect(page.locator(selector)).to_be_enabled()
                expect(page.locator("#upload-reconstruct")).to_be_checked()
                expect(page.locator("#upload-discover")).not_to_be_checked()
                expect(page.locator("#upload-discovery-market")).to_have_value("TW")
                expect(page.locator("#upload-discovery-market")).to_be_disabled()
                expect(page.locator("#upload-discovery-option")).to_contain_text(
                    "AI 預覽不作為品牌或商品比對依據"
                )
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                page.locator(".mode-fieldset").evaluate(
                    "el => el.scrollIntoView({block: 'start', behavior: 'instant'})"
                )
                page.screenshot(path=output / f"outfit-defaults-{width}.png", animations="disabled")

                page.locator(f'[data-item-id="{item["id"]}"]').click()
                dialog = page.locator("#item-dialog")
                expect(dialog).to_be_visible()
                shopping = dialog.locator(".free-shopping-handoff")
                expect(shopping).to_be_visible()
                expect(shopping).to_contain_text("US$0 模型費")
                expect(shopping).to_contain_text("Google 購物")
                expect(shopping).to_contain_text("Bing Shopping")
                expect(shopping).to_contain_text("Google 網頁搜尋")
                shopping_links = shopping.locator(".free-shopping-actions a")
                expect(shopping_links).to_have_count(3)
                assert all(
                    urlsplit(link.get_attribute("href")).hostname in {"www.google.com", "www.bing.com"}
                    for link in shopping_links.all()
                )
                expect(page.locator("#item-media-region")).to_have_attribute("data-selected", "context")
                for key, path in (
                    ("context", item["image_url"]),
                    ("source", item.get("original_url") or item["source_url"]),
                    ("cutout", item["cutout_url"]),
                ):
                    page.locator(f'#item-media-region [data-item-media-key="{key}"]').click()
                    expect(page.locator("#item-media-region")).to_have_attribute("data-selected", key)
                    image = page.locator("#item-media-content img")
                    expect(image).to_have_attribute("src", urljoin(base, path))
                    assert image.evaluate("img => img.decode().then(() => img.naturalWidth > 0)")
                    assert dialog.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
                    if key != "cutout":
                        page.screenshot(path=output / f"detail-{key}-{width}.png", animations="disabled")
                shopping.scroll_into_view_if_needed()
                page.screenshot(path=output / f"free-shopping-{width}.png", animations="disabled")
                expect(page.locator("#item-media-caption")).to_contain_text("不代表整件衣物")
                page.locator('[data-close="item-dialog"]').click()
                expect(dialog).not_to_be_visible()
                report["viewports"].append(
                    {
                        "width": width,
                        "height": height,
                        "catalogue_images_decoded": images.count(),
                        "outfit_reconstruction_default": page.locator("#upload-reconstruct").is_checked(),
                        "outfit_discovery_default": page.locator("#upload-discover").is_checked(),
                        "discovery_market_default": page.locator("#upload-discovery-market").input_value(),
                        "detail_media_decoded": ["context", "source", "cutout"],
                        "free_shopping_links": 3,
                        "overflow": False,
                        "status": "passed",
                    }
                )
                write_report()
            for key in (
                "page_errors",
                "console_errors",
                "blocked_mutations",
                "outside_requests",
                "failed_requests",
            ):
                assert not report[key], (key, report[key])
            context.close()
            browser.close()
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        after = snapshot()
        report["after_api_sha256"] = {path: digest(body) for path, body in after.items()}
        report["after_combined_sha256"] = digest(after)
        report["api_unchanged"] = after == before
        if not report["api_unchanged"]:
            report["status"] = "failed"
        write_report()
    assert report["api_unchanged"], "Production API data changed during this read-only check."
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
