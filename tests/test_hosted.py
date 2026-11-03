import asyncio
import hashlib
import io
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.responses import Response, StreamingResponse
from fastapi.testclient import TestClient
from PIL import Image

from wardrobe.accounts import AccountError
from wardrobe.config import Settings
from wardrobe.hosted import HostedError, _AuthWorkers, _RateLimiter, create_hosted_app
from wardrobe.store import Store

ORIGIN = "https://rewear.example"
PASSWORD = "test-only-password-2847"
RECORD = {
    "category": "top",
    "subtype": "shirt",
    "brand": None,
    "size_label": None,
    "colors": ["blue"],
    "pattern": None,
    "material_guess": None,
    "condition": "good",
    "defects": [],
    "confidence": 0.9,
    "needs_review": False,
}


class FakeProviders:
    def __init__(self, settings):
        self.settings = settings
        self.calls = self.closed = 0
        self.delay = 0
        self.cancelled = False
        self.cleaned = False

    async def health(self):
        return {"local": {"available": True, "model": "fixture", "error": None}, "meta": {"configured": True}}

    async def inspect(self, path, provider, *, context_path=None, wearing=False):
        self.calls += 1
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            await asyncio.sleep(0.01)
            self.cleaned = True
            raise
        return {"garment": dict(RECORD), "target": "one_garment", "visibility": "complete", "reason": None}

    async def close(self):
        self.closed += 1


def make_app(tmp_path, **kwargs):
    instances = []

    def factory(settings):
        service = FakeProviders(settings)
        instances.append(service)
        return service

    app = create_hosted_app(
        Settings(data_dir=tmp_path / "local", api_key="fixture-key", max_queue=4),
        origin=kwargs.pop("origin", ORIGIN),
        hosted_dir=kwargs.pop("hosted_dir", tmp_path / "hosted"),
        providers_factory=factory,
        **kwargs,
    )
    return app, instances


def register(client, username, *, origin=ORIGIN):
    client.cookies.clear()
    response = client.post(
        "/auth/register",
        json={"username": username, "password": PASSWORD, "display_name": username},
        headers={"Origin": origin, "X-Rewear-Request": "1"},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    result["cookie"] = response.headers["set-cookie"].split(";", 1)[0]
    client.cookies.clear()
    return result


def headers(auth, *, csrf=True, origin=ORIGIN):
    value = {"Cookie": auth["cookie"], "Origin": origin, "X-Rewear-Request": "1"}
    if csrf:
        value["X-CSRF-Token"] = auth["csrf_token"]
    return value


def seed(app, client, auth, *, color="blue", job_id=None, item_id=None):
    assert client.get("/api/jobs", headers=headers(auth)).status_code == 200
    runtime = app.state.registry.runtimes[auth["user"]["id"]]
    job_id = job_id or uuid4().hex
    job = runtime.app.state.store.create_job(
        id=job_id, mode="single", provider="local", status="completed", filename="source.jpg"
    )
    directory = runtime.directory / "jobs" / job_id
    directory.mkdir()
    image = Image.new("RGB", (24, 32), color)
    for filename in ("input.jpg", "preview.jpg", "single.jpg"):
        image.save(directory / filename)
    record = {**RECORD, "subtype": f"private-{auth['user']['username']}", "colors": [color]}
    item = runtime.app.state.store.add_item(
        job_id,
        **({"id": item_id} if item_id else {}),
        record=record,
        image_url=f"/media/{job_id}/single.jpg",
        source_url=f"/media/{job_id}/preview.jpg",
        original_url=f"/media/{job_id}/preview.jpg",
        quality={"version": 3, "status": "ready", "visibility": "complete", "target": "one_garment"},
    )
    return runtime, job, item


def jpg():
    stream = io.BytesIO()
    Image.new("RGB", (24, 32), "blue").save(stream, "JPEG")
    return stream.getvalue()


def eventually(function):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = function()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("fixture work did not reach the required state")


@pytest.mark.parametrize(
    "origin",
    [
        "http://rewear.example",
        "https://user:password@rewear.example",
        "https://rewear.example/",
        "https://rewear.example/x",
        "https://rewear.example?q=x",
        "https://rewear.example#x",
        "https://rewear.example?",
        "https://rewear.example#",
        "ftp://rewear.example",
        "https://rewear.example\\evil",
        "https://rewear.example:99999",
        "https://rewear..example",
        "https://rewear.example\n",
        "http://127.0.0.1:8765",
    ],
)
def test_invalid_origin_rejected_before_any_account_or_local_file(tmp_path, origin):
    with pytest.raises(ValueError):
        make_app(tmp_path, origin=origin)
    assert list(tmp_path.iterdir()) == []


def test_explicit_loopback_http_and_secure_cookie_contract(tmp_path):
    origin = "http://127.0.0.1:8765"
    app, _ = make_app(tmp_path, origin=origin, allow_insecure_local=True)
    with TestClient(app, base_url=origin) as client:
        auth = register(client, "local-test", origin=origin)
        assert auth["cookie"].startswith("rewear_local_session=")
        assert client.get("/api/jobs", headers=headers(auth, origin=origin)).status_code == 200
    with pytest.raises(ValueError):
        make_app(tmp_path / "bad", origin="http://192.168.1.4", allow_insecure_local=True)


def test_anonymous_auth_boundary_and_cookie_flags(tmp_path):
    app, providers = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/", follow_redirects=False).status_code == 303
        assert client.get("/auth/session").json() == {"mode": "hosted", "authenticated": False, "accounts": "open"}
        assert client.get("/healthz").json() == {"status": "ok"}
        for method, path in [
            ("GET", "/api/jobs"),
            ("GET", "/api/export?format=json"),
            ("GET", "/api/openapi.json"),
            ("HEAD", "/media/foreign/input.jpg"),
            ("POST", "/api/generate"),
        ]:
            response = client.request(method, path)
            assert response.status_code == 401
            assert response.headers["cache-control"] == "private, no-store"
        assert providers == [] and not app.state.registry.runtimes
        response = client.post(
            "/auth/register",
            json={"username": "alice", "password": PASSWORD},
            headers={"Origin": ORIGIN, "X-Rewear-Request": "1"},
        )
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert cookie.startswith("__Host-rewear_session=")
        assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
        assert "Domain=" not in cookie
        assert response.headers["x-rewear-account"] == response.json()["user"]["id"]


def test_two_accounts_all_data_families_are_bound_before_routes(tmp_path):
    local = Store(tmp_path / "local")
    local.create_job(id="local-only", mode="single", provider="local", status="completed")
    before = {
        str(path.relative_to(tmp_path / "local")): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (tmp_path / "local").rglob("*")
        if path.is_file()
    }
    app, providers = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        alice, bob = register(client, "alice"), register(client, "bob")
        a, ajob, aitem = seed(app, client, alice)
        b, bjob, bitem = seed(app, client, bob, color="red")
        bitem2 = b.app.state.store.add_item(bjob["id"], record=dict(RECORD))
        group = b.app.state.store.merge_items([bitem["id"], bitem2["id"]], bitem["id"])
        batch = b.app.state.store.create_batch([], client_id="client-only-b", request_hash="fixture")
        assert a.app.state.manager.work_budget is b.app.state.manager.work_budget
        for path in [
            "/api/jobs",
            "/api/items?quality=all",
            "/api/garment-groups",
            "/api/batches",
            "/api/duplicates",
            "/api/export?format=json",
            "/api/export?format=csv",
        ]:
            response = client.get(path, headers=headers(alice))
            assert response.status_code == 200
            assert bjob["id"] not in response.text and bitem["id"] not in response.text
            assert "private-bob" not in response.text and "local-only" not in response.text
            assert response.headers["x-rewear-account"] == alice["user"]["id"]
        for path in [
            f"/api/jobs/{bjob['id']}",
            f"/api/items/{bitem['id']}",
            f"/api/batches/{batch['id']}",
            f"/api/export?job_id={bjob['id']}",
            bitem["image_url"],
        ]:
            assert client.get(path, headers=headers(alice)).status_code == 404
        requests = [
            ("POST", f"/api/jobs/{bjob['id']}/cancel", None),
            ("POST", f"/api/jobs/{bjob['id']}/retry", None),
            ("DELETE", f"/api/jobs/{bjob['id']}", None),
            ("PATCH", f"/api/items/{bitem['id']}", {"reviewed": True}),
            ("DELETE", f"/api/items/{bitem['id']}", None),
            ("POST", f"/api/items/{bitem['id']}/reconstruction", None),
            ("POST", f"/api/items/{bitem['id']}/reconstruction/cancel", None),
            ("POST", f"/api/items/{bitem['id']}/discovery", {"market": "TW"}),
            ("POST", f"/api/items/{bitem['id']}/discovery/cancel", None),
            ("POST", f"/api/batches/{batch['id']}/cancel", None),
            ("POST", f"/api/batches/{batch['id']}/retry", None),
            ("DELETE", f"/api/garment-groups/{group['id']}", None),
            (
                "POST",
                "/api/garment-groups",
                {"item_ids": [aitem["id"], bitem["id"]], "primary_item_id": aitem["id"]},
            ),
        ]
        snapshot = b.app.state.store.item(bitem["id"])
        for method, path, body in requests:
            assert client.request(method, path, headers=headers(alice), json=body).status_code == 404
        assert b.app.state.store.item(bitem["id"]) == snapshot
        assert b.app.state.store.job(bjob["id"]) is not None
        assert (b.directory / "jobs" / bjob["id"] / "input.jpg").is_file()
        assert b.app.state.store.groups() == [group]
        assert all(provider.calls == 0 for provider in providers)
    after = {
        str(path.relative_to(tmp_path / "local")): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (tmp_path / "local").rglob("*")
        if path.is_file()
    }
    assert before == after


def test_same_ids_media_and_untrusted_headers_never_select_another_tenant(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        alice, bob = register(client, "alice"), register(client, "bob")
        shared_job, shared_item = uuid4().hex, uuid4().hex
        a, _, ai = seed(app, client, alice, job_id=shared_job, item_id=shared_item)
        b, _, bi = seed(app, client, bob, job_id=shared_job, item_id=shared_item, color="red")
        a_bytes = (a.directory / "jobs" / shared_job / "single.jpg").read_bytes()
        b_bytes = (b.directory / "jobs" / shared_job / "single.jpg").read_bytes()
        assert a_bytes != b_bytes
        spoof = {
            **headers(alice),
            "X-User": bob["user"]["id"],
            "X-Account-Id": bob["user"]["id"],
            "X-Rewear-Account": bob["user"]["id"],
        }
        response = client.get(f"/api/items/{shared_item}?account_id={bob['user']['id']}", headers=spoof)
        assert response.json()["record"]["subtype"] == "private-alice"
        assert response.headers["x-rewear-account"] == alice["user"]["id"]
        for auth, expected in [(alice, a_bytes), (bob, b_bytes)]:
            response = client.get(ai["image_url"], headers=headers(auth))
            assert response.content == expected
            assert response.headers["cache-control"] == "private, no-store"
            assert "Cookie" in response.headers["vary"]
            assert response.headers["x-rewear-account"] == auth["user"]["id"]
            partial = client.get(bi["image_url"], headers={**headers(auth), "Range": "bytes=0-9"})
            assert partial.status_code == 206 and partial.content == expected[:10]
        a.app.add_api_route(
            "/api/spoof-response", lambda: Response(headers={"X-Rewear-Account": bob["user"]["id"]})
        )
        assert (
            client.get("/api/spoof-response", headers=headers(alice)).headers["x-rewear-account"]
            == alice["user"]["id"]
        )


def test_same_batch_client_key_is_independent_between_accounts(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        alice, bob = register(client, "alice"), register(client, "bob")
        results = []
        for auth in [alice, bob]:
            response = client.post(
                "/api/batches",
                headers=headers(auth),
                files=[("files", ("shirt.jpg", jpg(), "image/jpeg"))],
                data={"client_id": "shared-browser-id", "mode": "single", "provider": "local"},
            )
            assert response.status_code == 202, response.text
            results.append(response.json())
        assert results[0]["id"] != results[1]["id"]
        response = client.post(
            "/api/batches",
            headers=headers(alice),
            files=[("files", ("shirt.jpg", jpg(), "image/jpeg"))],
            data={"client_id": "shared-browser-id", "mode": "single", "provider": "local"},
        )
        assert response.status_code == 202 and response.json()["id"] == results[0]["id"]


@pytest.mark.parametrize(
    "change", ["missing_origin", "foreign_origin", "missing_request", "missing_csrf", "wrong_csrf"]
)
def test_csrf_blocks_mutation_before_creating_work(tmp_path, change):
    app, services = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        auth = register(client, "alice")
        values = headers(auth)
        if change == "missing_origin":
            values.pop("Origin")
        elif change == "foreign_origin":
            values["Origin"] = "https://attacker.example"
        elif change == "missing_request":
            values.pop("X-Rewear-Request")
        elif change == "missing_csrf":
            values.pop("X-CSRF-Token")
        else:
            values["X-CSRF-Token"] = "wrong"
        response = client.post(
            "/api/jobs", headers=values, files={"file": ("shirt.jpg", jpg(), "image/jpeg")}
        )
        assert response.status_code == 403
        assert not app.state.registry.runtimes and services == []
        assert client.post("/auth/logout", headers=values).status_code == 403
        assert client.get("/auth/session", headers=headers(auth)).json()["authenticated"]


def test_login_register_recover_require_same_origin_and_marker(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        for endpoint in ["register", "login", "recover"]:
            assert client.post(f"/auth/{endpoint}", json={}).status_code == 403
            assert client.post(f"/auth/{endpoint}", json={}, headers={"Origin": ORIGIN}).status_code == 403
        assert app.state.accounts.count_users() == 0
        assert client.get("/healthz", headers={"Host": "attacker.example"}).status_code == 400


def test_logout_password_recovery_and_expired_or_forged_session(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        auth = register(client, "alice")
        logged_out = client.post("/auth/logout", headers=headers(auth))
        assert logged_out.status_code == 204
        assert client.get("/api/jobs", headers=headers(auth)).status_code == 401
        mutation = {"Origin": ORIGIN, "X-Rewear-Request": "1"}
        response = client.post(
            "/auth/login", headers=mutation, json={"username": "alice", "password": PASSWORD}
        )
        assert response.status_code == 200
        current = {**response.json(), "cookie": response.headers["set-cookie"].split(";", 1)[0]}
        client.cookies.clear()
        response = client.post(
            "/auth/password",
            headers=headers(current),
            json={"current_password": PASSWORD, "new_password": PASSWORD + "new"},
        )
        assert response.status_code == 200
        changed = {**response.json(), "cookie": response.headers["set-cookie"].split(";", 1)[0]}
        client.cookies.clear()
        assert client.get("/api/jobs", headers=headers(current)).status_code == 401
        assert client.get("/api/jobs", headers=headers(changed)).status_code == 200
        response = client.post(
            "/auth/recover",
            headers=mutation,
            json={
                "username": "alice",
                "recovery_code": auth["recovery_code"],
                "new_password": PASSWORD + "recovered",
            },
        )
        assert response.status_code == 200 and response.json()["recovery_code"] != auth["recovery_code"]
        assert "set-cookie" not in response.headers
        assert client.get("/api/jobs", headers=headers(changed)).status_code == 401
        assert client.get("/auth/session").json()["authenticated"] is False
        response = client.post(
            "/auth/login", headers=mutation, json={"username": "alice", "password": PASSWORD + "recovered"}
        )
        assert response.status_code == 200
        latest = {**response.json(), "cookie": response.headers["set-cookie"].split(";", 1)[0]}
        client.cookies.clear()
        app.state.accounts._clock = lambda: time.time() + 9 * 3600
        assert client.get("/api/jobs", headers=headers(latest)).status_code == 401
        assert client.get("/auth/session", headers=headers(latest)).json()["authenticated"] is False
        assert (
            client.get("/api/jobs", headers={"Cookie": "__Host-rewear_session=" + "a" * 43}).status_code
            == 401
        )


def test_disabled_account_cannot_reuse_session(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        auth = register(client, "alice")
        app.state.accounts.disable_user(auth["user"]["id"])
        assert client.get("/api/jobs", headers=headers(auth)).status_code == 401
        assert not app.state.registry.runtimes


def test_auth_body_limit_shapes_and_safe_errors(tmp_path):
    app, _ = make_app(tmp_path, max_accounts=1)
    with TestClient(app, base_url=ORIGIN) as client:
        mutation = {"Origin": ORIGIN, "X-Rewear-Request": "1", "Content-Type": "application/json"}
        for raw, expected in [
            (b"x" * 8193, 413),
            (b"{bad json", 400),
            (b"[]", 422),
            (b'{"username":3,"password":"secret-invalid"}', 422),
            (b'{"username":"alice","password":"secret-invalid","extra":"private"}', 422),
        ]:
            response = client.post("/auth/register", headers=mutation, content=raw)
            assert response.status_code == expected
            assert "secret-invalid" not in response.text and "private" not in response.text
        assert app.state.accounts.count_users() == 0
        register(client, "alice")
        response = client.post(
            "/auth/register", headers=mutation, json={"username": "bob", "password": PASSWORD}
        )
        assert response.status_code == 409 and app.state.accounts.count_users() == 1
        assert not app.state.registry.runtimes


def test_rate_limit_uses_peer_not_forwarded_headers_and_has_bounded_storage(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path)
    calls = []

    def rejected(*args):
        calls.append(True)
        raise AccountError("authentication_failed")

    with TestClient(app, base_url=ORIGIN) as client:
        monkeypatch.setattr(app.state.accounts, "authenticate_session", rejected)
        for index in range(30):
            response = client.post(
                "/auth/login",
                json={"username": f"missing-{index}", "password": PASSWORD},
                headers={"Origin": ORIGIN, "X-Rewear-Request": "1", "X-Forwarded-For": f"10.0.0.{index}"},
            )
            assert response.status_code == 401
        response = client.post(
            "/auth/login",
            json={"username": "new-account", "password": PASSWORD},
            headers={"Origin": ORIGIN, "X-Rewear-Request": "1", "X-Forwarded-For": "192.0.2.5"},
        )
        assert response.status_code == 429 and len(calls) == 30
    now = [0]
    limiter = _RateLimiter(clock=lambda: now[0], max_keys=2)
    limiter.check("ip-one", "account-one", "login")
    with pytest.raises(HostedError):
        limiter.check("ip-two", "account-two", "login")
    assert len(limiter.buckets) == 2
    now[0] = 301
    limiter.check("ip-two", "account-two", "login")
    assert len(limiter.buckets) == 2


def test_symlinked_tenant_root_and_invalid_session_id_never_open_other_data(tmp_path, monkeypatch):
    app, services = make_app(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "wardrobe.sqlite3"
    sentinel.write_bytes(b"unchanged private data")
    with TestClient(app, base_url=ORIGIN) as client:
        auth = register(client, "alice")
        (app.state.registry.tenants / auth["user"]["id"]).symlink_to(outside, target_is_directory=True)
        assert client.get("/api/jobs", headers=headers(auth)).status_code == 503
        assert sentinel.read_bytes() == b"unchanged private data" and not services
        monkeypatch.setattr(
            app.state.accounts,
            "get_session",
            lambda _: {"user": {"id": "../../outside"}, "csrf_token": "fake"},
        )
        response = client.get("/api/jobs", headers=headers(auth))
        assert response.status_code == 401 and "x-rewear-account" not in response.headers


def test_media_symlink_cannot_cross_tenant_root(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        alice, bob = register(client, "alice"), register(client, "bob")
        a, ajob, _ = seed(app, client, alice)
        b, bjob, _ = seed(app, client, bob, color="red")
        (a.directory / "jobs" / ajob["id"] / "foreign.jpg").symlink_to(
            b.directory / "jobs" / bjob["id"] / "input.jpg"
        )
        assert client.get(f"/media/{ajob['id']}/foreign.jpg", headers=headers(alice)).status_code == 404


def test_shutdown_cancels_owned_work_and_waits_for_provider_cleanup(tmp_path):
    app, services = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        auth = register(client, "alice")
        client.get("/api/jobs", headers=headers(auth))
        services[0].delay = 999
        response = client.post(
            "/api/jobs",
            headers=headers(auth),
            files={"file": ("shirt.jpg", jpg(), "image/jpeg")},
            data={"mode": "single", "provider": "local"},
        )
        assert response.status_code == 202
        job_id = response.json()["id"]
        eventually(lambda: services[0].calls == 1)
        runtime = app.state.registry.runtimes[auth["user"]["id"]]
    assert services[0].cancelled and services[0].cleaned and services[0].closed == 1
    assert runtime.app.state.store.job(job_id)["status"] == "cancelled"
    assert app.state.work_budget.pending == 0 and not app.state.registry.runtimes


async def test_registry_singleflight_busy_capacity_and_idle_eviction(tmp_path):
    app, services = make_app(tmp_path, max_active_tenants=1)
    registry = app.state.registry
    first, second = uuid4().hex, uuid4().hex
    async with app.router.lifespan_context(app):
        async with registry.acquire(first) as runtime:
            async with registry.acquire(first) as same:
                assert same is runtime and len(services) == 1
            with pytest.raises(HostedError) as error:
                async with registry.acquire(second):
                    pass
            assert error.value.status == 503
        background = asyncio.create_task(asyncio.sleep(999))
        runtime.app.state.manager.tasks["fixture-busy"] = background
        with pytest.raises(HostedError):
            async with registry.acquire(second):
                pass
        background.cancel()
        await asyncio.gather(background, return_exceptions=True)
        async with registry.acquire(second) as replacement:
            assert replacement is not runtime
            assert services[0].closed == 1
            assert first not in registry.runtimes and len(registry.runtimes) == 1
    assert all(service.closed == 1 for service in services)


async def test_stream_refcount_prevents_eviction_and_shutdown_waits_for_release(tmp_path):
    app, services = make_app(tmp_path, max_active_tenants=1)
    registry = app.state.registry
    entered, finished = asyncio.Event(), asyncio.Event()
    async with app.router.lifespan_context(app):
        accounts = app.state.accounts
        user = await asyncio.to_thread(accounts.register, "alice", PASSWORD)
        session = accounts.create_session(user["user"]["id"])
        async with registry.acquire(user["user"]["id"]) as runtime:

            async def stream():
                try:
                    yield b"first"
                    entered.set()
                    await asyncio.sleep(999)
                finally:
                    finished.set()

            runtime.app.add_api_route("/api/slow-stream", lambda: StreamingResponse(stream()))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
            request = asyncio.create_task(
                client.get(
                    "/api/slow-stream", headers={"Cookie": f"__Host-rewear_session={session['token']}"}
                )
            )
            await asyncio.wait_for(entered.wait(), 2)
            assert runtime.requests == 1
            with pytest.raises(HostedError):
                async with registry.acquire(uuid4().hex):
                    pass
            await registry.close()
            assert request.done() and finished.is_set() and runtime.requests == 0
            assert services[0].closed == 1
            await asyncio.gather(request, return_exceptions=True)


async def test_cancelled_argon2_worker_keeps_capacity_until_thread_finishes():
    import threading

    entered, finish = threading.Event(), threading.Event()
    workers = _AuthWorkers()

    def blocked():
        entered.set()
        finish.wait(2)
        return True

    task = asyncio.create_task(workers.run(blocked))
    await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert workers.tasks and not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not workers.tasks
    await workers.close()


def test_hosted_process_lock_rejects_second_instance_without_provider_work(tmp_path):
    first, first_services = make_app(tmp_path)
    second, second_services = make_app(tmp_path)
    with TestClient(first, base_url=ORIGIN):
        with pytest.raises(RuntimeError, match="single process"):
            with TestClient(second, base_url=ORIGIN):
                pass
    assert not first_services and not second_services


def test_factory_and_rejected_start_do_not_open_account_database(tmp_path, monkeypatch):
    import wardrobe.hosted as hosted

    first, _ = make_app(tmp_path)
    assert first.state.accounts is None
    assert not (tmp_path / "hosted" / "control" / "accounts.sqlite3").exists()
    with TestClient(first, base_url=ORIGIN) as client:
        auth = register(client, "alice")
        database = tmp_path / "hosted" / "control" / "accounts.sqlite3"
        before = database.read_bytes()
        calls = []

        def unexpected(*args, **kwargs):
            calls.append(True)
            raise AssertionError("must acquire hosted lock before touching account data")

        monkeypatch.setattr(hosted, "AccountStore", unexpected)
        second, _ = make_app(tmp_path)
        with pytest.raises(RuntimeError, match="single process"):
            with TestClient(second, base_url=ORIGIN):
                pass
        assert calls == [] and database.read_bytes() == before
        assert client.get("/auth/session", headers=headers(auth)).json()["authenticated"]
    assert first.state.accounts is None


def test_registration_session_failure_is_atomic_at_http_boundary(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:

        def failed_session(*args, **kwargs):
            raise AccountError("storage_unavailable")

        monkeypatch.setattr(app.state.accounts, "_insert_session", failed_session)
        response = client.post(
            "/auth/register",
            json={"username": "alice", "password": PASSWORD},
            headers={"Origin": ORIGIN, "X-Rewear-Request": "1"},
        )
        assert response.status_code == 503
        assert "set-cookie" not in response.headers
        assert "recovery_code" not in response.text and PASSWORD not in response.text
        assert app.state.accounts.count_users() == 0


def test_control_directory_replacement_is_rejected_before_session_lookup(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        auth = register(client, "alice")
        control = tmp_path / "hosted" / "control"
        saved = tmp_path / "saved-control"
        control.rename(saved)
        control.symlink_to(saved, target_is_directory=True)
        before = (saved / "accounts.sqlite3").read_bytes()
        assert client.get("/api/jobs", headers=headers(auth)).status_code == 503
        assert not app.state.registry.runtimes
        assert (saved / "accounts.sqlite3").read_bytes() == before


def test_other_account_cannot_cancel_live_paid_work(tmp_path):
    app, services = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        alice, bob = register(client, "alice"), register(client, "bob")
        client.get("/api/jobs", headers=headers(bob))
        services[0].delay = 999
        response = client.post(
            "/api/jobs",
            headers=headers(bob),
            files={"file": ("shirt.jpg", jpg(), "image/jpeg")},
            data={"mode": "single", "provider": "local"},
        )
        assert response.status_code == 202
        job_id = response.json()["id"]
        eventually(lambda: services[0].calls == 1)
        for method, suffix in [("POST", "/cancel"), ("DELETE", "")]:
            assert (
                client.request(method, f"/api/jobs/{job_id}{suffix}", headers=headers(alice)).status_code
                == 404
            )
        assert not services[0].cancelled and not services[0].cleaned
        assert client.post(f"/api/jobs/{job_id}/cancel", headers=headers(bob)).status_code == 200
        eventually(lambda: services[0].cleaned)
        assert client.get(f"/api/jobs/{job_id}", headers=headers(bob)).json()["status"] == "cancelled"


async def test_concurrent_first_requests_construct_only_one_runtime(tmp_path):
    app, services = make_app(tmp_path)
    async with app.router.lifespan_context(app):
        registry = app.state.registry
        account_id = uuid4().hex
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []

        async def request():
            async with registry.acquire(account_id) as runtime:
                seen.append(runtime)
                if len(seen) == 3:
                    entered.set()
                await release.wait()

        tasks = [asyncio.create_task(request()) for _ in range(3)]
        await asyncio.wait_for(entered.wait(), 2)
        assert len(services) == 1 and len({id(runtime) for runtime in seen}) == 1
        assert seen[0].requests == 3
        release.set()
        await asyncio.gather(*tasks)
        assert seen[0].requests == 0


async def test_concurrent_registration_capacity_and_chunked_auth_limit(tmp_path):
    app, _ = make_app(tmp_path, max_accounts=1)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
            mutation = {"Origin": ORIGIN, "X-Rewear-Request": "1", "Content-Type": "application/json"}

            async def chunks():
                yield b" " * 4096
                yield b" " * 4097

            oversized = await client.post("/auth/register", headers=mutation, content=chunks())
            assert oversized.status_code == 413
            malformed = await client.post(
                "/auth/register", headers=mutation, content=b"[" * 4000 + b"]" * 4000
            )
            assert malformed.status_code in {400, 422}
            assert app.state.accounts.count_users() == 0
            results = await asyncio.gather(
                *(
                    client.post(
                        "/auth/register", headers=mutation, json={"username": user, "password": PASSWORD}
                    )
                    for user in ("alice", "bob")
                )
            )
            assert sorted(result.status_code for result in results) == [200, 409]
            assert app.state.accounts.count_users() == 1


def test_job_quota_is_independent_and_delete_frees_a_slot_and_media(tmp_path):
    app, services = make_app(tmp_path, max_jobs_per_tenant=1)
    with TestClient(app, base_url=ORIGIN) as client:
        alice, bob = register(client, "alice"), register(client, "bob")
        runtime, job, _ = seed(app, client, alice)
        before = sorted(
            path.relative_to(runtime.directory) for path in runtime.directory.rglob("*") if path.is_file()
        )
        denied = client.post(
            "/api/jobs", headers=headers(alice), files={"file": ("shirt.jpg", jpg(), "image/jpeg")}
        )
        assert denied.status_code == 429
        assert (
            sorted(
                path.relative_to(runtime.directory) for path in runtime.directory.rglob("*") if path.is_file()
            )
            == before
        )
        assert len(runtime.app.state.store.jobs()) == 1 and services[0].calls == 0
        assert client.get("/api/export?format=json", headers=headers(alice)).status_code == 200
        own_b = client.post(
            "/api/jobs", headers=headers(bob), files={"file": ("shirt.jpg", jpg(), "image/jpeg")}
        )
        assert own_b.status_code == 202
        deleted = client.delete(f"/api/jobs/{job['id']}", headers=headers(alice))
        assert deleted.status_code == 204 and deleted.headers["x-rewear-cleanup"] == "complete"
        state = client.get("/api/storage", headers=headers(alice)).json()
        assert state["usage"]["jobs"] == 0 and state["usage"]["media_bytes"] == 0
        assert not list((runtime.directory / ".trash").iterdir())
        assert (
            client.post(
                "/api/jobs", headers=headers(alice), files={"file": ("shirt.jpg", jpg(), "image/jpeg")}
            ).status_code
            == 202
        )


def test_media_quota_blocks_all_file_growth_without_changing_existing_evidence(tmp_path):
    app, services = make_app(tmp_path, max_media_bytes_per_tenant=40 * 1024**2)
    with TestClient(app, base_url=ORIGIN) as client:
        auth = register(client, "alice")
        runtime, job, item = seed(app, client, auth)
        store = runtime.app.state.store
        store.update_job(job["id"], status="failed", input_name="input.jpg")
        filler = runtime.directory / "jobs" / job["id"] / "retained.bin"
        with filler.open("wb") as output:
            output.truncate(40 * 1024**2)

        async def no_call(*args, **kwargs):
            raise AssertionError("quota rejection must precede model work")

        for name in ("reconstruct", "identify_brand", "search_shopping", "verify_shopping"):
            setattr(services[0], name, no_call)
        snapshot = store.item(item["id"])
        before_paths = set(runtime.directory.rglob("*"))
        requests = [
            (f"/api/jobs/{job['id']}/retry", None),
            ("/api/generate", {"prompt": "Create a blue cotton shirt", "provider": "local"}),
            (f"/api/items/{item['id']}/reconstruction", None),
            (f"/api/items/{item['id']}/discovery", {"market": "TW"}),
        ]
        for path, body in requests:
            response = client.post(path, json=body, headers=headers(auth))
            assert response.status_code == 507, response.text
        assert (
            client.post(
                "/api/jobs", headers=headers(auth), files={"file": ("shirt.jpg", jpg(), "image/jpeg")}
            ).status_code
            == 507
        )
        assert (
            client.post(
                "/api/batches", headers=headers(auth), files=[("files", ("shirt.jpg", jpg(), "image/jpeg"))]
            ).status_code
            == 507
        )
        assert store.item(item["id"]) == snapshot and len(store.jobs()) == 1
        assert set(runtime.directory.rglob("*")) == before_paths
        assert client.get(item["image_url"], headers=headers(auth)).status_code == 200
        assert (
            client.get("/api/storage", headers=headers(auth)).json()["usage"]["media_bytes"] >= 40 * 1024**2
        )
        assert services[0].calls == 0


def test_full_quota_does_not_block_batch_idempotent_replay(tmp_path):
    app, _ = make_app(tmp_path, max_jobs_per_tenant=1)
    with TestClient(app, base_url=ORIGIN) as client:
        auth = register(client, "alice")

        def upload(key):
            return client.post(
                "/api/batches",
                headers=headers(auth),
                files=[("files", ("shirt.jpg", jpg(), "image/jpeg"))],
                data={"client_id": key, "mode": "single", "provider": "local"},
            )

        first = upload("fixed-replay")
        assert first.status_code == 202
        repeated = upload("fixed-replay")
        assert repeated.status_code == 202 and repeated.json()["id"] == first.json()["id"]
        assert upload("new-upload").status_code == 429


def test_failed_hosted_cleanup_remains_counted_and_explicitly_retryable(tmp_path, monkeypatch):
    import wardrobe.hosted as hosted

    app, _ = make_app(tmp_path, max_media_bytes_per_tenant=40 * 1024**2)
    with TestClient(app, base_url=ORIGIN) as client:
        auth = register(client, "alice")
        runtime, job, _ = seed(app, client, auth)
        with (runtime.directory / "jobs" / job["id"] / "retained.bin").open("wb") as output:
            output.truncate(40 * 1024**2)
        original = hosted.shutil.rmtree

        def unavailable(path, *args, **kwargs):
            if Path(path).parent == runtime.directory / ".trash":
                raise PermissionError("test-only unavailable storage")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(hosted.shutil, "rmtree", unavailable)
        response = client.delete(f"/api/jobs/{job['id']}", headers=headers(auth))
        assert response.status_code == 204 and response.headers["x-rewear-cleanup"] == "pending"
        state = client.get("/api/storage", headers=headers(auth)).json()
        assert state["cleanup"]["pending_directories"] == 1 and state["cleanup"]["error"]
        assert state["usage"]["jobs"] == 0 and state["usage"]["media_bytes"] >= 40 * 1024**2
        assert (
            client.post(
                "/api/jobs", headers=headers(auth), files={"file": ("shirt.jpg", jpg(), "image/jpeg")}
            ).status_code
            == 507
        )
        assert (
            client.post("/api/storage/cleanup", headers=headers(auth)).json()["cleanup"][
                "pending_directories"
            ]
            == 1
        )
        monkeypatch.setattr(hosted.shutil, "rmtree", original)
        cleaned = client.post("/api/storage/cleanup", headers=headers(auth)).json()
        assert cleaned["cleanup"] == {"pending_directories": 0, "error": None}
        assert cleaned["usage"]["media_bytes"] == 0
        assert (
            client.post(
                "/api/jobs", headers=headers(auth), files={"file": ("shirt.jpg", jpg(), "image/jpeg")}
            ).status_code
            == 202
        )


@pytest.mark.parametrize("limited_by,expected", [("jobs", 429), ("bytes", 507)])
async def test_concurrent_upload_reservations_cover_uncommitted_files(
    tmp_path, monkeypatch, limited_by, expected
):
    from starlette.datastructures import UploadFile

    app, services = make_app(
        tmp_path,
        max_jobs_per_tenant=1 if limited_by == "jobs" else 20,
        max_media_bytes_per_tenant=48 * 1024**2,
    )
    first_read, release = asyncio.Event(), asyncio.Event()
    original_read = UploadFile.read
    blocked = False

    async def waiting_read(self, size=-1):
        nonlocal blocked
        if not blocked:
            blocked = True
            first_read.set()
            await release.wait()
        return await original_read(self, size)

    monkeypatch.setattr(UploadFile, "read", waiting_read)
    async with app.router.lifespan_context(app):
        session = await asyncio.to_thread(app.state.accounts.register_session, "alice", PASSWORD)
        auth = {**session, "cookie": f"__Host-rewear_session={session['token']}"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
            await client.get("/api/jobs", headers=headers(auth))
            services[0].delay = 999
            first = asyncio.create_task(
                client.post(
                    "/api/jobs", headers=headers(auth), files={"file": ("shirt.jpg", jpg(), "image/jpeg")}
                )
            )
            await asyncio.wait_for(first_read.wait(), 2)
            pending = (await client.get("/api/storage", headers=headers(auth))).json()["usage"]
            assert pending["jobs"] == 0 and pending["reserved_jobs"] == 1
            assert pending["reserved_bytes"] >= 32 * 1024**2
            second = await client.post(
                "/api/jobs", headers=headers(auth), files={"file": ("shirt.jpg", jpg(), "image/jpeg")}
            )
            assert second.status_code == expected
            release.set()
            created = await first
            assert created.status_code == 202
            runtime = app.state.registry.runtimes[session["user"]["id"]]
            assert len(runtime.app.state.store.jobs()) == 1
            assert len(list((runtime.directory / "jobs").iterdir())) == 1
            pending = (await client.get("/api/storage", headers=headers(auth))).json()["usage"]
            assert pending["reserved_jobs"] == 0 and pending["reserved_bytes"] == 32 * 1024**2
    assert not runtime.app.state.storage_budget.reservations


def test_background_supplemental_reservation_and_idempotent_active_replay(tmp_path):
    app, services = make_app(tmp_path, max_media_bytes_per_tenant=96 * 1024**2)
    with TestClient(app, base_url=ORIGIN) as client:
        auth = register(client, "alice")
        runtime, _, item = seed(app, client, auth)
        calls = []

        async def reconstruction(*args, **kwargs):
            calls.append(True)
            await asyncio.sleep(999)

        services[0].reconstruct = reconstruction
        first = client.post(f"/api/items/{item['id']}/reconstruction", headers=headers(auth))
        assert first.status_code == 202
        eventually(lambda: bool(calls))
        status = client.get("/api/storage", headers=headers(auth)).json()
        assert status["usage"]["reserved_bytes"] == 64 * 1024**2
        runtime.app.state.storage_budget.max_media_bytes = 1
        replay = client.post(f"/api/items/{item['id']}/reconstruction", headers=headers(auth))
        assert (
            replay.status_code == 202
            and replay.json()["reconstruction"]["id"] == first.json()["reconstruction"]["id"]
        )
        assert len(calls) == 1
        assert (
            client.post(f"/api/items/{item['id']}/reconstruction/cancel", headers=headers(auth)).status_code
            == 200
        )
        eventually(lambda: not runtime.app.state.storage_budget.reservations)
        assert client.get("/api/storage", headers=headers(auth)).json()["usage"]["reserved_bytes"] == 0


def test_full_job_quota_rejects_batch_retry_before_copy_or_persistence(tmp_path):
    app, _ = make_app(tmp_path, max_jobs_per_tenant=1)
    with TestClient(app, base_url=ORIGIN) as client:
        auth = register(client, "alice")
        created = client.post(
            "/api/batches",
            headers=headers(auth),
            files=[("files", ("shirt.jpg", jpg(), "image/jpeg"))],
            data={"mode": "single", "reconstruct": "false"},
        )
        assert created.status_code == 202
        batch = created.json()
        runtime = app.state.registry.runtimes[auth["user"]["id"]]
        store = runtime.app.state.store
        job_id = batch["jobs"][0]["id"]
        eventually(lambda: store.job(job_id)["status"] not in {"queued", "running"})
        store.update_job(job_id, status="failed")
        before = set(runtime.directory.rglob("*"))
        response = client.post(f"/api/batches/{batch['id']}/retry", headers=headers(auth))
        assert response.status_code == 429
        assert len(store.jobs()) == len(store.batches()) == 1
        assert set(runtime.directory.rglob("*")) == before
        assert not runtime.app.state.storage_budget.reservations


def test_automatic_supplemental_work_obeys_storage_admission(tmp_path):
    app, services = make_app(tmp_path, max_media_bytes_per_tenant=1)
    with TestClient(app, base_url=ORIGIN) as client:
        auth = register(client, "alice")
        runtime, _, item = seed(app, client, auth)

        async def forbidden(*args, **kwargs):
            raise AssertionError("automatic work must not bypass storage admission")

        for name in ("reconstruct", "identify_brand", "search_shopping", "verify_shopping"):
            setattr(services[0], name, forbidden)
        client.portal.call(runtime.app.state.reconstruction.enqueue_automatic, item)
        client.portal.call(runtime.app.state.discovery.enqueue_automatic, item)
        stored = runtime.app.state.store.item(item["id"])
        for kind in ("reconstruction", "discovery"):
            assert stored[kind]["status"] == "failed"
            assert "Insufficient account storage" in stored[kind]["error"]
            assert not getattr(runtime.app.state, kind).tasks
        assert stored["record"] == item["record"]
        assert not runtime.app.state.storage_budget.reservations


def test_pending_deleted_media_survives_restart_and_can_be_cleaned(tmp_path, monkeypatch):
    import wardrobe.hosted as hosted

    app, _ = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        auth = register(client, "alice")
        runtime, job, _ = seed(app, client, auth)
        original = hosted.shutil.rmtree

        def unavailable(path, *args, **kwargs):
            if Path(path).parent == runtime.directory / ".trash":
                raise PermissionError("test-only cleanup failure")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(hosted.shutil, "rmtree", unavailable)
        removed = client.delete(f"/api/jobs/{job['id']}", headers=headers(auth))
        assert removed.status_code == 204 and removed.headers["x-rewear-cleanup"] == "pending"
        previous = client.get("/api/storage", headers=headers(auth)).json()
        assert previous["usage"]["media_bytes"] > 0
    monkeypatch.setattr(hosted.shutil, "rmtree", original)
    restarted, _ = make_app(tmp_path)
    with TestClient(restarted, base_url=ORIGIN) as client:
        current = client.get("/api/storage", headers=headers(auth)).json()
        assert current["cleanup"]["pending_directories"] == 1
        assert current["usage"] == previous["usage"]
        cleaned = client.post("/api/storage/cleanup", headers=headers(auth))
        assert cleaned.status_code == 200
        assert cleaned.json()["usage"]["media_bytes"] == 0
        assert cleaned.json()["cleanup"] == {"pending_directories": 0, "error": None}


def test_committed_delete_remains_successful_when_cleanup_scan_fails(tmp_path, monkeypatch):
    app, _ = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        auth = register(client, "alice")
        runtime, job, _ = seed(app, client, auth)

        def scan_unavailable():
            raise PermissionError("test-only failed filesystem scan")

        monkeypatch.setattr(runtime.app.state.storage_budget, "_files", scan_unavailable)
        removed = client.delete(f"/api/jobs/{job['id']}", headers=headers(auth))
        assert removed.status_code == 204 and removed.headers["x-rewear-cleanup"] == "pending"
        assert runtime.app.state.store.job(job["id"]) is None
        assert client.get("/api/storage", headers=headers(auth)).status_code == 503


def test_usage_tolerates_background_removal_of_enumerated_temporary_directory(tmp_path, monkeypatch):
    import wardrobe.hosted as hosted

    app, _ = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        auth = register(client, "alice")
        runtime, job, _ = seed(app, client, auth)
        temporary = runtime.directory / "jobs" / job["id"] / "attempt-temp"
        temporary.mkdir()
        original = hosted.os.scandir

        def removed(path):
            if Path(path) == temporary:
                temporary.rmdir()
            return original(path)

        monkeypatch.setattr(hosted.os, "scandir", removed)
        status = client.get("/api/storage", headers=headers(auth))
        assert status.status_code == 200
        assert status.json()["usage"]["media_bytes"] > 0


def test_managed_accounts_only_sign_in_with_operator_created_logins(tmp_path):
    app, _ = make_app(tmp_path, accounts_mode="managed")
    with TestClient(app, base_url=ORIGIN) as client:
        mutation = {"Origin": ORIGIN, "X-Rewear-Request": "1"}
        assert client.get("/auth/session").json() == {"mode": "hosted", "authenticated": False, "accounts": "managed"}
        refused = client.post(
            "/auth/register", headers=mutation, json={"username": "visitor", "password": PASSWORD}
        )
        assert refused.status_code == 403 and "provided by the organizers" in refused.json()["detail"]
        assert app.state.accounts.count_users() == 0
        app.state.accounts.register("judge", PASSWORD, "Judge")
        response = client.post("/auth/login", headers=mutation, json={"username": "judge", "password": PASSWORD})
        assert response.status_code == 200
        auth = {**response.json(), "cookie": response.headers["set-cookie"].split(";", 1)[0]}
        client.cookies.clear()
        assert client.get("/auth/session", headers=headers(auth)).json()["accounts"] == "managed"
        changed = client.post(
            "/auth/password",
            headers=headers(auth),
            json={"current_password": PASSWORD, "new_password": "another-password-1234"},
        )
        assert changed.status_code == 403 and "provided by the organizers" in changed.json()["detail"]
        recovered = client.post(
            "/auth/recover",
            headers=mutation,
            json={"username": "judge", "recovery_code": "x" * 43, "new_password": "another-password-1234"},
        )
        assert recovered.status_code == 403 and "provided by the organizers" in recovered.json()["detail"]
        again = client.post("/auth/login", headers=mutation, json={"username": "judge", "password": PASSWORD})
        assert again.status_code == 200


def test_open_accounts_are_the_default(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/auth/session").json()["accounts"] == "open"
        register(client, "alice")


@pytest.mark.parametrize("value", [0, -1, "5", float("nan"), 10_000])
def test_daily_spend_limit_must_be_a_positive_amount(tmp_path, value):
    with pytest.raises(ValueError):
        make_app(tmp_path, daily_spend_limit_usd=value)


def test_unknown_account_mode_is_refused(tmp_path):
    with pytest.raises(ValueError):
        make_app(tmp_path, accounts_mode="invite")


def test_daily_spend_limit_stops_new_paid_work_for_that_account_only(tmp_path):
    app, _ = make_app(tmp_path, daily_spend_limit_usd=0.5)
    with TestClient(app, base_url=ORIGIN) as client:
        alice, bob = register(client, "alice"), register(client, "bob")
        assert client.get("/api/jobs", headers=headers(alice)).status_code == 200
        store = app.state.registry.runtimes[alice["user"]["id"]].app.state.store
        old = "2000-01-01T00:00:00+00:00"
        with store.connect() as db:  # spend older than 24 hours no longer counts
            store._audit(db, "job-old", "model_usage", {"model": "sam-3.1", "estimated_usd": 9.0})
            db.execute("UPDATE audit SET created_at=? WHERE entity_id='job-old'", (old,))
        assert client.post("/api/items/missing/discovery", headers=headers(alice)).status_code == 404
        store.record_model_usage({"entity_id": "job-1", "model": "muse-image-1.0", "estimated_usd": 0.3})
        store.record_model_usage({"entity_id": "job-2", "model": "muse-spark-1.3", "estimated_usd": 0.25})
        assert store.model_spend_since(old) == pytest.approx(9.55)
        upload = client.post("/api/jobs", headers=headers(alice), files={"file": ("shirt.jpg", jpg(), "image/jpeg")})
        assert upload.status_code == 429 and "allowance" in upload.json()["detail"]
        for path in ("/api/items/missing/discovery", "/api/items/missing/reconstruction"):
            assert client.post(path, headers=headers(alice)).status_code == 429
        assert client.post("/api/items/missing/discovery", headers=headers(bob)).status_code == 404


def test_login_page_forms_never_put_credentials_in_the_url(tmp_path):
    app, _ = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        page = client.get("/login").text
        for form in ("login-form", "register-form", "recover-form"):
            assert f'<form id="{form}" method="post"' in page
