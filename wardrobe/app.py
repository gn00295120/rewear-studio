from __future__ import annotations

import asyncio
import csv
import io
import json
import mimetypes
import os
import shutil
import time
from contextlib import asynccontextmanager, contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import Settings
from .discovery import DiscoveryError, DiscoveryManager
from .batches import register_batches
from .pipeline import JobManager, media_url
from .providers import Providers
from .quality import result_status
from .reconstruction import ReconstructionError, ReconstructionManager
from .reconstruction_quality import is_preview_ready, validate_report
from .schemas import (
    Category,
    DiscoveryRequest,
    Garment,
    GenerateRequest,
    ItemPatch,
    MergeRequest,
    Mode,
    Provider,
    ReconstructionRequest,
    ReconstructionSelection,
    person_box as parse_person_box,
)
from .store import ACTIVE, Store, now

PACKAGE = Path(__file__).resolve().parent
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".m4v"}


@contextmanager
def data_directory_lock(directory: Path):
    """Exclude another service or offline backup even during schema setup."""
    import fcntl

    directory.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(directory / ".server.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise RuntimeError("Unable to open data directory lock.") from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError(
                "Another server or backup is already running on this data directory; use the existing server or a different REWEAR_DATA_DIR."
            ) from None
        yield
    finally:
        os.close(fd)


def lan_hosts() -> list[str]:
    """Private LAN addresses the CLI's --lan flag opted into for same-network phones."""
    return [host.strip() for host in os.environ.get("REWEAR_LAN_HOSTS", "").split(",") if host.strip()]


def public_job(job: dict) -> dict:
    return {key: value for key, value in job.items() if key not in {"input_name", "options"}}


class BodyTooLarge(HTTPException):
    def __init__(self):
        super().__init__(status_code=413, detail="File exceeds upload size limit.")


class BodyLimitMiddleware:
    def __init__(self, app, limit, batch_limit=None):
        self.app, self.limit, self.batch_limit = app, limit, batch_limit or limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        limit = self.batch_limit if scope.get("path") == "/api/batches" else self.limit
        length = headers.get(b"content-length", b"0")
        if length.isdigit() and int(length) > limit:
            return await JSONResponse({"detail": "File exceeds upload size limit."}, status_code=413)(
                scope, receive, send
            )
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLarge()
            return message

        await self.app(scope, bounded_receive, send)


def create_app(
    settings: Settings | None = None,
    providers=None,
    *,
    allowed_hosts=None,
    expected_origin=None,
    work_budget=None,
    storage_budget=None,
    daily_spend_limit_usd=None,
) -> FastAPI:
    settings = settings or Settings()
    with data_directory_lock(settings.data_dir):
        store = Store(settings.data_dir)
        (settings.data_dir / "jobs").mkdir(exist_ok=True)
    provider_service = providers or Providers(settings)
    if callable(getattr(provider_service, "bind_usage_recorder", None)):
        provider_service.bind_usage_recorder(store.record_model_usage)
    if storage_budget is not None:
        storage_budget.bind(store)
    reconstruction = ReconstructionManager(
        settings, store, provider_service, work_budget=work_budget, storage_budget=storage_budget
    )
    discovery = DiscoveryManager(
        settings, store, provider_service, work_budget=work_budget, storage_budget=storage_budget
    )
    manager = JobManager(
        settings,
        store,
        provider_service,
        reconstruction,
        discovery,
        work_budget=work_budget,
        storage_budget=storage_budget,
    )

    @asynccontextmanager
    async def lifespan(app):
        with data_directory_lock(settings.data_dir):
            store.recover()
            try:
                yield
            finally:
                await manager.close()
                await reconstruction.close()
                await discovery.close()
                await provider_service.close()

    app = FastAPI(
        title="Rewear Studio",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.store, app.state.manager, app.state.settings = store, manager, settings
    app.state.reconstruction = reconstruction
    app.state.discovery = discovery
    app.state.storage_budget = storage_budget
    app.state.health_cache = None
    demo_lock = asyncio.Lock()

    app.add_middleware(
        BodyLimitMiddleware,
        limit=settings.max_upload_mb * 1024 * 1024 + 1024 * 1024,
        batch_limit=settings.max_batch_upload_mb * 1024 * 1024 + 1024 * 1024,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=allowed_hosts or ["localhost", "127.0.0.1", "[::1]", "testserver", *lan_hosts()],
    )

    @app.exception_handler(BodyTooLarge)
    async def too_large(_request, _exc):
        return JSONResponse({"detail": "File exceeds upload size limit."}, status_code=413)

    @app.middleware("http")
    async def browser_boundary(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            permitted_origin = expected_origin or f"{request.url.scheme}://{request.headers.get('host', '')}"
            if (expected_origin is not None or origin) and origin != permitted_origin:
                return JSONResponse(
                    {"detail": "Please submit this action from the local application."}, status_code=403
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/auth/session", include_in_schema=False)
    async def local_session():
        # Hosted authentication is handled by the outer gateway, before any
        # tenant route can be reached. This keeps local mode explicitly distinct.
        return JSONResponse({"mode": "local", "authenticated": False}, headers={"Cache-Control": "no-store"})

    def get_job(job_id):
        job = store.job(job_id)
        if not job:
            raise HTTPException(404, "Analysis not found.")
        return job

    def require_queue():
        if not manager.available():
            raise HTTPException(
                429, "Pending analysis limit reached; please wait for current analysis to complete."
            )

    def require_allowance():
        """Hosted demo accounts can be capped on estimated model spend over a rolling 24 hours."""
        if daily_spend_limit_usd is None:
            return
        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        if store.model_spend_since(since) >= daily_spend_limit_usd:
            raise HTTPException(
                429, "This account has used today's analysis allowance. Please try again in 24 hours."
            )

    def require_meta():
        if not settings.api_key:
            raise HTTPException(
                503, "This feature requires a Meta API key. Please set MODEL_API_KEY and restart."
            )

    health_lock = asyncio.Lock()

    async def model_health(*, refresh=False):
        cached = app.state.health_cache
        if not refresh and cached and time.monotonic() - cached[0] <= 10:
            return cached[1]
        # The deadline includes contention, not just the provider request.
        async with asyncio.timeout(5):
            async with health_lock:
                cached = app.state.health_cache
                if not refresh and cached and time.monotonic() - cached[0] <= 10:
                    return cached[1]
                state = await provider_service.health()
                app.state.health_cache = time.monotonic(), state
                return state

    async def require_provider(provider):
        require_allowance()
        if provider == "meta":
            require_meta()
            return
        # Check before storing another upload or paying for SAM / generation.
        # A previous healthy response must not queue work for a now-offline model.
        try:
            state = await model_health(refresh=True)
            local = state.get("local") or {}
            available = local.get("available") is True
        except Exception:
            available = False
        if not available:
            raise HTTPException(
                503,
                "Local analysis model is not ready. Please start Ollama and install the required model, "
                "or explicitly select Meta cloud analysis; the system will not auto-switch or transmit photos.",
            )

    @app.get("/api/health")
    async def health(refresh: bool = False):
        state = await model_health(refresh=refresh)
        return {
            "status": "ok",
            **state,
            "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
            "reconstruction": {
                "available": reconstruction.available,
                "automatic_outfit": True,
                "model": "muse-image-1.0",
                "default_mode": "preview",
                "preview_candidates": 3,
                "preview_standard_estimate_usd": 0.03,
                "verified_mode": True,
            },
            "discovery": {
                "available": discovery.available,
                "model": "muse-spark-1.3",
                "markets": ["TW", "global"],
                "image_search": settings.image_search_health(),
            },
            "limits": {
                "upload_mb": settings.max_upload_mb,
                "video_seconds": settings.max_video_seconds,
                "max_items": settings.max_items,
                "batch_files": settings.max_batch_files,
                "batch_upload_mb": settings.max_batch_upload_mb,
                "max_queue": settings.max_queue,
            },
            "model_usage": store.model_usage(),
        }

    @app.get("/api/jobs")
    def jobs():
        return {"jobs": [public_job(job) for job in store.jobs()]}

    register_batches(
        app, settings, store, manager, public_job, require_provider, storage_budget=storage_budget
    )

    if storage_budget is not None:

        @app.get("/api/storage")
        async def storage_status():
            return storage_budget.snapshot()

        @app.post("/api/storage/cleanup")
        async def storage_cleanup():
            return storage_budget.cleanup()

    @app.post("/api/jobs", status_code=202)
    async def upload(
        file: Annotated[UploadFile, File()],
        provider: Annotated[Provider, Form()] = "local",
        mode: Annotated[Mode, Form()] = "single",
        concept: Annotated[str, Form(min_length=1, max_length=100)] = "garment",
        person_box: Annotated[str | None, Form(max_length=1000)] = None,
        reconstruct: Annotated[Literal["true", "false"], Form()] = "true",
        discover: Annotated[Literal["true", "false"], Form()] = "false",
        discovery_market: Annotated[Literal["TW", "global"], Form()] = "TW",
    ):
        # Missing boolean form fields are treated as unchecked by FastAPI.
        # Explicit text choices retain the intended default-on upload behavior.
        reconstruct = reconstruct == "true"
        discover = discover == "true"
        if discover and mode != "outfit":
            await file.close()
            raise HTTPException(422, "Auto-find shopping options applies to lifestyle outfit photos only.")
        require_queue()
        filename = Path((file.filename or "upload").replace("\\", "/")).name[:200]
        suffix = Path(filename).suffix.lower()
        if suffix not in IMAGE_SUFFIXES | VIDEO_SUFFIXES:
            await file.close()
            raise HTTPException(
                415, "Please upload a JPG, PNG, or WebP image, or an MP4, MOV, or WebM video."
            )
        is_video = suffix in VIDEO_SUFFIXES
        try:
            selected_box = parse_person_box(person_box)
            if (selected_box and mode != "outfit") or (mode == "outfit" and is_video):
                raise ValueError("Person selection and lifestyle outfit modes are for photos only.")
        except ValueError as error:
            await file.close()
            raise HTTPException(422, str(error)) from None
        if mode in {"rack", "outfit"} or is_video or provider == "meta":
            require_meta()
        if is_video and not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
            raise HTTPException(
                503, "Video analysis requires ffmpeg and ffprobe; please install them and retry."
            )
        concept = concept.strip()
        if not concept:
            raise HTTPException(422, "Please enter a segmentation prompt.")
        try:
            await require_provider(provider)
        except BaseException:
            await file.close()
            raise
        with (
            storage_budget.reserve(
                jobs=1,
                source_bytes=file.size if file.size is not None else settings.max_upload_mb * 1024 * 1024,
            )
            if storage_budget is not None
            else nullcontext()
        ):
            job_id = uuid4().hex
            directory = settings.data_dir / "jobs" / job_id
            directory.mkdir()
            destination = directory / ("source" + suffix)
            size = 0
            try:
                with destination.open("wb") as output:
                    while chunk := await file.read(1024 * 1024):
                        size += len(chunk)
                        if size > settings.max_upload_mb * 1024 * 1024:
                            raise HTTPException(413, "File exceeds upload size limit.")
                        output.write(chunk)
                if size == 0:
                    raise HTTPException(422, "File is empty; please select another.")
                # Other uploads may have finished while this one awaited file reads.
                # No await may separate this recheck from persisting the queued job.
                require_queue()
                job = store.create_job(
                    id=job_id,
                    filename=filename,
                    media_type="video" if is_video else "image",
                    provider=provider,
                    mode=mode,
                    concept=concept,
                    input_name=destination.name,
                    source_url=media_url(job_id, destination, directory),
                    options={
                        "reconstruct": reconstruct,
                        "discover": discover,
                        "discovery_market": discovery_market,
                        **({"person_box": selected_box} if selected_box else {}),
                    },
                )
                manager.enqueue(job_id)
            except BaseException:
                shutil.rmtree(directory)
                raise
            finally:
                await file.close()
            return public_job(job)

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str):
        return {**public_job(get_job(job_id)), "items": store.items(job_id)}

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str):
        active = get_job(job_id)["status"] in ACTIVE
        if not active and not reconstruction.active_for_job(job_id) and not discovery.active_for_job(job_id):
            raise HTTPException(409, "This analysis has already ended.")
        if active:
            manager.cancel(job_id)
        await asyncio.gather(reconstruction.stop_job(job_id), discovery.stop_job(job_id))
        await asyncio.sleep(0)
        return public_job(get_job(job_id))

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    async def retry_job(job_id: str):
        old = get_job(job_id)
        if old["status"] not in {"failed", "partial", "cancelled"}:
            raise HTTPException(409, "Only incomplete analyses can be retried.")
        require_queue()
        if old["provider"] == "meta" or old["mode"] in {"rack", "outfit"} or old["media_type"] == "video":
            require_meta()
        if old["media_type"] == "video" and not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
            raise HTTPException(
                503, "Video analysis requires ffmpeg and ffprobe; please install them and retry."
            )
        await require_provider(old["provider"])
        # Deletion or another transition may have happened during the health
        # request. Never resurrect a deleted generated job from its old prompt.
        old = get_job(job_id)
        if old["status"] not in {"failed", "partial", "cancelled"}:
            raise HTTPException(409, "Only incomplete analyses can be retried.")
        require_queue()
        new_id = uuid4().hex
        jobs = settings.data_dir / "jobs"
        original = jobs / job_id
        if (
            jobs.is_symlink()
            or original.is_symlink()
            or not original.resolve().is_relative_to(jobs.resolve())
        ):
            raise HTTPException(409, "Original file location is invalid; please re-upload.")
        source = original / old["input_name"] if old["input_name"] else None
        if source is not None and not source.resolve().is_relative_to(original.resolve()):
            raise HTTPException(409, "Original file location is invalid; please re-upload.")
        source_exists = source is not None and source.is_file()
        if not source_exists:
            if old["media_type"] == "generated":
                require_meta()
            else:
                raise HTTPException(409, "Original file no longer exists; please re-upload.")
        with (
            storage_budget.reserve(jobs=1, source_bytes=source.stat().st_size if source_exists else 0)
            if storage_budget is not None
            else nullcontext()
        ):
            directory = jobs / new_id
            directory.mkdir()
            source_url = None
            try:
                if source_exists:
                    destination = directory / old["input_name"]
                    shutil.copy2(source, destination)
                    source_url = media_url(new_id, destination, directory)
                job = store.create_job(
                    id=new_id,
                    filename=old["filename"],
                    media_type=old["media_type"],
                    provider=old["provider"],
                    mode=old["mode"],
                    concept=old["concept"],
                    input_name=old["input_name"] if source_url else None,
                    source_url=source_url,
                    options=old["options"],
                )
            except BaseException:
                shutil.rmtree(directory)
                raise
            manager.enqueue(new_id)
            return public_job(job)

    @app.delete("/api/jobs/{job_id}", status_code=204)
    async def delete_job(job_id: str):
        if get_job(job_id)["status"] in ACTIVE:
            raise HTTPException(409, "Please cancel the analysis before deleting.")
        with reconstruction.deletion(job_id=job_id), discovery.deletion(job_id=job_id):
            await asyncio.gather(reconstruction.stop_job(job_id), discovery.stop_job(job_id))
            directory = settings.data_dir / "jobs" / job_id
            trash = settings.data_dir / ".trash"
            trash.mkdir(exist_ok=True)
            target = trash / f"{job_id}-{uuid4().hex[:8]}"
            moved = directory.exists()
            if moved:
                directory.rename(target)
            try:
                store.delete_job(job_id)
            except Exception:
                if moved:
                    target.rename(directory)
                raise
        if storage_budget is not None:
            try:
                state = storage_budget.cleanup(target)
                pending = bool(state["cleanup"]["pending_directories"])
            except HTTPException:
                # The database deletion has already committed. A subsequent
                # filesystem scan failure must not claim that it was undone.
                pending = True
            return Response(
                status_code=204,
                headers={"X-Rewear-Cleanup": "pending" if pending else "complete"},
            )
        return Response(status_code=204)

    @app.post("/api/generate", status_code=202)
    async def generate(body: GenerateRequest):
        require_meta()
        require_queue()
        await require_provider(body.provider)
        require_queue()
        with storage_budget.reserve(jobs=1) if storage_budget is not None else nullcontext():
            job_id = uuid4().hex
            (settings.data_dir / "jobs" / job_id).mkdir()
            job = store.create_job(
                id=job_id,
                filename="AI generated garment.png",
                media_type="generated",
                provider=body.provider,
                mode=body.mode,
                concept=body.concept,
                options={"prompt": body.prompt},
            )
            manager.enqueue(job_id)
            return public_job(job)

    @app.post("/api/demo")
    async def demo():
        async with demo_lock:
            existing = next((job for job in store.jobs() if job["is_demo"]), None)
            if existing:
                return public_job(existing)
            fixture_dir = PACKAGE / "demo"
            if not (fixture_dir / "records.json").is_file():
                raise HTTPException(503, "Bundled historical demo data not found.")
            records = json.loads((fixture_dir / "records.json").read_text())
            with (
                storage_budget.reserve(
                    jobs=1,
                    source_bytes=sum(path.stat().st_size for path in fixture_dir.iterdir() if path.is_file()),
                )
                if storage_budget is not None
                else nullcontext()
            ):
                job_id = uuid4().hex
                directory = settings.data_dir / "jobs" / job_id
                directory.mkdir()
                shutil.copy2(fixture_dir / "source.jpg", directory / "preview.jpg")
                url = media_url(job_id, directory / "preview.jpg", directory)
                store.create_job(
                    id=job_id,
                    filename="Sample wardrobe · three full garments",
                    is_demo=True,
                    status="completed",
                    stage="Sample wardrobe loaded",
                    progress=100,
                    preview_url=url,
                    source_url=url,
                    warnings=[
                        "These are full-garment previews saved on 2026-10-22; no new model analysis was run."
                    ],
                )
                for name, entry in records.items():
                    record = Garment.model_validate(entry["record"]).model_dump()
                    shutil.copy2(fixture_dir / name, directory / name)
                    store.add_item(
                        job_id,
                        image_url=media_url(job_id, directory / name, directory),
                        record=record,
                        model_record=record,
                        provenance="Sample wardrobe · Muse Image preview · 2026-10-22 · no new analysis",
                    )
                return public_job(store.job(job_id))

    @app.get("/api/items")
    def items(
        q: str = Query(default="", max_length=200),
        category: Category | None = None,
        review: Literal["all", "pending", "reviewed"] = "all",
        quality: Literal["all", "ready", "needs_retake", "unassessed", "needs_confirmation"] = "all",
        job_id: str | None = None,
    ):
        if job_id:
            get_job(job_id)
        result = store.items(job_id)
        if q.strip():
            result = [
                item
                for item in result
                if q.strip().casefold() in json.dumps(item["record"], ensure_ascii=False).casefold()
            ]
        if category:
            result = [item for item in result if item["record"]["category"] == category]
        if review == "pending":
            result = [item for item in result if not item["reviewed"]]
        elif review == "reviewed":
            result = [item for item in result if item["reviewed"]]
        if quality != "all":
            result = [item for item in result if result_status(item) == quality]
        return {"items": result}

    @app.get("/api/garment-groups")
    def garment_groups():
        return {"groups": store.groups()}

    @app.post("/api/garment-groups", status_code=201)
    async def merge_garments(body: MergeRequest):
        try:
            scope = store.merge_scope(body.item_ids, body.primary_item_id)
            with reconstruction.membership_change(scope["item_ids"]):
                await reconstruction.stop_membership(scope["item_ids"], scope["group_ids"])
                return store.merge_items(body.item_ids, body.primary_item_id)
        except KeyError:
            raise HTTPException(404, "Garments to merge not found.") from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @app.delete("/api/garment-groups/{group_id}", status_code=204)
    async def unmerge_garments(group_id: str):
        group = next((group for group in store.groups() if group["id"] == group_id), None)
        if not group:
            raise HTTPException(404, "Garment group not found.")
        with reconstruction.membership_change(group["item_ids"]):
            await reconstruction.stop_membership(group["item_ids"], [group_id])
            if not store.unmerge(group_id):
                raise HTTPException(404, "Garment group not found.")
        return Response(status_code=204)

    @app.get("/api/duplicates")
    def duplicate_suggestions():
        from .duplicates import suggest_duplicates

        return {
            "suggestions": suggest_duplicates(store.items(), store.groups()),
            "notice": "Similarity score is not the probability of being the same garment; please verify sources before confirming the merge.",
        }

    @app.get("/api/items/{item_id}")
    def item_detail(item_id: str):
        item = store.item(item_id)
        if item is None:
            raise HTTPException(404, "Garment not found.")
        return item

    @app.post("/api/items/{item_id}/reconstruction", status_code=202)
    async def reconstruct_item(item_id: str, body: ReconstructionRequest | None = None):
        require_allowance()
        try:
            return reconstruction.enqueue(
                item_id,
                body.reference_item_ids if body else None,
                mode=body.mode if body else "preview",
            )
        except ReconstructionError as error:
            raise HTTPException(error.status_code, str(error)) from None

    @app.post("/api/items/{item_id}/reconstruction/select")
    async def select_reconstruction(item_id: str, body: ReconstructionSelection):
        try:
            return reconstruction.select_candidate(item_id, body.candidate_id)
        except ReconstructionError as error:
            raise HTTPException(error.status_code, str(error)) from None

    @app.post("/api/items/{item_id}/discovery", status_code=202)
    async def discover_item(item_id: str, body: DiscoveryRequest | None = None):
        require_allowance()
        try:
            return discovery.enqueue(item_id, body.market if body else "TW")
        except DiscoveryError as error:
            raise HTTPException(error.status_code, str(error)) from None

    @app.post("/api/items/{item_id}/discovery/cancel")
    async def cancel_discovery(item_id: str):
        if store.item(item_id) is None:
            raise HTTPException(404, "Garment not found.")
        await discovery.stop_item(item_id)
        return store.item(item_id)

    @app.post("/api/items/{item_id}/reconstruction/cancel")
    async def cancel_reconstruction(item_id: str):
        if store.item(item_id) is None:
            raise HTTPException(404, "Garment not found.")
        await reconstruction.stop_item(item_id)
        return store.item(item_id)

    @app.patch("/api/items/{item_id}")
    def edit_item(item_id: str, body: ItemPatch):
        item = store.item(item_id)
        if not item:
            raise HTTPException(404, "Garment not found.")
        changes = {}
        if body.record is not None:
            try:
                changes["record"] = Garment.model_validate({**item["record"], **body.record}).model_dump()
            except ValidationError:
                raise HTTPException(
                    422,
                    "Garment field format is invalid; please check category, condition, and confidence values.",
                ) from None
            changes["reviewed"] = False
        if body.reviewed is not None:
            changes["reviewed"] = body.reviewed
        if not changes:
            raise HTTPException(422, "No fields provided for update.")
        return store.update_item(item_id, **changes)

    @app.delete("/api/items/{item_id}", status_code=204)
    async def delete_item(item_id: str):
        with reconstruction.deletion(item_id=item_id), discovery.deletion(item_id=item_id):
            await asyncio.gather(reconstruction.stop_source_item(item_id), discovery.stop_item(item_id))
            if not store.delete_item(item_id):
                raise HTTPException(404, "Garment not found.")
        return Response(status_code=204)

    @app.get("/api/export")
    def export(format: Literal["json", "csv"] = "json", job_id: str | None = None):
        if job_id:
            get_job(job_id)
        entries = store.items(job_id)
        included = {item["id"] for item in entries}
        groups = [group for group in store.groups() if included.intersection(group["item_ids"])]
        membership = {ident: group for group in groups for ident in group["item_ids"]}
        headers = {"Content-Disposition": f'attachment; filename="rewear-catalogue.{format}"'}
        if format == "json":
            return Response(
                json.dumps(
                    {"exported_at": now(), "items": entries, "groups": groups}, ensure_ascii=False, indent=2
                ),
                media_type="application/json",
                headers=headers,
            )
        fields = [
            "id",
            "job_id",
            "category",
            "subtype",
            "brand",
            "size_label",
            "colors",
            "pattern",
            "material_guess",
            "condition",
            "defects",
            "confidence",
            "needs_review",
            "reviewed",
            "quality_status",
            "visibility",
            "quality_reasons",
            "provenance",
            "garment_group_id",
            "primary_item_id",
            "reconstruction_status",
            "reconstruction_url",
            "reconstruction_candidate_url",
            "reconstruction_quality",
            "reconstruction_issues",
            "reconstruction_image_quality",
            "reconstruction_image_issues",
            "reconstruction_candidate_quality",
            "reconstruction_candidate_issues",
            "reconstruction_preview_url",
            "reconstruction_preview_status",
            "reconstruction_preview_inferred_areas",
            "reconstruction_preview_warnings",
            "reconstruction_preview_candidates",
            "reconstruction_selected_candidate",
            "reconstruction_is_synthetic",
            "reconstruction_note",
            "discovery_status",
            "brand_candidate",
            "brand_evidence_status",
            "brand_evidence",
            "brand_confidence",
            "product_code",
            "shopping_status",
            "shopping_market",
            "shopping_checked_at",
            "shopping_verification_version",
            "shopping_urls",
            "shopping_exclusions",
        ]
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for item in entries:
            row = {**item, **item["record"]}
            quality = item.get("quality") or {}
            generated = item.get("reconstruction") or {}
            preview_ready = bool(generated.get("preview_url")) and is_preview_ready(
                generated.get("preview_validation")
            )
            preview = (
                (generated.get("preview_validation") or {}).get("preview") or {} if preview_ready else {}
            )
            reports = {}
            for key in ("validation", "image_validation", "candidate_validation"):
                try:
                    reports[key] = validate_report(generated.get(key))
                except (ValueError, TypeError):
                    raw = generated.get(key)
                    # Manager error reports retain useful review reasons without
                    # establishing any accepted image or complete audit.
                    reports[key] = {
                        "status": raw["status"]
                        if isinstance(raw, dict) and raw.get("status") in {"needs_review", "rejected"}
                        else "unverified",
                        "issues": [text for text in raw.get("issues", []) if isinstance(text, str)]
                        if isinstance(raw, dict) and isinstance(raw.get("issues"), list)
                        else [],
                    }
            discovered = item.get("discovery") or {}
            found = discovered.get("result") or {}
            identity = found.get("identity") or {}
            shopping = found.get("shopping") or {}
            group = membership.get(item["id"], {})
            row.update(
                quality_status=result_status(item),
                visibility=quality.get("visibility"),
                quality_reasons=quality.get("reasons", []),
                garment_group_id=group.get("id"),
                primary_item_id=group.get("primary_item_id"),
                reconstruction_status=generated.get("status"),
                reconstruction_url=generated.get("image_url"),
                reconstruction_candidate_url=generated.get("candidate_url"),
                reconstruction_quality=reports["validation"]["status"],
                reconstruction_issues=reports["validation"]["issues"],
                reconstruction_image_quality=reports["image_validation"]["status"]
                if generated.get("image_url")
                else None,
                reconstruction_image_issues=reports["image_validation"]["issues"]
                if generated.get("image_url")
                else [],
                reconstruction_candidate_quality=reports["candidate_validation"]["status"]
                if generated.get("candidate_url")
                else None,
                reconstruction_candidate_issues=reports["candidate_validation"]["issues"]
                if generated.get("candidate_url")
                else [],
                reconstruction_is_synthetic=generated.get("is_synthetic"),
                reconstruction_preview_url=generated.get("preview_url") if preview_ready else None,
                reconstruction_preview_status=preview.get("status"),
                reconstruction_preview_inferred_areas=[
                    f"{area['area']}: {area['detail']}" for area in preview.get("inferred_areas", [])
                ],
                reconstruction_preview_warnings=preview.get("mismatch_warnings", []),
                reconstruction_preview_candidates=[
                    candidate.get("variant_url")
                    for candidate in generated.get("preview_candidates", [])
                    if isinstance(candidate, dict) and isinstance(candidate.get("variant_url"), str)
                ],
                reconstruction_selected_candidate=generated.get("selected_candidate_id"),
                reconstruction_note=generated.get("note"),
                discovery_status=discovered.get("status"),
                brand_candidate=identity.get("brand"),
                brand_evidence_status=identity.get("status"),
                brand_evidence=identity.get("evidence"),
                brand_confidence=identity.get("confidence"),
                product_code=identity.get("product_code"),
                shopping_status=shopping.get("status"),
                shopping_market=found.get("market"),
                shopping_checked_at=shopping.get("searched_at"),
                shopping_verification_version=shopping.get("version", 1) if shopping else None,
                shopping_urls=[
                    offer["url"]
                    for offer in shopping.get("offers", [])
                    if shopping.get("version") == 2
                    and (offer.get("verification") or {}).get("status") == "verified"
                ],
                shopping_exclusions=[
                    candidate["title"] + ": " + " / ".join(candidate.get("reasons") or [])
                    for candidate in shopping.get("checked_candidates", [])
                    if candidate.get("status") != "accepted"
                ],
            )
            safe = {}
            for field in fields:
                value = row.get(field)
                if isinstance(value, list):
                    value = "; ".join(map(str, value))
                text = "" if value is None else str(value)
                if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
                    text = "'" + text
                safe[field] = text
            writer.writerow(safe)
        return Response(
            output.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8", headers=headers
        )

    @app.get("/media/{job_id}/{filename:path}")
    def file(job_id: str, filename: str):
        get_job(job_id)
        jobs = settings.data_dir / "jobs"
        raw_directory = jobs / job_id
        if jobs.is_symlink() or raw_directory.is_symlink():
            raise HTTPException(404, "Media file not found.")
        directory = raw_directory.resolve()
        path = (directory / filename).resolve()
        if (
            not directory.is_relative_to(jobs.resolve())
            or not path.is_relative_to(directory)
            or not path.is_file()
            or path.suffix.lower() not in IMAGE_SUFFIXES | VIDEO_SUFFIXES
        ):
            raise HTTPException(404, "Media file not found.")
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(PACKAGE / "static" / "index.html")

    app.mount("/static", StaticFiles(directory=PACKAGE / "static", check_dir=False), name="static")
    return app


def __getattr__(name):
    # Importing the factory for a hosted tenant must not open the old local DB.
    # Uvicorn's existing "wardrobe.app:app" entry point still constructs once.
    if name == "app":
        default_app = create_app()
        globals()["app"] = default_app
        return default_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
