"""Isolated multi-original reconstruction UI fixtures; no model or production DB.

Serves current static files on an ephemeral loopback HTTP server. API state and
explicit image fixtures live only in memory. Browser mutations are restricted to
fixture reconstruction/cancel and same-account reauthentication routes.
"""

import argparse
import copy
import hashlib
import io
import json
import mimetypes
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageDraw
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "wardrobe/static"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(Path.home() / ".agents/skills/webapp-testing/scripts"))
from network_policy import LocalNetworkPolicy, install_network_policy  # noqa: E402
from test_multiview_ui_contract import GROUP, REFERENCES, TARGET, source_manifest  # noqa: E402
from test_preview_ui_contract import preview_report  # noqa: E402

STAMP = "2026-10-22T10:00:00Z"
PASSWORD = "Explicit fixture reauthentication 123"
EXCLUDED = ["e" * 32, "f" * 32, "1" * 32, "2" * 32]
OUTSIDE = "3" * 32


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def media_bytes(label, synthetic=False):
    picture = Image.new("RGB", (280, 350), "#ece8dd")
    draw = ImageDraw.Draw(picture)
    if not synthetic:
        draw.ellipse((110, 20, 170, 80), fill="#b6987c")
    draw.polygon(
        [
            (95, 85),
            (55, 110),
            (35, 210),
            (78, 222),
            (95, 168),
            (95, 290),
            (185, 290),
            (185, 168),
            (202, 222),
            (245, 210),
            (225, 110),
            (185, 85),
        ],
        fill="#34566b",
    )
    draw.text((18, 310), label, fill="#263e35")
    draw.text((18, 328), "EXPLICIT UI FIXTURE", fill="#263e35")
    stream = io.BytesIO()
    picture.save(stream, "PNG")
    return stream.getvalue()


def make_item(ident):
    return {
        "id": ident,
        "job_id": ident,
        "created_at": STAMP,
        "updated_at": STAMP,
        "record": {
            "category": "top",
            "subtype": "明確測試衣物 · " + ident[0],
            "brand": None,
            "colors": ["藍色"],
            "pattern": "素色",
            "material_guess": None,
            "size_label": None,
            "condition": "unknown",
            "defects": [],
            "confidence": 0.9,
            "needs_review": True,
        },
        "quality": {
            "status": "needs_confirmation",
            "visibility": "partial",
            "reasons": ["明確測試：部分衣物被遮擋。"],
        },
        "reviewed": False,
        "provenance": "Explicit in-memory multi-photo UI fixture",
        "frame": None,
        "image_url": f"/media/{ident}/crops/context.jpg",
        "cutout_url": f"/media/{ident}/crops/cutout.jpg",
        "source_url": f"/media/{ident}/preview.jpg",
        "original_url": f"/media/{ident}/source.jpg",
        "reconstruction": None,
        "discovery": None,
    }


def make_job(ident):
    return {
        "id": ident,
        "status": "completed",
        "stage": "completed",
        "progress": 100,
        "filename": "明確測試原照.jpg",
        "provider": "meta",
        "mode": "outfit",
        "media_type": "image",
        "item_count": 1,
        "confirmation_count": 1,
        "usable_count": 0,
        "created_at": STAMP,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "screens").mkdir()
    fixture = {}
    media = {
        ident: media_bytes("ORIGINAL " + ident[0]) for ident in [TARGET, *REFERENCES, *EXCLUDED, OUTSIDE]
    }
    synthetic = media_bytes("SYNTHETIC FIXTURE", synthetic=True)
    lock = threading.RLock()
    report = {
        "status": "running",
        "scope": "Explicit in-memory HTTP/image fixtures; no production backend/database/model calls.",
        "mechanism": "Native loopback ThreadingHTTPServer and headless Chromium; LocalNetworkPolicy before pages; service workers blocked; CSP enforced. No supervisor sandbox receipt claimed.",
        "scenarios": [],
        "requests": [],
        "mutations": [],
        "page_errors": [],
        "console_errors": [],
        "contract_failures": [],
        "outside_requests": [],
        "unexpected_mutations": [],
        "static_sha256": {
            name: hashlib.sha256((STATIC / name).read_bytes()).hexdigest()
            for name in ("app.js", "styles.css", "index.html")
        },
    }

    def reset():
        with lock:
            items = {ident: make_item(ident) for ident in [TARGET, *REFERENCES, *EXCLUDED, OUTSIDE]}
            items[REFERENCES[2]]["record"]["subtype"] = "明確測試長中文款式名稱與手機排版🙂" * 5
            jobs = {ident: make_job(ident) for ident in items if ident != EXCLUDED[1]}
            jobs[EXCLUDED[0]]["media_type"] = "generated"
            items[EXCLUDED[0]]["provenance"] = "Meta / muse-spark-1.3"
            items[EXCLUDED[2]]["record"]["category"] = "other"
            items[EXCLUDED[2]]["quality"]["status"] = "needs_retake"
            items[EXCLUDED[3]]["image_url"] = f"/media/{EXCLUDED[3]}/reconstructions/not-original.png"
            fixture.clear()
            fixture.update(
                items=items,
                jobs=jobs,
                groups=[
                    {
                        "id": GROUP,
                        "primary_item_id": REFERENCES[0],
                        "item_ids": [TARGET, *REFERENCES, *EXCLUDED],
                        "created_at": STAMP,
                    }
                ],
                username="alice",
                authenticated=True,
                csrf="explicit-multiview-csrf",
                fail_groups=0,
                fail_jobs=0,
                expire_on_generate=False,
                next_error=None,
                attempt=0,
            )

    def principal():
        return {
            "id": ("4" if fixture["username"] == "alice" else "5") * 32,
            "username": fixture["username"],
            "display_name": "明確測試帳號 " + fixture["username"],
            "created_at": STAMP,
            "disabled": False,
        }

    def manifest(extras, normalized=True):
        value = source_manifest(extras, normalized=normalized)
        for entry in value["sources"]:
            entry["file_sha256"] = hashlib.sha256(media[entry["item_id"]]).hexdigest()
            entry["reference_sha256"] = entry["file_sha256"] if normalized else None
        return value

    def saved_reconstruction(extra=REFERENCES[0]):
        source = manifest([extra])
        validation = preview_report(tucked=True)
        validation["plan"]["reference_manifest"] = source
        path = f"/media/{TARGET}/reconstructions/old-preview.png"
        return {
            "id": "old-fixture-attempt",
            "status": "needs_review",
            "model": "fixture-no-model",
            "image_url": None,
            "preview_url": path,
            "candidate_url": path,
            "error": None,
            "validation": copy.deepcopy(validation),
            "preview_validation": copy.deepcopy(validation),
            "candidate_validation": copy.deepcopy(validation),
            "reference_manifest": copy.deepcopy(source),
            "preview_reference_manifest": copy.deepcopy(source),
            "candidate_reference_manifest": copy.deepcopy(source),
        }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, value, status=200, kind="application/json", account=True):
            raw = json.dumps(value, ensure_ascii=False).encode() if kind == "application/json" else value
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'",
            )
            if account:
                self.send_header("X-Rewear-Account", principal()["id"])
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            path = urlsplit(self.path).path
            with lock:
                report["requests"].append({"method": "GET", "path": path})
                if path == "/auth/session":
                    return self.send(
                        {
                            "mode": "hosted",
                            "authenticated": fixture["authenticated"],
                            **(
                                {"user": principal(), "csrf_token": fixture["csrf"]}
                                if fixture["authenticated"]
                                else {}
                            ),
                        },
                        account=False,
                    )
                if path.startswith(("/api/", "/media/")) and not fixture["authenticated"]:
                    return self.send({"detail": "請先登入。"}, 401)
                if path == "/api/garment-groups" and fixture["fail_groups"]:
                    fixture["fail_groups"] -= 1
                    return self.send({"detail": "明確測試：群組資料暫時無法讀取。"}, 503)
                if path == "/api/jobs" and fixture["fail_jobs"]:
                    fixture["fail_jobs"] -= 1
                    return self.send({"detail": "明確測試：來源紀錄暫時無法讀取。"}, 503)
                values = {
                    "/api/health": {
                        "status": "ok",
                        "local": {"available": True},
                        "meta": {"configured": True},
                        "reconstruction": {"available": True},
                        "discovery": {"available": False},
                        "limits": {"upload_mb": 50, "batch_files": 32, "batch_upload_mb": 256},
                    },
                    "/api/items": {"items": list(fixture["items"].values())},
                    "/api/jobs": {"jobs": list(fixture["jobs"].values())},
                    "/api/garment-groups": {"groups": fixture["groups"]},
                    "/api/duplicates": {"suggestions": []},
                    "/api/batches": {"batches": []},
                }
                if path in values:
                    return self.send(copy.deepcopy(values[path]))
                match = re.fullmatch(r"/api/items/([a-f0-9]{32})", path)
                if match and match[1] in fixture["items"]:
                    return self.send(copy.deepcopy(fixture["items"][match[1]]))
                match = re.fullmatch(r"/media/([a-f0-9]{32})/(.+)", path)
                if match and match[1] in media:
                    return self.send(
                        synthetic if "/reconstructions/" in path else media[match[1]], kind="image/png"
                    )
                relative = "index.html" if path == "/" else path.removeprefix("/static/")
                target = (STATIC / relative).resolve()
                if target.is_relative_to(STATIC) and target.is_file():
                    return self.send(
                        target.read_bytes(),
                        kind=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
                        account=False,
                    )
                return self.send({"detail": "No explicit fixture route"}, 404)

        def do_POST(self):
            path = urlsplit(self.path).path
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.loads(raw) if raw else None
            with lock:
                protection = {
                    "marker": self.headers.get("X-Rewear-Request") == "1",
                    "origin": self.headers.get("Origin") == base,
                    "csrf": self.headers.get("X-CSRF-Token") == fixture["csrf"],
                }
                if (
                    not protection["marker"]
                    or not protection["origin"]
                    or path != "/auth/login"
                    and not protection["csrf"]
                ):
                    report["contract_failures"].append({"path": path, **protection})
                    return self.send({"detail": "明確測試：請求保護不符。"}, 403)
                entry = {"path": path, "method": "POST", "has_body": bool(raw), **protection}
                if path != "/auth/login":
                    entry["body"] = body
                report["mutations"].append(entry)
                if path == "/auth/login":
                    if body.get("username") != fixture["username"] or body.get("password") != PASSWORD:
                        return self.send({"detail": "登入資訊不正確。"}, 401)
                    fixture["authenticated"] = True
                    fixture["csrf"] += "-rotated"
                    return self.send({"user": principal(), "csrf_token": fixture["csrf"]})
                match = re.fullmatch(r"/api/items/([a-f0-9]{32})/reconstruction(/cancel)?", path)
                if not match or match[1] not in fixture["items"]:
                    return self.send({"detail": "No explicit fixture mutation"}, 404)
                item = fixture["items"][match[1]]
                if match[2]:
                    assert body is None, "Cancellation retains its empty request contract"
                    item["reconstruction"]["status"] = "cancelled"
                    return self.send(copy.deepcopy(item))
                if fixture["expire_on_generate"]:
                    fixture["expire_on_generate"] = False
                    fixture["authenticated"] = False
                    return self.send({"detail": "登入已失效。"}, 401)
                if fixture["next_error"]:
                    status, message = fixture["next_error"]
                    fixture["next_error"] = None
                    return self.send({"detail": message}, status)
                extras = body.get("reference_item_ids", []) if body else []
                assert body is None or set(body) == {"reference_item_ids"}
                assert len(extras) <= 2 and len(set(extras)) == len(extras)
                group = next((g for g in fixture["groups"] if item["id"] in g["item_ids"]), None)
                if extras and (not group or any(value not in group["item_ids"] for value in extras)):
                    return self.send({"detail": "參考照片或同件群組已變更，請重新選擇。"}, 409)
                fixture["attempt"] += 1
                old = copy.deepcopy(item.get("reconstruction") or {})
                item["reconstruction"] = {
                    **old,
                    "id": "fixture-attempt-" + str(fixture["attempt"]),
                    "status": "queued",
                    "error": None,
                    "validation": None,
                    "reference_manifest": manifest(extras, normalized=False),
                }
                return self.send(copy.deepcopy(item), 202)

    reset()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    report["url"] = base
    policies = []

    def scenario(name, **details):
        report["scenarios"].append({"name": name, "status": "passed", **details})

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)

            def fresh(width=1440, prepare=None):
                reset()
                if prepare:
                    prepare()
                context = browser.new_context(
                    viewport={"width": width, "height": 1000 if width > 600 else 844}, service_workers="block"
                )
                policy = LocalNetworkPolicy.for_http(base)
                policies.append(policy)
                install_network_policy(context, policy)

                def guard(route):
                    request = route.request
                    path = urlsplit(request.url).path
                    if urlsplit(request.url).netloc != urlsplit(base).netloc:
                        report["outside_requests"].append(request.url)
                        return route.abort("blockedbyclient")
                    if request.method != "GET" and not (
                        request.method == "POST"
                        and (
                            path == "/auth/login"
                            or re.fullmatch(r"/api/items/[a-f0-9]{32}/reconstruction(?:/cancel)?", path)
                        )
                    ):
                        report["unexpected_mutations"].append({"path": path, "method": request.method})
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
                expect(page.locator(f'[data-item-id="{REFERENCES[0]}"]')).to_have_count(1)
                page.locator(f'[data-item-id="{REFERENCES[0]}"]').click()
                page.locator(f'[data-group-item="{TARGET}"]').click()
                expect(page.locator("#item-subtype")).to_have_value(
                    fixture["items"][TARGET]["record"]["subtype"]
                )
                expect(page.locator("#item-reconstruction")).to_be_visible()
                return context, page

            def picker(page):
                details = page.locator(".reconstruction-reference-picker")
                if not details.evaluate("element => element.open"):
                    details.locator("summary").click()
                return details

            def checkbox(page, ident):
                return page.locator(f'[data-reconstruction-reference="{ident}"]')

            def choose(page, *identifiers):
                picker(page)
                for ident in identifiers:
                    checkbox(page, ident).check()

            def submit(page, action="generate"):
                with page.expect_response(
                    lambda response: (
                        response.request.method == "POST"
                        and response.url.endswith(
                            "/reconstruction" + ("/cancel" if action == "cancel" else "")
                        )
                    )
                ) as response:
                    page.locator(f'[data-reconstruction-action="{action}"]').click()
                return response.value

            def refresh(page):
                with page.expect_response(lambda response: response.url.endswith("/api/items")):
                    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")

            def screenshot(page, name, selector):
                page.locator(selector).scroll_into_view_if_needed()
                page.locator("img:visible").evaluate_all(
                    "async imgs => { await Promise.all(imgs.map(img => img.decode())); }"
                )
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                assert page.locator("#item-dialog").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
                page.screenshot(path=output / "screens" / f"{name}.png")

            # Selection remains separate from switching which group record is edited.
            context, page = fresh()
            picker(page)
            expect(page.locator("[data-reconstruction-reference]")).to_have_count(3)
            expect(page.locator(".reference-primary img")).to_have_attribute(
                "src", base + fixture["items"][TARGET]["image_url"]
            )
            page.locator("#item-brand").fill("尚未儲存的衣物草稿")
            checkbox(page, REFERENCES[0]).focus()
            page.keyboard.press("Space")
            expect(checkbox(page, REFERENCES[0])).to_be_checked()
            expect(checkbox(page, REFERENCES[0])).to_be_focused()
            choose(page, REFERENCES[1])
            expect(checkbox(page, REFERENCES[2])).to_be_disabled()
            expect(page.locator(".reference-count")).to_have_text("已選 2 / 2 張其他照片")
            expect(page.locator("#item-brand")).to_have_value("尚未儲存的衣物草稿")
            screenshot(page, "two-selected-desktop", ".reference-primary")
            for ident in EXCLUDED + [OUTSIDE, TARGET]:
                expect(checkbox(page, ident)).to_have_count(0)
            scenario(
                "confirmed-group-originals-two-reference-keyboard-cap",
                candidate_count=3,
                primary_is_current_item=True,
            )

            before_record = digest(fixture["items"][TARGET]["record"])
            assert submit(page).status == 202
            last = report["mutations"][-1]
            assert last["body"] == {"reference_item_ids": REFERENCES[:2]}
            expect(page.locator('[data-reconstruction-action="cancel"]')).to_be_visible()
            for ident in REFERENCES:
                expect(checkbox(page, ident)).to_be_disabled()
            expect(page.locator("#reconstruction-current-references figure")).to_have_count(3)
            expect(page.locator("#item-brand")).to_have_value("尚未儲存的衣物草稿")
            with lock:
                fixture["items"][TARGET]["reconstruction"]["status"] = "running"
            refresh(page)
            expect(page.locator(".reconstruction-status")).to_contain_text("正在生成")
            assert submit(page, "cancel").status == 200
            expect(checkbox(page, REFERENCES[0])).to_be_enabled()
            expect(checkbox(page, REFERENCES[0])).to_be_checked()
            assert digest(fixture["items"][TARGET]["record"]) == before_record
            screenshot(page, "cancelled-inputs-retained-desktop", "#reconstruction-current-references")
            scenario(
                "json-reference-request-lock-poll-cancel-keeps-form",
                request_body=last["body"],
                record_unchanged=True,
            )
            context.close()

            context, page = fresh()
            assert submit(page).status == 202
            assert report["mutations"][-1]["body"] is None and not report["mutations"][-1]["has_body"]
            assert submit(page, "cancel").status == 200
            scenario("zero-reference-preserves-no-body-single-photo-contract")
            context.close()

            # All saved images retain their own source history across a failed newer attempt.
            def prepare_history():
                saved = saved_reconstruction()
                saved.update(
                    status="failed",
                    validation=None,
                    error="明確測試：新嘗試未能完成。",
                    reference_manifest=manifest([REFERENCES[1]], normalized=False),
                    candidate_url=f"/media/{TARGET}/reconstructions/separate-candidate.png",
                    candidate_validation=None,
                    candidate_reference_manifest=manifest([REFERENCES[2]]),
                    image_url=f"/media/{TARGET}/reconstructions/legacy-image.png",
                    image_validation=None,
                    image_reference_manifest=manifest([]),
                )
                fixture["items"][TARGET]["reconstruction"] = saved

            context, page = fresh(prepare=prepare_history)
            expect(page.locator('#item-media-region [data-item-media-key="preview"]')).to_have_attribute(
                "aria-pressed", "true"
            )
            old = page.locator("#reconstruction-image-references")
            current = page.locator("#reconstruction-current-references")
            expect(old.locator('[data-reference-role="extra"] img')).to_have_attribute(
                "src", base + fixture["items"][REFERENCES[0]]["image_url"]
            )
            expect(current.locator('[data-reference-role="extra"] img')).to_have_attribute(
                "src", base + fixture["items"][REFERENCES[1]]["image_url"]
            )
            expect(page.locator(".preview-inferences").first).to_contain_text("AI 預覽・含推測")
            screenshot(page, "old-preview-new-attempt-distinct-history", "#reconstruction-current-references")
            page.locator('#item-media-region [data-item-media-key="candidate"]').click()
            expect(old.locator('[data-reference-role="extra"] img')).to_have_attribute(
                "src", base + fixture["items"][REFERENCES[2]]["image_url"]
            )
            page.locator('#item-media-region [data-item-media-key="legacy"]').click()
            expect(old.locator("figure")).to_have_count(1)
            # Missing report manifest uses only the same image's saved fallback.
            with lock:
                fixture["items"][TARGET]["reconstruction"]["preview_validation"]["plan"].pop(
                    "reference_manifest"
                )
            refresh(page)
            page.locator('#item-media-region [data-item-media-key="preview"]').click()
            expect(old.locator('[data-reference-role="extra"] img')).to_have_attribute(
                "src", base + fixture["items"][REFERENCES[0]]["image_url"]
            )
            with lock:
                fixture["items"][TARGET]["reconstruction"].pop("candidate_reference_manifest")
            refresh(page)
            page.locator('#item-media-region [data-item-media-key="candidate"]').click()
            expect(old).to_contain_text("未記錄可確認的參考來源")
            expect(old.locator("img")).to_have_count(0)
            scenario("saved-preview-image-candidate-manifests-never-borrow-current-attempt")
            context.close()

            context, page = fresh(
                prepare=lambda: fixture["items"][TARGET].update(reconstruction=saved_reconstruction())
            )
            saved = copy.deepcopy(fixture["items"][TARGET]["reconstruction"])
            with lock:
                fixture["groups"] = []
            refresh(page)
            expect(
                page.locator("#reconstruction-image-references [data-reference-role=extra] img")
            ).to_have_attribute("src", base + fixture["items"][REFERENCES[0]]["image_url"])
            expect(picker(page)).to_contain_text("請先在「相似衣物」核對並合併")
            request_boundary = len(report["requests"])
            with lock:
                fixture["items"].pop(REFERENCES[0])
                fixture["jobs"].pop(REFERENCES[0])
            refresh(page)
            history = page.locator("#reconstruction-image-references")
            expect(history).to_contain_text("來源照片已移除")
            expect(history.locator('[data-reference-role="extra"] img')).to_have_count(0)
            expect(page.locator("#item-media-content img").last).to_have_attribute(
                "src", base + saved["preview_url"]
            )
            assert fixture["items"][TARGET]["reconstruction"] == saved
            assert not any(
                entry["path"].startswith(f"/media/{REFERENCES[0]}/")
                for entry in report["requests"][request_boundary:]
            )
            screenshot(page, "saved-preview-deleted-extra-history", "#reconstruction-image-references")
            scenario("completed-preview-keeps-own-manifest-after-ungroup-and-reference-deletion")
            context.close()

            context, page = fresh()
            choose(page, REFERENCES[0])
            page.locator("#item-brand").fill("服務失敗時仍保留")
            with lock:
                fixture["next_error"] = (503, "明確測試：重建服務暫時無法使用。")
            assert submit(page).status == 503
            expect(page.locator("#item-reconstruction .inline-error")).to_contain_text("暫時無法使用")
            expect(checkbox(page, REFERENCES[0])).to_be_checked()
            expect(page.locator("#item-brand")).to_have_value("服務失敗時仍保留")
            assert submit(page).status == 202
            assert submit(page, "cancel").status == 200
            scenario("request-error-explicit-retry-retains-selection-and-edit")
            context.close()

            context, page = fresh()
            choose(page, REFERENCES[0])
            page.locator("#item-brand").fill("群組變更不丟草稿")
            with lock:
                fixture["groups"][0]["item_ids"].remove(REFERENCES[0])
            assert submit(page).status == 409
            expect(picker(page)).to_contain_text("同件來源已變更")
            expect(page.locator('[data-reconstruction-action="generate"]')).to_be_disabled()
            expect(page.locator("#item-brand")).to_have_value("群組變更不丟草稿")
            page.locator("[data-reference-reset]").click()
            expect(page.locator('[data-reconstruction-action="generate"]')).to_be_enabled()
            expect(checkbox(page, REFERENCES[0])).to_have_count(0)
            screenshot(page, "stale-reference-reselected", ".reconstruction-reference-picker")
            scenario("server-group-conflict-prompts-reselection-without-silent-single-fallback")
            context.close()

            context, page = fresh()
            choose(page, REFERENCES[0])
            assert submit(page).status == 202
            with lock:
                fixture["groups"] = []
                fixture["items"][TARGET]["reconstruction"]["status"] = "cancelled"
                fixture["items"][TARGET]["reconstruction"]["error"] = "參考來源已變更。"
            refresh(page)
            expect(picker(page)).to_contain_text("同件來源已變更")
            expect(page.locator('[data-reconstruction-action="generate"]')).to_be_disabled()
            page.locator("[data-reference-reset]").click()
            expect(page.locator('[data-reconstruction-action="generate"]')).to_be_enabled()
            expect(picker(page)).to_contain_text("請先在「相似衣物」核對並合併")
            scenario("ungroup-cancels-inflight-inputs-require-explicit-reset")
            context.close()

            context, page = fresh()
            choose(page, REFERENCES[1])
            page.locator("#item-brand").fill("來源讀取失敗仍保留")
            with lock:
                fixture["fail_groups"] = 1
            refresh(page)
            expect(picker(page)).to_contain_text("暫時無法確認同件來源")
            expect(page.locator('[data-reconstruction-action="generate"]')).to_be_disabled()
            page.locator("[data-reference-refresh]").click()
            expect(checkbox(page, REFERENCES[1])).to_be_enabled()
            expect(checkbox(page, REFERENCES[1])).to_be_checked()
            with lock:
                fixture["fail_jobs"] = 1
            refresh(page)
            expect(page.locator('[data-reconstruction-action="generate"]')).to_be_disabled()
            page.locator("[data-reference-refresh]").click()
            expect(page.locator('[data-reconstruction-action="generate"]')).to_be_enabled()
            expect(page.locator("#item-brand")).to_have_value("來源讀取失敗仍保留")
            scenario("failed-group-and-job-refresh-block-multiview-until-recovery")
            context.close()

            context, page = fresh(width=390)
            choose(page, REFERENCES[2])
            page.locator("#item-brand").fill("手機與登入草稿🙂")
            screenshot(page, "mobile-long-name-selection", ".reference-options")
            with lock:
                fixture["expire_on_generate"] = True
            assert submit(page).status == 401
            expect(page.locator("#session-dialog")).to_be_visible()
            expect(page.locator("#item-brand")).to_have_value("手機與登入草稿🙂")
            page.locator("#reauth-password").fill(PASSWORD)
            page.locator('#reauth-form button[type="submit"]').click()
            expect(page.locator("#session-dialog")).not_to_be_visible()
            expect(checkbox(page, REFERENCES[2])).to_be_checked()
            expect(page.locator("#item-brand")).to_have_value("手機與登入草稿🙂")
            # A broadcast contains only a non-secret change signal. The next session read detects B.
            with lock:
                fixture["username"] = "bob"
            page.evaluate(
                "{ const channel = new BroadcastChannel('rewear-session'); channel.postMessage('session-changed'); channel.close(); }"
            )
            expect(page.locator("#session-title")).to_have_text("帳號已變更。")
            expect(page.locator("#item-dialog-body")).to_be_empty()
            expect(page.locator("[data-reconstruction-reference]")).to_have_count(0)
            assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
            scenario("mobile-long-labels-same-account-reauth-keeps-drafts-cross-account-clears")
            context.close()

            # Mobile generation and own-image provenance remain readable without inferred fidelity claims.
            context, page = fresh(
                width=390,
                prepare=lambda: fixture["items"][TARGET].update(reconstruction=saved_reconstruction()),
            )
            picker(page)
            choose(page, REFERENCES[1])
            assert submit(page).status == 202
            expect(page.locator("#reconstruction-current-references figure")).to_have_count(2)
            expect(page.locator("#reconstruction-image-references figure")).to_have_count(2)
            screenshot(page, "mobile-running-old-preview-own-sources", "#reconstruction-current-references")
            assert submit(page, "cancel").status == 200
            page.locator('#item-media-region [data-item-media-key="preview"]').click()
            screenshot(page, "mobile-original-and-ai-preview", "#item-media-region")
            expect(page.locator("#item-media-content figure")).to_have_count(2)
            expect(page.locator("#item-media-caption")).to_contain_text("AI")
            scenario("mobile-running-and-cancelled-preview-keeps-original-pair-and-own-history")
            context.close()

            assert not report["page_errors"] and not report["contract_failures"]
            assert not report["outside_requests"] and not report["unexpected_mutations"]
            assert all("Failed to load resource" in message for message in report["console_errors"]), report[
                "console_errors"
            ]
            browser.close()
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        report["error_type"] = type(error).__name__
        report["error"] = str(error)
        raise
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        report["server_stopped"] = not thread.is_alive()
        report["blocked_requests"] = sum(policy.blocked_requests for policy in policies)
        report["passed_count"] = len(report["scenarios"])
        report["source_image_sha256"] = {
            key: hashlib.sha256(value).hexdigest() for key, value in media.items()
        }
        report["script_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {"status": report["status"], "passed": len(report["scenarios"]), "output": str(output)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
