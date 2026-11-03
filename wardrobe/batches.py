"""Bounded batch upload, durable progress, cancellation and non-destructive retries."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import File, Form, HTTPException, UploadFile

from .pipeline import media_url
from .schemas import Mode, Provider, person_box
from .store import ACTIVE

IMAGES = {".jpg", ".jpeg", ".png", ".webp"}
VIDEOS = {".mp4", ".mov", ".webm", ".m4v"}


def register_batches(app, settings, store, manager, public_job, require_provider, *, storage_budget=None):
    def present(batch):
        return {
            key: [public_job(job) for job in value] if key == "jobs" else value
            for key, value in batch.items()
            if key not in {"request_hash", "client_id"}
        }

    def get(ident):
        batch = store.batch(ident)
        if not batch:
            raise HTTPException(404, "找不到這批分析。")
        return batch

    def capacity(count):
        active = sum(job["status"] in ACTIVE for job in store.jobs())
        if count + active > settings.max_queue or (
            manager.work_budget is not None and not manager.work_budget.has_capacity(count)
        ):
            raise HTTPException(429, "分析佇列已滿，請等待現有分析完成後，再重試這批上傳。")

    def enqueue(batch):
        for job in batch["jobs"]:
            manager.enqueue(job["id"])

    @app.get("/api/batches")
    def list_batches():
        return {"batches": [present(batch) for batch in store.batches()]}

    @app.get("/api/batches/{batch_id}")
    def batch_detail(batch_id: str):
        return present(get(batch_id))

    @app.post("/api/batches", status_code=202)
    async def upload_batch(
        files: Annotated[list[UploadFile], File()],
        provider: Annotated[Provider, Form()] = "local",
        mode: Annotated[Mode, Form()] = "rack",
        concept: Annotated[str, Form(min_length=1, max_length=100)] = "garment",
        selections: Annotated[str | None, Form(max_length=10000)] = None,
        client_id: Annotated[str | None, Form(min_length=1, max_length=100)] = None,
        reconstruct: Annotated[Literal["true", "false"], Form()] = "true",
        discover: Annotated[Literal["true", "false"], Form()] = "false",
        discovery_market: Annotated[Literal["TW", "global"], Form()] = "TW",
    ):
        reconstruct = reconstruct == "true"
        discover = discover == "true"
        directories, committed, lease = [], False, None
        try:
            if discover and mode != "outfit":
                raise HTTPException(422, "自動查找購買選項適用生活穿搭照片。")
            if not 1 <= len(files) <= settings.max_batch_files:
                raise HTTPException(422, f"每批請選擇 1 至 {settings.max_batch_files} 個檔案。")
            concept = concept.strip()
            if not concept:
                raise HTTPException(422, "請輸入分割提示。")
            try:
                raw = json.loads(selections) if selections else [None] * len(files)
                if not isinstance(raw, list) or len(raw) != len(files):
                    raise ValueError("人物範圍數量必須與檔案數一致。")
                boxes = [person_box(value) for value in raw]
                if mode != "outfit" and any(boxes):
                    raise ValueError("人物框選只適用生活穿搭照片。")
            except (ValueError, TypeError) as error:
                raise HTTPException(
                    422, str(error) if isinstance(error, ValueError) else "人物範圍格式不正確。"
                ) from None
            names = [Path((file.filename or "upload").replace("\\", "/")).name[:200] for file in files]
            fingerprint_options = [names, provider, mode, concept, boxes, reconstruct]
            # Keep old default-off replays compatible, while binding opt-in
            # discovery and its market to the durable request identity.
            if discover or discovery_market != "TW":
                fingerprint_options.extend([discover, discovery_market])
            fingerprint = hashlib.sha256(json.dumps(fingerprint_options, sort_keys=True).encode())
            total = 0
            # A replay key is bound to bytes and options, not merely filenames.
            for file in files:
                file_hash = hashlib.sha256()
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > settings.max_batch_upload_mb * 1024 * 1024:
                        raise HTTPException(413, "整批檔案超過上傳大小上限，請分批上傳。")
                    file_hash.update(chunk)
                fingerprint.update(file_hash.digest())
                await file.seek(0)
            request_hash = fingerprint.hexdigest()
            existing = store.batch_by_client(client_id) if client_id else None
            if existing:
                if existing["request_hash"] != request_hash:
                    raise HTTPException(409, "這個批次識別已用於不同檔案，請建立新批次。")
                return present(existing)
            if storage_budget is not None:
                lease = storage_budget.reserve(jobs=len(files), source_bytes=total)
            if (provider == "meta" or mode in {"rack", "outfit"}) and not settings.api_key:
                raise HTTPException(503, "此功能需要 Meta API 金鑰。請設定 MODEL_API_KEY 後重新啟動。")
            await require_provider(provider)
            jobs, errors = [], []
            for index, (file, filename, box) in enumerate(zip(files, names, boxes)):
                suffix = Path(filename).suffix.lower()
                video = suffix in VIDEOS
                error = None
                if suffix not in IMAGES | VIDEOS:
                    error = "不支援此檔案格式。請使用 JPG、PNG、WebP、MP4、MOV、WebM。"
                elif video and mode == "outfit":
                    error = "生活穿搭框選適用照片；影片請使用多件／影片模式。"
                elif video and not settings.api_key:
                    error = "影片分析需要 Meta API 金鑰。"
                elif video and not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
                    error = "影片分析需要 ffmpeg 與 ffprobe。"
                if error:
                    errors.append({"index": index, "filename": filename, "error": error})
                    continue
                job_id = uuid4().hex
                directory = settings.data_dir / "jobs" / job_id
                directory.mkdir()
                directories.append(directory)
                destination, size = directory / ("source" + suffix), 0
                with destination.open("wb") as output:
                    while chunk := await file.read(1024 * 1024):
                        size += len(chunk)
                        if size > settings.max_upload_mb * 1024 * 1024:
                            error = "檔案超過單檔上傳大小上限。"
                            break
                        output.write(chunk)
                if not size:
                    error = "檔案是空的。"
                if error:
                    shutil.rmtree(directory)
                    directories.remove(directory)
                    errors.append({"index": index, "filename": filename, "error": error})
                    continue
                jobs.append(
                    {
                        "id": job_id,
                        "filename": filename,
                        "media_type": "video" if video else "image",
                        "provider": provider,
                        "mode": mode,
                        "concept": concept,
                        "input_name": destination.name,
                        "source_url": media_url(job_id, destination, directory),
                        "batch_index": index,
                        "options": {
                            "reconstruct": reconstruct,
                            "discover": discover,
                            "discovery_market": discovery_market,
                            **({"person_box": box} if box else {}),
                        },
                    }
                )
            # No await between the capacity/idempotency recheck and the atomic commit.
            existing = store.batch_by_client(client_id) if client_id else None
            if existing:
                if existing["request_hash"] != request_hash:
                    raise HTTPException(409, "這個批次識別已用於不同檔案。")
                return present(existing)
            capacity(len(jobs))
            batch = store.create_batch(
                jobs,
                client_id=client_id,
                request_hash=request_hash,
                provider=provider,
                mode=mode,
                concept=concept,
                filename=f"{len(files)} 個檔案的生活紀錄",
                file_count=len(files),
                errors=errors,
            )
            committed = True
            enqueue(batch)
            return present(batch)
        finally:
            try:
                if not committed:
                    for directory in directories:
                        shutil.rmtree(directory, ignore_errors=True)
                for file in files:
                    await file.close()
            finally:
                if lease is not None:
                    lease.close()

    @app.post("/api/batches/{batch_id}/cancel")
    async def cancel_batch(batch_id: str):
        for job in get(batch_id)["jobs"]:
            if job["status"] in ACTIVE:
                manager.cancel(job["id"])
            else:
                manager.cancel_reconstructions(job["id"])
        await asyncio.sleep(0)
        return present(get(batch_id))

    @app.post("/api/batches/{batch_id}/retry", status_code=202)
    async def retry_batch(batch_id: str):
        old = get(batch_id)
        if any(job["status"] in ACTIVE for job in old["jobs"]):
            raise HTTPException(409, "請等待這批分析完成或取消後，再重試。")
        failed = [job for job in old["jobs"] if job["status"] in {"failed", "partial", "cancelled"}]
        if not failed:
            raise HTTPException(409, "這批沒有可重試的未完成分析；上傳前被拒絕的檔案請修正後重新選取。")
        capacity(len(failed))
        if (
            any(
                j["provider"] == "meta" or j["mode"] in {"rack", "outfit"} or j["media_type"] == "video"
                for j in failed
            )
            and not settings.api_key
        ):
            raise HTTPException(503, "重試這批分析需要 Meta API 金鑰。")
        if any(j["media_type"] == "video" for j in failed) and not (
            shutil.which("ffmpeg") and shutil.which("ffprobe")
        ):
            raise HTTPException(503, "影片分析需要 ffmpeg 與 ffprobe，請安裝後重試。")
        for provider in {job["provider"] for job in failed}:
            await require_provider(provider)
        capacity(len(failed))
        jobs, directories, committed = [], [], False
        lease = None
        try:
            if storage_budget is not None:
                source_bytes = 0
                for job in failed:
                    jobs_root = settings.data_dir / "jobs"
                    original = jobs_root / job["id"]
                    source = original / (job["input_name"] or "missing")
                    if (
                        jobs_root.is_symlink()
                        or original.is_symlink()
                        or not original.resolve().is_relative_to(jobs_root.resolve())
                        or not source.is_file()
                        or not source.resolve().is_relative_to(original.resolve())
                    ):
                        raise HTTPException(409, "部分來源照片已不存在，請重新上傳。")
                    source_bytes += source.stat().st_size
                lease = storage_budget.reserve(jobs=len(failed), source_bytes=source_bytes)
            for index, job in enumerate(failed):
                jobs_root = settings.data_dir / "jobs"
                original = jobs_root / job["id"]
                source = original / (job["input_name"] or "missing")
                if (
                    jobs_root.is_symlink()
                    or original.is_symlink()
                    or not original.resolve().is_relative_to(jobs_root.resolve())
                    or not source.is_file()
                    or not source.resolve().is_relative_to(original.resolve())
                ):
                    raise HTTPException(409, "部分來源照片已不存在，請重新上傳。")
                ident = uuid4().hex
                directory = settings.data_dir / "jobs" / ident
                directory.mkdir()
                directories.append(directory)
                destination = directory / source.name
                shutil.copy2(source, destination)
                jobs.append(
                    {
                        "id": ident,
                        **{
                            key: job[key]
                            for key in ("filename", "media_type", "provider", "mode", "concept", "options")
                        },
                        "input_name": destination.name,
                        "source_url": media_url(ident, destination, directory),
                        "retry_of": job["id"],
                        "batch_index": index,
                    }
                )
            batch = store.create_batch(
                jobs,
                provider=old["provider"],
                mode=old["mode"],
                concept=old["concept"],
                filename=f"重試 · {old['filename']}",
                retry_of=batch_id,
                file_count=len(jobs),
            )
            committed = True
            enqueue(batch)
            return present(batch)
        finally:
            try:
                if not committed:
                    for directory in directories:
                        shutil.rmtree(directory, ignore_errors=True)
            finally:
                if lease is not None:
                    lease.close()
