"""Explicit local HTTP fixtures for first-upload availability and privacy behavior."""

import argparse
import copy
import hashlib
import io
import json
import mimetypes
import subprocess
import sys
import tempfile
import threading
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageDraw
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
POLICY = Path.home() / ".agents/skills/webapp-testing/scripts"
sys.path.insert(0, str(POLICY))
from network_policy import LocalNetworkPolicy, install_network_policy  # noqa: E402


def health(local=True, meta=True, ffmpeg=True):
    return {
        "status": "ok",
        "local": {
            "available": local,
            "model": "explicit fixture",
            "error": None if local else "Fixture offline",
        },
        "meta": {"configured": meta},
        "ffmpeg": ffmpeg,
        "reconstruction": {
            "available": meta,
            "default_mode": "preview",
            "preview_candidates": 3,
            "preview_standard_estimate_usd": 0.03,
        },
        "discovery": {"available": meta},
        "model_usage": {
            "pricing_version": "explicit-browser-fixture",
            "currency": "USD",
            "event_count": 4,
            "generated_images": 3,
            "web_search_queries": 1,
            "estimated_usd": 0.0345,
            "token_usage_complete": True,
        },
        "limits": {"upload_mb": 50, "batch_files": 32, "batch_upload_mb": 256},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    fixture = {
        "health": health(),
        "health_code": 200,
        "posts": [],
        "health_requests": [],
        "jobs": [],
    }
    report = {
        "status": "running",
        "scope": "Explicit loopback HTTP fixtures; no application database or inference provider",
        "scenarios": [],
        "page_errors": [],
        "outside_requests": [],
    }
    report["static_sha256"] = {
        name: hashlib.sha256((ROOT / "wardrobe/static" / name).read_bytes()).hexdigest()
        for name in ("index.html", "app.js", "styles.css")
    }
    picture = Image.new("RGB", (300, 400), "#ede8dd")
    ImageDraw.Draw(picture).text((20, 180), "EXPLICIT TEST FIXTURE", fill="#25463c")
    encoded = io.BytesIO()
    picture.save(encoded, "JPEG")
    photo = {"name": "explicit-ui-fixture.jpg", "mimeType": "image/jpeg", "buffer": encoded.getvalue()}

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
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/auth/session":
                return self.send({"mode": "local", "authenticated": False})
            if path == "/api/health":
                fixture["health_requests"].append(self.path)
                return self.send(
                    copy.deepcopy(fixture["health"])
                    if fixture["health_code"] == 200
                    else {"detail": "明確測試：健康檢查暫時中斷"},
                    fixture["health_code"],
                )
            collection = {
                "/api/items": ("items", []),
                "/api/jobs": ("jobs", fixture["jobs"]),
                "/api/batches": ("batches", []),
                "/api/garment-groups": ("groups", []),
                "/api/duplicates": ("suggestions", []),
            }.get(path)
            if collection:
                return self.send({collection[0]: collection[1]})
            relative = "index.html" if path == "/" else path.removeprefix("/static/")
            target = (ROOT / "wardrobe/static" / relative).resolve()
            if target.is_relative_to(ROOT / "wardrobe/static") and target.is_file():
                return self.send(
                    target.read_bytes(),
                    content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
                )
            return self.send({"detail": "No fixture route"}, 404)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/api/generate":
                fields, files = json.loads(raw), []
            else:
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
                files = [part.get_filename() for part in message.iter_parts() if part.get_filename()]
            fixture["posts"].append({"path": self.path, "fields": fields, "files": files})
            job = {
                "id": f"fixture-job-{len(fixture['posts'])}",
                "status": "completed",
                "stage": "completed",
                "progress": 100,
                "filename": "明確介面測試",
                "mode": fields["mode"],
                "provider": fields["provider"],
                "media_type": "image",
                "item_count": 0,
                "created_at": "2026-10-22T00:00:00Z",
            }
            fixture["jobs"].append(job)
            return self.send(job, 202)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def record(name, **details):
        report["scenarios"].append({"name": name, "status": "passed", **details})
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))

    try:
        with (
            tempfile.TemporaryDirectory(prefix="rewear-onboarding-") as temporary,
            sync_playwright() as playwright,
        ):
            video_path = Path(temporary) / "explicit-fixture.mp4"
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=gray:s=128x128:d=0.2",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(video_path),
                ],
                check=True,
            )
            video = {"name": video_path.name, "mimeType": "video/mp4", "buffer": video_path.read_bytes()}
            browser = playwright.chromium.launch(headless=True)

            policies = []

            def open_page(availability, width=1440, code=200, delay_response=None):
                fixture.update(
                    health=availability,
                    health_code=code,
                    posts=[],
                    health_requests=[],
                    jobs=[],
                )
                context = browser.new_context(
                    viewport={"width": width, "height": 1050 if width == 1440 else 844},
                    service_workers="block",
                )
                policy = LocalNetworkPolicy.for_http(base)
                policies.append(policy)
                install_network_policy(context, policy)
                if delay_response:
                    # Keep network requests under the loopback policy. Delay only
                    # browser consumption of one successful fixture response, so
                    # the policy's synchronous 5-second proxy cannot mask a race.
                    context.add_init_script(
                        """(() => {
                          const target = %s;
                          let first = true;
                          let release;
                          const wait = new Promise(resolve => { release = resolve; });
                          window.__onboardingResponse = { received: false, released: false, consumed: false,
                            release: () => { window.__onboardingResponse.released = true; release(); } };
                          const original = window.fetch.bind(window);
                          window.fetch = async (...args) => {
                            const response = await original(...args);
                            const input = args[0];
                            const path = new URL(typeof input === 'string' ? input : input.url, location.href).pathname;
                            if (first && path === target) {
                              first = false;
                              window.__onboardingResponse.received = response.ok;
                              await wait;
                              window.__onboardingResponse.consumed = true;
                            }
                            return response;
                          };
                        })();"""
                        % json.dumps(delay_response)
                    )

                def check_origin(route):
                    if urlsplit(route.request.url).netloc != urlsplit(base).netloc:
                        report["outside_requests"].append(route.request.url)
                        route.abort("blockedbyclient")
                    else:
                        route.fallback()

                context.route("**/*", check_origin)
                page = context.new_page()
                page.set_default_timeout(10000)
                page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
                page.goto(base, wait_until="networkidle")
                return context, page

            def check_layout(page):
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

            def recheck(page, availability):
                fixture["health"] = availability
                page.locator("#health-button").click()
                page.locator("#health-refresh").click()
                expect(page.locator("#health-refresh")).to_be_enabled()
                page.locator('[data-close="status-dialog"]').click()
                assert fixture["health_requests"][-1] == "/api/health?refresh=true"

            for width in (1440, 390):
                context, page = open_page(health(local=False), width)
                expect(page.locator("#provider-select")).to_have_value("local")
                expect(page.locator("#upload-button")).to_be_disabled()
                expect(page.locator("#provider-readiness")).to_contain_text("本機分析目前未連線")
                expect(page.locator('#provider-select option[value="local"]')).to_be_disabled()
                expect(page.locator("#provider-cloud-disclosure")).to_contain_text("傳送至 Meta 雲端")
                page.locator("#file-input").set_input_files(photo)
                page.locator("#provider-recovery").evaluate(
                    "el => el.scrollIntoView({block: 'center', behavior: 'instant'})"
                )
                page.screenshot(path=output / f"offline-first-{width}.png")
                page.locator("#upload-form").evaluate(
                    "el => el.dispatchEvent(new Event('submit', {bubbles:true, cancelable:true}))"
                )
                expect(page.locator("#upload-error")).to_contain_text("本機分析目前未連線")
                assert not fixture["posts"]
                page.locator("#provider-use-cloud").focus()
                page.keyboard.press("Enter")
                expect(page.locator("#provider-select")).to_have_value("meta")
                expect(page.locator("#provider-note")).to_contain_text("照片會送至 Meta Spark")
                expect(page.locator("#upload-button")).to_be_enabled()
                assert not fixture["posts"], "Switching provider must not upload a file."
                expect(page.locator("#file-queue")).to_contain_text(photo["name"])
                page.locator("#provider-select").evaluate(
                    "el => el.scrollIntoView({block: 'center', behavior: 'instant'})"
                )
                page.screenshot(path=output / f"explicit-cloud-{width}.png")
                page.locator("#upload-button").click()
                expect(page.locator("#file-queue")).not_to_be_visible()
                assert fixture["posts"][0]["fields"]["provider"] == "meta"
                assert fixture["posts"][0]["files"] == [photo["name"]]
                check_layout(page)
                record(f"offline-first-explicit-cloud-{width}", captured_upload=fixture["posts"][0])
                context.close()

            context, page = open_page(health())
            page.locator("#health-button").click()
            expect(page.locator("#status-dialog")).to_be_visible()
            expect(page.locator("#status-dialog-body")).to_contain_text("$0.0345")
            expect(page.locator("#status-dialog-body")).to_contain_text("4 次模型呼叫")
            expect(page.locator("#status-dialog-body")).to_contain_text("3 張生成圖")
            page.screenshot(path=output / "model-usage-cost.png", animations="disabled")
            record("persisted-model-cost-is-visible-and-labelled-estimate")
            context.close()

            context, page = open_page(health())
            page.locator("#provider-select").select_option("local")
            page.locator("#file-input").set_input_files(photo)
            recheck(page, health(local=False))
            expect(page.locator("#provider-select")).to_have_value("local")
            expect(page.locator("#upload-button")).to_be_disabled()
            assert not fixture["posts"]
            fixture["health"] = health()
            page.locator("#provider-recheck").click()
            expect(page.locator("#upload-button")).to_be_enabled()
            expect(page.locator("#provider-select")).to_have_value("local")
            assert fixture["health_requests"][-1] == "/api/health?refresh=true"
            page.locator("#upload-button").click()
            expect(page.locator("#file-queue")).not_to_be_visible()
            assert fixture["posts"][0]["fields"]["provider"] == "local"
            record("explicit-local-stays-local-offline-and-after-recovery")
            context.close()

            context, page = open_page(health(local=False, meta=False), 390)
            expect(page.locator("#provider-use-cloud")).not_to_be_visible()
            expect(page.locator("#upload-button")).to_be_disabled()
            expect(page.locator("#generate-button")).to_be_disabled()
            for value in ("local", "meta"):
                expect(page.locator(f'#provider-select option[value="{value}"]')).to_be_disabled()
            page.locator("#provider-recovery").scroll_into_view_if_needed()
            page.screenshot(path=output / "both-unavailable-390.png")
            check_layout(page)
            record("both-services-unavailable-no-invalid-cloud-switch")
            context.close()

            context, page = open_page(health(meta=False))
            expect(page.locator("#upload-button")).to_be_enabled()
            page.locator("label.outfit-mode").click()
            expect(page.locator("#upload-button")).to_be_disabled()
            expect(page.locator("#provider-readiness")).to_contain_text("需要 Meta 分割")
            page.locator('input[name="mode"][value="single"]').check()
            expect(page.locator("#upload-button")).to_be_enabled()
            expect(page.locator("#generate-button")).to_be_disabled()
            record("local-only-photo-allowed-outfit-and-generation-blocked")
            context.close()

            context, page = open_page(health(), code=503)
            expect(page.locator("#upload-button")).to_be_disabled()
            expect(page.locator("#provider-readiness")).to_contain_text("無法確認模型連線")
            fixture["health_code"] = 200
            page.locator("#provider-recheck").click()
            expect(page.locator("#upload-button")).to_be_enabled()
            assert fixture["health_requests"][-1] == "/api/health?refresh=true"
            record("health-failure-recovers-with-forced-refresh")
            context.close()

            context, page = open_page(health(local=False), delay_response="/api/health")
            page.wait_for_function("window.__onboardingResponse.received === true")
            expect(page.locator("#upload-button")).to_be_disabled()
            fixture["health"] = health()
            page.locator("#health-button").click()
            page.locator("#health-refresh").click()
            expect(page.locator("#health-refresh")).to_be_enabled()
            expect(page.locator("#upload-button")).to_be_enabled()
            page.evaluate("window.__onboardingResponse.release()")
            page.wait_for_function("window.__onboardingResponse.consumed === true")
            page.evaluate(
                "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
            )
            expect(page.locator("#upload-button")).to_be_enabled()
            expect(page.locator('#provider-select option[value="local"]')).to_have_text(
                "Glimmer · 本機分析（已連線）"
            )
            assert len(fixture["health_requests"]) == 2
            record("overlapping-health-old-offline-response-cannot-overwrite-new-ready")
            context.close()

            context, page = open_page(health())
            page.locator("#file-input").set_input_files([photo, video])
            expect(page.locator('input[name="mode"][value="single"]')).to_be_checked()
            expect(page.locator("#provider-select")).to_have_value("local")
            expect(page.locator("#provider-note")).to_contain_text("影片會送至 Meta SAM")
            expect(page.locator("#provider-note")).not_to_contain_text("照片留在這台裝置")
            recheck(page, health(ffmpeg=False))
            expect(page.locator("#upload-button")).to_be_disabled()
            expect(page.locator("#provider-readiness")).to_contain_text("FFmpeg")
            expect(page.locator("#provider-use-cloud")).not_to_be_visible()
            assert not fixture["posts"]
            record("mixed-photo-video-discloses-SAM-and-blocks-missing-ffmpeg")
            context.close()

            context, page = open_page(health(local=False))
            page.locator("#generate-entry summary").click()
            page.locator("#generate-prompt").fill("明確測試：單件白色衣物放在桌上")
            page.locator("#generate-form").evaluate(
                "el => el.dispatchEvent(new Event('submit', {bubbles:true, cancelable:true}))"
            )
            expect(page.locator("#generate-error")).to_contain_text("本機分析目前未連線")
            assert not fixture["posts"]
            page.locator("#provider-use-cloud").click()
            page.locator("#generate-button").click()
            expect(page.locator("#generate-button")).to_be_enabled()
            assert fixture["posts"][0]["path"] == "/api/generate"
            assert fixture["posts"][0]["fields"]["provider"] == "meta"
            record("generation-cannot-spend-before-offline-local-analysis")
            context.close()

            context, page = open_page(health(), delay_response="/api/jobs")
            page.locator("#file-input").set_input_files(photo)
            page.locator("#upload-button").click()
            page.wait_for_function("window.__onboardingResponse.received === true")
            expect(page.locator("#upload-button")).to_contain_text("正在上傳")
            recheck(page, health(local=False))
            page.evaluate("window.__onboardingResponse.release()")
            expect(page.locator("#file-queue")).not_to_be_visible()
            expect(page.locator("#upload-button")).to_be_disabled()
            expect(page.locator("#provider-select")).to_have_value("local")
            record("pending-upload-finally-preserves-new-offline-state")
            context.close()

            assert not report["page_errors"], report["page_errors"]
            assert not report["outside_requests"], report["outside_requests"]
            report["network_policy_blocked_requests"] = sum(policy.blocked_requests for policy in policies)
            assert report["network_policy_blocked_requests"] == 0
            browser.close()
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
