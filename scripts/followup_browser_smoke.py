"""Replay saved real API observations to test follow-up progress; never run inference."""

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
    assert not (output / "report.json").exists(), "Use a fresh output directory."
    screens = output / "screens"
    screens.mkdir(exist_ok=True)
    inputs = {
        "pending": evidence / "stages/04.json",
        "terminal": evidence / "final-api.json",
        "media": evidence / "media-manifest.json",
        "flow_report": evidence / "report.json",
    }
    initial = json.loads(inputs["pending"].read_text())
    terminal = json.loads(inputs["terminal"].read_text())
    assert initial["jobs"] == terminal["jobs"], "The regression requires unchanged analysis jobs."
    assert len(initial["jobs"]["jobs"]) == 1
    job = initial["jobs"]["jobs"][0]
    assert job["status"] == "completed" and len(initial["items"]["items"]) == 2
    health = json.loads(inputs["flow_report"].read_text())["initial_health"]
    media = {}
    for entry in json.loads(inputs["media"].read_text()):
        path = (evidence / entry["file"]).resolve(strict=True)
        assert path.is_relative_to(evidence) and sha(path) == entry["sha256"]
        media[entry["url"]] = path
    fixture = {"snapshot": copy.deepcopy(initial)}
    report = {
        "status": "running",
        "scope": "Explicit local HTTP replay fixtures from one preserved real flow. No application DB or model provider; all mutations denied.",
        "fixture_changes": "Terminal replay changes only items. Failure and legacy cases are explicitly modified clones, not new real outcomes.",
        "source_sha256": {key: sha(path) for key, path in inputs.items()},
        "static_sha256": {
            name: sha(ROOT / "wardrobe/static" / name) for name in ("index.html", "app.js", "styles.css")
        },
        "script_sha256": sha(Path(__file__)),
        "scenarios": [],
        "page_errors": [],
        "console_errors": [],
        "outside_requests": [],
        "mutations": [],
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
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/auth/session":
                return self.send({"mode": "local", "authenticated": False})
            snapshot = copy.deepcopy(fixture["snapshot"])
            if path == "/api/health":
                return self.send(health)
            if path in {"/api/items", "/api/jobs", "/api/batches"}:
                return self.send(snapshot[path.rsplit("/", 1)[-1]])
            if path == "/api/garment-groups":
                return self.send({"groups": []})
            if path == "/api/duplicates":
                return self.send({"suggestions": []})
            for collection in ("jobs", "items", "batches"):
                if path.startswith(f"/api/{collection}/"):
                    entity = next(
                        (
                            entry
                            for entry in snapshot[collection][collection]
                            if entry["id"] == path.rsplit("/", 1)[-1]
                        ),
                        None,
                    )
                    return self.send(entity or {"detail": "Unknown replay entity"}, 200 if entity else 404)
            if path in media:
                target = media[path]
            else:
                relative = "index.html" if path == "/" else path.removeprefix("/static/")
                target = (ROOT / "wardrobe/static" / relative).resolve()
                if not target.is_relative_to(ROOT / "wardrobe/static") or not target.is_file():
                    return self.send({"detail": "Unknown replay route"}, 404)
            return self.send(
                target.read_bytes(),
                content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
            )

        def reject_mutation(self):
            report["mutations"].append({"method": self.command, "path": self.path})
            self.send({"detail": "Read-only fixture"}, 405)

        do_POST = do_PATCH = do_DELETE = do_PUT = reject_mutation

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

            def open_page(width, data):
                fixture["snapshot"] = copy.deepcopy(data)
                context = browser.new_context(
                    viewport={"width": width, "height": 1050 if width > 600 else 844}, service_workers="block"
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
                page.on(
                    "console",
                    lambda message: (
                        report["console_errors"].append(message.text) if message.type == "error" else None
                    ),
                )
                page.goto(base, wait_until="networkidle")
                page.locator('[data-view="history"]').click()
                expect(page.locator(".job-row .status-pill")).to_have_text("分析完成")
                return context, page

            def check_pending(region):
                expect(region).to_contain_text("分析已結束，後續工作仍在進行")
                expect(region).to_contain_text("1 件生成中 · 1 件等待中")
                expect(region).to_contain_text("1 件查找中 · 1 件等待中")
                expect(region.get_by_role("button", name="查看衣物與生成進度")).to_be_visible()

            def no_overflow(page):
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                assert page.locator("dialog[open]").evaluate_all(
                    "els => els.every(el => el.scrollWidth <= el.clientWidth + 1)"
                )

            def capture(page, name, region=None):
                if region:
                    region.scroll_into_view_if_needed()
                page.screenshot(path=screens / name)
                no_overflow(page)

            context, page = open_page(1440, initial)
            history = page.locator("#jobs-region .job-followup")
            check_pending(history)
            capture(page, "history-running-1440.png", history)
            record("analysis-complete-with-real-running-and-queued-followups", width=1440)
            page.locator(".job-row").click()
            detail = page.locator("#job-detail-followup")
            check_pending(detail)
            detail.get_by_role("button", name="查看衣物與生成進度").focus()
            fixture["snapshot"]["items"] = copy.deepcopy(terminal["items"])
            expect(detail).to_contain_text(
                "2 件已產生（含推測） · 1 件有已知差異 · 1 件待核對", timeout=15000
            )
            expect(detail).to_contain_text("2 件查找結束 · 已執行 4 次搜尋 · 尚無通過核對的商品")
            expect(detail).not_to_contain_text("仍在進行")
            expect(detail.get_by_role("button", name="查看衣物與生成進度")).to_be_focused()
            expect(page.locator("#job-status-row .status-pill")).to_have_text("分析完成")
            expect(history).to_contain_text("2 件已產生")
            capture(page, "detail-terminal-1440.png", detail)
            record(
                "items-only-poll-updates-history-and-open-job-detail",
                job_unchanged=True,
                focus_preserved=True,
                offers=0,
            )
            detail.get_by_role("button", name="查看衣物與生成進度").press("Enter")
            expect(page.locator("#job-dialog")).not_to_be_visible()
            expect(page.locator("#items-region .garment-card")).to_have_count(2)
            decoded = page.locator("#items-region .garment-image img").evaluate_all(
                "async imgs => { await Promise.all(imgs.map(img => img.decode())); return imgs.map(img => ({src: img.getAttribute('src'), width: img.naturalWidth})); }"
            )
            assert len(decoded) == 2 and all(row["width"] > 0 for row in decoded)
            record("result-entry-opens-both-existing-clothes-without-new-tasks", decoded_images=decoded)
            context.close()

            failed = copy.deepcopy(terminal)
            failed_item = failed["items"]["items"][0]
            failed_item["reconstruction"].update(status="failed", error="明確介面測試：本次生成中斷。")
            failed_item["discovery"].update(status="failed", error="明確介面測試：本次查找中斷。")
            context, page = open_page(1440, failed)
            history = page.locator("#jobs-region .job-followup")
            for text in (
                "1 件生成失敗",
                "1 件已產生",
                "先前預覽仍保留",
                "1 件查找失敗",
                "1 件查找結束",
                "先前查找結果仍保留",
            ):
                expect(history).to_contain_text(text)
            expect(history).not_to_contain_text("仍在進行")
            capture(page, "history-partial-failure-1440.png", history)
            record("explicit-partial-failure-retains-previous-preview-and-search-results", width=1440)
            context.close()

            context, page = open_page(390, initial)
            history = page.locator("#jobs-region .job-followup")
            check_pending(history)
            capture(page, "history-running-390.png", history)
            link = history.get_by_role("button", name="查看衣物與生成進度")
            link.focus()
            link.press("Enter")
            expect(page.locator("#items-region .garment-card")).to_have_count(2)
            expect(page.locator("#history-view")).not_to_be_visible()
            no_overflow(page)
            record("mobile-history-result-entry-works-with-keyboard", width=390)
            page.locator('[data-view="history"]').click()
            page.locator(".job-row").click()
            detail = page.locator("#job-detail-followup")
            check_pending(detail)
            capture(page, "detail-running-390.png", detail)
            fixture["snapshot"]["items"] = copy.deepcopy(terminal["items"])
            expect(detail).to_contain_text(
                "2 件已產生（含推測） · 1 件有已知差異 · 1 件待核對", timeout=15000
            )
            expect(detail).to_contain_text("尚無通過核對的商品")
            capture(page, "detail-terminal-390.png", detail)
            record("mobile-open-detail-follows-terminal-state", width=390)
            context.close()

            legacy = copy.deepcopy(initial)
            for item in legacy["items"]["items"]:
                item["reconstruction"] = item["discovery"] = None
            context, page = open_page(390, legacy)
            history = page.locator("#jobs-region .job-followup")
            expect(history.get_by_role("button", name="查看 2 筆分析結果")).to_be_visible()
            expect(history).not_to_contain_text("仍在進行")
            page.locator(".job-row").click()
            expect(page.locator("#job-detail-followup")).not_to_be_visible()
            expect(page.locator("#job-detail-actions [data-job-items]")).to_have_count(1)
            record("historical-record-with-no-followups-has-no-invented-pending-work", width=390)
            context.close()
            browser.close()
        assert not report["page_errors"] and not report["console_errors"]
        assert not report["mutations"] and not report["outside_requests"]
        report["network_policy_blocked_requests"] = sum(policy.blocked_requests for policy in policies)
        assert report["network_policy_blocked_requests"] == 0
        assert report["source_sha256"] == {key: sha(path) for key, path in inputs.items()}
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
