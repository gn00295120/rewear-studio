"""Real hosted HTTP/account/storage/browser integration with fake model inference.

Starts a single-process Uvicorn server bound to an ephemeral loopback socket.
All account/media state is written into a new explicit output directory. It uses
the real gateway, Argon2, SQLite, cookies, CSRF, uploads, edits and exports.
Browser egress is restricted before pages are created. No cloud calls are made.
"""

import argparse
import hashlib
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(Path.home() / ".agents/skills/webapp-testing/scripts"))
from network_policy import LocalNetworkPolicy, install_network_policy  # noqa: E402
from test_app import FakeProviders, image_bytes  # noqa: E402
from wardrobe.config import Settings  # noqa: E402
from wardrobe.hosted import create_hosted_app  # noqa: E402

PASSWORD = "Isolated browser password 123"
NEW_PASSWORD = "Browser replacement password 456"
RECOVERED_PASSWORD = "Browser recovered password 789"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "screens").mkdir()
    os.environ["REWEAR_DATA_DIR"] = str(output / "unused-local-data")
    report = {
        "status": "running",
        "scope": "Real hosted HTTP/backend/browser integration; only model providers are explicit fixtures.",
        "mechanism": "Native Uvicorn thread on ephemeral loopback socket, LocalNetworkPolicy before pages, service workers blocked, CSP enforced; no supervisor sandbox receipt claimed.",
        "scenarios": [],
        "http_errors": [],
        "page_errors": [],
        "outside_requests": [],
        "hashes": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "wardrobe/hosted.py",
                "wardrobe/accounts.py",
                "wardrobe/app.py",
                "wardrobe/static/app.js",
                "wardrobe/static/index.html",
                "wardrobe/static/auth.js",
                "wardrobe/static/auth.html",
                "scripts/hosted_backend_browser.py",
            )
        },
    }
    services = []

    def provider_factory(settings):
        service = FakeProviders()
        services.append(service)
        return service

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    report["url"] = base
    app = create_hosted_app(
        Settings(data_dir=output / "unused-local-data", api_key=None),
        origin=base,
        hosted_dir=output / "hosted-data",
        allow_insecure_local=True,
        providers_factory=provider_factory,
    )
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        if not thread.is_alive():
            raise RuntimeError("Hosted server failed to start")
        time.sleep(0.05)
    assert server.started

    def scenario(name):
        report["scenarios"].append({"name": name, "status": "passed"})

    def snapshot(page, name):
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        page.screenshot(
            path=output / "screens" / f"{name}.png",
            full_page=True,
            animations="disabled",
            mask=[page.locator('input[type="password"]'), page.locator("#recovery-value")],
        )

    def register(page, username, display_name):
        page.goto(base + "/login", wait_until="networkidle")
        page.locator("#tab-register").click()
        page.locator("#register-username").fill(username)
        page.locator("#register-display-name").fill(display_name)
        page.locator("#register-password").fill(PASSWORD)
        page.locator("#register-confirm").fill(PASSWORD)
        page.locator('#register-form [type="submit"]').click()
        expect(page.locator("#recovery-card")).to_be_visible()
        code = page.locator("#recovery-value").input_value()
        assert len(code) == 43
        expect(page.locator("#recovery-continue")).to_be_disabled()
        snapshot(page, username + "-recovery-redacted")
        page.locator("#recovery-saved").check()
        page.locator("#recovery-continue").click()
        expect(page.locator("#account-display-name")).to_have_text(display_name)
        expect(page.locator("#total-count")).to_have_text("0")
        return code

    def login(page, username, password):
        page.goto(base + "/login", wait_until="networkidle")
        page.locator("#login-username").fill(username)
        page.locator("#login-password").fill(password)
        page.locator('#login-form [type="submit"]').click()
        expect(page.locator("#account-button")).to_be_visible()

    def get(context, path):
        assert path.startswith("/") and not path.startswith("//")
        return context.request.get(base + path)

    def auth_headers(context):
        session = get(context, "/auth/session").json()
        return {"Origin": base, "X-Rewear-Request": "1", "X-CSRF-Token": session["csrf_token"]}

    try:
        # Explicit read-only redirect check outside the redirect-blocking browser policy.
        response = httpx.get(base + "/", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/login"
        assert httpx.get(base + "/api/items").status_code == 401
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            contexts = []

            def context(width):
                ctx = browser.new_context(
                    viewport={"width": width, "height": 1000 if width > 400 else 844}, service_workers="block"
                )
                install_network_policy(ctx, LocalNetworkPolicy.for_http(base))
                ctx.on(
                    "request",
                    lambda request: (
                        report["outside_requests"].append(request.url)
                        if not request.url.startswith(base + "/") and not request.url.startswith("blob:")
                        else None
                    ),
                )
                ctx.on(
                    "page",
                    lambda page: page.on("pageerror", lambda error: report["page_errors"].append(str(error))),
                )
                ctx.on(
                    "response",
                    lambda response: (
                        report["http_errors"].append(
                            {
                                "path": response.url.removeprefix(base).split("?", 1)[0],
                                "status": response.status,
                            }
                        )
                        if response.status >= 400
                        else None
                    ),
                )
                contexts.append(ctx)
                return ctx

            a, b, c = context(1440), context(390), context(1440)
            pa, pb, pc = a.new_page(), b.new_page(), c.new_page()
            recovery_a = register(pa, "browser-alice", "Alice 的測試衣櫃")
            register(pb, "browser-bob", "Bob 的測試衣櫃")
            scenario("desktop-mobile-real-registration-recovery-gate")
            for ctx in (a, b):
                cookie = next(cookie for cookie in ctx.cookies() if cookie["name"] == "rewear_local_session")
                assert cookie["httpOnly"] and cookie["sameSite"] == "Lax"
            expect(pa.locator("#provider-note")).to_contain_text("伺服器")
            expect(pa.locator("#provider-select")).to_have_value("local")
            pa.locator("#file-input").set_input_files(
                {"name": "isolated-shirt.jpg", "mimeType": "image/jpeg", "buffer": image_bytes()}
            )
            expect(pa.locator("#upload-button")).to_be_enabled()
            pa.locator("#upload-button").click()
            expect(pa.locator("#total-count")).to_have_text("1", timeout=20000)
            expect(pa.locator(".garment-card")).to_have_count(1)
            items_a = get(a, "/api/items").json()["items"]
            item_a = items_a[0]
            account_a = get(a, "/auth/session").json()["user"]["id"]
            account_b = get(b, "/auth/session").json()["user"]["id"]
            assert account_a != account_b
            assert get(b, "/api/items").json()["items"] == []
            assert get(b, "/api/jobs").json()["jobs"] == []
            assert get(b, f"/api/items/{item_a['id']}").status == 404
            assert get(b, item_a["image_url"]).status == 404
            assert get(c, item_a["image_url"]).status == 401
            media = get(a, item_a["image_url"])
            assert media.status == 200 and media.headers["x-rewear-account"] == account_a
            assert "no-store" in media.headers["cache-control"]
            scenario("real-upload-persistence-and-cross-account-api-media-isolation")
            snapshot(pa, "alice-catalogue-1440")
            snapshot(pb, "bob-empty-390")

            pa.locator(".garment-card").first.click()
            pa.locator('#item-form [name="brand"]').fill("Alice-only draft")
            # Revoke only A's real session while her unsaved edit remains in the browser.
            assert a.request.post(base + "/auth/logout", headers=auth_headers(a)).status == 204
            pa.locator('#item-form button[type="submit"]').first.click()
            expect(pa.locator("#session-dialog")).to_be_visible()
            expect(pa.locator('#item-form [name="brand"]')).to_have_value("Alice-only draft")
            pa.locator("#reauth-password").fill(PASSWORD)
            pa.locator('#reauth-form [type="submit"]').click()
            expect(pa.locator("#session-dialog")).not_to_be_visible()
            expect(pa.locator('#item-form [name="brand"]')).to_have_value("Alice-only draft")
            pa.locator('#item-form button[type="submit"]').first.click()
            expect(pa.locator("#item-dialog")).not_to_be_visible()
            assert get(a, "/api/items").json()["items"][0]["record"]["brand"] == "Alice-only draft"
            assert get(b, "/api/items").json()["items"] == []
            scenario("real-session-revocation-reauthentication-retains-draft")

            pa.locator(".export-menu summary").click()
            with pa.expect_download() as downloading:
                pa.locator('.export-menu a[href*="format=json"]').click()
            downloaded = downloading.value
            export_path = output / "alice-export.json"
            downloaded.save_as(export_path)
            exported = json.loads(export_path.read_text())
            assert len(exported["items"]) == 1 and exported["items"][0]["id"] == item_a["id"]
            scenario("authenticated-browser-blob-export")

            login(pc, "browser-alice", PASSWORD)
            pa.locator("#account-button").click()
            pa.locator("#current-password").fill("An incorrect password 123")
            pa.locator("#new-password").fill(NEW_PASSWORD)
            pa.locator("#confirm-password").fill(NEW_PASSWORD)
            pa.locator('#password-form [type="submit"]').click()
            expect(pa.locator("#password-error")).to_be_visible()
            expect(pa.locator("#session-dialog")).not_to_be_visible()
            pa.locator("#current-password").fill(PASSWORD)
            pa.locator('#password-form [type="submit"]').click()
            expect(pa.locator("#password-success")).to_be_visible()
            assert get(c, "/api/items").status == 401
            assert get(a, "/api/items").status == 200
            scenario("real-password-rotation-invalidates-other-session")
            pa.locator("#logout-button").click()
            expect(pa).to_have_url(base + "/login")
            assert get(a, "/api/items").status == 401
            pa.locator("#tab-recover").click()
            pa.locator("#recover-username").fill("browser-alice")
            pa.locator("#recover-code").fill(recovery_a)
            pa.locator("#recover-password").fill(RECOVERED_PASSWORD)
            pa.locator("#recover-confirm").fill(RECOVERED_PASSWORD)
            pa.locator('#recover-form [type="submit"]').click()
            expect(pa.locator("#recovery-card")).to_be_visible()
            replacement_code = pa.locator("#recovery-value").input_value()
            assert replacement_code != recovery_a and len(replacement_code) == 43
            assert get(a, "/api/items").status == 401
            replay = a.request.post(
                base + "/auth/recover",
                headers={"Origin": base, "X-Rewear-Request": "1"},
                data={"username": "browser-alice", "recovery_code": recovery_a, "new_password": PASSWORD},
            )
            assert replay.status == 401
            pa.locator("#recovery-saved").check()
            pa.locator("#recovery-continue").click()
            login(pa, "browser-alice", RECOVERED_PASSWORD)
            expect(pa.locator("#total-count")).to_have_text("1")
            scenario("real-one-time-recovery-no-autologin-and-data-retention")

            pb.locator("#file-input").set_input_files(
                {"name": "bob-shirt.jpg", "mimeType": "image/jpeg", "buffer": image_bytes()}
            )
            pb.locator("#upload-button").click()
            expect(pb.locator("#total-count")).to_have_text("1", timeout=20000)
            item_b = get(b, "/api/items").json()["items"][0]
            assert item_b["id"] != item_a["id"]
            assert get(a, f"/api/items/{item_b['id']}").status == 404
            assert get(a, "/api/items").json()["items"][0]["id"] == item_a["id"]
            snapshot(pb, "bob-catalogue-390")
            scenario("mobile-second-account-upload-remains-isolated")
            for page in (pa, pb):
                storage = page.evaluate(
                    "JSON.stringify({local:{...localStorage}, session:{...sessionStorage}})"
                )
                for secret in (PASSWORD, NEW_PASSWORD, RECOVERED_PASSWORD, recovery_a, replacement_code):
                    assert secret not in storage
            assert not report["page_errors"] and not report["outside_requests"]
            for ctx in contexts:
                ctx.close()
            browser.close()
        report["model_fixture_inspections"] = sum(service.calls for service in services)
        assert report["model_fixture_inspections"] == 2
        assert not (output / "unused-local-data").exists()
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        report["error_type"] = type(error).__name__
        raise
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()
        report["server_stopped"] = not thread.is_alive()
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        assert not thread.is_alive(), "Hosted server did not stop"
    print(
        json.dumps(
            {
                "status": report["status"],
                "scenarios": len(report["scenarios"]),
                "server_stopped": report["server_stopped"],
            }
        )
    )


if __name__ == "__main__":
    main()
