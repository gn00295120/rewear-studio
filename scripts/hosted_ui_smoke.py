"""Exercise hosted-account UI with explicit, ephemeral loopback HTTP fixtures.

The fixture implements the HTTP contract, not production password cryptography.
It imports no wardrobe runtime, touches no user database, and calls no model.
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
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image, ImageDraw
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "wardrobe/static"
sys.path.insert(0, str(Path.home() / ".agents/skills/webapp-testing/scripts"))
from network_policy import LocalNetworkPolicy, install_network_policy  # noqa: E402

STAMP = "2026-10-22T05:00:00Z"
PASSWORD = "Explicit fixture password 123"
NEW_PASSWORD = "已更新的明確測試密碼🙂123"
RECOVERY = "0123456789abcdef" * 4
NEW_RECOVERY = "fedcba9876543210" * 4


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def user(username):
    number = {"alice": "1", "bob": "2"}.get(username, "3")
    return {
        "id": number * 32,
        "username": username,
        "display_name": {"alice": "愛麗絲的衣物室", "bob": "柏宇的衣物室"}.get(username, "新使用者"),
        "created_at": STAMP,
        "disabled": False,
    }


def item(username):
    number = {"alice": "a", "bob": "b"}.get(username, "c")
    job = {"alice": "d", "bob": "e"}.get(username, "f") * 32
    return {
        "id": number * 32,
        "job_id": job,
        "created_at": STAMP,
        "updated_at": STAMP,
        "reviewed": False,
        "provenance": "Explicit hosted UI fixture",
        "record": {
            "category": "top",
            "subtype": f"{username}-only garment",
            "brand": None,
            "colors": ["深綠色"],
            "pattern": "素色",
            "material_guess": None,
            "size_label": None,
            "condition": "unknown",
            "defects": [],
            "confidence": 0.9,
            "needs_review": True,
        },
        "quality": {"status": "ready", "visibility": "complete", "reasons": []},
        "image_url": f"/media/{job}/crops/context-fixture.jpg",
        "cutout_url": f"/media/{job}/crops/crop-fixture.jpg",
        "source_url": f"/media/{job}/preview.jpg",
        "original_url": f"/media/{job}/preview.jpg",
        "reconstruction": None,
        "discovery": None,
    }


def job(value):
    return {
        "id": value["job_id"],
        "status": "completed",
        "stage": "completed",
        "progress": 100,
        "filename": "explicit-hosted-fixture.jpg",
        "provider": "local",
        "mode": "single",
        "media_type": "image",
        "item_count": 1,
        "usable_count": 1,
        "created_at": STAMP,
    }


def health():
    return {
        "status": "ok",
        "local": {"available": True, "model": "Explicit fixture"},
        "meta": {"configured": True},
        "reconstruction": {"available": True},
        "discovery": {"available": True},
        "ffmpeg": True,
        "limits": {"upload_mb": 50, "batch_files": 32, "batch_upload_mb": 256},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    assert not (output / "report.json").exists(), "Use a fresh evidence directory."
    (output / "screens").mkdir(exist_ok=True)
    fixture = {}
    report = {
        "status": "running",
        "scope": "Explicit ephemeral HTTP account fixtures, no production runtime/database/models. Actual hosted backend is tested separately by root.",
        "mechanism": "Native ThreadingHTTPServer; headless Playwright; LocalNetworkPolicy installed before new_page; service workers blocked; CSP enforced. No supervisor sandbox receipt claimed.",
        "static_sha256": {
            name: sha(STATIC / name)
            for name in ("app.js", "index.html", "styles.css", "auth.html", "auth.js", "auth.css")
        },
        "script_sha256": sha(Path(__file__)),
        "scenarios": [],
        "page_errors": [],
        "console_errors": [],
        "outside_requests": [],
        "requests": [],
        "contract_failures": [],
        "unexpected_mutations": [],
    }
    picture = Image.new("RGB", (300, 400), "#eee9de")
    draw = ImageDraw.Draw(picture)
    draw.rectangle((80, 75, 220, 310), fill="#364b40")
    draw.text((25, 350), "EXPLICIT ACCOUNT FIXTURE", fill="#364b40")
    buffer = io.BytesIO()
    picture.save(buffer, "JPEG")
    image_bytes = buffer.getvalue()

    def reset(mode="hosted"):
        fixture.clear()
        fixture.update(
            mode=mode,
            users={name: user(name) for name in ("alice", "bob")},
            passwords={name: PASSWORD for name in ("alice", "bob")},
            recovery={name: RECOVERY for name in ("alice", "bob")},
            items={name: item(name) for name in ("alice", "bob")},
            sessions={},
            serial=0,
            session_failures=0,
            rate_login=False,
            expire_patch=False,
            mismatch_path=None,
            missing_header=False,
            deleted_users=set(),
            storage={},
            storage_failures=0,
            quota_code=None,
            cleanup_header="complete",
            cleanup_failures=0,
            cleanup_pending_once=False,
        )

    def storage_state(username):
        return fixture["storage"].setdefault(
            username,
            {
                "mode": "hosted",
                "limits": {"jobs": 5, "media_bytes": 20 * 1024**2},
                "usage": {
                    "jobs": 1,
                    "media_bytes": 2 * 1024**2,
                    "reserved_jobs": 1,
                    "reserved_bytes": 1024**2,
                },
                "cleanup": {"pending_directories": 0, "error": None},
                "notice": "Explicit fixture: measured files plus in-flight reservations.",
            },
        )

    def make_session(username):
        fixture["serial"] += 1
        sid = f"explicit-fixture-session-{fixture['serial']}"
        session = {"username": username, "csrf": f"explicit-fixture-csrf-{fixture['serial']}"}
        fixture["sessions"][sid] = session
        return sid, session

    def revoke(username):
        fixture["sessions"] = {
            sid: session for sid, session in fixture["sessions"].items() if session["username"] != username
        }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def session(self):
            cookies = SimpleCookie()
            cookies.load(self.headers.get("Cookie", ""))
            sid = cookies["fixture_sid"].value if "fixture_sid" in cookies else ""
            return fixture["sessions"].get(sid)

        def send(
            self,
            body=None,
            code=200,
            content_type="application/json",
            principal=None,
            cookie=None,
            cleanup=None,
        ):
            raw = (
                b""
                if code == 204
                else json.dumps(body, ensure_ascii=False).encode()
                if content_type == "application/json"
                else body
            )
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'",
            )
            if principal and fixture["mode"] == "hosted":
                self.send_header("X-Rewear-Account", fixture["users"][principal]["id"])
            if cleanup is not None:
                self.send_header("X-Rewear-Cleanup", cleanup)
            if cookie is not None:
                self.send_header(
                    "Set-Cookie",
                    f"fixture_sid={cookie}; Path=/; HttpOnly; SameSite=Lax"
                    + ("; Max-Age=0" if not cookie else ""),
                )
            self.end_headers()
            if raw:
                self.wfile.write(raw)

        def do_GET(self):
            path = urlsplit(self.path).path
            report["requests"].append({"method": "GET", "path": path})
            session = self.session()
            if path == "/auth/session":
                if fixture["session_failures"]:
                    fixture["session_failures"] -= 1
                    return self.send({"detail": "明確測試：帳號服務暫時無法連線。"}, 503)
                if fixture["mode"] == "local":
                    return self.send({"mode": "local", "authenticated": False})
                return self.send(
                    {
                        "mode": "hosted",
                        "authenticated": bool(session),
                        **(
                            {"user": fixture["users"][session["username"]], "csrf_token": session["csrf"]}
                            if session
                            else {}
                        ),
                    }
                )
            if path.startswith(("/api/", "/media/")):
                if fixture["mode"] == "hosted" and not session:
                    return self.send({"detail": "請先登入。"}, 401)
                principal = session["username"] if session else "alice"
                if fixture["mismatch_path"] == path:
                    fixture["mismatch_path"] = None
                    time.sleep(0.15)  # Allow other same-account responses to settle first.
                    principal = "bob"
                value = fixture["items"][principal]
                deleted = principal in fixture["deleted_users"]
                if path == "/api/storage":
                    if fixture["mode"] == "local":
                        return self.send({"detail": "No storage endpoint in local mode"}, 404)
                    if fixture["storage_failures"]:
                        fixture["storage_failures"] -= 1
                        return self.send(
                            {"detail": "無法確認帳號儲存容量，請稍後重試。"}, 503, principal=principal
                        )
                    return self.send(copy.deepcopy(storage_state(principal)), principal=principal)
                values = {
                    "/api/health": health(),
                    "/api/items": {"items": [] if deleted else [value]},
                    "/api/jobs": {"jobs": [] if deleted else [job(value)]},
                    "/api/batches": {"batches": []},
                    "/api/garment-groups": {"groups": []},
                    "/api/duplicates": {"suggestions": []},
                    f"/api/items/{value['id']}": value,
                    f"/api/jobs/{value['job_id']}": job(value),
                    "/api/export": {"items": [value]},
                }
                if path in values:
                    if fixture["missing_header"] and path == "/api/items":
                        fixture["missing_header"] = False
                        return self.send(copy.deepcopy(values[path]))
                    return self.send(copy.deepcopy(values[path]), principal=principal)
                if path.startswith(f"/media/{value['job_id']}/"):
                    return self.send(image_bytes, content_type="image/jpeg", principal=principal)
                return self.send({"detail": "Fixture route not found"}, 404, principal=principal)
            relative = {"/": "index.html", "/login": "auth.html"}.get(path, path.removeprefix("/static/"))
            target = (STATIC / relative).resolve()
            if target.is_relative_to(STATIC) and target.is_file():
                return self.send(
                    target.read_bytes(),
                    content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
                )
            return self.send({"detail": "Fixture route not found"}, 404)

        def mutate(self):
            path = urlsplit(self.path).path
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            session = self.session()
            is_json = self.headers.get("Content-Type", "").startswith("application/json")
            body = json.loads(raw or b"{}") if is_json else {}
            origin_ok = self.headers.get("Origin") == f"http://127.0.0.1:{self.server.server_port}"
            request_ok = self.headers.get("X-Rewear-Request") == "1"
            csrf_ok = bool(session and self.headers.get("X-CSRF-Token") == session["csrf"])
            entry = {
                "method": self.command,
                "path": path,
                "origin_ok": origin_ok,
                "request_marker": request_ok,
                "csrf_valid": csrf_ok,
                "json_fields": sorted(body),
                "multipart": self.headers.get("Content-Type", "").startswith("multipart/form-data;"),
            }
            report["requests"].append(entry)
            if fixture["mode"] == "hosted" and (not origin_ok or not request_ok or (session and not csrf_ok)):
                report["contract_failures"].append(entry)
                return self.send({"detail": "Fixture rejected invalid request protection"}, 403)
            if path in ("/auth/register", "/auth/login", "/auth/recover"):
                username = body.get("username", "").casefold()
                if path.endswith("login"):
                    if fixture["rate_login"]:
                        fixture["rate_login"] = False
                        return self.send({"detail": "操作過於頻繁。"}, 429)
                    if fixture["passwords"].get(username) != body.get("password"):
                        return self.send({"detail": "帳號或密碼不正確。"}, 401)
                elif path.endswith("register"):
                    if username in fixture["users"]:
                        return self.send({"detail": "無法使用這個帳號名稱。"}, 409)
                    fixture["users"][username] = {**user(username), "display_name": body["display_name"]}
                    fixture["passwords"][username] = body["password"]
                    fixture["recovery"][username] = RECOVERY
                    fixture["items"][username] = item(username)
                else:
                    if fixture["recovery"].get(username) != body.get("recovery_code"):
                        return self.send({"detail": "帳號或復原碼不正確。"}, 401)
                    fixture["passwords"][username] = body["new_password"]
                    fixture["recovery"][username] = NEW_RECOVERY
                    revoke(username)
                    return self.send({"recovery_code": NEW_RECOVERY})
                sid, current = make_session(username)
                return self.send(
                    {
                        "user": fixture["users"][username],
                        "csrf_token": current["csrf"],
                        **({"recovery_code": RECOVERY} if path.endswith("register") else {}),
                    },
                    cookie=sid,
                )
            if fixture["mode"] == "hosted" and not session:
                return self.send({"detail": "請先登入。"}, 401)
            principal = session["username"] if session else "alice"
            if path == "/auth/logout":
                revoke(principal)
                return self.send(code=204, cookie="")
            if path == "/auth/password":
                if fixture["passwords"][principal] != body.get("current_password"):
                    return self.send({"detail": "帳號或密碼不正確。"}, 401)
                fixture["passwords"][principal] = body["new_password"]
                revoke(principal)
                sid, current = make_session(principal)
                return self.send(
                    {"user": fixture["users"][principal], "csrf_token": current["csrf"]}, cookie=sid
                )
            value = fixture["items"][principal]
            if self.command == "DELETE" and path == f"/api/jobs/{value['job_id']}":
                fixture["deleted_users"].add(principal)
                current = storage_state(principal)
                current["usage"].update(jobs=0, reserved_jobs=0, reserved_bytes=0)
                pending = fixture["mode"] == "hosted" and fixture["cleanup_header"] == "pending"
                current["usage"]["media_bytes"] = 2 * 1024**2 if pending else 0
                current["cleanup"] = {
                    "pending_directories": int(pending),
                    "error": "明確測試：檔案清理暫時失敗。" if pending else None,
                }
                return self.send(
                    code=204,
                    principal=principal,
                    cleanup=fixture["cleanup_header"] if fixture["mode"] == "hosted" else None,
                )
            if path == "/api/storage/cleanup":
                current = storage_state(principal)
                assert current["cleanup"]["pending_directories"] > 0
                if fixture["cleanup_failures"]:
                    fixture["cleanup_failures"] -= 1
                    return self.send({"detail": "明確測試：清理服務暫時無法連線。"}, 503, principal=principal)
                if fixture["cleanup_pending_once"]:
                    fixture["cleanup_pending_once"] = False
                    return self.send(copy.deepcopy(current), principal=principal)
                current["usage"]["media_bytes"] = 0
                current["cleanup"] = {"pending_directories": 0, "error": None}
                return self.send(copy.deepcopy(current), principal=principal)
            if self.command == "PATCH" and path == f"/api/items/{value['id']}":
                if fixture["expire_patch"]:
                    fixture["expire_patch"] = False
                    revoke(principal)
                    return self.send({"detail": "請先登入。"}, 401)
                value["record"].update(body["record"])
                value["reviewed"] = body["reviewed"]
                return self.send(copy.deepcopy(value), principal=principal)
            if path == "/api/jobs" and entry["multipart"]:
                assert b'name="file"' in raw and image_bytes in raw
                if fixture["quota_code"]:
                    code = fixture["quota_code"]
                    detail = (
                        "帳號儲存空間不足，請先刪除工作或重試清理待刪檔案；仍可讀取、匯出與取消。"
                        if code == 507
                        else "已達帳號保留工作上限，請先刪除不需要的工作；仍可讀取、匯出與取消。"
                    )
                    return self.send({"detail": detail}, code, principal=principal)
                return self.send(job(value), 202, principal=principal)
            report["unexpected_mutations"].append(entry)
            return self.send({"detail": "Fixture mutation not authorized"}, 405, principal=principal)

        do_POST = mutate
        do_PATCH = mutate
        do_DELETE = mutate

    reset()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    policies = []

    def record(name, **details):
        report["scenarios"].append({"name": name, "status": "passed", **details})
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)

            def new_context(width=1440, mode="hosted"):
                reset(mode)
                context = browser.new_context(
                    viewport={"width": width, "height": 1000 if width > 600 else 844},
                    service_workers="block",
                    accept_downloads=True,
                )
                policy = LocalNetworkPolicy.for_http(base)
                policies.append(policy)
                install_network_policy(context, policy)

                def guard(route):
                    request = route.request
                    parsed = urlsplit(request.url)
                    if parsed.netloc != urlsplit(base).netloc:
                        report["outside_requests"].append(request.url)
                        return route.abort("blockedbyclient")
                    allowed = (
                        request.method == "GET"
                        or (
                            request.method == "POST"
                            and parsed.path
                            in (
                                "/auth/login",
                                "/auth/register",
                                "/auth/recover",
                                "/auth/password",
                                "/auth/logout",
                                "/api/jobs",
                                "/api/storage/cleanup",
                            )
                        )
                        or (request.method == "PATCH" and re.fullmatch(r"/api/items/[a-f]{32}", parsed.path))
                        or (request.method == "DELETE" and re.fullmatch(r"/api/jobs/[a-f]{32}", parsed.path))
                    )
                    if not allowed:
                        report["unexpected_mutations"].append({"method": request.method, "path": parsed.path})
                        return route.abort("blockedbyclient")
                    route.fallback()

                context.route("**/*", guard)
                context.on("page", attach)
                page = context.new_page()
                return context, page

            def attach(page):
                page.set_default_timeout(12000)
                page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
                page.on(
                    "console",
                    lambda message: (
                        report["console_errors"].append(message.text) if message.type == "error" else None
                    ),
                )

            def goto(page, path="/login"):
                page.goto(base + path, wait_until="networkidle")

            def login(page, username="alice", password=PASSWORD):
                expect(page.locator("#login-form")).to_be_visible()
                page.locator("#login-username").fill(username)
                page.locator("#login-password").fill(password)
                page.locator('#login-form [type="submit"]').click()
                expect(page.locator("#account-display-name")).to_have_text(
                    fixture["users"][username]["display_name"]
                )
                expect(page.locator(f'[data-item-id="{fixture["items"][username]["id"]}"]')).to_have_count(1)

            def storage_empty(page):
                assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
                assert "fixture_sid" not in page.evaluate("document.cookie")

            def screenshot(page, name, selector=None):
                page.locator("dialog[open]").evaluate_all(
                    "async els => { await Promise.all(els.flatMap(el => el.getAnimations()).map(a => a.finished)); }"
                )
                if selector:
                    page.locator(selector).scroll_into_view_if_needed()
                page.locator("img:visible").evaluate_all(
                    "async imgs => { await Promise.all(imgs.map(img => img.decode())); }"
                )
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), (
                    "page overflow"
                )
                assert page.locator("dialog[open]").evaluate_all(
                    "els => els.every(el => el.scrollWidth <= el.clientWidth + 1)"
                ), "dialog overflow"
                page.screenshot(path=output / "screens" / f"{name}.png")

            def open_item(page, username="alice"):
                page.locator(f'[data-item-id="{fixture["items"][username]["id"]}"]').click()
                expect(page.locator("#item-subtype")).to_be_visible()

            def request_delete(page):
                page.locator('[data-view="history"]').click()
                page.locator(f'[data-job-id="{fixture["items"]["alice"]["job_id"]}"]').first.click()
                page.locator('[data-job-action="delete"]').click()
                expect(page.locator("#confirm-dialog")).to_be_visible()

            # Local mode keeps the established anonymous workspace and API shape.
            context, page = new_context(mode="local")
            first_request = len(report["requests"])
            goto(page, "/")
            expect(page.locator('[data-item-id="' + "a" * 32 + '"]')).to_have_count(1)
            expect(page.locator("#account-button")).to_be_hidden()
            expect(page.locator("#provider-note")).to_contain_text("照片留在這台裝置")
            screenshot(page, "local-unchanged-1440")
            request_delete(page)
            expect(page.locator("#confirm-message")).to_contain_text("來源媒體會保留在本機回收目錄")
            expect(page.locator("#confirm-message")).not_to_contain_text("永久移除")
            page.locator("#confirm-submit").click()
            expect(page.locator("#toast-region")).to_contain_text("來源媒體保留於本機回收目錄")
            assert not any(
                entry["path"].startswith("/api/storage") for entry in report["requests"][first_request:]
            )
            goto(page)
            expect(page.locator("#auth-local")).to_be_visible()
            expect(page.locator("#auth-content")).to_be_hidden()
            storage_empty(page)
            context.close()
            record("local-mode-no-login-controls", viewport=1440)

            # Registration, code-point length handling, explicit save gate and code download.
            context, page = new_context()
            goto(page)
            page.locator("#tab-login").focus()
            page.keyboard.press("ArrowRight")
            expect(page.locator("#tab-register")).to_be_focused()
            page.locator("#register-display-name").fill("新使用者 <不執行 HTML>🙂")
            page.locator("#register-username").fill("new.person")
            too_long = "🙂" * 129
            page.locator("#register-password").fill(too_long)
            page.locator("#register-confirm").fill(too_long)
            before = sum(entry["path"] == "/auth/register" for entry in report["requests"])
            page.locator('#register-form [type="submit"]').click()
            expect(page.locator("#register-error")).to_contain_text("12–128")
            assert page.locator("#register-password").input_value() == too_long
            assert sum(entry["path"] == "/auth/register" for entry in report["requests"]) == before
            unicode_password = "🙂" * 12
            page.locator("#register-password").fill(unicode_password)
            page.locator("#register-confirm").fill(unicode_password)
            page.locator('#register-form [type="submit"]').click()
            expect(page.locator("#recovery-card")).to_be_visible()
            expect(page.locator("#recovery-continue")).to_be_disabled()
            assert fixture["passwords"]["new.person"] == unicode_password
            expect(page.locator("#recovery-value")).to_have_value(RECOVERY)
            page.locator("#recovery-copy").click()
            expect(page.locator("#recovery-copy-status")).not_to_be_empty()
            context.grant_permissions(["clipboard-read", "clipboard-write"], origin=base)
            page.locator("#recovery-copy").click()
            expect(page.locator("#recovery-copy-status")).to_contain_text("已複製")
            assert page.evaluate("navigator.clipboard.readText()") == RECOVERY
            with page.expect_download() as info:
                page.locator("#recovery-download").click()
            downloaded = Path(info.value.path()).read_bytes()
            assert RECOVERY.encode() in downloaded and b"new.person" in downloaded
            storage_empty(page)
            screenshot(page, "register-recovery-1440")
            page.locator("#recovery-saved").check()
            page.locator("#recovery-continue").click()
            expect(page.locator("#account-display-name")).to_have_text("新使用者 <不執行 HTML>🙂")
            expect(page.locator("#account-display-name img")).to_have_count(0)
            storage_empty(page)
            screenshot(page, "registered-wardrobe-1440")
            context.close()
            record(
                "registration-unicode-one-time-recovery-save-gate",
                viewport=1440,
                recovery_download_sha256=hashlib.sha256(downloaded).hexdigest(),
                truncation=False,
                persistent_secrets=False,
            )

            # Password update, multipart CSRF, checked export and logout at phone width.
            context, page = new_context(390)
            goto(page)
            screenshot(page, "login-390", "#login-form")
            login(page)
            expect(page.locator("#provider-note")).to_contain_text("照片會上傳至衣序伺服器")
            page.locator("#file-input").set_input_files(
                {"name": "explicit-fixture.jpg", "mimeType": "image/jpeg", "buffer": image_bytes}
            )
            page.locator("#upload-button").click()
            expect(page.locator("#file-queue [data-remove-file]")).to_have_count(0)
            uploads = [
                entry
                for entry in report["requests"]
                if entry["method"] == "POST" and entry["path"] == "/api/jobs"
            ]
            assert uploads and uploads[-1]["csrf_valid"] and uploads[-1]["multipart"]
            page.locator("#account-button").click()
            page.locator("#current-password").fill("explicit wrong current password")
            page.locator("#new-password").fill(NEW_PASSWORD)
            page.locator("#confirm-password").fill(NEW_PASSWORD)
            page.locator('#password-form [type="submit"]').click()
            expect(page.locator("#password-error")).to_contain_text("帳號或密碼不正確")
            expect(page.locator("#session-dialog")).not_to_be_visible()
            expect(page.locator("#new-password")).to_have_value(NEW_PASSWORD)
            page.locator("#current-password").fill(PASSWORD)
            page.locator('#password-form [type="submit"]').click()
            expect(page.locator("#password-success")).to_contain_text("密碼已更新")
            expect(page.locator("#new-password")).to_have_value("")
            assert fixture["passwords"]["alice"] == NEW_PASSWORD
            assert len(fixture["sessions"]) == 1
            screenshot(page, "account-password-390")
            page.locator('[data-close="account-dialog"]').click()
            page.locator(".export-menu summary").click()
            with page.expect_download() as info:
                page.locator('a[href="/api/export?format=json"]').click()
            exported = json.loads(Path(info.value.path()).read_bytes())
            assert exported["items"][0]["id"] == "a" * 32
            page.locator("#account-button").click()
            page.locator("#logout-button").click()
            expect(page.locator("#login-form")).to_be_visible()
            assert "alice-only garment" not in page.content()
            assert not fixture["sessions"]
            storage_empty(page)
            login(page, password=NEW_PASSWORD)
            screenshot(page, "hosted-wardrobe-390")
            context.close()
            record("mobile-login-password-multipart-csrf-export-logout", viewport=390, protected_export=True)

            # Recovery is an explicit, non-email path; no automatic sign-in.
            context, page = new_context(390)
            goto(page)
            page.locator("#tab-recover").click()
            page.locator("#recover-username").fill("alice")
            page.locator("#recover-code").fill(RECOVERY)
            page.locator("#recover-password").fill(NEW_PASSWORD)
            page.locator("#recover-confirm").fill(NEW_PASSWORD)
            page.locator('#recover-form [type="submit"]').click()
            expect(page.locator("#recovery-value")).to_have_value(NEW_RECOVERY)
            expect(page.locator("#recovery-intro")).to_contain_text("舊復原碼失效")
            expect(page.locator("#recovery-continue")).to_be_disabled()
            assert not fixture["sessions"]
            screenshot(page, "recovered-new-code-390", "#recovery-card")
            page.locator("#recovery-saved").check()
            page.locator("#recovery-continue").click()
            expect(page.locator("#login-username")).to_have_value("alice")
            expect(page.locator("#recovery-value")).to_have_value("")
            assert not fixture["sessions"]
            login(page, password=NEW_PASSWORD)
            storage_empty(page)
            context.close()
            record("mobile-recovery-rotates-code-without-auto-login", viewport=390)

            # Clear actionable error states; editable values are retained, never logged.
            context, page = new_context()
            goto(page)
            page.locator("#login-username").fill("alice")
            page.locator("#login-password").fill("explicit wrong password")
            page.locator('#login-form [type="submit"]').click()
            expect(page.locator("#login-error")).to_contain_text("帳號或密碼不正確")
            expect(page.locator("#login-password")).to_have_value("explicit wrong password")
            expect(page.locator("#login-password")).to_be_editable()
            fixture["rate_login"] = True
            page.locator("#login-password").fill(PASSWORD)
            page.locator('#login-form [type="submit"]').click()
            expect(page.locator("#login-error")).to_contain_text("請稍候再試")
            screenshot(page, "login-rate-limit-1440")
            login(page)
            context.close()
            record("failed-login-and-429-recoverable-inputs", viewport=1440)

            # A 401 after successful preflight keeps the unsaved edit, resumes only A.
            context, page = new_context()
            goto(page)
            login(page)
            open_item(page)
            draft = "尚未儲存的同帳號草稿🙂"
            page.locator("#item-subtype").fill(draft)
            fixture["expire_patch"] = True
            page.locator('#item-form [type="submit"]').first.click()
            expect(page.locator("#session-dialog")).to_be_visible()
            expect(page.locator("#item-subtype")).to_have_value(draft)
            expect(page.locator("#reauth-username")).to_have_value("alice")
            expect(page.locator("#reauth-username")).not_to_be_editable()
            assert fixture["items"]["alice"]["record"]["subtype"] == "alice-only garment"
            page.keyboard.press("Escape")
            expect(page.locator("#session-dialog")).to_be_visible()
            screenshot(page, "expired-draft-retained-1440")
            page.locator("#reauth-password").fill(PASSWORD)
            page.locator('#reauth-form [type="submit"]').click()
            expect(page.locator("#session-dialog")).not_to_be_visible()
            expect(page.locator("#item-subtype")).to_have_value(draft)
            page.locator('#item-form [type="submit"]').first.click()
            expect(page.locator("#item-dialog")).not_to_be_visible()
            assert fixture["items"]["alice"]["record"]["subtype"] == draft
            context.close()
            record("expired-session-after-preflight-retains-draft", viewport=1440)

            # Shared cookie changes are broadcast without secrets; A is cleared, B reloads.
            context, page = new_context(390)
            goto(page)
            login(page)
            open_item(page)
            page.locator("#item-subtype").fill("A 帳號未儲存私人草稿")
            second = context.new_page()
            goto(second)
            login(second, "bob")
            expect(page.locator("#session-title")).to_have_text("帳號已變更。")
            expect(page.locator("#reauth-form")).to_be_hidden()
            expect(page.locator("#item-dialog-body")).to_be_empty()
            expect(page.locator("#items-region")).to_be_empty()
            assert "A 帳號未儲存私人草稿" not in page.content()
            assert "alice-only garment" not in page.content()
            assert "bob-only garment" not in page.content()
            screenshot(page, "cross-tab-cleared-390")
            page.locator("#session-leave").click()
            expect(page.locator("#account-display-name")).to_have_text(
                fixture["users"]["bob"]["display_name"]
            )
            expect(page.locator('[data-item-id="' + "b" * 32 + '"]')).to_have_count(1)
            storage_empty(page)
            context.close()
            record(
                "cross-tab-account-change-clears-old-data",
                viewport=390,
                broadcast_payload="session-changed only",
            )

            # A response can be bound to B even after preflight A; reject all settled data.
            context, page = new_context()
            goto(page)
            login(page)
            fixture["mismatch_path"] = "/api/jobs"
            page.locator('[data-view="history"]').click()
            page.locator("#history-refresh").click()
            expect(page.locator("#session-title")).to_have_text("帳號已變更。")
            expect(page.locator("#items-region")).to_be_empty()
            expect(page.locator("#jobs-region")).to_be_empty()
            expect(page.locator("#job-count")).to_be_hidden()
            assert "alice-only garment" not in page.content()
            assert "bob-only garment" not in page.content()
            screenshot(page, "response-account-mismatch-1440")
            context.close()
            record("response-account-binding-and-settled-request-race", viewport=1440)

            # Fail closed on missing mode endpoint; retry re-enables normal operation.
            context, page = new_context()
            fixture["session_failures"] = 1
            before = len(report["requests"])
            goto(page, "/")
            expect(page.locator("#account-bootstrap")).to_be_visible()
            assert page.locator("#main-content").evaluate("el => el.inert")
            assert not any(entry["path"].startswith("/api/") for entry in report["requests"][before:])
            screenshot(page, "session-service-unavailable-1440")
            page.locator("#account-bootstrap-retry").click()
            expect(page.locator("#login-form")).to_be_visible()
            login(page)
            context.close()
            record("session-bootstrap-fails-closed-and-retries", viewport=1440)

            # Missing account header must not be mistaken for legacy local mode.
            context, page = new_context()
            goto(page)
            login(page)
            open_item(page)
            page.locator("#item-subtype").fill("未確認回應時保留草稿")
            other = context.new_page()
            goto(other)
            login(other)  # Same-account session rotation preserves this page's draft.
            expect(page.locator("#item-subtype")).to_have_value("未確認回應時保留草稿")
            other.close()
            page.bring_to_front()
            fixture["missing_header"] = True
            page.locator("#history-refresh").dispatch_event(
                "click"
            )  # Background-refresh path while editor is open.
            expect(page.locator("#session-dialog")).to_be_visible()
            expect(page.locator("#session-message")).to_contain_text("無法確認服務回應所屬帳號")
            expect(page.locator("#item-subtype")).to_have_value("未確認回應時保留草稿")
            page.locator("#reauth-password").fill(PASSWORD)
            page.locator('#reauth-form [type="submit"]').click()
            expect(page.locator("#session-dialog")).not_to_be_visible()
            expect(page.locator("#item-subtype")).to_have_value("未確認回應時保留草稿")
            context.close()
            record("missing-account-header-fails-closed-with-draft", viewport=1440)

            # Quota is a capacity problem, not a sign-in failure or a generic retry timer.
            context, page = new_context()
            goto(page)
            login(page)
            fixture["quota_code"] = 507
            page.locator("#file-input").set_input_files(
                {"name": "explicit-fixture.jpg", "mimeType": "image/jpeg", "buffer": image_bytes}
            )
            page.locator("#upload-button").click()
            expect(page.locator("#upload-error")).to_contain_text("帳號儲存空間不足")
            expect(page.locator("#upload-error")).to_contain_text("處理紀錄")
            expect(page.locator("#file-queue [data-remove-file]")).to_have_count(1)
            expect(page.locator("#storage-alert")).to_contain_text("上次工作需要的儲存空間不足")
            page.locator("#storage-manage").click()
            expect(page.locator("#storage-jobs")).to_have_text("1 / 5 筆")
            expect(page.locator("#storage-bytes")).to_have_text("2 MB / 20 MB")
            expect(page.locator("#storage-reserved")).to_contain_text("另預留 1 筆、1 MB")
            expect(page.locator("#storage-pending")).to_be_hidden()
            screenshot(page, "storage-capacity-1440", "#account-storage")
            page.locator("#storage-history").click()
            request_delete(page)
            expect(page.locator("#confirm-message")).to_contain_text("本次照片／影片及生成圖將永久移除")
            expect(page.locator("#confirm-message")).to_contain_text("無法復原")
            screenshot(page, "hosted-permanent-delete-1440")
            page.locator("#confirm-submit").click()
            expect(page.locator("#toast-region")).to_contain_text("已永久移除")
            expect(page.locator("#jobs-region .job-row")).to_have_count(0)
            expect(page.locator("#storage-alert")).not_to_contain_text("媒體仍待清理")
            page.locator("#account-button").click()
            expect(page.locator("#storage-jobs")).to_have_text("0 / 5 筆")
            expect(page.locator("#storage-bytes")).to_have_text("0 B / 20 MB")
            expect(page.locator("#storage-cleanup")).to_be_hidden()
            context.close()
            record("quota-507-usage-reservations-and-permanent-delete", viewport=1440)

            # A committed deletion can leave media pending. Failure and repeat cleanup are honest.
            context, page = new_context(390)
            goto(page)
            login(page)
            fixture["cleanup_header"] = "pending"
            fixture["cleanup_failures"] = 1
            fixture["cleanup_pending_once"] = True
            request_delete(page)
            page.locator("#confirm-submit").click()
            expect(page.locator("#storage-alert")).to_contain_text("紀錄已刪除，但媒體仍待清理")
            expect(page.locator("#jobs-region .job-row")).to_have_count(0)
            page.locator("#storage-manage").click()
            expect(page.locator("#storage-jobs")).to_have_text("0 / 5 筆")
            expect(page.locator("#storage-bytes")).to_have_text("2 MB / 20 MB")
            expect(page.locator("#storage-cleanup-message")).to_contain_text("1 組媒體檔案尚待清理")
            page.locator("#new-password").fill(NEW_PASSWORD)
            page.locator("#storage-cleanup").click()
            expect(page.locator("#storage-error")).to_contain_text("清理服務暫時無法連線")
            expect(page.locator("#storage-cleanup")).to_be_enabled()
            expect(page.locator("#new-password")).to_have_value(NEW_PASSWORD)
            screenshot(page, "storage-cleanup-retry-390", "#account-storage")
            expect(page.locator('[data-close="account-dialog"]')).to_be_in_viewport()
            page.locator("#storage-cleanup").click()
            expect(page.locator("#toast-region")).to_contain_text("仍有媒體未能清理")
            expect(page.locator("#storage-bytes")).to_have_text("2 MB / 20 MB")
            expect(page.locator("#storage-cleanup")).to_be_enabled()
            page.locator("#storage-cleanup").click()
            expect(page.locator("#storage-pending")).to_be_hidden()
            expect(page.locator("#storage-alert")).to_be_hidden()
            expect(page.locator("#storage-bytes")).to_have_text("0 B / 20 MB")
            expect(page.locator("#new-password")).to_have_value(NEW_PASSWORD)
            screenshot(page, "storage-cleanup-complete-390", "#account-storage")
            expect(page.locator('[data-close="account-dialog"]')).to_be_in_viewport()
            context.close()
            record(
                "mobile-pending-deletion-failed-cleanup-retry-completion", viewport=390, draft_preserved=True
            )

            # Job quota 429 is distinguished from rate limiting; storage reads can recover.
            context, page = new_context()
            goto(page)
            login(page)
            fixture["quota_code"] = 429
            page.locator("#file-input").set_input_files(
                {"name": "explicit-fixture.jpg", "mimeType": "image/jpeg", "buffer": image_bytes}
            )
            page.locator("#upload-button").click()
            expect(page.locator("#upload-error")).to_contain_text("帳號保留工作上限")
            expect(page.locator("#upload-error")).not_to_contain_text("避免連續送出")
            fixture["storage_failures"] = 1
            page.locator("#storage-manage").click()
            expect(page.locator("#storage-error")).to_contain_text("無法確認帳號儲存容量")
            expect(page.locator("#storage-jobs")).to_have_text("—")
            expect(page.locator("#storage-cleanup")).to_be_hidden()
            page.locator("#storage-refresh").click()
            expect(page.locator("#storage-jobs")).to_have_text("1 / 5 筆")
            expect(page.locator("#storage-error")).to_be_hidden()
            context.close()
            record("job-quota-429-and-storage-read-recovery", viewport=1440)

            # Missing cleanup metadata does not claim the bytes have been removed.
            context, page = new_context(390)
            goto(page)
            login(page)
            fixture["cleanup_header"] = None
            request_delete(page)
            page.locator("#confirm-submit").click()
            expect(page.locator("#storage-alert")).to_contain_text("媒體清理狀態尚無法確認")
            expect(page.locator("#toast-region")).not_to_contain_text("已永久移除")
            page.locator("#storage-manage").click()
            expect(page.locator("#storage-jobs")).to_have_text("0 / 5 筆")
            expect(page.locator("#storage-cleanup")).to_be_hidden()
            expect(page.locator("#storage-alert")).to_be_hidden()
            context.close()
            record("missing-cleanup-header-stays-unconfirmed-until-refresh", viewport=390)

            browser.close()
        assert not report["page_errors"], report["page_errors"]
        assert not report["outside_requests"]
        assert not report["contract_failures"], report["contract_failures"]
        assert not report["unexpected_mutations"]
        blocked = sum(policy.blocked_requests for policy in policies)
        assert not blocked, blocked
        unexpected_console = [
            message
            for message in report["console_errors"]
            if not re.fullmatch(
                r"Failed to load resource: the server responded with a status of (401|429|503|507) \([^)]*\)",
                message,
            )
        ]
        assert not unexpected_console, unexpected_console
        report["expected_http_error_console_count"] = len(report["console_errors"])
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        report["failure"] = str(error)
        raise
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        report["passed_count"] = len(report["scenarios"])
        report["loopback_server_stopped"] = not thread.is_alive()
        report["blocked_requests"] = sum(policy.blocked_requests for policy in policies)
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
