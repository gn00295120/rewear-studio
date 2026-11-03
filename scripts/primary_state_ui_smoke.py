"""GET-only static UI fixtures for primary front-state fidelity and preview labels."""

import argparse
import copy
import hashlib
import json
import mimetypes
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(Path.home() / ".agents/skills/webapp-testing/scripts"))
from network_policy import LocalNetworkPolicy, install_network_policy  # noqa: E402
from preview_browser_smoke import image_bytes  # noqa: E402
from test_preview_ui_contract import fixture_item  # noqa: E402
from test_primary_state_ui_contract import FRONT_REASON, OTHER_REASON, primary_report, set_candidate  # noqa: E402

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)


def fixtures():
    forged = primary_report()
    set_candidate(forged, "closed")
    selected = primary_report()
    selected["candidate_front_state"]["evidence"] = "明確偽造測試：此觀察屬於另一張圖。"
    rows = [
        ("open-preserved", primary_report(), "reconstruction"),
        ("closed-preview", primary_report(candidate="closed"), "preview"),
        ("other-failures", primary_report(candidate="closed", prior_fail=True), "preview"),
        ("candidate-missing", primary_report(candidate=None), "preview"),
        ("forged-allpass", forged, "context"),
        ("selected-mismatch", selected, "context"),
        ("legacy-compatible", primary_report(None, None), "reconstruction"),
    ]
    items = {}
    for name, report, expected in rows:
        item = fixture_item(name, report)
        if expected == "context":
            # Deliberately forged persisted URLs must not create a verified hero.
            item["reconstruction"].update(
                status="completed",
                image_url="/media/fixture-job/reconstructions/preview.png",
                image_validation=copy.deepcopy(report),
                preview_url="/media/fixture-job/reconstructions/preview.png",
                preview_validation=copy.deepcopy(report),
            )
        items[name] = item
    return items, {name: expected for name, _, expected in rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "out/verification/primary-state-20261022/frontend-browser"
    )
    output = parser.parse_args().output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    items, expected = fixtures()
    fixture_before = json.dumps(items, sort_keys=True, ensure_ascii=False)
    source, synthetic = image_bytes("source"), image_bytes("preview")
    requests = []
    job = {
        "id": "fixture-job",
        "status": "completed",
        "mode": "outfit",
        "provider": "meta",
        "filename": "明確離線介面測試照片.png",
        "media_type": "image",
        "created_at": "2026-10-22T08:00:00Z",
        "progress": 100,
        "stage": "分析完成",
        "item_count": len(items),
    }
    routes = {
        "/auth/session": {"mode": "local", "authenticated": False},
        "/api/health": {
            "local": {"available": True, "model": "explicit offline UI fixture"},
            "meta": {"configured": False},
            "reconstruction": {"available": False},
            "discovery": {"available": False},
            "ffmpeg": True,
            "limits": {
                "upload_mb": 50,
                "batch_files": 32,
                "batch_upload_mb": 256,
                "max_items": 24,
                "video_seconds": 30,
            },
        },
        "/api/items": {"items": list(items.values())},
        "/api/jobs": {"jobs": [job]},
        "/api/batches": {"batches": []},
        "/api/garment-groups": {"groups": []},
        "/api/duplicates": {"suggestions": []},
    }
    static_files = {"/": ROOT / "wardrobe/static/index.html"}
    for name in ("index.html", "app.js", "styles.css"):
        static_files["/static/" + name] = ROOT / "wardrobe/static" / name
    media = {
        "/media/fixture-job/context.jpg": source,
        "/media/fixture-job/source.jpg": source,
        "/media/fixture-job/cutout.jpg": source,
        "/media/fixture-job/reconstructions/preview.png": synthetic,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            requests.append({"method": self.command, "path": path})
            if path in routes:
                data, mime, status = (
                    json.dumps(routes[path], ensure_ascii=False).encode(),
                    "application/json",
                    200,
                )
            elif path in media:
                data, mime, status = media[path], "image/png", 200
            elif path in static_files:
                data = static_files[path].read_bytes()
                mime, status = mimetypes.guess_type(static_files[path].name)[0], 200
            else:
                data, mime, status = (
                    b'{"detail":"Explicit fixture route unavailable"}',
                    "application/json",
                    404,
                )
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    report = {
        "status": "running",
        "scope": "Explicit offline fixture API and drawn test images; no database, real model, paid service or production port.",
        "base_url": origin,
        "scenarios": [],
        "errors": [],
        "network_errors": [],
        "forbidden_requests": [],
        "app_js_sha256": hashlib.sha256((ROOT / "wardrobe/static/app.js").read_bytes()).hexdigest(),
    }
    (output / "fixtures.json").write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n")
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for width, height in ((1440, 1000), (390, 844)):
                    context = browser.new_context(
                        viewport={"width": width, "height": height}, service_workers="block"
                    )
                    policy = LocalNetworkPolicy.for_http(origin)
                    install_network_policy(context, policy)

                    def only_get(route):
                        request = route.request
                        if request.method != "GET" or not request.url.startswith(origin + "/"):
                            report["forbidden_requests"].append(
                                {"method": request.method, "url": request.url}
                            )
                            route.abort()
                        else:
                            route.fallback()

                    context.route("**/*", only_get)
                    page = context.new_page()
                    page.set_default_timeout(10000)
                    page.on("pageerror", lambda error: report["errors"].append(str(error)))
                    page.on(
                        "console",
                        lambda message: (
                            report["errors"].append(message.text) if message.type == "error" else None
                        ),
                    )
                    page.on(
                        "requestfailed",
                        lambda request: report["network_errors"].append(
                            {"url": request.url, "failure": request.failure}
                        ),
                    )
                    page.on(
                        "response",
                        lambda response: (
                            report["network_errors"].append({"url": response.url, "status": response.status})
                            if response.status >= 400
                            else None
                        ),
                    )
                    page.goto(origin, wait_until="networkidle")
                    expect(page.locator(".garment-card")).to_have_count(len(items))
                    for name, key in expected.items():
                        card = page.locator(f'.garment-card:has([data-item-id="{name}"])')
                        if key == "reconstruction":
                            expect(card.locator(".card-reconstruction")).to_have_text("AI 展示圖 · 已比對")
                        elif key == "preview":
                            expect(card.locator(".card-reconstruction")).to_have_text("AI 預覽・含推測")
                        else:
                            expect(card.locator(".card-reconstruction")).to_have_count(0)
                            assert card.locator("img").get_attribute("src").endswith("/context.jpg")
                        card.locator("[data-item-id]").click()
                        expect(page.locator("#item-media-region")).to_have_attribute("data-selected", key)
                        if key != "context":
                            expect(page.locator("#item-media-content figure")).to_have_count(2)
                            expect(page.locator("#item-media-content figcaption").first).to_have_text(
                                "原圖 · 可見依據"
                            )
                        if name in ("closed-preview", "other-failures"):
                            expect(page.locator(".preview-mismatches")).to_contain_text(FRONT_REASON)
                            if name == "other-failures":
                                expect(page.locator(".preview-mismatches")).to_contain_text(OTHER_REASON)
                            page.locator(".preview-mismatches").scroll_into_view_if_needed()
                            page.screenshot(
                                path=output / f"{name}-warning-{width}.png", animations="disabled"
                            )
                        if name == "candidate-missing":
                            expect(page.locator(".reconstruction-review")).to_contain_text(
                                "候選缺少可確認的前襟展示觀察"
                            )
                        if key == "context":
                            page.locator('[data-item-media-key="legacy"]').first.focus()
                            page.keyboard.press("Enter")
                            expect(page.locator("#item-media-region")).to_have_attribute(
                                "data-selected", "legacy"
                            )
                            expect(page.locator("#item-media-caption")).to_contain_text("不採用為目錄主圖")
                            expect(page.locator("#item-media-content figure")).to_have_count(2)
                        else:
                            page.locator('[data-item-media-key="context"]').first.focus()
                            page.keyboard.press("Enter")
                            expect(page.locator("#item-media-region")).to_have_attribute(
                                "data-selected", "context"
                            )
                            page.locator(f'[data-item-media-key="{key}"]').first.focus()
                            page.keyboard.press("Enter")
                        page.locator("#item-media-content img").evaluate_all(
                            "images => Promise.all(images.map(image => image.decode()))"
                        )
                        assert page.locator("#item-dialog").evaluate(
                            "element => element.scrollWidth <= element.clientWidth + 1"
                        )
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                        page.locator("#item-dialog").evaluate("element => element.scrollTop = 0")
                        page.screenshot(path=output / f"{name}-{width}.png", animations="disabled")
                        page.locator("#item-dialog > .dialog-close").click()
                        report["scenarios"].append(
                            {"name": name, "width": width, "expected_default": key, "status": "passed"}
                        )
                    assert policy.blocked_requests == 0
                    context.close()
                assert (
                    not report["errors"] and not report["network_errors"] and not report["forbidden_requests"]
                )
                report["status"] = "passed"
            finally:
                browser.close()
    except Exception as error:
        report["status"] = "failed"
        report["failure"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        report["server_closed"] = not thread.is_alive()
        report["get_only_requests"] = requests
        report["fixture_data_unchanged"] = (
            json.dumps(items, sort_keys=True, ensure_ascii=False) == fixture_before
        )
        report["app_js_unchanged_during_test"] = (
            hashlib.sha256((ROOT / "wardrobe/static/app.js").read_bytes()).hexdigest()
            == report["app_js_sha256"]
        )
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "scenarios": len(report["scenarios"]),
                    "report": str(output / "report.json"),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
