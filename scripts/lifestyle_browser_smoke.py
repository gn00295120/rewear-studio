"""Lifestyle UI integration against temporary real API/SQLite and explicit model fixtures.

No existing catalogue or model service is contacted. Generated mannequin images,
segmentation boxes and inspection answers are labeled fixtures; uploads, EXIF
normalization, selection crops, batches, similarity, grouping and exports are real.
"""

import argparse
import asyncio
import copy
import io
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


ROOT = Path(__file__).resolve().parent.parent
RECORD = {
    "category": "top",
    "subtype": "tshirt",
    "brand": None,
    "size_label": None,
    "colors": ["navy"],
    "pattern": "solid",
    "material_guess": None,
    "condition": "unknown",
    "defects": [],
    "confidence": 0.9,
    "needs_review": True,
}


class LifestyleFixtures:
    """Scripted responses exercise UI state, without making accuracy claims."""

    def __init__(self):
        self.failed_once = False
        self.slow_calls = 0

    async def health(self):
        return {
            "local": {"available": True, "model": "explicit-lifestyle-browser-fixture"},
            "meta": {"configured": True},
        }

    async def close(self):
        pass

    async def segment(self, path, concept, video=False):
        if concept == "fixture-slow":
            self.slow_calls += 1
            await asyncio.sleep(10 if self.slow_calls == 1 else 1)
        if concept == "fixture-fail-once" and not self.failed_once:
            from wardrobe.providers import ProviderError

            self.failed_once = True
            raise ProviderError("明確測試用：第一次分割失敗，重試可恢復。")
        with Image.open(path) as source:
            width, height = source.size
        return [
            {
                "kind": "box",
                "object_id": "explicit-shirt-fixture",
                "frame": None,
                "left": int(width * 0.15),
                "top": int(height * 0.18),
                "right": int(width * 0.85),
                "bottom": int(height * 0.7),
            }
        ]

    async def inspect(self, path, provider, *, context_path=None, wearing=False):
        return {
            "garment": copy.deepcopy(RECORD),
            "target": "one_garment",
            "visibility": "partial" if wearing else "complete",
            "reason": "明確測試用的穿著局部可見回應，不是模型推論。",
        }

    async def extract(self, path, provider):
        return copy.deepcopy(RECORD)


def serve(directory, port):
    os.environ["REWEAR_DATA_DIR"] = str(directory)
    sys.path.insert(0, str(ROOT))
    import uvicorn
    from wardrobe.app import create_app
    from wardrobe.config import Settings

    uvicorn.run(
        create_app(Settings(data_dir=directory, api_key="fixture-no-credential"), LifestyleFixtures()),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )


def photo(exif=False):
    image = Image.new("RGB", (640, 800), "#e8e7df")
    draw = ImageDraw.Draw(image)
    draw.ellipse((265, 60, 375, 170), fill="#bba382")
    draw.polygon(
        [
            (252, 175),
            (185, 211),
            (125, 338),
            (204, 377),
            (242, 320),
            (242, 526),
            (398, 526),
            (398, 320),
            (436, 377),
            (515, 338),
            (455, 211),
            (388, 175),
        ],
        fill="#294758",
    )
    draw.polygon(
        [(250, 525), (390, 525), (414, 725), (337, 725), (320, 580), (301, 725), (224, 725)], fill="#677064"
    )
    draw.text((244, 763), "EXPLICIT TEST FIXTURE", fill="#364b3a")
    buffer = io.BytesIO()
    if exif:
        # Stored landscape pixels + Orientation 6 display as the upright portrait.
        rotated = image.transpose(Image.Transpose.ROTATE_90)
        metadata = Image.Exif()
        metadata[274] = 6
        rotated.save(buffer, "JPEG", quality=92, exif=metadata)
    else:
        image.save(buffer, "JPEG", quality=92)
    return buffer.getvalue()


def file_payload(name, exif=False):
    return {"name": name, "mimeType": "image/jpeg", "buffer": photo(exif)}


def wait_batch(client, ident, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/batches/{ident}")
        response.raise_for_status()
        result = response.json()
        if result["status"] not in {"queued", "running"}:
            return result
        time.sleep(0.05)
    raise AssertionError(f"Batch did not settle: {result}")


def no_overflow(page, selector="html"):
    value = page.locator(selector).evaluate("e => ({width:e.clientWidth, scroll:e.scrollWidth})")
    assert value["scroll"] <= value["width"] + 1, value
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("out/verification/lifestyle-browser"))
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
        "environment": "TemporaryDirectory API/SQLite; no user data; explicit fake inference; real HTTP/media/batches/grouping",
        "network": "loopback-only bundled network policy installed before pages; service workers blocked",
    }
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="rewear-lifestyle-browser-") as temporary:
        directory = Path(temporary)
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", temporary),
            "LANG": "en_US.UTF-8",
            "REWEAR_DATA_DIR": str(directory),
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
                        viewport={"width": 1440, "height": 1000}, service_workers="block", has_touch=True
                    )
                    install_network_policy(context, LocalNetworkPolicy.for_http(url))
                    page = context.new_page()
                    page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
                    page.set_default_timeout(10000)
                    page.goto(url, wait_until="networkidle")
                    shared = {}

                    def screenshot(name, full=False):
                        if full:
                            page.evaluate("window.scrollTo({top:0,behavior:'instant'})")
                        page.screenshot(path=output / name, full_page=full, animations="disabled")

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

                    def refresh(view="wardrobe"):
                        page.locator('[data-view="history"]').click()
                        page.locator("#history-refresh").click()
                        expect(page.locator("#history-refresh")).to_be_enabled()
                        if view == "wardrobe":
                            page.locator('[data-view="wardrobe"]').click()

                    def submit_batch():
                        with page.expect_response(
                            lambda response: (
                                response.url == url + "/api/batches" and response.request.method == "POST"
                            )
                        ) as response:
                            page.locator("#upload-button").click()
                        value = response.value
                        assert value.status == 202, value.text()
                        return value.json()

                    def queue_and_select():
                        page.locator('input[name="mode"][value="outfit"]').check()
                        page.locator("#file-input").set_input_files(
                            [file_payload(f"limit-fixture-{index}.jpg") for index in range(33)]
                        )
                        expect(page.locator(".queue-item")).to_have_count(32)
                        expect(page.locator("#upload-error")).to_contain_text("另有 1 個未加入")
                        page.locator("[data-remove-file]").first.click()
                        expect(page.locator(".queue-item")).to_have_count(31)
                        page.locator("#clear-file").click()
                        page.locator("#file-input").set_input_files(
                            {"name": "empty-photo.jpg", "mimeType": "image/jpeg", "buffer": b""}
                        )
                        expect(page.locator(".queue-error")).to_contain_text("檔案是空的")
                        page.locator("#upload-button").click()
                        expect(page.locator("#upload-error")).to_contain_text("請先移除")
                        assert client.get("/api/batches").json()["batches"] == []
                        page.locator("#clear-file").click()
                        page.locator("#file-input").set_input_files(
                            [
                                file_payload("orientation-6-fixture.jpg", True),
                                file_payload("another-day-fixture.jpg"),
                            ]
                        )
                        expect(page.locator(".queue-item")).to_have_count(2)
                        page.locator("[data-person-file]").first.click()
                        image = page.locator("#person-photo")
                        expect(image).to_be_visible()
                        image.evaluate("image => image.decode()")
                        assert image.evaluate("image => [image.naturalWidth,image.naturalHeight]") == [
                            640,
                            800,
                        ]
                        bounds = page.locator("#person-stage").bounding_box()
                        page.mouse.move(
                            bounds["x"] + bounds["width"] * 0.15, bounds["y"] + bounds["height"] * 0.1
                        )
                        page.mouse.down()
                        page.mouse.move(
                            bounds["x"] + bounds["width"] * 0.85,
                            bounds["y"] + bounds["height"] * 0.9,
                            steps=12,
                        )
                        page.mouse.up()
                        expect(page.locator("#person-box")).to_be_visible()
                        assert abs(float(page.locator("#person-left").input_value()) - 15) < 0.5
                        screenshot("desktop-person-selection.png")
                        page.locator("#person-save").click()
                        page.locator("[data-person-file]").nth(1).click()
                        page.locator("#person-default").click()
                        page.keyboard.press("ArrowRight")
                        assert float(page.locator("#person-left").input_value()) == 21
                        page.locator("#person-clear").click()
                        expect(page.locator("#person-box")).not_to_be_visible()
                        for edge, value in zip(("left", "top", "right", "bottom"), (15, 10, 85, 90)):
                            page.locator(f"#person-{edge}").fill(str(value))
                        page.locator("#person-save").click()
                        expect(page.locator(".queue-info").filter(has_text="已框選人物")).to_have_count(2)
                        no_overflow(page)
                        screenshot("desktop-two-photo-queue.png", True)
                        return {
                            "queue": 2,
                            "queue_limits": "33rd file rejected, individual removal works, empty file blocked before POST",
                            "selection": "real pointer drag plus keyboard fields/arrow/clear",
                            "EXIF_orientation_6": "browser preview 640x800, selection uses normalized axes",
                        }

                    def upload_and_confirmation():
                        submitted = submit_batch()
                        batch = wait_batch(client, submitted["id"])
                        assert batch["status"] == "completed" and batch["job_count"] == 2, batch
                        items = client.get("/api/items").json()["items"]
                        assert len(items) == 2
                        assert all(
                            item["quality"]["status"] == "needs_confirmation"
                            and item["record"]["category"] == "top"
                            and item["record"]["colors"]
                            for item in items
                        )
                        jobs = [client.get(f"/api/jobs/{job['id']}").json() for job in batch["jobs"]]
                        for job in jobs:
                            assert job["selection_url"] and job["selection_bbox"], job
                            assert all(
                                abs(actual - expected) <= 2
                                for actual, expected in zip(job["selection_bbox"], (96, 80, 544, 720))
                            ), job["selection_bbox"]
                        shared["items"], shared["first_batch"] = items, batch
                        refresh()
                        expect(page.locator("#total-count")).to_have_text("2")
                        expect(page.locator("#usable-count")).to_have_text("0")
                        expect(page.locator("#confirmation-count")).to_have_text("2")
                        expect(page.locator(".result-group.confirmation .garment-card")).to_have_count(2)
                        expect(page.locator(".result-group.confirmation")).to_contain_text("navy")
                        page.locator(".card-button").first.click()
                        page.locator('[data-item-media-key="source"]').click()
                        original = page.locator("#item-media-content img").get_attribute("src")
                        assert original.endswith("preview.jpg")
                        page.locator('[data-item-media-key="selected"]').click()
                        assert (
                            page.locator("#item-media-content img")
                            .get_attribute("src")
                            .endswith("selected.jpg")
                        )
                        page.keyboard.press("Escape")
                        screenshot("desktop-outfit-confirmation.png", True)
                        return {
                            "batch_jobs": 2,
                            "confirmation": 2,
                            "usable": 0,
                            "selection_bbox": [job["selection_bbox"] for job in jobs],
                            "full_original_and_selected_media": True,
                        }

                    def review_visible_details():
                        primary = shared["items"][0]["id"]
                        shared["primary"] = primary
                        page.locator(f'[data-item-id="{primary}"]').click()
                        expect(page.locator("#item-colors")).to_have_value("navy")
                        expect(page.locator(".outfit-assessment")).to_contain_text("可見")
                        page.get_by_role("button", name="儲存並確認", exact=True).click()
                        expect(page.locator("#item-dialog")).not_to_be_visible()
                        expect(page.locator("#usable-count")).to_have_text("1")
                        expect(page.locator("#confirmation-count")).to_have_text("1")
                        page.locator("#confirmation-filter").click()
                        expect(page.locator(".garment-card")).to_have_count(1)
                        page.locator('.review-tabs [data-review="all"]').click()
                        updated = next(
                            item for item in client.get("/api/items").json()["items"] if item["id"] == primary
                        )
                        assert updated["reviewed"] and updated["quality"]["status"] == "needs_confirmation"
                        return {
                            "human_review": "adds usable garment; original quality retained",
                            "confirmation_filter": 1,
                        }

                    def merge_and_undo():
                        refresh()
                        suggestions = client.get("/api/duplicates").json()["suggestions"]
                        assert suggestions, (
                            "Real similarity endpoint should suggest the matching fixture records"
                        )
                        page.locator("#duplicates-button").click()
                        page.locator("[data-suggestion-id]").first.click()
                        expect(page.locator("#merge-submit")).to_be_disabled()
                        page.locator(f'input[name="primary_item_id"][value="{shared["primary"]}"]').check()
                        page.locator("#merge-confirm").check()
                        screenshot("desktop-pair-review.png")
                        page.locator("#merge-submit").click()
                        expect(page.locator("#item-dialog")).to_be_visible()
                        expect(page.locator(".source-strip [data-group-item]")).to_have_count(2)
                        group = client.get("/api/garment-groups").json()["groups"][0]
                        assert group["primary_item_id"] == shared["primary"] and len(group["item_ids"]) == 2
                        assert len(client.get("/api/items").json()["items"]) == 2
                        expect(page.locator("#total-count")).to_have_text("2")
                        expect(page.locator("#usable-count")).to_have_text("1")
                        other = next(ident for ident in group["item_ids"] if ident != shared["primary"])
                        page.locator(f'[data-group-item="{other}"]').click()
                        expect(page.locator("#item-reviewed")).not_to_be_checked()
                        expect(page.locator(".outfit-assessment")).to_contain_text("尚未列入")
                        screenshot("desktop-group-source-review.png")
                        page.keyboard.press("Escape")
                        expect(page.locator(".garment-card")).to_have_count(1)
                        expect(page.locator(".card-sources")).to_contain_text("2 張來源")
                        exported = client.get("/api/export?format=json").json()
                        assert len(exported["items"]) == 2 and len(exported["groups"]) == 1
                        page.locator(".card-button").click()
                        page.locator("[data-ungroup]").click()
                        expect(page.locator("#item-dialog")).not_to_be_visible()
                        expect(page.locator(".garment-card")).to_have_count(2)
                        assert client.get("/api/garment-groups").json()["groups"] == []
                        return {
                            "merge": "explicit checkbox and primary choice",
                            "records_preserved": 2,
                            "source_switch_reviewed_separately": True,
                            "export_all_evidence": True,
                            "undo": True,
                        }

                    def partial_error_and_retry():
                        page.locator("#file-input").set_input_files(
                            [
                                file_payload("valid-with-invalid-neighbours.jpg"),
                                {
                                    "name": "unsupported-note.txt",
                                    "mimeType": "text/plain",
                                    "buffer": b"explicit invalid fixture",
                                },
                                {"name": "empty-neighbour.jpg", "mimeType": "image/jpeg", "buffer": b""},
                            ]
                        )
                        expect(page.locator(".queue-item")).to_have_count(3)
                        expect(page.locator(".queue-error")).to_have_count(2)
                        mixed = wait_batch(client, submit_batch()["id"])
                        assert mixed["job_count"] == 1 and mixed["counts"]["completed"] == 1
                        assert [(error["index"], error["filename"]) for error in mixed["errors"]] == [
                            (1, "unsupported-note.txt"),
                            (2, "empty-neighbour.jpg"),
                        ]
                        expect(page.locator("#upload-button")).to_be_enabled()
                        expect(page.locator(".queue-item")).to_have_count(2)
                        expect(page.locator(".queue-error").nth(0)).to_contain_text("不支援")
                        expect(page.locator(".queue-error").nth(1)).to_contain_text("空的")
                        refresh("history")
                        mixed_row = page.locator(f'[data-batch-id="{mixed["id"]}"]')
                        expect(mixed_row.locator("strong")).to_contain_text("3 個檔案")
                        expect(mixed_row.locator("small").first).to_have_text(
                            "3 個檔案 · 1 已完成 · 2 未能加入"
                        )
                        screenshot("mixed-invalid-files-retained.png", True)
                        mixed_row.click()
                        expect(page.locator("#batch-dialog-title")).to_contain_text("3 個檔案")
                        expect(page.locator("#batch-dialog .batch-status p")).to_have_text(
                            "3 個檔案 · 1 已完成 · 2 未能加入"
                        )
                        page.keyboard.press("Escape")
                        batch_count = len(client.get("/api/batches").json()["batches"])
                        page.locator("#upload-button").click()
                        expect(page.locator("#upload-error")).to_contain_text("請先移除")
                        assert len(client.get("/api/batches").json()["batches"]) == batch_count
                        page.locator("[data-remove-file]").first.click()
                        expect(page.locator(".queue-item")).to_have_count(1)
                        expect(page.locator(".queue-item")).to_contain_text("empty-neighbour.jpg")
                        page.locator("#clear-file").click()
                        removed = client.delete(f"/api/jobs/{mixed['jobs'][0]['id']}")
                        assert removed.status_code == 204, removed.text
                        refresh("history")
                        expect(mixed_row.locator("strong")).to_contain_text("3 個檔案")
                        expect(mixed_row.locator("small").first).to_have_text(
                            "3 個檔案 · 1 已移除 · 2 未能加入"
                        )
                        mixed_row.click()
                        expect(page.locator("#batch-dialog-title")).to_contain_text("3 個檔案")
                        expect(page.locator("#batch-dialog .batch-status p")).to_have_text(
                            "3 個檔案 · 1 已移除 · 2 未能加入"
                        )
                        screenshot("batch-removed-source-count.png")
                        page.keyboard.press("Escape")
                        page.locator("#file-input").set_input_files(
                            [
                                file_payload("valid-lifestyle.jpg"),
                                {
                                    "name": "video-not-for-outfit.mp4",
                                    "mimeType": "video/mp4",
                                    "buffer": b"explicit-rejected-video-fixture",
                                },
                            ]
                        )
                        submitted = submit_batch()
                        result = wait_batch(client, submitted["id"])
                        assert result["job_count"] == 1 and len(result["errors"]) == 1
                        expect(page.locator(".queue-item")).to_have_count(1)
                        expect(page.locator(".queue-error")).to_contain_text("影片")
                        page.locator(f'.batch-row[data-batch-id="{result["id"]}"]').click()
                        expect(page.locator("#batch-dialog-body")).to_contain_text("video-not-for-outfit.mp4")
                        expect(page.locator("#batch-dialog-body")).to_contain_text("未能加入")
                        screenshot("batch-partial-upload-error.png")
                        page.keyboard.press("Escape")
                        page.locator("#clear-file").click()
                        page.locator(".advanced-options summary").click()
                        page.locator("#concept-input").fill("fixture-fail-once")
                        page.locator("#file-input").set_input_files(
                            [file_payload("fail-once-a.jpg"), file_payload("success-b.jpg")]
                        )
                        failed = wait_batch(client, submit_batch()["id"])
                        assert failed["counts"]["failed"] == 1 and failed["counts"]["completed"] == 1, failed
                        refresh("history")
                        page.locator(f'[data-batch-id="{failed["id"]}"]').click()
                        with page.expect_response(
                            lambda response: response.url.endswith(f"/api/batches/{failed['id']}/retry")
                        ) as retried:
                            page.locator('#batch-dialog [data-batch-action="retry"]').click()
                        retry = retried.value.json()
                        assert retry["id"] != failed["id"] and retry["job_count"] == 1
                        done = wait_batch(client, retry["id"])
                        assert done["status"] == "completed"
                        assert client.get(f"/api/batches/{failed['id']}").json()["counts"]["completed"] == 1
                        page.locator("#concept-input").fill("garment")
                        return {
                            "mixed_unsupported_and_empty": "valid JPG completed; two per-file errors persisted and retained in removable queue; all-invalid retry blocked",
                            "batch_file_counts": "history and dialog show 3 files, 1 completed, 2 rejected; after source deletion, total remains 3 with 1 removed",
                            "invalid_video": "per-file error retained; valid photo processed",
                            "failed_retry": "new batch with only failed job; previous completed result remains",
                        }

                    def cancel_and_retry():
                        page.locator("#concept-input").fill("fixture-slow")
                        page.locator("#file-input").set_input_files(
                            [file_payload("cancel-a.jpg"), file_payload("cancel-b.jpg")]
                        )
                        submitted = submit_batch()
                        page.locator(f'[data-batch-id="{submitted["id"]}"]').first.click()
                        with page.expect_response(
                            lambda response: response.url.endswith(f"/api/batches/{submitted['id']}/cancel")
                        ) as cancel_response:
                            page.locator('#batch-dialog [data-batch-action="cancel"]').click()
                        assert cancel_response.value.status == 200
                        cancelled = wait_batch(client, submitted["id"])
                        assert cancelled["counts"]["cancelled"] >= 1, cancelled
                        expect(page.locator('#batch-dialog [data-batch-action="retry"]')).to_be_visible(
                            timeout=15000
                        )
                        screenshot("batch-cancelled-retry.png")
                        with page.expect_response(
                            lambda response: response.url.endswith(f"/api/batches/{submitted['id']}/retry")
                        ) as retried:
                            page.locator('#batch-dialog [data-batch-action="retry"]').click()
                        retry = retried.value.json()
                        assert retry["id"] != submitted["id"]
                        assert wait_batch(client, retry["id"])["status"] == "completed"
                        page.locator("#concept-input").fill("garment")
                        return {
                            "cancelled_jobs": cancelled["counts"]["cancelled"],
                            "retry_jobs": retry["job_count"],
                            "original_batch_retained": True,
                        }

                    def mobile_touch():
                        page.set_viewport_size({"width": 390, "height": 844})
                        page.locator('[data-view="wardrobe"]').click()
                        page.locator("#file-input").set_input_files([file_payload("mobile-person.jpg")])
                        page.locator("[data-person-file]").click()
                        page.locator("#person-photo").evaluate("image => image.decode()")
                        bounds = page.locator("#person-stage").bounding_box()
                        session = context.new_cdp_session(page)
                        start = {
                            "x": bounds["x"] + bounds["width"] * 0.2,
                            "y": bounds["y"] + bounds["height"] * 0.15,
                        }
                        end = {
                            "x": bounds["x"] + bounds["width"] * 0.8,
                            "y": bounds["y"] + bounds["height"] * 0.85,
                        }
                        session.send(
                            "Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [start]}
                        )
                        session.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [end]})
                        session.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
                        session.detach()
                        expect(page.locator("#person-box")).to_be_visible()
                        assert abs(float(page.locator("#person-left").input_value()) - 20) < 1
                        no_overflow(page, "#person-dialog")
                        screenshot("mobile-touch-person-selection.png")
                        page.locator("#person-save").click()
                        no_overflow(page)
                        screenshot("mobile-lifestyle-workspace.png", True)
                        page.locator("#clear-file").click()
                        page.set_viewport_size({"width": 1440, "height": 1000})
                        return {
                            "viewport": [390, 844],
                            "input": "Chromium touchStart/touchMove/touchEnd create real pointer events",
                            "horizontal_overflow": False,
                        }

                    def upload_response_lost():
                        before = len(client.get("/api/batches").json()["batches"])
                        page.locator("#file-input").set_input_files(
                            [file_payload("network-retry-a.jpg"), file_payload("network-retry-b.jpg")]
                        )
                        fault = {"once": True, "accepted": None}

                        def lose_response(route):
                            if route.request.method == "POST" and fault["once"]:
                                fault["once"] = False
                                response = route.fetch()
                                assert response.status == 202
                                fault["accepted"] = response.json()
                                route.abort("failed")
                            else:
                                route.fallback()

                        context.route(url + "/api/batches", lose_response)
                        try:
                            page.locator("#upload-button").click()
                            expect(page.locator("#upload-error")).to_contain_text("無法連線")
                            expect(page.locator("#upload-button")).to_be_enabled()
                            expect(page.locator(".queue-item")).to_have_count(2)
                            retried = submit_batch()
                            assert retried["id"] == fault["accepted"]["id"]
                            assert len(client.get("/api/batches").json()["batches"]) == before + 1
                            assert wait_batch(client, retried["id"])["job_count"] == 2
                        finally:
                            context.unroute(url + "/api/batches", lose_response)
                        return {
                            "fault": "real server accepted multipart, browser response deliberately aborted",
                            "retry": "same client_id returns original batch; no duplicate jobs",
                        }

                    try:
                        check(
                            "multiple files with EXIF-aligned pointer and keyboard person selection",
                            queue_and_select,
                        )
                        check(
                            "real batch upload preserves visible attributes in confirmation group",
                            upload_and_confirmation,
                        )
                        check("human review moves one outfit into usable wardrobe", review_visible_details)
                        check(
                            "similarity review, primary selection, source evidence and reversible merge",
                            merge_and_undo,
                        )
                        check(
                            "per-file upload error and failed-job-only batch retry", partial_error_and_retry
                        )
                        check("whole-batch cancellation and non-destructive retry", cancel_and_retry)
                        check("390px real touch selection and responsive workspace", mobile_touch)
                        check("lost upload response retries idempotently", upload_response_lost)
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
        if len(report["scenarios"]) == 8
        and not failed
        and not report["page_errors"]
        and "error" not in report
        else "failed"
    )
    report["summary"] = {
        "passed": len(report["scenarios"]) - len(failed),
        "failed": len(failed),
        "expected": 8,
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
