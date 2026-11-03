"""Test manual original-image downloads with isolated saved-response fixtures.

The official Lens link is checked as a link, never visited or uploaded to.
"""

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
sys.path.insert(0, str(Path.home() / ".agents/skills/webapp-testing/scripts"))
from network_policy import LocalNetworkPolicy, install_network_policy  # noqa: E402


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evidence = args.source_evidence.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    assert not (output / "report.json").exists(), "Use a fresh evidence directory."
    (output / "screens").mkdir(exist_ok=True)
    (output / "downloads").mkdir(exist_ok=True)
    source_path = evidence / "final-api.json"
    source = json.loads(source_path.read_text())
    item = next(entry for entry in source["items"]["items"] if entry["record"]["subtype"] == "zip-up hoodie")
    assert item["reconstruction"]["preview_url"] and item["cutout_url"] != item["image_url"]
    assert not item["discovery"]["result"]["shopping"]["offers"]
    source_job = next(job for job in source["jobs"]["jobs"] if job["id"] == item["job_id"])
    health = json.loads((evidence / "report.json").read_text())["initial_health"]
    media = {}
    for entry in json.loads((evidence / "media-manifest.json").read_text()):
        target = (evidence / entry["file"]).resolve(strict=True)
        assert target.is_relative_to(evidence) and sha(target) == entry["sha256"]
        media[entry["url"]] = target
    fixture = {"item": copy.deepcopy(item), "job": copy.deepcopy(source_job)}
    report = {
        "status": "running",
        "scope": "Read-only local HTTP replay of preserved images and API records; modified guard cases are explicit fixtures. No application DB, model, web search or Lens upload.",
        "lens_validation": "Official URL, target and rel attributes only; the external link is not opened.",
        "source_sha256": sha(source_path),
        "static_sha256": {
            name: sha(ROOT / "wardrobe/static" / name) for name in ("index.html", "app.js", "styles.css")
        },
        "script_sha256": sha(Path(__file__)),
        "scenarios": [],
        "page_errors": [],
        "console_errors": [],
        "expected_console_errors": [],
        "outside_requests": [],
        "mutations": [],
    }
    requests = []

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
            requests.append({"method": "GET", "path": path})
            if path == "/auth/session":
                return self.send({"mode": "local", "authenticated": False})
            if path == "/api/health":
                return self.send(health)
            if path == "/api/jobs" and fixture.get("jobs_unavailable"):
                return self.send({"detail": "明確介面測試：來源任務暫時無法讀取。"}, 503)
            collections = {
                "/api/items": {"items": [fixture["item"]]},
                "/api/jobs": {"jobs": [fixture["job"]]},
                "/api/batches": {"batches": []},
                "/api/garment-groups": {"groups": []},
                "/api/duplicates": {"suggestions": []},
            }
            if path in collections:
                return self.send(copy.deepcopy(collections[path]))
            if path in media:
                target = media[path]
            else:
                relative = "index.html" if path == "/" else path.removeprefix("/static/")
                target = (ROOT / "wardrobe/static" / relative).resolve()
                if not target.is_relative_to(ROOT / "wardrobe/static") or not target.is_file():
                    return self.send({"detail": "Unknown fixture route"}, 404)
            return self.send(
                target.read_bytes(),
                content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
            )

        def reject_mutation(self):
            report["mutations"].append({"method": self.command, "path": self.path})
            return self.send({"detail": "Read-only fixtures"}, 405)

        do_POST = do_PUT = do_PATCH = do_DELETE = reject_mutation

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    policies = []

    def record(name, **values):
        report["scenarios"].append({"name": name, "status": "passed", **values})
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)

            def open_item(width, value, job=None, jobs_unavailable=False):
                fixture.update(
                    item=copy.deepcopy(value),
                    job=copy.deepcopy(job or source_job),
                    jobs_unavailable=jobs_unavailable,
                )
                context = browser.new_context(
                    viewport={"width": width, "height": 1050 if width > 600 else 844},
                    service_workers="block",
                    accept_downloads=True,
                )
                policy = LocalNetworkPolicy.for_http(base)
                policies.append(policy)
                install_network_policy(context, policy)

                def guard(route):
                    request = route.request
                    if request.method != "GET":
                        report["mutations"].append({"method": request.method, "url": request.url})
                        return route.abort("blockedbyclient")
                    if urlsplit(request.url).netloc != urlsplit(base).netloc:
                        report["outside_requests"].append(request.url)
                        return route.abort("blockedbyclient")
                    route.fallback()

                context.route("**/*", guard)
                page = context.new_page()
                page.set_default_timeout(15000)
                page.on("pageerror", lambda error: report["page_errors"].append(str(error)))

                def console_message(message):
                    if message.type != "error":
                        return
                    if (
                        jobs_unavailable
                        and message.location.get("url") == base + "/api/jobs"
                        and "503" in message.text
                    ):
                        report["expected_console_errors"].append(
                            {"url": message.location["url"], "text": message.text}
                        )
                    else:
                        report["console_errors"].append(message.text)

                page.on("console", console_message)
                page.goto(base, wait_until="networkidle")
                page.locator(f'[data-item-id="{value["id"]}"]').click()
                expect(page.locator("#item-dialog")).to_be_visible()
                return context, page

            def check_download(width, value, expected_url, label, name):
                context, page = open_item(width, value)
                handoff = page.locator(".shopping-image-handoff")
                expect(handoff).to_have_count(1)
                expect(handoff).to_contain_text(
                    "下載原圖後，在 Google Lens 上傳；這是外部手動搜尋，尚未核對同款與庫存。"
                )
                download_link = handoff.get_by_role("link", name=label)
                assert urlsplit(download_link.get_attribute("href")).path == expected_url
                assert expected_url != item["reconstruction"]["preview_url"]
                lens = handoff.get_by_role("link", name="開啟 Google Lens")
                expect(lens).to_have_attribute("href", "https://lens.google/")
                expect(lens).to_have_attribute("target", "_blank")
                expect(lens).to_have_attribute("rel", "noopener noreferrer")
                assert handoff.locator('input[type="file"], form').count() == 0
                page.locator("#item-dialog").evaluate(
                    "async el => { await Promise.all(el.getAnimations().map(animation => animation.finished)); }"
                )
                page.locator("#item-media-content img").evaluate_all(
                    "async imgs => { await Promise.all(imgs.map(img => img.decode())); }"
                )
                handoff.evaluate("el => el.scrollIntoView({block:'center',behavior:'instant'})")
                page.screenshot(path=output / f"screens/{name}.png")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                assert page.locator("#item-dialog").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
                marker = len(requests)
                download_link.focus()
                with page.expect_download() as pending:
                    download_link.press("Enter")
                download = pending.value
                target = output / "downloads" / f"{name}-{download.suggested_filename}"
                download.save_as(target)
                assert download.failure() is None
                assert target.read_bytes() == media[expected_url].read_bytes()
                assert target.read_bytes() != media[item["reconstruction"]["preview_url"]].read_bytes()
                new_requests = requests[marker:]
                assert any(request["path"] == expected_url for request in new_requests)
                assert all("/reconstructions/" not in request["path"] for request in new_requests)
                record(
                    name,
                    width=width,
                    original_url=expected_url,
                    downloaded_sha256=sha(target),
                    original_sha256=sha(media[expected_url]),
                    bytes=target.stat().st_size,
                    download_requests=new_requests,
                    external_link_opened=False,
                )
                context.close()

            check_download(1440, item, item["cutout_url"], "下載原始辨識區域", "desktop-original-cutout")
            fallback = copy.deepcopy(item)
            fallback["cutout_url"] = None
            check_download(390, fallback, item["image_url"], "下載原圖上下文", "mobile-original-context")
            no_result = copy.deepcopy(item)
            no_result["discovery"] = None
            context, page = open_item(390, no_result)
            expect(page.locator(".shopping-image-handoff")).to_have_count(1)
            expect(page.locator("#item-discovery")).to_contain_text("外部手動搜尋")
            record("manual-entry-without-previous-automated-search", width=390)
            context.close()
            for name in ("no-original-reference", "generated-source-job", "ineligible-clothing"):
                blocked = copy.deepcopy(item)
                job = copy.deepcopy(source_job)
                if name == "no-original-reference":
                    blocked["cutout_url"] = blocked["image_url"] = item["reconstruction"]["preview_url"]
                elif name == "generated-source-job":
                    job["media_type"] = "generated"
                    blocked["provenance"] = "Meta / muse-spark-1.3"
                else:
                    blocked["record"]["category"] = "other"
                context, page = open_item(390, blocked, job)
                expect(page.locator(".shopping-image-handoff")).to_have_count(0)
                expect(page.locator("[data-original-search-download], [data-lens-link]")).to_have_count(0)
                record(name, width=390, explicit_modified_fixture=True)
                context.close()
            unavailable = copy.deepcopy(item)
            unavailable.update(provenance="Meta / muse-spark-1.3", image_url=item["source_url"])
            context, page = open_item(390, unavailable, jobs_unavailable=True)
            expect(page.locator("#service-error")).to_be_visible()
            expect(page.locator("#service-error-message")).to_contain_text("來源任務暫時無法讀取")
            expect(page.locator("[data-original-search-download], [data-lens-link]")).to_have_count(0)
            page.locator("#item-discovery").evaluate(
                "el => el.scrollIntoView({block:'start',behavior:'instant'})"
            )
            page.locator("#item-dialog").evaluate(
                "async el => { await Promise.all(el.getAnimations().map(animation => animation.finished)); }"
            )
            page.screenshot(path=output / "screens/jobs-unavailable-no-handoff-390.png")
            record(
                "items-loaded-but-source-job-unavailable-hides-handoff",
                width=390,
                provenance=unavailable["provenance"],
                explicit_modified_fixture=True,
            )
            fixture["jobs_unavailable"] = False
            expect(page.locator(".shopping-image-handoff")).to_have_count(1, timeout=15000)
            expect(page.locator("#service-error")).not_to_be_visible()
            expect(page.locator("[data-original-search-download]")).to_have_attribute(
                "href", base + item["cutout_url"]
            )
            record(
                "confirmed-original-job-restores-handoff-in-open-detail", width=390, via_existing_poll=True
            )
            context.close()
            browser.close()
        assert not report["page_errors"] and not report["console_errors"]
        assert not report["outside_requests"] and not report["mutations"]
        report["network_policy_blocked_requests"] = sum(policy.blocked_requests for policy in policies)
        assert report["network_policy_blocked_requests"] == 0
        assert sha(source_path) == report["source_sha256"]
        report["source_evidence_unchanged"] = True
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        report["error"] = str(error)
        raise
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {"status": report["status"], "scenarios": len(report["scenarios"]), "output": str(output)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
