"""Exercise the affordable three-preview UI against temporary real API/SQLite.

The image provider is an explicit local fixture. Upload, scheduling, media,
candidate selection, free shopping handoff, persistence and browser behavior
are real. No model service, external site or existing catalogue is contacted.
"""

from __future__ import annotations

import argparse
import asyncio
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

from lifestyle_browser_smoke import LifestyleFixtures, no_overflow, photo


ROOT = Path(__file__).resolve().parents[1]


class AffordablePreviewFixtures(LifestyleFixtures):
    async def reconstruct(self, *_args, **_kwargs):
        raise AssertionError("The affordable browser flow must use reconstruct_preview")

    async def reconstruct_preview(
        self,
        cutout_path,
        context_path,
        record,
        destinations,
        *,
        source_bundle=None,
    ):
        assert Path(cutout_path).is_file() and Path(context_path).is_file()
        assert record["category"] == "top" and len(destinations) == 3
        await asyncio.sleep(0.15)
        for number, destination in enumerate(destinations, 1):
            image = Image.new("RGB", (640, 800), "#f5f2e9")
            draw = ImageDraw.Draw(image)
            color = ("#294758", "#315667", "#1f3e50")[number - 1]
            draw.polygon(
                [
                    (245, 140),
                    (170, 190),
                    (85, 345),
                    (190, 395),
                    (230, 315),
                    (230, 650),
                    (410, 650),
                    (410, 315),
                    (450, 395),
                    (555, 345),
                    (470, 190),
                    (395, 140),
                ],
                fill=color,
            )
            draw.arc((270, 110, 370, 205), 0, 180, fill="#f5f2e9", width=15)
            draw.line((245, 330 + number * 12, 395, 330 + number * 12), fill="#d7ddd2", width=5)
            draw.text((170, 725), f"EXPLICIT PREVIEW FIXTURE {number}", fill="#405a46")
            destination = Path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            image.save(destination, "PNG")
        return {
            "reference_manifest": source_bundle.manifest.model_dump() if source_bundle else None,
            "candidate_count": 3,
        }


def serve(directory: Path, port: int):
    os.environ["REWEAR_DATA_DIR"] = str(directory)
    sys.path.insert(0, str(ROOT))
    import uvicorn

    from wardrobe.app import create_app
    from wardrobe.config import Settings

    uvicorn.run(
        create_app(
            Settings(data_dir=directory, api_key="explicit-preview-fixture"), AffordablePreviewFixtures()
        ),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
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
    if args.output is None:
        parser.error("--output is required")

    sys.path.insert(0, str(args.policy_dir))
    from network_policy import LocalNetworkPolicy, install_network_policy

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "status": "running",
        "scope": "Temporary real API, SQLite, upload, media and browser; explicit local image fixture",
        "model_calls": 0,
        "outside_requests": [],
        "page_errors": [],
        "scenarios": [],
    }

    def record(name, details=None):
        report["scenarios"].append({"name": name, "status": "passed", **(details or {})})
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="rewear-affordable-preview-") as temporary:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", temporary),
            "LANG": "en_US.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "REWEAR_DATA_DIR": temporary,
        }
        with (output / "server.log").open("w") as server_log:
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
                stdout=server_log,
                stderr=subprocess.STDOUT,
            )
            try:
                for _ in range(80):
                    try:
                        if httpx.get(base + "/api/health", timeout=1).status_code == 200:
                            break
                    except httpx.RequestError:
                        pass
                    if server.poll() is not None:
                        raise RuntimeError("Temporary server exited; see server.log")
                    time.sleep(0.2)
                else:
                    raise RuntimeError("Temporary server did not start")

                with httpx.Client(base_url=base, timeout=30) as client:
                    response = client.post(
                        "/api/jobs",
                        files={"file": ("explicit-lifestyle.jpg", photo(), "image/jpeg")},
                        data={
                            "provider": "meta",
                            "mode": "outfit",
                            "concept": "garment",
                            "reconstruct": "false",
                            "discover": "false",
                        },
                    )
                    response.raise_for_status()
                    job_id = response.json()["id"]
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        job = client.get(f"/api/jobs/{job_id}").json()
                        if job["status"] not in {"queued", "running"}:
                            break
                        time.sleep(0.05)
                    assert job["status"] == "completed" and job["item_count"] == 1, job
                    item = next(
                        entry
                        for entry in client.get("/api/items").json()["items"]
                        if entry["job_id"] == job_id
                    )

                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(headless=True)
                        context = browser.new_context(
                            viewport={"width": 1440, "height": 1000}, service_workers="block"
                        )
                        policy = LocalNetworkPolicy.for_http(base)
                        install_network_policy(context, policy)
                        page = context.new_page()
                        page.set_default_timeout(15000)
                        page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
                        page.on(
                            "request",
                            lambda request: (
                                report["outside_requests"].append(request.url)
                                if not request.url.startswith(base)
                                else None
                            ),
                        )
                        page.goto(base, wait_until="networkidle")
                        page.locator(f'[data-item-id="{item["id"]}"]').click()
                        expect(page.locator("#item-dialog")).to_be_visible()
                        expect(page.locator(".free-shopping-handoff")).to_be_visible()
                        expect(page.locator(".free-shopping-handoff")).to_contain_text("US$0 模型費")
                        expect(page.locator(".free-shopping-handoff")).to_contain_text("Google 購物")
                        expect(page.locator(".free-shopping-handoff")).to_contain_text("Bing Shopping")
                        expect(page.locator(".free-shopping-handoff")).to_contain_text("Google 網頁搜尋")
                        links = page.locator(".free-shopping-actions a")
                        assert links.count() == 3
                        assert "tbm=shop" in links.nth(0).get_attribute("href")
                        assert "google.com/search?q=" in links.nth(2).get_attribute("href")
                        assert links.nth(0).get_attribute("rel") == "noopener noreferrer"
                        record("free-shopping-links-need-no-model-call")

                        generate = page.locator(
                            '[data-reconstruction-action="generate"][data-reconstruction-mode="preview"]'
                        )
                        expect(generate).to_contain_text("產生 3 張預覽 · 約 US$0.03")
                        with page.expect_response(
                            lambda result: (
                                result.url == base + f"/api/items/{item['id']}/reconstruction"
                                and result.request.method == "POST"
                            )
                        ) as submitted:
                            generate.click()
                        assert submitted.value.status == 202
                        candidates = page.locator(".preview-candidate-grid button")
                        expect(candidates).to_have_count(3)
                        expect(page.locator(".preview-cost-note")).to_contain_text("US$0.03")
                        expect(page.locator(".synthetic-label")).to_contain_text("未逐項比對")
                        page.screenshot(path=output / "desktop-three-candidates.png", animations="disabled")
                        record("one-request-produces-three-clearly-labelled-candidates")

                        with page.expect_response(
                            lambda result: (
                                result.url == base + f"/api/items/{item['id']}/reconstruction/select"
                                and result.request.method == "POST"
                            )
                        ) as selected:
                            candidates.nth(1).click()
                        assert selected.value.status == 200
                        expect(candidates.nth(1)).to_have_attribute("aria-pressed", "true")
                        saved = client.get(f"/api/items/{item['id']}").json()
                        assert saved["reconstruction"]["selected_candidate_id"] == "2"
                        assert saved["reconstruction"]["candidate_url"].endswith("-preview-2.png")
                        expect(page.locator('#item-media-content img[alt*="AI 完整預覽"]')).to_have_attribute(
                            "src", base + saved["reconstruction"]["candidate_url"]
                        )
                        page.locator("#item-dialog").press("Escape")
                        card = page.locator(f'[data-item-id="{item["id"]}"]')
                        expect(card.locator(".card-reconstruction")).to_contain_text("AI 完整預覽 · 已選")
                        expect(card.locator("img").first).to_have_attribute(
                            "src", base + saved["reconstruction"]["candidate_url"]
                        )
                        record("candidate-selection-persists-and-updates-card")

                        page.set_viewport_size({"width": 390, "height": 844})
                        card.click()
                        expect(page.locator("#item-dialog")).to_be_visible()
                        no_overflow(page)
                        page.locator(".preview-candidate-picker").scroll_into_view_if_needed()
                        page.screenshot(path=output / "mobile-selected-preview.png", animations="disabled")
                        record("mobile-three-candidate-layout-has-no-page-overflow")
                        context.close()
                        browser.close()

                    assert not report["page_errors"], report["page_errors"]
                    assert not report["outside_requests"], report["outside_requests"]
                    media = [
                        client.get(entry["variant_url"])
                        for entry in saved["reconstruction"]["preview_candidates"]
                    ]
                    assert all(
                        result.status_code == 200 and result.content.startswith(b"\x89PNG")
                        for result in media
                    )
                    record("all-three-persisted-media-files-are-readable")
            except Exception as error:
                report.update(status="failed", error=str(error), traceback=traceback.format_exc())
                (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
                raise
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
    report["status"] = "passed"
    report["passed"] = len(report["scenarios"])
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({"passed": report["passed"], "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
