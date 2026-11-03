"""Isolated UI fixtures for previews and purchasing; no app DB or model calls."""

import argparse
import copy
import io
import json
import mimetypes
import sys
import threading
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageDraw
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_preview_ui_contract import fixture_item, preview_report, shopping_result  # noqa: E402
from test_shopping_ui_contract import empty_shopping_result  # noqa: E402

POLICY = Path.home() / ".agents/skills/webapp-testing/scripts"
sys.path.insert(0, str(POLICY))
from network_policy import LocalNetworkPolicy, install_network_policy  # noqa: E402


def image_bytes(kind):
    image = Image.new("RGB", (420, 540), "#ede8dd")
    draw = ImageDraw.Draw(image)
    if kind == "source":
        draw.ellipse((165, 32, 252, 122), fill="#c49b7c")
        draw.rectangle((135, 124, 285, 356), fill="#33596b")
        draw.rectangle((148, 356, 273, 500), fill="#303b33")
    else:
        draw.polygon(
            [
                (150, 105),
                (73, 140),
                (35, 267),
                (104, 292),
                (135, 225),
                (135, 433),
                (286, 433),
                (286, 225),
                (316, 292),
                (385, 267),
                (348, 140),
                (270, 105),
            ],
            fill="#33596b",
        )
        draw.arc((177, 81, 242, 137), 0, 180, fill="#ede8dd", width=9)
    draw.text((24, 516), "EXPLICIT UI FIXTURE - NO MODEL", fill="#344334")
    encoded = io.BytesIO()
    image.save(encoded, "PNG")
    return encoded.getvalue()


def fixtures():
    items = [
        fixture_item("side-preview", preview_report("side", tucked=True)),
        fixture_item("mismatch-preview", preview_report(mismatch=True)),
        fixture_item("strict-image", preview_report()),
        fixture_item("lookup-none"),
        fixture_item("lookup-pending"),
        fixture_item("lookup-failed"),
        fixture_item("lookup-results", preview_report(tucked=True)),
        fixture_item("lookup-empty"),
    ]
    by_id = {item["id"]: item for item in items}
    by_id["lookup-pending"]["discovery"] = {
        "id": "pending",
        "status": "running",
        "stage": "verifying",
        "market": "TW",
    }
    by_id["lookup-failed"]["discovery"] = {
        "id": "failed",
        "status": "failed",
        "market": "TW",
        "error": "明確測試：查找服務暫時無法連線。",
    }
    by_id["lookup-results"]["discovery"] = {
        "id": "found",
        "status": "completed",
        "market": "TW",
        "result": shopping_result(),
    }
    empty = empty_shopping_result()
    by_id["lookup-empty"]["discovery"] = {
        "id": "empty",
        "status": "completed",
        "market": "TW",
        "result": empty,
    }
    return by_id


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("out/verification/preview-shopping-20261022/ui"))
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    items = fixtures()
    requests = []
    uploads = []
    batches = []
    source_png, preview_png = image_bytes("source"), image_bytes("preview")
    job = {
        "id": "fixture-job",
        "status": "completed",
        "mode": "outfit",
        "provider": "meta",
        "filename": "明確介面測試照片.jpg",
        "media_type": "image",
        "created_at": "2026-10-22T08:00:00Z",
        "progress": 100,
        "stage": "分析完成",
        "item_count": len(items),
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, body, code=200, content_type="application/json"):
            payload = (
                json.dumps(body, ensure_ascii=False).encode() if content_type == "application/json" else body
            )
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/auth/session":
                return self.send({"mode": "local", "authenticated": False})
            if path == "/api/health":
                return self.send(
                    {
                        "local": {"available": True, "model": "explicit fixture"},
                        "meta": {"configured": True},
                        "reconstruction": {"available": True},
                        "discovery": {"available": True},
                        "ffmpeg": True,
                        "limits": {
                            "upload_mb": 50,
                            "batch_files": 32,
                            "batch_upload_mb": 256,
                            "max_items": 24,
                            "video_seconds": 30,
                        },
                    }
                )
            if path == "/api/items":
                return self.send({"items": list(copy.deepcopy(items).values())})
            if path == "/api/jobs":
                return self.send({"jobs": [job]})
            if path == "/api/batches":
                return self.send({"batches": batches})
            if path == "/api/garment-groups":
                return self.send({"groups": []})
            if path == "/api/duplicates":
                return self.send({"suggestions": []})
            if path.startswith("/media/fixture-job/"):
                return self.send(
                    preview_png if "/reconstructions/" in path or "/shopping/" in path else source_png,
                    content_type="image/png",
                )
            relative = "index.html" if path == "/" else path.removeprefix("/static/")
            target = (ROOT / "wardrobe/static" / relative).resolve()
            if target.is_relative_to(ROOT / "wardrobe/static") and target.is_file():
                return self.send(
                    target.read_bytes(),
                    content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
                )
            return self.send({"detail": "Explicit fixture route unavailable"}, 404)

        def do_POST(self):
            path = urlsplit(self.path).path
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if path == "/api/batches":
                message = BytesParser(policy=default).parsebytes(
                    ("Content-Type: " + self.headers["Content-Type"] + "\r\n\r\n").encode() + raw
                )
                fields = {
                    part.get_param("name", header="content-disposition"): part.get_payload(
                        decode=True
                    ).decode()
                    for part in message.iter_parts()
                    if part.get_filename() is None
                }
                uploads.append(fields)
                if len(uploads) <= 2:
                    return self.send({"detail": "明確測試：暫時無法上傳，請重試。"}, 503)
                batch = {
                    "id": f"fixture-batch-{len(uploads)}",
                    "status": "completed",
                    "file_count": 1,
                    "job_count": 1,
                    "jobs": [],
                    "errors": [],
                    "progress": 100,
                    "counts": {"completed": 1},
                    "created_at": "2026-10-22T08:00:00Z",
                }
                batches.append(batch)
                return self.send(batch, 202)
            parts = path.strip("/").split("/")
            if len(parts) >= 4 and parts[:2] == ["api", "items"] and parts[3] == "discovery":
                body = json.loads(raw) if raw else {}
                requests.append({"path": path, "body": body})
                item = items[parts[2]]
                old = item.get("discovery") or {}
                cancel = len(parts) == 5 and parts[4] == "cancel"
                item["discovery"] = {
                    **old,
                    "id": "fixture-current",
                    "status": "cancelled" if cancel else "queued",
                    "stage": "identifying",
                    "market": body.get("market", old.get("market", "TW")),
                }
                return self.send(copy.deepcopy(item), 200 if cancel else 202)
            return self.send({"detail": "Explicit fixture action unavailable"}, 404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    report = {
        "status": "running",
        "scope": "Actual static app with in-process loopback fixture API and explicit test images; no production API/database or model calls",
        "scenarios": [],
        "page_errors": [],
        "viewports": [1440, 390],
    }
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1050}, service_workers="block")
            install_network_policy(context, LocalNetworkPolicy.for_http(origin))
            page = context.new_page()
            page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
            page.set_default_timeout(12000)
            expect.set_options(timeout=12000)
            page.goto(origin, wait_until="networkidle")
            expect(page.locator(".garment-card")).to_have_count(len(items))

            def check(name, fn):
                result = {"name": name}
                try:
                    result.update(status="passed", details=fn() or {})
                except Exception as error:
                    result.update(status="failed", error=str(error))
                    page.screenshot(path=output / "failure.png", full_page=True, animations="disabled")
                    raise
                finally:
                    report["scenarios"].append(result)
                    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
                    print(json.dumps(result, ensure_ascii=False), flush=True)

            def close():
                page.locator("#item-dialog > .dialog-close").click()

            def card(item_id):
                return page.locator(f'.garment-card:has([data-item-id="{item_id}"])')

            def previews():
                expect(card("side-preview").locator(".card-reconstruction")).to_have_text("AI 預覽・含推測")
                expect(card("strict-image").locator(".card-reconstruction")).to_have_text(
                    "AI 展示圖 · 已比對"
                )
                expect(card("mismatch-preview").locator(".card-reconstruction-notice")).to_contain_text(
                    "字樣"
                )
                page.locator('[data-item-id="side-preview"]').click()
                expect(page.locator("#item-media-region")).to_have_attribute("data-selected", "preview")
                expect(page.locator("#item-media-content figure")).to_have_count(2)
                expect(page.locator(".preview-inferences")).to_contain_text("下襬")
                expect(page.locator("#item-media-caption")).to_contain_text("正面由 AI 推測")
                page.screenshot(path=output / "side-preview-1440.png", animations="disabled")
                close()
                page.locator('[data-item-id="mismatch-preview"]').click()
                expect(page.locator(".preview-mismatches")).to_contain_text("字樣的位置")
                expect(page.locator("#item-media-region")).to_have_attribute("data-selected", "preview")
                close()

            check("ready previews remain distinct from strict fidelity and show mismatches", previews)

            def shopping_cards():
                expect(card("lookup-none").locator(".card-shopping")).to_contain_text("尚未查找")
                expect(card("lookup-pending").locator(".card-shopping")).to_contain_text("正在核對")
                expect(card("lookup-failed").locator(".card-shopping")).to_contain_text("可重試")
                expect(card("lookup-results").locator(".card-shopping")).to_contain_text("1 個相似款")
                expect(card("lookup-empty").locator(".card-shopping")).to_contain_text("已查找 2 次")
                page.locator('[data-open-discovery="lookup-results"]').click()
                expect(page.locator(".brand-finding h4")).to_have_text("無法確認品牌")
                expect(page.locator(".offer-match")).to_have_text("相似款")
                expect(page.locator(".offer-availability")).to_contain_text("庫存未確認")
                assert "/reconstructions/" not in page.locator(".discovery-image-evidence img").get_attribute(
                    "src"
                )
                assert page.locator(".shopping-offers a").first.get_attribute("rel") == "noopener noreferrer"
                page.screenshot(path=output / "shopping-1440.png", animations="disabled")
                close()

            check("visible purchasing states, unknown brand and original-only evidence", shopping_cards)

            def search_telemetry():
                for width, height in ((1440, 1050), (390, 844)):
                    page.set_viewport_size({"width": width, "height": height})
                    page.locator('[data-open-discovery="lookup-empty"]').click()
                    expect(page.locator(".shopping-attempts")).to_contain_text("已查找 2 次")
                    expect(page.locator(".shopping-attempts li")).to_have_count(2)
                    checked = page.locator(".shopping-checks")
                    assert checked.evaluate("el => el.open")
                    expect(checked.locator("a")).to_have_count(3)
                    expect(checked.locator(".candidate-status")).to_have_text(
                        ["已排除", "尚未完成核對", "尚未完成核對"]
                    )
                    expect(checked).to_contain_text("商品頁回傳 403")
                    expect(checked).to_contain_text("需要執行 JavaScript")
                    expect(page.locator(".shopping-offers")).to_have_count(0)
                    for link in checked.locator("a").all():
                        assert link.get_attribute("rel") == "noopener noreferrer"
                        assert link.get_attribute("target") == "_blank"
                    assert page.locator("#item-dialog").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
                    page.locator(".shopping-attempts").scroll_into_view_if_needed()
                    page.screenshot(path=output / f"search-telemetry-{width}.png", animations="disabled")
                    checked.screenshot(
                        path=output / f"unverified-candidates-{width}.png", animations="disabled"
                    )
                    close()
                page.set_viewport_size({"width": 1440, "height": 1050})

            check("actual search telemetry and unverified candidate links stay visible", search_telemetry)

            def manual_lookup():
                page.locator('[data-open-discovery="lookup-none"]').click()
                expect(page.locator(".discovery-status")).to_contain_text("等待開始查找")
                assert requests[-1]["body"] == {"market": "TW"}
                page.locator("#item-subtype").fill("保留這份尚未儲存的草稿")
                items["lookup-none"]["discovery"].update(status="running", stage="verifying")
                expect(page.locator(".discovery-status")).to_contain_text("核對商品頁")
                expect(page.locator("#item-subtype")).to_have_value("保留這份尚未儲存的草稿")
                page.locator('[data-discovery-action="cancel"]').click()
                expect(page.locator(".discovery-status")).to_contain_text("已取消")
                page.locator("#discovery-market").select_option("global")
                page.locator('[data-discovery-action="start"]').click()
                expect(page.locator(".discovery-status")).to_contain_text("等待開始")
                assert requests[-1]["body"] == {"market": "global"}
                close()

            check(
                "card starts lookup, progress preserves draft, cancel and retry use selected market",
                manual_lookup,
            )

            def upload_options():
                page.locator('input[name="mode"][value="outfit"]').check()
                expect(page.locator("#upload-reconstruct")).to_be_checked()
                expect(page.locator("#upload-discover")).not_to_be_checked()
                expect(page.locator("#upload-discovery-market")).to_be_disabled()
                expect(page.locator("#upload-discovery-market")).to_have_value("TW")
                page.locator("#upload-discover").check()
                page.locator("#upload-discovery-market").select_option("global")
                page.locator("#file-input").set_input_files(
                    {"name": "explicit-fixture.jpg", "mimeType": "image/jpeg", "buffer": source_png}
                )
                for _ in range(2):
                    page.locator("#upload-button").click()
                    expect(page.locator("#upload-error")).to_contain_text("明確測試")
                    expect(page.locator("#upload-button")).to_be_enabled()
                assert uploads[0]["discover"] == "true" and uploads[0]["discovery_market"] == "global"
                assert uploads[0]["client_id"] == uploads[1]["client_id"]
                page.locator("#upload-discover").uncheck()
                expect(page.locator("#upload-discovery-market")).to_be_disabled()
                page.locator("#upload-button").click()
                expect(page.locator("#file-queue")).to_be_hidden()
                expect(page.locator("#upload-button")).to_be_enabled()
                assert (
                    uploads[2]["discover"] == "false" and uploads[2]["client_id"] != uploads[1]["client_id"]
                )
                page.locator("#upload-discover").check()
                page.locator("#upload-discovery-market").select_option("TW")
                page.locator("#file-input").set_input_files(
                    {"name": "explicit-second.jpg", "mimeType": "image/jpeg", "buffer": source_png}
                )
                page.locator("#upload-button").click()
                expect(page.locator("#file-queue")).to_be_hidden()
                assert uploads[3]["discover"] == "true" and uploads[3]["discovery_market"] == "TW"
                return {"requests": len(uploads), "same_retry_key": True, "changed_option_gets_new_key": True}

            check("outfit automatic shopping options and idempotent upload retry", upload_options)

            def mobile():
                expect(page.locator("#upload-button")).to_be_enabled()
                page.locator('[data-view="wardrobe"]').click()
                page.set_viewport_size({"width": 390, "height": 844})
                page.locator('[data-item-id="side-preview"]').click()
                expect(page.locator("#item-media-content figure")).to_have_count(2)
                assert page.locator("#item-dialog").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
                page.locator('[data-item-media-key="context"]').first.focus()
                page.keyboard.press("Enter")
                expect(page.locator("#item-media-region")).to_have_attribute("data-selected", "context")
                page.locator('[data-item-media-key="preview"]').first.focus()
                page.keyboard.press("Enter")
                expect(page.locator("#item-media-region")).to_have_attribute("data-selected", "preview")
                page.locator("#item-dialog").evaluate("el => el.scrollTop = 0")
                page.screenshot(path=output / "side-preview-390.png", animations="disabled")
                close()
                page.locator('[data-open-discovery="lookup-results"]').click()
                expect(page.locator(".offer-match")).to_have_text("相似款")
                assert page.locator("#item-dialog").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
                page.screenshot(path=output / "shopping-390.png", animations="disabled")
                close()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                for selector in (".card-shopping p", ".card-shopping .text-button", ".card-reconstruction"):
                    assert page.locator(selector).first.evaluate(
                        "el => parseFloat(getComputedStyle(el).fontSize) >= 11"
                    )
                page.locator("#items-region").scroll_into_view_if_needed()
                page.screenshot(path=output / "catalogue-390.png", animations="disabled")

            check("390px preview and purchasing remain readable and keyboard operable", mobile)
            assert report["page_errors"] == []
            report.update(status="passed", upload_fields=uploads, discovery_requests=requests)
            context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        report["fixture_server_closed"] = not thread.is_alive()
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {"status": report["status"], "scenarios": len(report["scenarios"]), "output": str(output)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
