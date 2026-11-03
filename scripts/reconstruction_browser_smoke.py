"""Reconstruction UI integration with real temporary API/SQLite and explicit fake models.

The fixture writes a real PNG; it does not contact Meta or claim image fidelity.
Original user data and the main server are never accessed.
"""

import argparse
import asyncio
import copy
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import httpx
from PIL import Image, ImageDraw
from playwright.sync_api import expect, sync_playwright

from lifestyle_browser_smoke import LifestyleFixtures, file_payload, no_overflow, wait_batch


ROOT = Path(__file__).resolve().parent.parent


class ReconstructionFixtures(LifestyleFixtures):
    """Scripted latency and failures; only HTTP, storage and UI are under test."""

    def __init__(self):
        super().__init__()
        self.reconstruction_calls = {}

    async def reconstruct(self, cutout_path, context_path, record, destination):
        assert Path(cutout_path).is_file(), "Fixture requires a real reference crop"
        assert Path(context_path).is_file(), "Fixture requires real source context"
        subtype = record.get("subtype", "")
        call = self.reconstruction_calls.get(subtype, 0) + 1
        self.reconstruction_calls[subtype] = call
        await asyncio.sleep(20 if subtype == "fixture-cancel" else 4)
        if subtype == "fixture-retry" and call == 1:
            from wardrobe.providers import ProviderError

            raise ProviderError("明確測試用：重建服務第一次失敗，請重新生成。")
        image = Image.new("RGB", (640, 800), "#faf8f0")
        draw = ImageDraw.Draw(image)
        draw.polygon(
            [
                (246, 160),
                (170, 205),
                (94, 350),
                (195, 398),
                (235, 320),
                (235, 625),
                (405, 625),
                (405, 320),
                (445, 398),
                (546, 350),
                (470, 205),
                (394, 160),
            ],
            fill="#294758",
        )
        draw.arc((270, 130, 370, 210), 0, 180, fill="#faf8f0", width=14)
        draw.text((135, 730), "AI RECONSTRUCTION - EXPLICIT TEST FIXTURE", fill="#435740")
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination, "PNG")
        return destination


def serve(directory, port):
    os.environ["REWEAR_DATA_DIR"] = str(directory)
    sys.path.insert(0, str(ROOT))
    import uvicorn
    from wardrobe.app import create_app
    from wardrobe.config import Settings

    uvicorn.run(
        create_app(Settings(data_dir=directory, api_key="fixture-no-credential"), ReconstructionFixtures()),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )


def original_attributes(item):
    return copy.deepcopy({key: item[key] for key in ("record", "quality", "reviewed")})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("out/verification/reconstruction-browser"))
    parser.add_argument(
        "--policy-dir", type=Path, default=Path.home() / ".agents/skills/webapp-testing/scripts"
    )
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        serve(args.data_dir, args.port)
        return
    sys.path.insert(0, str(args.policy_dir))
    from network_policy import LocalNetworkPolicy, install_network_policy

    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    report = {
        "status": "running",
        "scenarios": [],
        "page_errors": [],
        "environment": "Temporary API/SQLite; real upload, scheduling, PNG/media and mutations; explicitly fake SAM/inspection/reconstruction",
        "network": "loopback-only policy installed before pages; service workers blocked; no main catalogue or model calls",
    }
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="rewear-reconstruction-browser-") as temporary:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", temporary),
            "LANG": "en_US.UTF-8",
            "REWEAR_DATA_DIR": temporary,
            "PYTHONUNBUFFERED": "1",
        }
        with (output / "server.log").open("w") as log:
            server = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--serve",
                    "--data-dir",
                    temporary,
                    "--port",
                    str(port),
                ],
                cwd=ROOT,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                for _ in range(80):
                    try:
                        if httpx.get(url + "/api/health", timeout=1).status_code == 200:
                            break
                    except httpx.RequestError:
                        pass
                    if server.poll() is not None:
                        raise RuntimeError("Temporary server exited; see server.log")
                    time.sleep(0.2)
                else:
                    raise RuntimeError("Temporary server did not start")
                with httpx.Client(base_url=url, timeout=30) as client, sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    context = browser.new_context(
                        viewport={"width": 1440, "height": 1000}, service_workers="block"
                    )
                    install_network_policy(context, LocalNetworkPolicy.for_http(url))
                    page = context.new_page()
                    page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
                    page.set_default_timeout(15000)
                    expect.set_options(timeout=15000)
                    page.goto(url, wait_until="networkidle")
                    shared = {}

                    def screenshot(name):
                        full_page = not page.locator("dialog[open]").count()
                        if full_page:
                            page.evaluate("window.scrollTo({top:0,behavior:'instant'})")
                        page.screenshot(
                            path=output / name,
                            full_page=full_page,
                            animations="disabled",
                        )

                    def check(name, function):
                        entry = {"name": name}
                        try:
                            entry.update(status="passed", details=function() or {})
                        except Exception as error:
                            entry.update(status="failed", error=str(error), traceback=traceback.format_exc())
                            screenshot(f"failure-{len(report['scenarios']) + 1}.png")
                            raise
                        finally:
                            report["scenarios"].append(entry)
                            (output / "report.json").write_text(
                                json.dumps(report, ensure_ascii=False, indent=2)
                            )
                            print(json.dumps(entry, ensure_ascii=False), flush=True)

                    def refresh():
                        page.locator('[data-view="history"]').click()
                        page.locator("#history-refresh").click()
                        expect(page.locator("#history-refresh")).to_be_enabled()
                        page.locator('[data-view="wardrobe"]').click()

                    def upload(name, automatic=True):
                        page.locator('input[name="mode"][value="outfit"]').check()
                        page.locator("#upload-reconstruct").set_checked(automatic)
                        page.locator("#file-input").set_input_files(file_payload(name))
                        with page.expect_response(
                            lambda r: r.url == url + "/api/batches" and r.request.method == "POST"
                        ) as response:
                            page.locator("#upload-button").click()
                        result = response.value
                        assert result.status == 202, result.text()
                        batch = wait_batch(client, result.json()["id"])
                        assert batch["counts"]["completed"] == 1, batch
                        item = next(
                            item
                            for item in client.get("/api/items").json()["items"]
                            if item["job_id"] == batch["jobs"][0]["id"]
                        )
                        expect(page.locator("#upload-button")).to_be_enabled()
                        refresh()
                        return item

                    def item_now(ident):
                        response = client.get(f"/api/items/{ident}")
                        response.raise_for_status()
                        return response.json()

                    def open_item(ident):
                        page.locator(f'[data-item-id="{ident}"]').click()
                        expect(page.locator("#item-dialog")).to_be_visible()

                    def reconstruction_click(ident, action="generate", keyboard=False):
                        path = f"/api/items/{ident}/reconstruction" + (
                            "/cancel" if action == "cancel" else ""
                        )
                        button = page.locator(f'[data-reconstruction-action="{action}"]')
                        with page.expect_response(
                            lambda r: r.url == url + path and r.request.method == "POST"
                        ) as response:
                            if keyboard:
                                button.focus()
                                page.keyboard.press("Enter")
                            else:
                                button.click()
                        result = response.value
                        assert result.status == (200 if action == "cancel" else 202), result.text()
                        if keyboard:
                            next_action = "generate" if action == "cancel" else "cancel"
                            expect(
                                page.locator(f'[data-reconstruction-action="{next_action}"]')
                            ).to_be_focused()
                        return result.json()

                    def automatic_and_edit_preservation():
                        expect(page.locator("#upload-reconstruct")).to_be_checked()
                        item = upload("automatic-outfit.jpg")
                        ident = item["id"]
                        shared["automatic"] = ident
                        assert item["reconstruction"]["status"] in {"queued", "running"}, item
                        before = original_attributes(item)
                        open_item(ident)
                        expect(page.locator('[data-reconstruction-action="cancel"]')).to_be_visible()
                        page.locator("#item-brand").fill("尚未儲存的編輯 🧵")
                        page.locator("#item-defects").fill("等待生成時輸入，輪詢應保留。")
                        expect(page.locator(".reconstruction-status")).to_have_text("完整展示圖已生成")
                        expect(page.locator("#item-brand")).to_have_value("尚未儲存的編輯 🧵")
                        expect(page.locator("#item-defects")).to_have_value("等待生成時輸入，輪詢應保留。")
                        expect(page.locator("#item-defects")).to_be_focused()
                        after = item_now(ident)
                        assert original_attributes(after) == before
                        assert after["reconstruction"]["is_synthetic"] is True
                        image_response = client.get(after["reconstruction"]["image_url"])
                        assert image_response.status_code == 200 and image_response.content.startswith(
                            b"\x89PNG"
                        )
                        page.locator('#item-media-region [data-item-media-key="reconstruction"]').click()
                        expect(page.locator("#item-media-caption")).to_contain_text(
                            "遮住的部分由 AI 推測，請核對款式與細節"
                        )
                        expect(page.locator("#item-media-caption")).to_contain_text("不是原始照片的證據")
                        expect(page.locator("#item-media-content img")).to_have_attribute(
                            "src", url + after["reconstruction"]["image_url"]
                        )
                        for key, path in [
                            ("context", after["image_url"]),
                            ("source", after["original_url"]),
                            ("cutout", after["cutout_url"]),
                        ]:
                            page.locator(f'#item-media-region [data-item-media-key="{key}"]').click()
                            expect(page.locator("#item-media-content img")).to_have_attribute(
                                "src", url + path
                            )
                        page.locator('#item-media-region [data-item-media-key="reconstruction"]').click()
                        page.locator("#item-dialog").evaluate("e => { e.scrollTop = 0; }")
                        screenshot("desktop-reconstruction-editor.png")
                        page.keyboard.press("Escape")
                        card = page.locator(f'[data-item-id="{ident}"]')
                        expect(card.locator(".card-reconstruction")).to_have_text("AI 重建圖")
                        expect(card.locator(".garment-image img")).to_have_attribute(
                            "src", url + after["reconstruction"]["image_url"]
                        )
                        screenshot("desktop-reconstruction-card.png")
                        return {
                            "automatic_queue": True,
                            "PNG": "actual fixture PNG served",
                            "draft_preserved": True,
                            "record_quality_reviewed_unchanged": True,
                            "original_context_source_cutout_retained": True,
                        }

                    def opt_out_and_manual_keyboard():
                        item = upload("manual-only-outfit.jpg", automatic=False)
                        ident = item["id"]
                        shared["manual"] = ident
                        assert item.get("reconstruction") is None, item
                        before = original_attributes(item)
                        card = page.locator(f'[data-item-id="{ident}"]')
                        expect(card.locator(".garment-image img")).to_have_attribute(
                            "src", url + item["image_url"]
                        )
                        expect(card.locator(".card-reconstruction")).to_have_count(0)
                        open_item(ident)
                        expect(page.locator('[data-reconstruction-action="generate"]')).to_have_text(
                            "生成完整衣物圖"
                        )
                        reconstruction_click(ident, keyboard=True)
                        expect(page.locator(".reconstruction-status")).to_have_text("完整展示圖已生成")
                        assert original_attributes(item_now(ident)) == before
                        page.keyboard.press("Escape")
                        expect(card.locator(".card-reconstruction")).to_be_visible()
                        return {
                            "opt_out": "no automatic reconstruction",
                            "manual": "keyboard Enter starts generation",
                            "original_attributes": "unchanged",
                        }

                    def duplicates_use_evidence():
                        items = [item_now(shared[key]) for key in ("automatic", "manual")]
                        refresh()
                        page.locator("#duplicates-button").click()
                        expect(page.locator(".duplicate-pair")).to_have_count(1)
                        thumbnails = page.locator(".duplicate-thumbnails img")
                        assert sorted(thumbnails.evaluate_all("nodes => nodes.map(n => n.src)")) == sorted(
                            url + item["image_url"] for item in items
                        )
                        page.locator(".duplicate-pair").click()
                        candidate_sources = page.locator(".merge-candidate img").evaluate_all(
                            "nodes => nodes.map(n => n.src)"
                        )
                        assert sorted(candidate_sources) == sorted(url + item["image_url"] for item in items)
                        assert not any(
                            item["reconstruction"]["image_url"] in source
                            for item in items
                            for source in candidate_sources
                        )
                        screenshot("duplicate-review-original-evidence.png")
                        page.keyboard.press("Escape")
                        return {"list_and_pair_review": "real context crops, never synthetic reconstruction"}

                    def failure_retry_preserves_previous():
                        ident = shared["automatic"]
                        response = client.patch(
                            f"/api/items/{ident}", json={"record": {"subtype": "fixture-retry"}}
                        )
                        response.raise_for_status()
                        before = original_attributes(response.json())
                        previous_image = response.json()["reconstruction"]["image_url"]
                        refresh()
                        open_item(ident)
                        reconstruction_click(ident)
                        expect(page.locator(".reconstruction-status")).to_have_text("展示圖生成失敗")
                        expect(page.locator("#item-reconstruction .inline-error")).to_contain_text(
                            "明確測試用"
                        )
                        expect(page.locator(".reconstruction-previous")).to_contain_text("保留上次")
                        failed = item_now(ident)
                        assert failed["reconstruction"]["image_url"] == previous_image
                        assert original_attributes(failed) == before
                        expect(page.locator("#item-media-content img")).to_have_attribute(
                            "src", url + previous_image
                        )
                        screenshot("reconstruction-failure-retains-image.png")
                        reconstruction_click(ident)
                        expect(page.locator(".reconstruction-status")).to_have_text("完整展示圖已生成")
                        assert original_attributes(item_now(ident)) == before
                        expect(page.locator("#item-reconstruction .inline-error")).to_have_count(0)
                        page.keyboard.press("Escape")
                        return {
                            "failure": "error surfaced with previous image retained",
                            "retry": "successful",
                            "record_quality_reviewed": "unchanged through failure and retry",
                        }

                    def cancellation_preserves_previous():
                        ident = shared["automatic"]
                        response = client.patch(
                            f"/api/items/{ident}", json={"record": {"subtype": "fixture-cancel"}}
                        )
                        response.raise_for_status()
                        before = original_attributes(response.json())
                        image = response.json()["reconstruction"]["image_url"]
                        refresh()
                        open_item(ident)
                        reconstruction_click(ident)
                        expect(page.locator(".reconstruction-status")).to_contain_text("生成完整展示圖")
                        reconstruction_click(ident, "cancel")
                        expect(page.locator(".reconstruction-status")).to_have_text("已取消這次生成")
                        cancelled = item_now(ident)
                        assert cancelled["reconstruction"]["status"] == "cancelled"
                        assert cancelled["reconstruction"]["image_url"] == image
                        assert original_attributes(cancelled) == before
                        screenshot("reconstruction-cancelled-retains-image.png")
                        page.keyboard.press("Escape")
                        return {
                            "cancel": "active generation stopped",
                            "previous_image": "retained",
                            "attributes": "unchanged",
                        }

                    def mobile_keyboard_and_media():
                        ident = shared["automatic"]
                        page.set_viewport_size({"width": 390, "height": 844})
                        open_item(ident)
                        no_overflow(page, "#item-dialog")
                        no_overflow(page)
                        expect(page.locator(".reconstruction-disclaimer")).to_contain_text(
                            "遮住的部分由 AI 推測"
                        )
                        for key in ("context", "source", "cutout", "reconstruction"):
                            button = page.locator(f'#item-media-region [data-item-media-key="{key}"]')
                            button.focus()
                            page.keyboard.press("Enter")
                            expect(button).to_have_attribute("aria-pressed", "true")
                        reconstruction_click(ident, keyboard=True)
                        reconstruction_click(ident, "cancel", keyboard=True)
                        expect(page.locator(".reconstruction-status")).to_have_text("已取消這次生成")
                        no_overflow(page, "#item-dialog")
                        page.locator("#item-reconstruction").scroll_into_view_if_needed()
                        screenshot("mobile-reconstruction-controls.png")
                        page.locator("#item-dialog").evaluate("e => { e.scrollTop = 0; }")
                        screenshot("mobile-reconstruction-editor.png")
                        page.keyboard.press("Escape")
                        screenshot("mobile-reconstruction-cards.png")
                        return {
                            "viewport": [390, 844],
                            "keyboard": "all media tabs, generate and cancel",
                            "horizontal_overflow": False,
                        }

                    def unavailable_service_is_clear():
                        health = client.get("/api/health").json()
                        health["reconstruction"] = {
                            "available": False,
                            "automatic_outfit": True,
                            "model": "muse-image-1.0",
                        }

                        def unavailable(route):
                            route.fulfill(status=200, json=health)

                        context.route(url + "/api/health", unavailable)
                        try:
                            page.reload(wait_until="networkidle")
                            open_item(shared["manual"])
                            expect(page.locator('[data-reconstruction-action="generate"]')).to_be_disabled()
                            expect(page.locator(".reconstruction-explanation")).to_contain_text(
                                "請設定服務憑證"
                            )
                            expect(
                                page.locator('#item-media-region [data-item-media-key="reconstruction"]')
                            ).to_be_visible()
                            page.keyboard.press("Escape")
                        finally:
                            context.unroute(url + "/api/health", unavailable)
                        return {
                            "explicit_health_fixture": "unavailable",
                            "generation": "disabled with explanation",
                            "existing_image": "still accessible",
                        }

                    try:
                        check(
                            "automatic outfit reconstruction and independent polling preserve edits",
                            automatic_and_edit_preservation,
                        )
                        check(
                            "opt out, real image fallback and keyboard manual generation",
                            opt_out_and_manual_keyboard,
                        )
                        check("duplicate comparison keeps original evidence", duplicates_use_evidence)
                        check(
                            "generation failure and retry preserve image and attributes",
                            failure_retry_preserves_previous,
                        )
                        check(
                            "active cancellation preserves existing image and source facts",
                            cancellation_preserves_previous,
                        )
                        check(
                            "390px keyboard generation, cancellation and media tabs",
                            mobile_keyboard_and_media,
                        )
                        check(
                            "unavailable generation service is shown honestly", unavailable_service_is_clear
                        )
                    finally:
                        context.close()
                        browser.close()
            except Exception as error:
                report["error"] = str(error)
                report["traceback"] = traceback.format_exc()
            finally:
                server.terminate()
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
    failed = [entry for entry in report["scenarios"] if entry["status"] != "passed"]
    report["status"] = (
        "passed"
        if len(report["scenarios"]) == 7
        and not failed
        and not report["page_errors"]
        and "error" not in report
        else "failed"
    )
    report["summary"] = {
        "passed": len(report["scenarios"]) - len(failed),
        "failed": len(failed),
        "expected": 7,
    }
    report["temporary_server_stopped"] = True
    report["temporary_data_removed"] = True
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {"status": report["status"], "summary": report["summary"], "report": str(output / "report.json")}
        ),
        flush=True,
    )
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
