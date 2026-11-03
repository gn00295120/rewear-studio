"""Exercise image-search disclosure and history with explicit loopback fixtures.

No application runtime, database, Google upload, Meta call or paid API is used.
"""

import argparse
import copy
import hashlib
import io
import json
import mimetypes
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageDraw
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "wardrobe/static"
sys.path.insert(0, str(Path.home() / ".agents/skills/webapp-testing/scripts"))
from network_policy import LocalNetworkPolicy, install_network_policy  # noqa: E402

ITEM_ID = "a" * 32
JOB_ID = "b" * 32
IMAGE_QUERY = "原始衣物圖片 · Google Web Detection"
STAMP = "2026-10-22T05:00:00Z"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def health(enabled=False, configured=False, legacy=False):
    result = {
        "status": "ok",
        "local": {"available": True, "model": "Explicit UI fixture"},
        "meta": {"configured": True},
        "reconstruction": {"available": True},
        "discovery": {"available": True},
        "ffmpeg": True,
        "limits": {"upload_mb": 50, "batch_files": 32, "batch_upload_mb": 256},
    }
    if not legacy:
        result["discovery"]["image_search"] = {
            "enabled": enabled,
            "provider": "google_vision" if enabled else None,
            "configured": configured,
            "notice": '明確介面測試 <img src="https://invalid.example/fixture">；沒有連接外部服務。',
        }
    return result


def fixture_item(attempts=None):
    item = {
        "id": ITEM_ID,
        "job_id": JOB_ID,
        "created_at": STAMP,
        "updated_at": STAMP,
        "reviewed": False,
        "provenance": "Explicit UI fixture",
        "record": {
            "category": "top",
            "subtype": "明確介面測試上衣",
            "brand": None,
            "colors": ["黑色"],
            "pattern": "素色",
            "material_guess": None,
            "size_label": None,
            "condition": "unknown",
            "defects": [],
            "confidence": 0.9,
            "needs_review": True,
        },
        "quality": {"status": "ready", "visibility": "complete", "reasons": []},
        "image_url": f"/media/{JOB_ID}/crops/context-fixture.jpg",
        "cutout_url": f"/media/{JOB_ID}/crops/crop-fixture.jpg",
        "source_url": f"/media/{JOB_ID}/preview.jpg",
        "original_url": f"/media/{JOB_ID}/preview.jpg",
        "reconstruction": None,
        "discovery": None,
    }
    if attempts is not None:
        item["discovery"] = {
            "id": "c" * 32,
            "status": "completed",
            "stage": "completed",
            "market": "TW",
            "error": None,
            "created_at": STAMP,
            "updated_at": STAMP,
            "result": {
                "market": "TW",
                "identity": {
                    "status": "unknown",
                    "brand": None,
                    "evidence_type": "none",
                    "evidence": "明確介面測試：原圖沒有可讀品牌標記。",
                },
                "shopping": {
                    "version": 2,
                    "status": "no_results",
                    "query": "黑色短袖上衣 台灣",
                    "searched_at": STAMP,
                    "search_attempts": copy.deepcopy(attempts),
                    "offers": [],
                    "sources": [],
                    "checked_candidates": [
                        {
                            "title": "明確介面測試的未核對候選",
                            "url": "https://www.nike.com/tw/t/explicit-ui-fixture",
                            "status": "unverified",
                            "reasons": ["明確測試：尚未取得足夠商品頁證據。"],
                        }
                    ],
                    "notice": "搜尋來源仍需核對，不代表同款或可購買。",
                },
                "search_links": [],
                "source_urls": [],
            },
        }
    return item


def text_attempt(query="黑色短袖上衣 台灣", legacy=False):
    result = {
        "query": query,
        "status": "searched",
        "retrieved_count": 3,
        "candidate_count": 1,
        "error": None,
    }
    if not legacy:
        result["kind"] = "text"
    return result


def image_attempt(unavailable=False):
    return {
        "kind": "image",
        "query": IMAGE_QUERY,
        "status": "unavailable" if unavailable else "searched",
        "retrieved_count": 0 if unavailable else 4,
        "candidate_count": 0 if unavailable else 2,
        "error": "原圖搜尋尚待設定；圖片未傳送至 Google。" if unavailable else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    assert not (output / "report.json").exists(), "Use a fresh evidence directory."
    (output / "screens").mkdir(exist_ok=True)
    fixture = {"health": health(), "item": fixture_item()}
    report = {
        "status": "running",
        "scope": "Explicit loopback HTTP fixtures; no application runtime/database or external inference/search.",
        "mechanism": "Native Python ThreadingHTTPServer with Playwright LocalNetworkPolicy before new_page, service workers blocked. No supervisor sandbox receipt claimed.",
        "static_sha256": {name: sha(STATIC / name) for name in ("app.js", "index.html", "styles.css")},
        "script_sha256": sha(Path(__file__)),
        "scenarios": [],
        "page_errors": [],
        "console_errors": [],
        "outside_requests": [],
        "unexpected_mutations": [],
        "explicit_fixture_posts": [],
        "health_requests": [],
    }
    picture = Image.new("RGB", (300, 400), "#eee9de")
    draw = ImageDraw.Draw(picture)
    draw.rectangle((80, 75, 220, 310), fill="#364b40")
    draw.text((25, 350), "EXPLICIT UI FIXTURE", fill="#364b40")
    buffer = io.BytesIO()
    picture.save(buffer, "JPEG")
    image_bytes = buffer.getvalue()
    job = {
        "id": JOB_ID,
        "status": "completed",
        "stage": "completed",
        "progress": 100,
        "filename": "explicit-ui-fixture.jpg",
        "provider": "meta",
        "mode": "outfit",
        "media_type": "image",
        "item_count": 1,
        "usable_count": 1,
        "created_at": STAMP,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, body, code=200, content_type="application/json"):
            raw = (
                json.dumps(body, ensure_ascii=False).encode() if content_type == "application/json" else body
            )
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'",
            )
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/auth/session":
                return self.send({"mode": "local", "authenticated": False})
            if path == "/api/health":
                report["health_requests"].append(self.path)
                return self.send(copy.deepcopy(fixture["health"]))
            collections = {
                "/api/items": {"items": [fixture["item"]]},
                "/api/jobs": {"jobs": [job]},
                "/api/batches": {"batches": []},
                "/api/garment-groups": {"groups": []},
                "/api/duplicates": {"suggestions": []},
            }
            if path in collections:
                return self.send(copy.deepcopy(collections[path]))
            if path == f"/api/items/{ITEM_ID}":
                return self.send(copy.deepcopy(fixture["item"]))
            if path in (
                fixture["item"]["image_url"],
                fixture["item"]["cutout_url"],
                fixture["item"]["source_url"],
            ):
                return self.send(image_bytes, content_type="image/jpeg")
            relative = "index.html" if path == "/" else path.removeprefix("/static/")
            target = (STATIC / relative).resolve()
            if target.is_relative_to(STATIC) and target.is_file():
                return self.send(
                    target.read_bytes(),
                    content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
                )
            return self.send({"detail": "Unknown fixture route"}, 404)

        def do_POST(self):
            if self.path != f"/api/items/{ITEM_ID}/discovery":
                report["unexpected_mutations"].append({"method": "POST", "path": self.path})
                return self.send({"detail": "Fixture route not authorized"}, 405)
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            report["explicit_fixture_posts"].append({"path": self.path, "body": body})
            fixture["item"]["discovery"] = {
                "id": "c" * 32,
                "status": "queued",
                "stage": "queued",
                "market": body["market"],
                "created_at": STAMP,
                "updated_at": STAMP,
                "result": None,
            }
            return self.send(copy.deepcopy(fixture["item"]), 202)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    policies = []

    def record(name, **details):
        report["scenarios"].append({"name": name, "status": "passed", **details})
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)

            def open_page(availability, value=None, width=1440):
                fixture.update(
                    health=copy.deepcopy(availability), item=copy.deepcopy(value or fixture_item())
                )
                context = browser.new_context(
                    viewport={"width": width, "height": 1050 if width > 600 else 844},
                    service_workers="block",
                )
                policy = LocalNetworkPolicy.for_http(base)
                policies.append(policy)
                install_network_policy(context, policy)

                def guard(route):
                    request = route.request
                    parsed = urlsplit(request.url)
                    if parsed.netloc != urlsplit(base).netloc:
                        report["outside_requests"].append(request.url)
                        return route.abort("blockedbyclient")
                    if request.method != "GET" and not (
                        request.method == "POST" and parsed.path == f"/api/items/{ITEM_ID}/discovery"
                    ):
                        report["unexpected_mutations"].append({"method": request.method, "url": request.url})
                        return route.abort("blockedbyclient")
                    route.fallback()

                context.route("**/*", guard)
                page = context.new_page()
                page.set_default_timeout(12000)
                page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
                page.on(
                    "console",
                    lambda message: (
                        report["console_errors"].append(message.text) if message.type == "error" else None
                    ),
                )
                page.goto(base, wait_until="networkidle")
                policy.require_allowed(page.url)
                expect(page.locator("#health-summary")).to_have_text("本機模型就緒")
                expect(page.locator(f'[data-item-id="{ITEM_ID}"]')).to_have_count(1)
                page.locator(f'[data-item-id="{ITEM_ID}"]').scroll_into_view_if_needed()
                page.locator(f'[data-item-id="{ITEM_ID}"] img').evaluate_all(
                    "async imgs => { await Promise.all(imgs.map(img => img.decode())); }"
                )
                page.evaluate("window.scrollTo({top:0,behavior:'instant'})")
                return context, page

            def check_layout(page):
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                assert page.locator("dialog[open]").evaluate_all(
                    "els => els.every(el => el.scrollWidth <= el.clientWidth + 1)"
                )

            def screenshot(page, name, selector=None):
                page.locator("dialog[open]").evaluate_all(
                    "async els => { await Promise.all(els.flatMap(el => el.getAnimations()).map(animation => animation.finished)); }"
                )
                if selector:
                    page.locator(selector).scroll_into_view_if_needed()
                page.locator("img:visible").evaluate_all(
                    "async imgs => { await Promise.all(imgs.map(img => img.decode())); }"
                )
                check_layout(page)
                page.screenshot(path=output / "screens" / f"{name}.png")

            def open_item(page):
                page.locator(f'[data-item-id="{ITEM_ID}"]').click()
                expect(page.locator("#item-dialog")).to_be_visible()

            for name, enabled, configured, width, label, phrase in (
                ("disabled-even-with-settings", False, True, 1440, "未啟用", "Google 原圖檢索未啟用"),
                (
                    "enabled-awaiting-settings",
                    True,
                    False,
                    1440,
                    "尚待設定",
                    "尚待設定，本次不會傳送圖片至 Google",
                ),
                (
                    "configured-desktop-disclosure",
                    True,
                    True,
                    1440,
                    "已設定（未驗證）",
                    "會將原始衣物圖片傳送至 Google",
                ),
                (
                    "configured-mobile-disclosure",
                    True,
                    True,
                    390,
                    "已設定（未驗證）",
                    "會將原始衣物圖片傳送至 Google",
                ),
            ):
                context, page = open_page(health(enabled, configured), width=width)
                page.locator('input[name="mode"][value="outfit"]').check()
                expect(page.locator("#upload-discovery-description")).to_contain_text(phrase)
                expect(page.locator("#upload-discover")).not_to_be_checked()
                expect(page.locator("#upload-discovery-market")).to_be_disabled()
                expect(page.locator("#upload-discovery-market")).to_have_value("TW")
                expect(page.locator("#upload-discovery-description")).to_contain_text("AI 預覽不作為")
                if configured and enabled:
                    expect(page.locator("#upload-discovery-description")).to_contain_text(
                        "不代表權限或搜尋結果已驗證"
                    )
                screenshot(page, f"{name}-upload", "#upload-discovery-option")
                page.locator("#health-button").click()
                expect(page.locator("#image-search-health .health-value")).to_have_text(label)
                expect(page.locator("#image-search-health")).to_contain_text(phrase)
                expect(page.locator("#image-search-health img")).to_have_count(0)
                screenshot(page, f"{name}-health", "#image-search-health")
                page.locator('[data-close="status-dialog"]').click()
                open_item(page)
                expect(page.locator(".discovery-explanation")).to_contain_text(phrase)
                expect(page.locator('[data-discovery-action="start"]')).to_be_enabled()
                screenshot(page, f"{name}-detail", "#item-discovery")
                record(name, width=width, image_search_enabled=enabled, configured=configured)
                context.close()

            context, page = open_page(health(legacy=True), fixture_item([text_attempt(legacy=True)]))
            open_item(page)
            expect(page.locator(".discovery-explanation")).to_contain_text("原圖檢索未啟用")
            expect(page.locator(".shopping-attempts li[data-search-kind=text]")).to_have_count(1)
            expect(page.locator(".shopping-attempts")).to_contain_text("搜尋詞：黑色短袖上衣 台灣")
            expect(page.locator(".shopping-attempts > strong")).to_have_text("已查找 1 次")
            expect(page.locator(".shopping-attempts li[data-search-kind=image]")).to_have_count(0)
            screenshot(page, "legacy-text-history", ".shopping-attempts")
            record("legacy-health-and-attempt-kind-remain-text", width=1440)
            context.close()

            mixed = [image_attempt(), text_attempt(), text_attempt("黑色上衣 購買")]
            context, page = open_page(health(True, True), fixture_item(mixed), width=390)
            open_item(page)
            expect(page.locator(".shopping-attempts li")).to_have_count(3)
            expect(page.locator(".shopping-attempts li[data-search-kind=image] > strong")).to_have_text(
                "原圖搜尋 · Google Web Detection"
            )
            expect(page.locator(".shopping-attempts li[data-search-kind=image]")).not_to_contain_text(
                "搜尋詞"
            )
            expect(page.locator(".shopping-attempts li[data-search-kind=text]")).to_have_count(2)
            expect(page.locator(".shopping-attempts > strong")).to_have_text(
                "已查找 3 次 · 原圖 1 次、文字 2 次"
            )
            expect(page.locator(".shopping-attempts")).to_contain_text("4 筆網頁來源 · 選出 2 個待核對候選")
            expect(page.locator(".shopping-attempts")).to_contain_text("不代表已確認可購買")
            expect(page.locator(".shopping-offers")).to_have_count(0)
            expect(page.locator(".shopping-checks")).to_have_attribute("open", "")
            expect(page.locator(".shopping-checks")).to_contain_text("尚未完成核對")
            screenshot(page, "mixed-image-text-history-390", ".shopping-attempts")
            record("one-image-two-text-attempts-visible-and-unverified", width=390, attempts=3)
            context.close()

            context, page = open_page(health(True, False), width=390)
            open_item(page)
            page.locator("#item-subtype").fill("未儲存草稿不可被搜尋輪詢覆蓋")
            page.locator("#discovery-market").select_option("global")
            start = page.locator('[data-discovery-action="start"]')
            start.focus()
            start.press("Enter")
            expect(page.locator('[data-discovery-action="cancel"]')).to_be_visible()
            expect(page.locator(".discovery-status")).to_contain_text("等待開始查找")
            assert report["explicit_fixture_posts"] == [
                {"path": f"/api/items/{ITEM_ID}/discovery", "body": {"market": "global"}}
            ]
            failed_image = [image_attempt(True), text_attempt(), text_attempt("黑色上衣 購買")]
            terminal = fixture_item(failed_image)
            terminal["discovery"]["market"] = "global"
            terminal["discovery"]["result"]["market"] = "global"
            fixture["item"] = terminal
            expect(page.locator(".shopping-attempts li[data-search-kind=image]")).to_have_count(1)
            expect(page.locator(".shopping-attempts")).to_contain_text("圖片未傳送至 Google")
            expect(page.locator(".shopping-attempts > strong")).to_have_text(
                "已嘗試查找 3 次（1 次未完成） · 原圖 1 次、文字 2 次"
            )
            expect(page.locator("#item-subtype")).to_have_value("未儲存草稿不可被搜尋輪詢覆蓋")
            expect(page.locator('[data-discovery-action="start"]')).to_be_enabled()
            expect(page.locator("#discovery-market")).to_have_value("global")
            screenshot(page, "unconfigured-image-falls-back-to-text-390", ".shopping-attempts")
            record(
                "missing-image-config-allows-text-with-draft-preserved", width=390, explicit_fixture_post=1
            )
            context.close()

            context, page = open_page(health(False, False), width=390)
            page.locator('input[name="mode"][value="outfit"]').check()
            expect(page.locator("#upload-discover")).not_to_be_checked()
            page.locator("#upload-discover").focus()
            page.locator("#upload-discover").press("Space")
            expect(page.locator("#upload-discover")).to_be_checked()
            expect(page.locator("#upload-discovery-market")).to_be_enabled()
            page.locator("#upload-discover").press("Space")
            expect(page.locator("#upload-discover")).not_to_be_checked()
            expect(page.locator("#upload-discovery-market")).to_be_disabled()
            page.locator("#health-button").click()
            fixture["health"] = health(True, True)
            page.locator("#health-refresh").click()
            expect(page.locator("#image-search-health .health-value")).to_have_text("已設定（未驗證）")
            assert "/api/health?refresh=true" in report["health_requests"]
            page.locator('[data-close="status-dialog"]').click()
            expect(page.locator("#upload-discover")).not_to_be_checked()
            expect(page.locator("#upload-discovery-description")).to_contain_text(
                "會將原始衣物圖片傳送至 Google"
            )
            screenshot(page, "settings-refresh-preserves-opt-out-390", "#upload-discovery-option")
            open_item(page)
            expect(page.locator(".discovery-explanation")).to_contain_text("會將原始衣物圖片傳送至 Google")
            record("refresh-shows-new-service-without-reenabling-upload-opt-out", width=390)
            context.close()

            report["network_policy_blocked_requests"] = sum(policy.blocked_requests for policy in policies)
            assert report["network_policy_blocked_requests"] == 0
            assert not report["page_errors"], report["page_errors"]
            assert not report["console_errors"], report["console_errors"]
            assert not report["outside_requests"], report["outside_requests"]
            assert not report["unexpected_mutations"], report["unexpected_mutations"]
            assert len(report["explicit_fixture_posts"]) == 1
            browser.close()
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        report["server_closed"] = not thread.is_alive()
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(
            json.dumps(
                {"status": report["status"], "scenarios": len(report["scenarios"]), "output": str(output)}
            )
        )


if __name__ == "__main__":
    main()
