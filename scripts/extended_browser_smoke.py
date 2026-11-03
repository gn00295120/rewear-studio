"""Additional UI scenarios against a temporary real API with an explicit fake provider.

This never connects to a model or to an existing catalogue. The generated image,
video, model response and test records are fixtures; persistence, HTTP endpoints,
media preparation/serving and the browser UI are the actual application.
"""

import argparse
import copy
import io
import json
import os
import shutil
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
LONG_NAME = (
    "隔離測試衣物名稱🧵🪡" + "這是特別加長的繁體中文衣物款式名稱用來確認完整顯示與自動換行" * 3 + "👕🌿"
)
FIXTURE_RECORD = {
    "category": "top",
    "subtype": "隔離測試上衣",
    "brand": None,
    "size_label": "M",
    "colors": ["藍色"],
    "pattern": None,
    "material_guess": None,
    "condition": "unknown",
    "defects": [],
    "confidence": 0.6,
    "needs_review": True,
}


class FixtureProviders:
    """Deterministic test data, explicitly not a model or inference result."""

    async def health(self):
        return {
            "local": {"available": True, "model": "extended-browser-fixture-no-inference", "error": None},
            "meta": {"configured": True},
        }

    async def inspect(self, path, provider, *, context_path=None):
        # Fixture images use distinct corner colors as a deterministic test
        # marker. This selects scripted responses; it is not visual inference.
        with Image.open(path) as source:
            red, green, blue = source.convert("RGB").getpixel((0, 0))
        target = (
            "no_garment"
            if red > 240 and green < 230
            else "multiple_garments"
            if blue > 240 and red < 225
            else "one_garment"
        )
        return {
            "garment": copy.deepcopy(FIXTURE_RECORD),
            "target": target,
            "visibility": "complete",
            "reason": "明確標示的瀏覽器測試資料，不是模型推論結果。",
        }

    async def extract(self, path, provider):
        return copy.deepcopy(FIXTURE_RECORD)

    async def segment(self, path, concept, video=False):
        # Video playback does not require a fabricated garment detection.
        return []

    async def generate(self, prompt, destination):
        raise AssertionError("Image generation is outside these browser scenarios")

    async def close(self):
        pass


def serve(data_dir: Path, port: int):
    # wardrobe.app creates its module-level app at import time. Set the temporary
    # location before importing it so even that unused app cannot touch user data.
    os.environ["REWEAR_DATA_DIR"] = str(data_dir)
    sys.path.insert(0, str(ROOT))
    import uvicorn

    from wardrobe.app import create_app
    from wardrobe.config import Settings

    settings = Settings(data_dir=data_dir, api_key="explicit-browser-fixture-not-a-credential")
    uvicorn.run(
        create_app(settings, providers=FixtureProviders()),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )


def image_fixture(target="one_garment"):
    backgrounds = {"one_garment": "#eeeade", "multiple_garments": "#dcebf5", "no_garment": "#f5e1dc"}
    image = Image.new("RGB", (256, 320), backgrounds[target])
    draw = ImageDraw.Draw(image)
    shirt = [
        (87, 60),
        (56, 76),
        (34, 124),
        (72, 142),
        (84, 119),
        (84, 255),
        (172, 255),
        (172, 119),
        (184, 142),
        (222, 124),
        (200, 76),
        (169, 60),
    ]
    if target == "no_garment":
        draw.ellipse((158, 110, 218, 186), outline="#39586c", width=12)
        draw.rounded_rectangle((70, 91, 178, 220), radius=12, fill="#39586c")
    elif target == "multiple_garments":
        for offset in (0, 128):
            draw.polygon([(x // 2 + offset, y * 3 // 4 + 30) for x, y in shirt], fill="#39586c")
    else:
        draw.polygon(shirt, fill="#39586c")
    draw.text((98, 280), "FIXTURE", fill="#2b4036")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def wait_job(client: httpx.Client, ident: str, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{ident}")
        response.raise_for_status()
        job = response.json()
        if job["status"] not in {"queued", "running"}:
            assert job["status"] == "completed", job
            return job
        time.sleep(0.05)
    raise AssertionError(f"Temporary job {ident} did not finish within {timeout}s")


def seed_items(client: httpx.Client):
    specifications = [
        {"subtype": LONG_NAME, "brand": "長文字測試🧵", "colors": [], "material_guess": None},
        {"category": "dress", "subtype": "篩選流程測試洋裝", "brand": "filter-fixture"},
        {"category": "outerwear", "subtype": "平板測試外套", "brand": "tablet-fixture"},
    ]
    seeded = []
    for index, record in enumerate(specifications):
        response = client.post(
            "/api/jobs",
            files={"file": (f"explicit-fixture-{index}.png", image_fixture(), "image/png")},
            data={"provider": "local", "mode": "single", "concept": "garment"},
        )
        response.raise_for_status()
        job = wait_job(client, response.json()["id"])
        assert len(job["items"]) == 1
        item = job["items"][0]
        update = client.patch(f"/api/items/{item['id']}", json={"record": record, "reviewed": index == 2})
        update.raise_for_status()
        seeded.append(update.json())
    return seeded


def assert_no_overflow(page, selector="html"):
    result = page.locator(selector).evaluate(
        "e => ({scroll: e.scrollWidth, client: e.clientWidth, width: window.innerWidth})"
    )
    assert result["scroll"] <= result["client"] + 1, result
    return result


def assert_controls_inside_dialog(page):
    result = page.locator("#item-dialog").evaluate(
        """dialog => {
            const box = dialog.getBoundingClientRect();
            return [...dialog.querySelectorAll('input,select,textarea,.item-form-actions button')]
                .map(e => ({name: e.id || e.textContent, rect: e.getBoundingClientRect()}))
                .filter(({rect}) => rect.width && (rect.left < box.left - 1 || rect.right > box.right + 1))
                .map(({name}) => name);
        }"""
    )
    assert not result, result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("out/verification/extended-browser"))
    parser.add_argument(
        "--policy-dir", type=Path, default=Path.home() / ".agents/skills/webapp-testing/scripts"
    )
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--data-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        if not args.data_dir or not args.port:
            parser.error("Internal server mode requires --data-dir and --port")
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
        "unexpected_request_failures": [],
        "expected_fault_requests": [],
        "viewports": [[1440, 1000], [320, 812], [768, 1024]],
        "enlargement": "200% computed text sizes, test injection on rendered elements; not a browser zoom setting",
        "network": "isolated temporary loopback API; bundled network policy; service workers blocked",
        "inference": "none: explicit FixtureProviders, generated fixture image and video",
        "persistence": "real application API and SQLite in TemporaryDirectory; no existing catalogue is opened",
    }

    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        port = candidate.getsockname()[1]
    url = f"http://127.0.0.1:{port}"

    with tempfile.TemporaryDirectory(prefix="rewear-extended-browser-") as temporary:
        directory = Path(temporary)
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", str(directory)),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "REWEAR_DATA_DIR": str(directory / "catalogue"),
            "PYTHONUNBUFFERED": "1",
        }
        with (output / "server.log").open("w") as log:
            server = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--serve",
                    "--data-dir",
                    str(directory / "catalogue"),
                    "--port",
                    str(port),
                ],
                cwd=ROOT,
                env=environment,
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
                        raise RuntimeError("Temporary API exited; see server.log")
                    time.sleep(0.2)
                else:
                    raise RuntimeError("Temporary API startup timed out")

                with httpx.Client(base_url=url, timeout=30) as client:
                    seeded = seed_items(client)
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(headless=True)
                        context = browser.new_context(
                            viewport={"width": 1440, "height": 1000}, service_workers="block"
                        )
                        policy = LocalNetworkPolicy.for_http(url)
                        install_network_policy(context, policy)
                        page = context.new_page()
                        page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
                        offline = {"enabled": False}

                        def failure(request):
                            entry = {"url": request.url, "failure": request.failure}
                            if offline["enabled"] and request.url == url + "/api/items":
                                report["expected_fault_requests"].append(entry)
                            else:
                                report["unexpected_request_failures"].append(entry)

                        page.on("requestfailed", failure)

                        def fresh(width=1440, height=1000):
                            page.set_viewport_size({"width": width, "height": height})
                            # A same-URL fragment navigation keeps open dialogs,
                            # filters and test-injected styles. Reload the actual
                            # document so every scenario starts with fresh UI state.
                            page.goto(url + "/", wait_until="networkidle")
                            page.reload(wait_until="networkidle")
                            page.locator('[data-view="wardrobe"]').click()
                            expect(page.locator("#total-count")).to_have_text("3")

                        def screenshot(name, full_page=False):
                            if full_page:
                                # Capture fixed elements from the document top;
                                # otherwise Chromium can place an offscreen skip
                                # link inside its stitched full-page screenshot.
                                page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
                            page.screenshot(path=output / name, full_page=full_page, animations="disabled")

                        def run(name, function):
                            result = {"name": name}
                            try:
                                details = function()
                                result.update(status="passed", details=details or {})
                            except Exception as error:
                                result.update(
                                    status="failed", error=str(error), traceback=traceback.format_exc()
                                )
                                try:
                                    screenshot(f"failure-{len(report['scenarios']) + 1}.png")
                                except Exception:
                                    pass
                            report["scenarios"].append(result)
                            (output / "report.json").write_text(
                                json.dumps(report, ensure_ascii=False, indent=2)
                            )
                            print(json.dumps(result, ensure_ascii=False), flush=True)

                        def unicode_and_empty_colors():
                            fresh()
                            card = page.locator(f'[data-item-id="{seeded[0]["id"]}"]')
                            expect(card.locator(".card-title")).to_have_text(LONG_NAME)
                            expect(card).to_contain_text("顏色待確認")
                            assert len(LONG_NAME) > 50
                            assert_no_overflow(page)
                            card.click()
                            expect(page.locator("#item-subtype")).to_have_value(LONG_NAME)
                            expect(page.locator("#item-colors")).to_have_value("")
                            assert_no_overflow(page, "#item-dialog")
                            assert_controls_inside_dialog(page)
                            screenshot("long-unicode-editor.png")
                            return {
                                "name_codepoints": len(LONG_NAME),
                                "empty_colors": "shown as pending; editor remains empty",
                            }

                        def narrow_320():
                            fresh(320, 812)
                            page.locator("#category-filter").select_option("outerwear")
                            page.locator('.review-tabs [data-review="reviewed"]').click()
                            expect(page.locator(".garment-card")).to_have_count(1)
                            assert_no_overflow(page)
                            page.locator(".card-button").click()
                            assert_no_overflow(page, "#item-dialog")
                            assert_controls_inside_dialog(page)
                            page.locator("#item-brand").fill("320px 實際編輯")
                            page.get_by_role("button", name="儲存變更", exact=True).click()
                            expect(page.locator("#item-dialog")).not_to_be_visible()
                            expect(page.locator(".garment-card")).to_have_count(1)
                            expect(page.locator("#category-filter")).to_have_value("outerwear")
                            saved = next(
                                i
                                for i in client.get("/api/items").json()["items"]
                                if i["id"] == seeded[2]["id"]
                            )
                            assert saved["record"]["brand"] == "320px 實際編輯"
                            screenshot("320px-filtered-catalogue.png", full_page=True)
                            return {"viewport": [320, 812], "real_patch_verified": True}

                        def tablet_768():
                            fresh(768, 1024)
                            page.keyboard.press("/")
                            expect(page.locator("#search-input")).to_be_focused()
                            page.locator("#search-input").fill("平板")
                            expect(page.locator(".garment-card")).to_have_count(1)
                            assert_no_overflow(page)
                            page.locator(".card-button").click()
                            assert_no_overflow(page, "#item-dialog")
                            assert_controls_inside_dialog(page)
                            page.locator("#item-category").focus()
                            page.keyboard.press("Tab")
                            expect(page.locator("#item-subtype")).to_be_focused()
                            screenshot("768px-keyboard-editor.png")
                            page.keyboard.press("Escape")
                            expect(page.locator("#item-dialog")).not_to_be_visible()
                            return {
                                "viewport": [768, 1024],
                                "keyboard": "search shortcut, field traversal and Escape",
                            }

                        def enlarged_text():
                            fresh()
                            page.locator(f'[data-item-id="{seeded[1]["id"]}"]').click()
                            font_sizes = page.evaluate(
                                """() => {
                                    const samples = [...document.querySelectorAll('body *')]
                                        .filter(e => !(e instanceof SVGElement))
                                        .map(e => [e, parseFloat(getComputedStyle(e).fontSize)]);
                                    const before = parseFloat(getComputedStyle(document.querySelector('#item-brand')).fontSize);
                                    for (const [e, size] of samples) if (size > 0) e.style.setProperty('font-size', `${size * 2}px`, 'important');
                                    return {before, after: parseFloat(getComputedStyle(document.querySelector('#item-brand')).fontSize)};
                                }"""
                            )
                            assert font_sizes["after"] == font_sizes["before"] * 2, font_sizes
                            assert_no_overflow(page, "#item-dialog")
                            assert_controls_inside_dialog(page)
                            page.locator("#item-size_label").fill("XL · 文字放大測試")
                            screenshot("200-percent-text-editor.png")
                            page.get_by_role("button", name="儲存變更", exact=True).click()
                            expect(page.locator("#item-dialog")).not_to_be_visible()
                            stored = next(
                                i
                                for i in client.get("/api/items").json()["items"]
                                if i["id"] == seeded[1]["id"]
                            )
                            assert stored["record"]["size_label"] == "XL · 文字放大測試"
                            return {"font_sizes": font_sizes, "save_reachable_and_persisted": True}

                        def filter_edit_transitions():
                            fresh()
                            page.locator("#search-input").fill("filter-fixture")
                            page.locator("#category-filter").select_option("dress")
                            page.locator('.review-tabs [data-review="pending"]').click()
                            expect(page.locator(".garment-card")).to_have_count(1)
                            page.locator(".card-button").click()
                            page.locator("#item-category").select_option("top")
                            page.get_by_role("button", name="儲存變更", exact=True).click()
                            expect(page.locator("#item-dialog")).not_to_be_visible()
                            expect(page.locator(".garment-card")).to_have_count(0)
                            expect(page.locator("#search-input")).to_have_value("filter-fixture")
                            expect(page.locator("#category-filter")).to_have_value("dress")
                            expect(page.locator('[data-review="pending"]')).to_have_attribute(
                                "aria-pressed", "true"
                            )
                            page.locator("#category-filter").select_option("top")
                            expect(page.locator(".garment-card")).to_have_count(1)
                            page.locator(".card-button").click()
                            page.get_by_role("button", name="儲存並確認", exact=True).click()
                            expect(page.locator("#item-dialog")).not_to_be_visible()
                            expect(page.locator(".garment-card")).to_have_count(0)
                            page.locator('.review-tabs [data-review="reviewed"]').click()
                            expect(page.locator(".garment-card")).to_have_count(1)
                            stored = next(
                                i
                                for i in client.get("/api/items").json()["items"]
                                if i["id"] == seeded[1]["id"]
                            )
                            assert stored["record"]["category"] == "top" and stored["reviewed"]
                            screenshot("filtered-save-transition.png", full_page=True)
                            return {
                                "category_move": "vanishes from dress; reappears under top",
                                "review_move": "vanishes from pending; reappears reviewed",
                                "filter_selection_preserved": True,
                            }

                        def offline_recovery():
                            fresh()
                            before_ids = {i["id"] for i in client.get("/api/items").json()["items"]}

                            def inject_disconnect(route):
                                if offline["enabled"]:
                                    route.abort("internetdisconnected")
                                else:
                                    route.fallback()

                            context.route(url + "/api/items", inject_disconnect)
                            try:
                                offline["enabled"] = True
                                page.locator('[data-view="history"]').click()
                                page.locator("#history-refresh").click()
                                expect(page.locator("#service-error")).to_be_visible()
                                expect(page.locator("#service-error-message")).to_contain_text("無法連線")
                                screenshot("network-failure-visible.png")
                                offline["enabled"] = False
                                page.locator("#refresh-button").click()
                                expect(page.locator("#service-error")).not_to_be_visible()
                                page.locator('[data-view="wardrobe"]').click()
                                expect(page.locator(".garment-card")).to_have_count(3)
                                assert {
                                    i["id"] for i in client.get("/api/items").json()["items"]
                                } == before_ids
                                screenshot("network-recovered.png", full_page=True)
                            finally:
                                offline["enabled"] = False
                                context.unroute(url + "/api/items", inject_disconnect)
                            return {
                                "fault": "one deliberate /api/items network abort",
                                "recovery": "Retry fetches real API; all three persisted IDs remain",
                            }

                        def actual_video_playback():
                            fresh()
                            ffmpeg = shutil.which("ffmpeg")
                            assert ffmpeg, "FFmpeg is required for the real playable fixture"
                            video_path = directory / "explicit-moving-fixture.mp4"
                            subprocess.run(
                                [
                                    ffmpeg,
                                    "-v",
                                    "error",
                                    "-y",
                                    "-f",
                                    "lavfi",
                                    "-i",
                                    "testsrc2=size=320x240:rate=10",
                                    "-t",
                                    "2",
                                    "-an",
                                    "-c:v",
                                    "libx264",
                                    "-pix_fmt",
                                    "yuv420p",
                                    "-movflags",
                                    "+faststart",
                                    str(video_path),
                                ],
                                check=True,
                                capture_output=True,
                                timeout=30,
                            )
                            upload = client.post(
                                "/api/jobs",
                                files={"file": (video_path.name, video_path.read_bytes(), "video/mp4")},
                                data={"provider": "local", "mode": "rack", "concept": "garment"},
                            )
                            upload.raise_for_status()
                            job = wait_job(client, upload.json()["id"], timeout=30)
                            assert job["media_type"] == "video" and job["item_count"] == 0
                            page.locator('[data-view="history"]').click()
                            page.locator("#history-refresh").click()
                            page.locator(f'[data-job-id="{job["id"]}"].job-row').click()
                            page.locator('[data-media-key="source"]').click()
                            video = page.locator("#job-media-content video")
                            expect(video).to_have_attribute("controls", "")
                            video.evaluate("video => { video.muted = true; return video.play(); }")
                            # wait_for_function compiles its predicate with eval,
                            # which this app correctly disallows in its CSP. Read
                            # the real media clock without bypassing that policy.
                            deadline = time.monotonic() + 10
                            while time.monotonic() < deadline:
                                if video.evaluate("video => video.currentTime") >= 0.25:
                                    break
                                time.sleep(0.05)
                            else:
                                raise AssertionError("Decoded video clock did not advance")
                            playback = video.evaluate(
                                "video => ({time: video.currentTime, paused: video.paused, readyState: video.readyState, duration: video.duration, width: video.videoWidth, height: video.videoHeight})"
                            )
                            assert (
                                not playback["paused"]
                                and playback["readyState"] >= 2
                                and playback["width"] > 0
                            ), playback
                            video.evaluate("video => video.pause()")
                            screenshot("real-video-playback.png")
                            page.keyboard.press("Escape")
                            return {
                                "playback": playback,
                                "media": "real FFmpeg-generated MP4 uploaded, normalized and served by temporary API",
                                "segmentation": "explicit empty fixture; no invented detections",
                            }

                        def multiple_and_non_garment_guidance():
                            fresh()
                            cases = [
                                ("multiple_garments", "請使用多件衣物模式", "在上傳區選擇「多件衣物」"),
                                ("no_garment", "未偵測到衣物", "請換成衣物照片"),
                            ]
                            created = []
                            for target, title, advice in cases:
                                response = client.post(
                                    "/api/jobs",
                                    files={
                                        "file": (
                                            f"explicit-{target}-fixture.png",
                                            image_fixture(target),
                                            "image/png",
                                        )
                                    },
                                    data={"provider": "local", "mode": "single", "concept": "garment"},
                                )
                                response.raise_for_status()
                                job = wait_job(client, response.json()["id"])
                                assert (
                                    job["item_count"] == 1
                                    and job["usable_count"] == 0
                                    and job["retake_count"] == 1
                                ), job
                                item = job["items"][0]
                                assert (
                                    item["quality"]["version"] == 3
                                    and item["quality"]["model_target"] == target
                                ), item["quality"]
                                assert (
                                    item["quality"]["status"] == "needs_retake"
                                    and item["quality"]["visibility"] == "complete"
                                )
                                assert item["record"]["category"] == "other" and not item["record"]["colors"]
                                assert item["model_record"] == FIXTURE_RECORD
                                created.append((item, title, advice))

                            page.reload(wait_until="networkidle")
                            expect(page.locator("#total-count")).to_have_text("5")
                            expect(page.locator("#usable-count")).to_have_text("3")
                            expect(page.locator("#attention-count")).to_have_text("2")
                            expect(page.locator(".result-group.retake .garment-card")).to_have_count(2)
                            for item, title, advice in created:
                                card = page.locator(f'[data-item-id="{item["id"]}"]')
                                expect(card.locator(".card-title")).to_have_text(title)
                                expect(card).not_to_contain_text(FIXTURE_RECORD["subtype"])
                                card.click()
                                expect(page.locator("#item-dialog-title")).to_have_text(title)
                                expect(page.locator(".quality-assessment h3")).to_have_text(title)
                                expect(page.locator(".retake-advice")).to_contain_text(advice)
                                expect(page.locator("#item-category")).to_have_value("other")
                                expect(page.locator("#item-colors")).to_have_value("")
                                screenshot(f"{item['quality']['model_target']}-guidance.png")
                                page.keyboard.press("Escape")
                                expect(page.locator("#item-dialog")).not_to_be_visible()
                            return {
                                "targets": [case[0] for case in cases],
                                "fixture_visibility": "complete",
                                "quality_gate": "both require retake; wrong garment guesses suppressed; original model record retained",
                                "catalogue_counts": {"analysis": 5, "usable": 3, "retake": 2},
                            }

                        try:
                            run(
                                "long Traditional Chinese / emoji name and empty colors",
                                unicode_and_empty_colors,
                            )
                            run("320px narrow-screen filtering and persisted edit", narrow_320)
                            run("768px tablet keyboard workflow", tablet_768)
                            run("200% rendered text remains editable and saveable", enlarged_text)
                            run(
                                "save and review while category/status filters are active",
                                filter_edit_transitions,
                            )
                            run(
                                "network failure is visible and Retry restores the real catalogue",
                                offline_recovery,
                            )
                            run("video preview advances actual decoded frames", actual_video_playback)
                            run(
                                "multiple garments and non-clothing receive distinct guidance",
                                multiple_and_non_garment_guidance,
                            )
                        finally:
                            context.close()
                            browser.close()
            except Exception as error:
                report["setup_error"] = str(error)
                report["setup_traceback"] = traceback.format_exc()
            finally:
                server.terminate()
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)

    failed = [scenario for scenario in report["scenarios"] if scenario["status"] != "passed"]
    report["status"] = (
        "passed"
        if len(report["scenarios"]) == 8
        and not failed
        and not report["page_errors"]
        and not report["unexpected_request_failures"]
        and "setup_error" not in report
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
            {"status": report["status"], "summary": report["summary"], "report": str(output / "report.json")},
            ensure_ascii=False,
        ),
        flush=True,
    )
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
