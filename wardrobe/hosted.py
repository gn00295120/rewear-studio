"""Opt-in authenticated gateway for isolated, single-process wardrobe apps.

Only a verified server-side session selects a tenant. The existing application
keeps its fixed Store, media root and background managers for its entire life.
This module never imports the default local ASGI application.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import shutil
import time
from contextvars import ContextVar
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from starlette.datastructures import MutableHeaders
from starlette.requests import ClientDisconnect
from starlette.staticfiles import StaticFiles

from .accounts import AccountError, AccountStore
from .config import Settings
from .work_budget import WorkBudget

PACKAGE = Path(__file__).resolve().parent
AUTH_BODY_LIMIT = 8 * 1024
_ACCOUNT_ID = re.compile(r"[a-f0-9]{32}\Z")
_MUTATIONS = {"POST", "PUT", "PATCH", "DELETE"}
_PUBLIC_AUTH = {"/auth/session", "/auth/login", "/auth/register", "/auth/recover"}
_AUTH_ROUTES = _PUBLIC_AUTH | {"/auth/logout", "/auth/password"}
_MIB = 1024 * 1024
_HEADROOM = {"analysis": 32 * _MIB, "reconstruction": 64 * _MIB, "discovery": 24 * _MIB}
_TRASH_JOB = re.compile(r"[a-f0-9]{32}-[a-f0-9]{8}\Z")


class _StorageReservation:
    def __init__(self, budget, jobs, source_bytes, headroom):
        self.budget, self.jobs = budget, jobs
        self.source_bytes, self.headroom = source_bytes, headroom
        self.tasks: set[asyncio.Task] = set()
        self.closed = False
        self.token = budget.current.set(self)

    @property
    def bytes(self):
        return (0 if self.closed else self.source_bytes) + self.headroom

    def track(self, task):
        self.tasks.add(task)

        def finished(done):
            self.tasks.discard(done)
            if self.closed and not self.tasks:
                self.budget.reservations.discard(self)

        task.add_done_callback(finished)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.jobs = 0
        self.budget.current.reset(self.token)
        if not self.tasks:
            self.budget.reservations.discard(self)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class TenantStorageBudget:
    """Measured files plus conservative in-flight admission reservations.

    This is not a filesystem quota: transient transcoding and model outputs may
    exceed their estimated headroom. Such bytes remain counted; later growth is
    refused. Only this tenant's jobs and deleted-job trash are traversed.
    """

    def __init__(self, directory, *, max_jobs=200, max_media_bytes=2 * 1024**3):
        self.directory = Path(directory)
        self.max_jobs, self.max_media_bytes = max_jobs, max_media_bytes
        self.store = None
        self.reservations: set[_StorageReservation] = set()
        self.current = ContextVar(f"storage-{self.directory.name}", default=None)

    def bind(self, store):
        if store.path.parent != self.directory:
            raise ValueError("Storage capacity and account data directory do not match.")
        self.store = store

    def _files(self):
        total, entries = 0, 0
        for root in (self.directory / "jobs", self.directory / ".trash"):
            if root.is_symlink():
                raise HTTPException(
                    503, "Account file location is invalid. Please contact the administrator."
                )
            if not root.exists():
                continue
            pending = [root]
            while pending:
                parent = pending.pop()
                try:
                    children = os.scandir(parent)
                except FileNotFoundError:
                    # Background cleanup may remove an already-enumerated
                    # temporary directory before this traversal reaches it.
                    continue
                with children:
                    for entry in children:
                        entries += 1
                        if entries > 200_000 or entry.is_symlink():
                            raise HTTPException(
                                503,
                                "Account file location or count is abnormal. Please contact the administrator.",
                            )
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                pending.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                total += entry.stat(follow_symlinks=False).st_size
                        except FileNotFoundError:
                            # A background attempt may have just removed a temp file.
                            continue
        return total

    def _trash(self):
        root = self.directory / ".trash"
        if root.is_symlink():
            raise HTTPException(503, "Deleted file location is invalid. Please contact the administrator.")
        if not root.exists():
            return []
        return sorted(path for path in root.iterdir() if _TRASH_JOB.fullmatch(path.name))

    def snapshot(self):
        try:
            used = self._files()
            pending = len(self._trash())
        except OSError:
            raise HTTPException(
                503, "Unable to verify account storage capacity. Please try again later."
            ) from None
        return {
            "mode": "hosted",
            "limits": {"jobs": self.max_jobs, "media_bytes": self.max_media_bytes},
            "usage": {
                "jobs": len(self.store.jobs()),
                "media_bytes": used,
                "reserved_jobs": sum(lease.jobs for lease in self.reservations),
                "reserved_bytes": sum(lease.bytes for lease in self.reservations),
            },
            "cleanup": {
                "pending_directories": pending,
                "error": "Deleted files are pending cleanup and still count toward capacity. Please retry cleanup."
                if pending
                else None,
            },
            "notice": "Capacity is calculated from this account's jobs and pending cleanup files, with space reserved for in-progress output. This is a job admission limit, not an OS-level disk quota.",
        }

    def reserve(self, *, jobs=0, source_bytes=0, kind="analysis"):
        if (
            type(jobs) is not int
            or jobs < 0
            or type(source_bytes) is not int
            or source_bytes < 0
            or kind not in _HEADROOM
        ):
            raise ValueError("Invalid storage reservation parameters.")
        state = self.snapshot()["usage"]
        headroom = _HEADROOM[kind] * (jobs if jobs else 1)
        if state["jobs"] + state["reserved_jobs"] + jobs > self.max_jobs:
            raise HTTPException(
                429,
                "Account job limit reached. Please delete unneeded jobs first. Reading, exporting, and canceling are still available.",
            )
        if state["media_bytes"] + state["reserved_bytes"] + source_bytes + headroom > self.max_media_bytes:
            raise HTTPException(
                507,
                "Insufficient account storage. Please delete jobs or retry cleanup of pending deletions. Reading, exporting, and canceling are still available.",
            )
        lease = _StorageReservation(self, jobs, source_bytes, headroom)
        self.reservations.add(lease)
        return lease

    def track(self, task):
        lease = self.current.get()
        if lease is not None and lease in self.reservations:
            lease.track(task)

    def cleanup(self, target=None):
        """Only remove committed deletions, never a live job or arbitrary path."""
        paths = [Path(target)] if target is not None else self._trash()
        for path in paths:
            if (
                path.parent != self.directory / ".trash"
                or not _TRASH_JOB.fullmatch(path.name)
                or (self.directory / ".trash").is_symlink()
                or path.is_symlink()
                or self.store.job(path.name[:32]) is not None
            ):
                continue
            try:
                if path.is_dir():
                    shutil.rmtree(path)
            except OSError:
                # The retained directory is a durable pending-cleanup record;
                # snapshot reports it, including after process restart.
                continue
        return self.snapshot()


class HostedError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message
        super().__init__(message)


def _origin(value: str, allow_insecure_local: bool) -> tuple[str, str]:
    """Validate an operator-supplied origin, never a client-supplied Host."""
    try:
        if not isinstance(value, str) or any(c.isspace() or c == "\\" for c in value):
            raise ValueError
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or "?" in value
            or "#" in value
            or "%" in value
        ):
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
            loopback = address.is_loopback
        except ValueError:
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
                raise ValueError from None
            if any(
                not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                for label in host.split(".")
            ):
                raise ValueError
            loopback = host == "localhost"
        if parsed.scheme == "http" and not (allow_insecure_local and loopback):
            raise ValueError
        authority = f"[{host}]" if ":" in host else host
        if port is not None and port != (443 if parsed.scheme == "https" else 80):
            authority += f":{port}"
        return f"{parsed.scheme}://{authority}", host
    except (ValueError, UnicodeError):
        raise ValueError(
            "Hosted origin must be an HTTPS origin with no path. HTTP is allowed only for explicitly enabled local testing."
        ) from None


def _private_directory(path: Path) -> Path:
    path = Path(path).expanduser().absolute()
    if path.is_symlink():
        raise ValueError("Hosted data directory must not be a symbolic link.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise ValueError("Hosted data directory is not usable.")
    return path.resolve()


async def _finish(task):
    """Cancellation must not abandon a writer or an Argon2 worker thread."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


class _AuthWorkers:
    def __init__(self, concurrency=4):
        self.semaphore = asyncio.Semaphore(concurrency)
        self.tasks: set[asyncio.Task] = set()
        self.closing = False

    async def run(self, function, *args):
        if self.closing or self.semaphore.locked():
            raise HostedError(429, "Account service is busy. Please try again later.")
        async with self.semaphore:
            task = asyncio.create_task(asyncio.to_thread(function, *args))
            self.tasks.add(task)
            try:
                return await _finish(task)
            finally:
                self.tasks.discard(task)

    async def close(self):
        self.closing = True
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)


class _RateLimiter:
    """Bounded fixed windows; a full table rejects new keys, never evicts limits."""

    def __init__(self, clock=time.monotonic, max_keys=4096):
        self.clock, self.max_keys = clock, max_keys
        self.buckets: dict[str, tuple[float, int]] = {}

    def check(self, client: str, username: str, operation: str):
        now = self.clock()
        self.buckets = {key: value for key, value in self.buckets.items() if value[0] > now}
        account = hashlib.sha256(username.casefold().encode("utf-8", errors="replace")).hexdigest()
        peer = hashlib.sha256(client.encode("utf-8", errors="replace")).hexdigest()
        limits = [(f"ip:{peer}", 30, 300), (f"{operation}:account:{account}", 10, 300)]
        if operation == "register":
            limits.append((f"register:ip:{peer}", 10, 3600))
        new_keys = sum(key not in self.buckets for key, _, _ in limits)
        if len(self.buckets) + new_keys > self.max_keys:
            raise HostedError(429, "Too many requests. Please try again later.")
        if any(self.buckets.get(key, (0, 0))[1] >= maximum for key, maximum, _ in limits):
            raise HostedError(429, "Too many requests. Please try again later.")
        for key, _, window in limits:
            expires, count = self.buckets.get(key, (now + window, 0))
            self.buckets[key] = (expires, count + 1)


@dataclass
class TenantRuntime:
    account_id: str
    app: FastAPI
    directory: Path
    lifespan: object
    requests: int = 0
    last_used: float = field(default_factory=time.monotonic)
    request_tasks: set[asyncio.Task] = field(default_factory=set)

    def background_busy(self):
        return any(
            not task.done()
            for name in ("manager", "reconstruction", "discovery")
            for task in getattr(self.app.state, name).tasks.values()
        )


class TenantRegistry:
    def __init__(
        self, settings, directory, origin, host, providers_factory, max_active, budget, storage_limits,
        daily_spend_limit_usd=None,
    ):
        self.settings, self.directory = settings, directory
        self.origin, self.host = origin, host
        self.providers_factory, self.max_active, self.budget = providers_factory, max_active, budget
        self.storage_limits = storage_limits
        self.daily_spend_limit_usd = daily_spend_limit_usd
        self.tenants = directory / "tenants"
        self.runtimes: dict[str, TenantRuntime] = {}
        self.lock = asyncio.Lock()
        self.closing = False

    def _path(self, account_id):
        if not isinstance(account_id, str) or not _ACCOUNT_ID.fullmatch(account_id):
            raise HostedError(401, "Session has expired. Please log in again.")
        directory = self.tenants / account_id
        if (
            self.directory.is_symlink()
            or self.tenants.is_symlink()
            or directory.is_symlink()
            or self.tenants.resolve() != self.directory / "tenants"
            or directory.resolve().parent != self.tenants
            or any(
                (directory / name).is_symlink()
                for name in (
                    "wardrobe.sqlite3",
                    "wardrobe.sqlite3-wal",
                    "wardrobe.sqlite3-shm",
                    ".server.lock",
                    "jobs",
                    ".trash",
                )
            )
        ):
            raise HostedError(503, "Account data directory is not usable.")
        return directory

    async def _create(self, account_id, directory):
        # Import the pure/lazy factory only after the hosted boundary is selected.
        from .app import create_app

        tenant_settings = replace(self.settings, data_dir=directory)
        storage = TenantStorageBudget(directory, **self.storage_limits)
        providers = self.providers_factory(tenant_settings) if self.providers_factory else None
        app = None
        try:
            app = create_app(
                tenant_settings,
                providers,
                allowed_hosts=[self.host],
                expected_origin=self.origin,
                work_budget=self.budget,
                storage_budget=storage,
                daily_spend_limit_usd=self.daily_spend_limit_usd,
            )
            lifespan = app.router.lifespan_context(app)
            await lifespan.__aenter__()
            return TenantRuntime(account_id, app, directory, lifespan)
        except BaseException:
            service = app.state.manager.providers if app is not None else providers
            if service is not None:
                await _finish(asyncio.create_task(service.close()))
            raise

    @asynccontextmanager
    async def acquire(self, account_id):
        request_task = asyncio.current_task()
        async with self.lock:
            if self.closing:
                raise HostedError(503, "Service is shutting down. Please try again later.")
            directory = self._path(account_id)
            runtime = self.runtimes.get(account_id)
            if runtime is None:
                if len(self.runtimes) >= self.max_active:
                    idle = [
                        runtime
                        for runtime in self.runtimes.values()
                        if not runtime.requests and not runtime.background_busy()
                    ]
                    if not idle:
                        raise HostedError(503, "Service is at capacity. Please try again later.")
                    old = min(idle, key=lambda runtime: runtime.last_used)
                    self.runtimes.pop(old.account_id)
                    await _finish(asyncio.create_task(old.lifespan.__aexit__(None, None, None)))
                try:
                    runtime = await self._create(account_id, directory)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise HostedError(503, "Account service is temporarily unavailable.") from None
                self.runtimes[account_id] = runtime
            runtime.requests += 1
            runtime.request_tasks.add(request_task)
        try:
            yield runtime
        finally:

            async def release():
                async with self.lock:
                    runtime.requests -= 1
                    runtime.request_tasks.discard(request_task)
                    runtime.last_used = time.monotonic()

            await _finish(asyncio.create_task(release()))

    async def close(self):
        async with self.lock:
            self.closing = True
            requests = {task for runtime in self.runtimes.values() for task in runtime.request_tasks}
        for task in requests:
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in requests if task is not asyncio.current_task()), return_exceptions=True
        )
        async with self.lock:
            runtimes = list(self.runtimes.values())
            self.runtimes.clear()
        outcomes = await asyncio.gather(
            *(runtime.lifespan.__aexit__(None, None, None) for runtime in runtimes),
            return_exceptions=True,
        )
        if any(isinstance(outcome, BaseException) for outcome in outcomes):
            raise RuntimeError("Account service shutdown did not complete.") from None


def _error(status, message):
    return JSONResponse({"detail": message}, status_code=status)


class _HostedBoundary:
    def __init__(self, app, *, accounts, account_io, registry, origin, cookie_name):
        self.app, self.accounts, self.registry = app, accounts, registry
        self.account_io = account_io
        self.origin, self.cookie_name = origin, cookie_name
        self.authority = urlsplit(origin).netloc

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        bound_account = None

        async def private_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "private, no-store"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
                headers["Content-Security-Policy"] = (
                    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                    "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
                    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
                )
                headers.add_vary_header("Cookie")
                if "X-Rewear-Account" in headers:
                    del headers["X-Rewear-Account"]
                actual_account = (
                    scope.get("state", {}).get("hosted_response_account", bound_account)
                    if scope["path"] in _AUTH_ROUTES
                    else bound_account
                )
                if actual_account is not None:
                    headers["X-Rewear-Account"] = actual_account
                if self.origin.startswith("https:"):
                    headers["Strict-Transport-Security"] = "max-age=31536000"
            await send(message)

        try:
            if request.headers.get("host", "").lower() != self.authority:
                raise HostedError(400, "Please use the configured site URL.")
            path = scope["path"]
            public = path in {"/login", "/favicon.ico", "/healthz"} or path.startswith("/static/")
            token = request.cookies.get(self.cookie_name)
            accounts = self.accounts() if not public else None
            session = await self.account_io.run(accounts.get_session, token) if token and not public else None
            if session is not None:
                account_id = session["user"]["id"]
                if not isinstance(account_id, str) or not _ACCOUNT_ID.fullmatch(account_id):
                    raise HostedError(401, "Session has expired. Please log in again.")
                bound_account = account_id
            is_auth = path in _AUTH_ROUTES
            if not public and path not in _PUBLIC_AUTH and session is None:
                response = (
                    RedirectResponse("/login", status_code=303)
                    if path == "/"
                    else _error(401, "Please log in first.")
                )
                await response(scope, receive, private_send)
                return
            if scope["method"] in _MUTATIONS:
                if (
                    request.headers.get("origin") != self.origin
                    or request.headers.get("x-rewear-request") != "1"
                ):
                    raise HostedError(403, "Please submit this action from the site.")
                if session is not None and not hmac.compare_digest(
                    request.headers.get("x-csrf-token", "").encode("utf-8"),
                    session["csrf_token"].encode("utf-8"),
                ):
                    raise HostedError(403, "Action verification has expired. Please refresh the page.")
            if public or is_auth:
                scope.setdefault("state", {})["hosted_session"] = session
                scope["state"]["hosted_token"] = token
                await self.app(scope, receive, private_send)
                return
            async with self.registry.acquire(bound_account) as runtime:
                await runtime.app(scope, receive, private_send)
        except HostedError as exc:
            await _error(exc.status, exc.message)(scope, receive, private_send)
        except AccountError as exc:
            await _error(exc.status_code, exc.message)(scope, receive, private_send)


async def _body(request, required, optional=()):
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        raise HostedError(415, "Please submit account data as JSON.")
    try:
        length = int(request.headers.get("content-length", "0"))
    except ValueError:
        raise HostedError(400, "Invalid account data format.") from None
    if length < 0 or length > AUTH_BODY_LIMIT:
        raise HostedError(413, "Account data exceeds the size limit.")
    raw = bytearray()
    try:
        async with asyncio.timeout(10):
            async for chunk in request.stream():
                if len(raw) + len(chunk) > AUTH_BODY_LIMIT:
                    raise HostedError(413, "Account data exceeds the size limit.")
                raw.extend(chunk)
        value = json.loads(raw)
    except TimeoutError:
        raise HostedError(408, "Account data reception timed out. Please resubmit.") from None
    except (ValueError, UnicodeError, RecursionError, ClientDisconnect):
        raise HostedError(400, "Invalid account data format.") from None
    if (
        not isinstance(value, dict)
        or set(value) - set(required) - set(optional)
        or not set(required) <= set(value)
        or any(not isinstance(item, str) for item in value.values())
    ):
        raise HostedError(422, "Invalid account data format.")
    return value


def create_hosted_app(
    settings: Settings,
    *,
    origin: str,
    hosted_dir: Path | None = None,
    providers_factory=None,
    allow_insecure_local=False,
    max_active_tenants=16,
    max_accounts=1000,
    max_jobs_per_tenant=200,
    max_media_bytes_per_tenant=2 * 1024**3,
    accounts_mode="open",
    daily_spend_limit_usd=None,
) -> FastAPI:
    if type(allow_insecure_local) is not bool:
        raise ValueError("Local HTTP option must be a boolean.")
    origin, host = _origin(origin, allow_insecure_local)
    if (
        type(max_active_tenants) is not int
        or max_active_tenants < 1
        or type(max_accounts) is not int
        or max_accounts < 1
        or type(max_jobs_per_tenant) is not int
        or max_jobs_per_tenant < 1
        or type(max_media_bytes_per_tenant) is not int
        or max_media_bytes_per_tenant < 1
    ):
        raise ValueError("Hosted account and capacity limits must be positive integers.")
    if accounts_mode not in ("open", "managed"):
        raise ValueError("Account mode must be 'open' or 'managed'.")
    if daily_spend_limit_usd is not None and (
        type(daily_spend_limit_usd) not in (int, float) or not 0 < daily_spend_limit_usd < 10_000
    ):
        raise ValueError("Daily spend limit must be a positive amount in US dollars.")
    directory = _private_directory(hosted_dir if hosted_dir is not None else settings.data_dir / "hosted")
    control = directory / "control"
    accounts, ready = None, False
    budget = WorkBudget(max_pending=settings.max_queue, per_kind=1)
    storage_limits = {"max_jobs": max_jobs_per_tenant, "max_media_bytes": max_media_bytes_per_tenant}
    registry = TenantRegistry(
        settings, directory, origin, host, providers_factory, max_active_tenants, budget, storage_limits,
        daily_spend_limit_usd=daily_spend_limit_usd,
    )
    workers, account_io, limiter = _AuthWorkers(), _AuthWorkers(concurrency=16), _RateLimiter()
    secure = origin.startswith("https:")
    cookie_name = "__Host-rewear_session" if secure else "rewear_local_session"

    @asynccontextmanager
    async def lifespan(app):
        import fcntl

        nonlocal accounts, ready
        if directory.is_symlink() or (directory / ".server.lock").is_symlink():
            raise RuntimeError("Hosted data directory is not usable.")
        fd = os.open(directory / ".server.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        acquired = False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError(
                    "Another service is already running on this hosted data directory. Please keep a single process."
                ) from None
            acquired = True
            _private_directory(control)
            _private_directory(registry.tenants)
            accounts = await _finish(
                asyncio.create_task(asyncio.to_thread(AccountStore, control, max_accounts=max_accounts))
            )
            app.state.accounts = accounts
            ready = True
            yield
        finally:
            try:
                if acquired:
                    ready = False
                    workers.closing = account_io.closing = True
                    try:
                        await registry.close()
                    finally:
                        try:
                            await workers.close()
                            await account_io.close()
                        finally:
                            accounts = None
                            app.state.accounts = None
            finally:
                os.close(fd)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.accounts = accounts
    app.state.registry = registry
    app.state.work_budget = budget
    app.state.auth_workers = workers
    app.state.account_io = account_io
    app.state.rate_limiter = limiter
    app.state.origin = origin
    app.state.cookie_name = cookie_name
    app.state.operational_limits = {
        "max_accounts": max_accounts,
        "max_active_tenants": max_active_tenants,
        "max_pending_work": settings.max_queue,
        "workers_per_kind": 1,
        "max_jobs_per_tenant": max_jobs_per_tenant,
        "max_media_bytes_per_tenant": max_media_bytes_per_tenant,
        "output_reservation_bytes": dict(_HEADROOM),
        "storage_enforcement": "admission_with_inflight_reservations_not_filesystem_quota",
        "accounts_mode": accounts_mode,
        "daily_spend_limit_usd": daily_spend_limit_usd,
    }
    managed = accounts_mode == "managed"

    def self_service():
        # Managed (for example judge/demo) accounts are created by the operator; letting one viewer
        # register, change or reset a shared password would lock everyone else out.
        if managed:
            raise HostedError(403, "Accounts on this site are provided by the organizers; sign in with the account you were given.")

    def account_store():
        if not ready or accounts is None:
            raise HostedError(503, "Account service has not started yet. Please try again later.")
        if (
            directory.is_symlink()
            or control.is_symlink()
            or control.resolve() != directory / "control"
            or any(
                (control / name).is_symlink()
                for name in ("accounts.sqlite3", "accounts.sqlite3-wal", "accounts.sqlite3-shm")
            )
        ):
            raise HostedError(503, "Account data directory is not usable.")
        return accounts

    @app.exception_handler(HostedError)
    async def hosted_error(_request, exc):
        return _error(exc.status, exc.message)

    @app.exception_handler(AccountError)
    async def account_error(_request, exc):
        return _error(exc.status_code, exc.message)

    def session_response(request, session, **extra):
        request.scope["state"]["hosted_response_account"] = session["user"]["id"]
        response = JSONResponse({"user": session["user"], "csrf_token": session["csrf_token"], **extra})
        response.set_cookie(
            cookie_name,
            session["token"],
            expires=datetime.fromisoformat(session["expires_at"]),
            secure=secure,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response

    def rate(request, username, operation):
        # scope.client is the transport peer; this code never trusts X-Forwarded-For.
        peer = request.client.host if request.client else "unknown"
        limiter.check(peer, username, operation)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/login")
    async def login_page():
        return FileResponse(PACKAGE / "static" / "auth.html")

    @app.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)

    @app.get("/auth/session")
    async def session(request: Request):
        current = request.state.hosted_session
        if current is None:
            return {"mode": "hosted", "authenticated": False, "accounts": accounts_mode}
        return {
            "mode": "hosted",
            "authenticated": True,
            "accounts": accounts_mode,
            "user": current["user"],
            "csrf_token": current["csrf_token"],
        }

    @app.post("/auth/register")
    async def register(request: Request):
        self_service()
        body = await _body(request, ("username", "password"), ("display_name",))
        rate(request, body["username"], "register")

        current = await workers.run(
            accounts.register_session, body["username"], body["password"], body.get("display_name", "")
        )
        return session_response(request, current, recovery_code=current["recovery_code"])

    @app.post("/auth/login")
    async def login(request: Request):
        body = await _body(request, ("username", "password"))
        rate(request, body["username"], "login")
        current = await workers.run(accounts.authenticate_session, body["username"], body["password"])
        return session_response(request, current)

    @app.post("/auth/logout")
    async def logout(request: Request):
        await account_io.run(accounts.revoke_session, request.state.hosted_token)
        response = Response(status_code=204)
        response.delete_cookie(cookie_name, path="/", secure=secure, httponly=True, samesite="lax")
        return response

    @app.post("/auth/password")
    async def password(request: Request):
        self_service()
        body = await _body(request, ("current_password", "new_password"))
        user = request.state.hosted_session["user"]
        rate(request, user["username"], "password")
        current = await workers.run(
            accounts.change_password_session, user["id"], body["current_password"], body["new_password"]
        )
        return session_response(request, current)

    @app.post("/auth/recover")
    async def recover(request: Request):
        self_service()
        body = await _body(request, ("username", "recovery_code", "new_password"))
        rate(request, body["username"], "recover")
        result = await workers.run(
            accounts.recover, body["username"], body["recovery_code"], body["new_password"]
        )
        return {"recovery_code": result["recovery_code"]}

    app.mount("/static", StaticFiles(directory=PACKAGE / "static"), name="static")
    app.add_middleware(
        _HostedBoundary,
        accounts=account_store,
        account_io=account_io,
        registry=registry,
        origin=origin,
        cookie_name=cookie_name,
    )
    return app
