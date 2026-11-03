"""GET-only verification of the saved-result gallery and its public allowlist."""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from playwright.sync_api import expect, sync_playwright

POLICY = Path.home() / ".agents/skills/webapp-testing/scripts"
sys.path.insert(0, str(POLICY))
from network_policy import LocalNetworkPolicy, install_network_policy  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = args.url.rstrip("/")
    assert urlsplit(base).hostname in {"localhost", "127.0.0.1"}
    assert urlsplit(base).port == 8777, "Only the explicitly authorized evidence gallery is in scope."
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "status": "running",
        "url": base,
        "scope": "Saved evidence gallery, GET-only; no external links, model calls, search calls or source-data writes",
        "page_errors": [],
        "blocked_mutations": [],
        "viewports": [],
        "private_paths": [],
    }

    def fetch(path):
        response = httpx.get(base + path, timeout=20)
        response.raise_for_status()
        return response

    def snapshot():
        return {
            path: hashlib.sha256(fetch(path).content).hexdigest() for path in ("/", "/gallery-notes.json")
        }

    def write_report():
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))

    try:
        report["before_sha256"] = snapshot()
        notes = fetch("/gallery-notes.json").json()
        assert notes["requested"] == 10 and len(notes["cases"]) == 10
        assert notes["preview_ready"] == 10 and notes["manual_visible_style_pass"] == 6
        repair = notes["detail_repair"]
        assert repair["requested"] == 4 and len(repair["cases"]) == 4
        assert repair["visible_style_pass"] == 1 and repair["selected_failure_regression"] is True
        assert notes["target_met"] is False and notes["target_visible_fidelity"] == 0.9
        report["saved_counts"] = {
            "baseline": 10,
            "complete_preview": 10,
            "baseline_visible_fidelity": 6,
            "selected_repair_denominator": 4,
            "selected_repair_visible_fidelity": 1,
            "target_met": False,
            "built_at": notes["built_at"],
        }
        for path in (
            "/original-before.sqlite3",
            "/review-data/wardrobe.sqlite3",
            "/%2e%2e/README.md",
            "/make_gallery.py",
            "/.env",
            "/shopping-gallery.json",
            "/detail-repair-1/report.json",
            "/review-data-provenance.json",
        ):
            response = httpx.get(base + path, timeout=20)
            report["private_paths"].append({"path": path, "status": response.status_code})
            assert response.status_code == 404, f"Private path should not be public: {path}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(service_workers="block")
            install_network_policy(context, LocalNetworkPolicy.for_http(base))

            def read_only(route):
                if route.request.method != "GET":
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
                response = page.goto(base, wait_until="networkidle")
                assert response.status == 200
                assert hashlib.sha256(response.body()).hexdigest() == report["before_sha256"]["/"]
                expect(page.locator("#cases > article")).to_have_count(10)
                expect(page.locator("#detail-repair > article")).to_have_count(4)
                expect(page.locator("#shopping > article")).to_have_count(4)
                expect(page.locator(".metrics strong")).to_have_text(["10 / 10", "6 / 10"])
                expect(page.locator("header")).to_contain_text("目前尚未達標")
                expect(page.locator("#detail-repair > p")).to_contain_text("目視符合 1 / 4")
                expect(page.locator("#detail-repair > p")).to_contain_text("首輪 6 / 10 保留")
                expect(page.locator("#shopping > p").first).to_contain_text("通過商品核對 0 / 3")
                expect(page.locator("#shopping > p").first).to_contain_text("沒有算進自動搜尋成功數")
                expect(page.locator("#shopping > p").nth(1)).to_contain_text("追加 2 次真搜尋")
                expect(page.locator("#shopping > p").nth(1)).to_contain_text("排除全黑水洗外套")
                for article in page.locator("#detail-repair > article").all():
                    expect(article.locator(".repair-pair figcaption")).to_have_text(
                        ["相同原圖", "第一輪", "細節修正後重測"]
                    )
                images = page.locator("img")
                expect(images).to_have_count(37)
                for image in images.all():
                    image.scroll_into_view_if_needed()
                    assert image.evaluate("img => img.decode().then(() => img.naturalWidth > 0)")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                page.evaluate("scrollTo({top: 0, behavior: 'instant'})")
                page.screenshot(path=output / f"overview-{width}.png", animations="disabled")

                floral = page.locator("#detail-repair > article").filter(has_text="泡泡袖")
                expect(floral).to_have_count(1)
                expect(floral).to_contain_text("這次退步")
                expect(floral).to_contain_text("第一輪的直袖口結果仍保留")
                floral.screenshot(path=output / f"repair-floral-{width}.png", animations="disabled")
                black = page.locator("#detail-repair > article").filter(has_text="袖章也移到相反邊")
                expect(black).to_have_count(1)
                expect(black).to_contain_text("仍不通過")
                black.screenshot(path=output / f"repair-black-{width}.png", animations="disabled")
                fallback = page.locator("#shopping > article").filter(has_text="側拍白色短袖")
                expect(fallback.locator("details")).to_have_count(1)
                assert fallback.locator("details").evaluate("el => el.open")
                expect(fallback).to_contain_text("完成兩次實際網路搜尋")
                expect(fallback).to_contain_text("尚未核對為可購買相似款")
                expect(fallback.locator(".product")).to_have_count(0)
                fallback.screenshot(path=output / f"shopping-fallback-{width}.png", animations="disabled")
                manual = page.locator("#shopping > article").filter(has_text="UNIQLO")
                expect(manual).to_have_count(1)
                expect(manual.locator(".badge")).to_have_text("人工搜尋・近似款")
                expect(manual).to_contain_text("沒有灌入應用程式的自動搜尋結果")
                expect(manual).to_contain_text("無法確認同款")
                expect(manual.locator(".product a[href^='https://www.uniqlo.com/']")).to_have_count(1)
                manual.screenshot(path=output / f"shopping-manual-near-{width}.png", animations="disabled")
                report["viewports"].append(
                    {
                        "width": width,
                        "height": height,
                        "baseline_articles": 10,
                        "repair_articles": 4,
                        "shopping_articles": 4,
                        "decoded_images": images.count(),
                        "overflow": False,
                        "status": "passed",
                    }
                )
                write_report()
            assert not report["page_errors"], report["page_errors"]
            assert not report["blocked_mutations"], report["blocked_mutations"]
            context.close()
            browser.close()
        report["after_sha256"] = snapshot()
        assert report["after_sha256"] == report["before_sha256"], (
            "Gallery changed during verification; preserve this version and rerun."
        )
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        write_report()
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
