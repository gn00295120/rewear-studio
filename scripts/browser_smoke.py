"""Exercise the UI against a disposable local server; never edit the user's catalogue."""

import argparse
import csv
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("out/verification/browser"))
    parser.add_argument(
        "--policy-dir", type=Path, default=Path.home() / ".agents/skills/webapp-testing/scripts"
    )
    args = parser.parse_args()
    sys.path.insert(0, str(args.policy_dir))
    from network_policy import LocalNetworkPolicy, install_network_policy

    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    root = Path(__file__).resolve().parent.parent
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        port = candidate.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    checks, errors, console_errors, bad_responses = [], [], [], []
    with (
        tempfile.TemporaryDirectory(prefix="rewear-browser-") as data_dir,
        (output / "server.log").open("w") as log,
    ):
        server = subprocess.Popen(
            [sys.executable, "-m", "wardrobe", "--port", str(port)],
            cwd=root,
            env={**os.environ, "REWEAR_DATA_DIR": data_dir},
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            for _ in range(60):
                try:
                    if httpx.get(url + "/api/health", timeout=1).status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                if server.poll() is not None:
                    raise RuntimeError("Isolated browser server failed; see server.log")
                time.sleep(0.25)
            else:
                raise RuntimeError("Isolated browser server did not become ready")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(
                    viewport={"width": 1440, "height": 1000}, service_workers="block", accept_downloads=True
                )
                policy = LocalNetworkPolicy.for_http(url)
                install_network_policy(context, policy)
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on(
                    "console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None
                )
                page.on(
                    "response",
                    lambda response: (
                        bad_responses.append({"url": response.url, "status": response.status})
                        if response.status >= 400
                        else None
                    ),
                )
                page.goto(url, wait_until="networkidle")
                expect(page.get_by_text("留一個位置，給你的衣物。")).to_be_visible()
                page.screenshot(path=output / "desktop-empty.png", full_page=True, animations="disabled")
                checks.append("empty state")

                page.locator("#health-button").click()
                expect(page.locator("#status-dialog")).to_be_visible()
                expect(page.locator("#status-dialog")).to_contain_text("實際可用狀態")
                page.keyboard.press("Escape")
                expect(page.locator("#status-dialog")).not_to_be_visible()
                page.keyboard.press("/")
                expect(page.locator("#search-input")).to_be_focused()
                checks.append("health dialog and keyboard")

                page.locator("#demo-button").click()
                expect(page.locator(".garment-card")).to_have_count(3)
                expect(page.locator(".card-sample")).to_have_count(3)
                page.locator("[data-item-id]").first.click()
                page.locator("#item-category").select_option("top")
                page.locator("#item-subtype").fill("測試襯衫")
                page.locator("#item-brand").fill("瀏覽器驗證")
                page.locator("#item-size_label").fill("3Y")
                page.get_by_role("button", name="儲存並確認", exact=True).click()
                expect(page.locator("#item-dialog")).not_to_be_visible()
                expect(page.locator("#reviewed-count")).to_have_text("1")
                page.reload(wait_until="networkidle")
                expect(page.locator("#reviewed-count")).to_have_text("1")
                page.locator("#search-input").fill("瀏覽器驗證")
                page.locator("#category-filter").select_option("top")
                page.locator('.review-tabs [data-review="reviewed"]').click()
                expect(page.locator(".garment-card")).to_have_count(1)
                checks.extend(
                    [
                        "historical demo labels",
                        "edit/review persistence",
                        "combined search/category/review filters",
                    ]
                )

                page.locator(".export-menu summary").click()
                with page.expect_download() as download:
                    page.get_by_role("link", name="CSV 試算表").click()
                download.value.save_as(output / "catalogue.csv")
                rows = list(
                    csv.DictReader(io.StringIO((output / "catalogue.csv").read_text(encoding="utf-8-sig")))
                )
                assert len(rows) == 3 and any(row["brand"] == "瀏覽器驗證" for row in rows)
                with page.expect_download() as download:
                    page.get_by_role("link", name="JSON 完整資料").click()
                download.value.save_as(output / "catalogue.json")
                assert len(json.loads((output / "catalogue.json").read_text())["items"]) == 3
                checks.append("CSV and JSON downloads")
                page.locator(".export-menu summary").click()

                page.locator("#search-input").fill("")
                page.locator("#category-filter").select_option("")
                page.locator('.review-tabs [data-review="all"]').click()
                expect(page.locator(".garment-card")).to_have_count(3)
                page.screenshot(path=output / "desktop-catalogue.png", full_page=True, animations="disabled")
                page.locator('[data-view="history"]').click()
                page.locator(".job-row").first.click()
                expect(page.locator("#job-dialog")).to_contain_text("沒有執行新的模型")
                expect(page.locator("#job-media-content img")).to_be_visible()
                page.screenshot(path=output / "desktop-job.png", full_page=False, animations="disabled")
                page.keyboard.press("Escape")
                page.locator('[data-view="wardrobe"]').click()
                checks.append("history/source preview")

                page.set_viewport_size({"width": 390, "height": 844})
                page.screenshot(path=output / "mobile-catalogue.png", full_page=True, animations="disabled")
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                page.locator("[data-item-id]").first.click()
                expect(page.locator("#item-dialog")).to_be_visible()
                page.screenshot(path=output / "mobile-editor.png", full_page=False, animations="disabled")
                assert page.locator("#item-dialog").evaluate("e => e.scrollWidth <= e.clientWidth + 1")
                page.locator("#item-brand").fill('<img src=x onerror="alert(1)">')
                page.get_by_role("button", name="儲存變更", exact=True).click()
                expect(page.locator("#item-dialog")).not_to_be_visible()
                expect(page.locator("img[src=x]")).to_have_count(0)
                page.locator("[data-item-id]").first.click()
                expect(page.locator("#item-brand")).to_have_value('<img src=x onerror="alert(1)">')
                page.locator("#delete-item-button").click()
                expect(page.locator("#confirm-dialog")).to_be_visible()
                page.locator("#confirm-cancel").click()
                expect(page.locator("#item-dialog")).to_be_visible()
                page.locator("#delete-item-button").click()
                page.locator("#confirm-submit").click()
                expect(page.locator("#item-dialog")).not_to_be_visible()
                expect(page.locator(".garment-card")).to_have_count(2)
                checks.extend(
                    [
                        "390px layout without overflow",
                        "mobile edit/delete",
                        "HTML content escaped",
                        "delete confirmation",
                    ]
                )

                page.locator("#file-input").set_input_files(
                    {"name": "unsupported.txt", "mimeType": "text/plain", "buffer": b"hello"}
                )
                expect(page.locator(".queue-item .queue-error")).to_contain_text("不支援此格式")
                page.get_by_role("button", name="移除 unsupported.txt", exact=True).click()
                expect(page.locator(".queue-item")).to_have_count(0)
                page.locator("#file-input").set_input_files(
                    {"name": "broken.jpg", "mimeType": "image/jpeg", "buffer": b"invalid jpeg"}
                )
                page.locator("#upload-button").click()
                expect(page.locator("#upload-button")).to_be_enabled()
                page.locator('[data-view="history"]').click()
                expect(page.locator(".job-row").filter(has_text="broken.jpg")).to_contain_text(
                    "處理失敗", timeout=15000
                )
                page.locator(".job-row").filter(has_text="broken.jpg").click()
                expect(page.locator('[data-job-action="retry"]')).to_be_visible()
                page.locator('[data-job-action="retry"]').click()
                expect(page.locator("#job-dialog")).to_contain_text("處理失敗", timeout=15000)
                page.keyboard.press("Escape")
                checks.extend(
                    ["unsupported file error", "real malformed-media failure", "retry creates a new job"]
                )

                assert not errors, errors
                assert not console_errors, console_errors
                assert not bad_responses, bad_responses
                report = {
                    "status": "passed",
                    "checks": checks,
                    "viewports": [[1440, 1000], [390, 844]],
                    "page_errors": errors,
                    "console_errors": console_errors,
                    "bad_responses": bad_responses,
                    "network": "loopback only; bundled webapp-testing policy; service workers blocked",
                    "inference": "none; historical fixture and malformed upload only",
                }
                (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
                print(json.dumps(report, ensure_ascii=False), flush=True)
                context.close()
                browser.close()
        finally:
            server.terminate()
            try:
                server.wait(timeout=20)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)


if __name__ == "__main__":
    main()
