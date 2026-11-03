"""Generate synthetic display images without changing observed garment evidence."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections import Counter
from contextlib import contextmanager, nullcontext
from pathlib import Path
from uuid import uuid4

from PIL import Image
from fastapi import HTTPException
from pydantic import ValidationError

from .providers import ProviderError
from .model_usage import begin_usage_scope, end_usage_scope
from .reconstruction_sources import ReconstructionSourceBundle, ReferenceManifest
from .reconstruction_quality import (
    CHECK_NAMES,
    is_accepted,
    is_preview_ready,
    validate_planning_diagnostics,
    validate_report,
)
from .store import now

NOTE = "AI 完整預覽；遮住的部分與未知衣長由 AI 推測，不代表同件款式已核對。原圖、款式差異與辨識資料另行保留。"

QUALITY_CHECKS = CHECK_NAMES


def accepted_validation(report: dict | None) -> bool:
    """A saved PNG or a provider's summary alone is not a fidelity pass."""
    return is_accepted(report)


class ReconstructionError(ValueError):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


class ReferencesChanged(ReconstructionError):
    def __init__(self):
        super().__init__(
            409,
            "Reference photos or the same-garment merge have changed; please reselect photos and generate again.",
        )


def eligible(item: dict) -> bool:
    return (item.get("record") or {}).get("category") not in (None, "other") and (
        bool(item.get("reviewed"))
        or (item.get("quality") or {}).get("status") in {"ready", "needs_confirmation"}
    )


class ReconstructionManager:
    def __init__(self, settings, store, providers, *, work_budget=None, storage_budget=None):
        self.settings, self.store, self.providers = settings, store, providers
        self.tasks: dict[str, asyncio.Task] = {}
        self.work_budget = work_budget
        self.storage_budget = storage_budget
        self.semaphore = work_budget.semaphore("reconstruction") if work_budget else asyncio.Semaphore(1)
        self.deleting_items = Counter()
        self.deleting_jobs = Counter()
        self.changing_sources = Counter()
        self.source_dependencies: dict[str, ReconstructionSourceBundle] = {}

    @contextmanager
    def deletion(self, *, item_id=None, job_id=None):
        """Block new work throughout cancellation and storage deletion/rollback."""
        guards, ident = (self.deleting_items, item_id) if item_id else (self.deleting_jobs, job_id)
        deleting = {item_id} if item_id else {item["id"] for item in self.store.items(job_id)}
        affected = set().union(
            *(
                set(group["item_ids"])
                for group in self.store.groups()
                if deleting.intersection(group["item_ids"])
            )
        )
        guards[ident] += 1
        try:
            with self.membership_change(affected):
                yield
        finally:
            guards[ident] -= 1
            if not guards[ident]:
                del guards[ident]

    @property
    def available(self):
        return bool(self.settings.api_key) and callable(getattr(self.providers, "reconstruct", None))

    def _path(self, item, url):
        jobs = self.settings.data_dir / "jobs"
        directory = jobs / item["job_id"]
        prefix = f"/media/{item['job_id']}/"
        if (
            not isinstance(url, str)
            or not url.startswith(prefix)
            or jobs.is_symlink()
            or directory.is_symlink()
            or not directory.is_dir()
        ):
            raise ReconstructionError(409, "找不到這件衣物的原始參考圖片，請重新上傳。")
        relative = url[len(prefix) :]
        path = (directory / relative).resolve()
        if (
            not path.is_relative_to(directory.resolve())
            or not path.is_file()
            or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}
            or {"reconstructions", "shopping"}.intersection(Path(relative).parts)
            or {"reconstructions", "shopping"}.intersection(path.relative_to(directory.resolve()).parts)
        ):
            raise ReconstructionError(409, "原始參考圖片位置不正確或已不存在，請重新上傳。")
        return path

    def _references(self, item):
        cutout = self._path(item, item.get("cutout_url") or item.get("image_url"))
        context_url = item.get("image_url") or item.get("source_url")
        context = self._path(item, context_url) if context_url else None
        return cutout, context if context != cutout else None

    @contextmanager
    def membership_change(self, item_ids):
        """Prevent new multi-photo work while group changes await cancellation."""
        identities = set(item_ids)
        self.changing_sources.update(identities)
        try:
            yield
        finally:
            self.changing_sources.subtract(identities)
            for ident in identities:
                if not self.changing_sources[ident]:
                    del self.changing_sources[ident]

    @staticmethod
    def _file_hash(path):
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _source_bundle(self, item_id, reference_item_ids):
        if not reference_item_ids:
            return None
        try:
            snapshot = self.store.reconstruction_snapshot(item_id, reference_item_ids)
        except KeyError:
            raise ReconstructionError(404, "找不到參考照片，請重新選擇。") from None
        items, jobs, group = snapshot["items"], snapshot["jobs"], snapshot["group"]
        if not group or not set(reference_item_ids).issubset(group["item_ids"]):
            raise ReconstructionError(409, "請先核對並合併為同一件衣物，再選擇其他照片。")
        if any(self.changing_sources[ident] for ident in group["item_ids"]):
            raise ReferencesChanged()
        for entry in items.values():
            if self.deleting_items[entry["id"]] or self.deleting_jobs[entry["job_id"]]:
                raise ReconstructionError(409, "參考照片正在刪除，請重新選擇。")
            if jobs[entry["job_id"]].get("media_type") not in {"image", "video"}:
                raise ReconstructionError(422, "多張參考只能使用原始照片，不能使用 AI 生成圖片。")
            if not eligible(entry):
                raise ReconstructionError(422, "請先核對參考照片中的衣物種類與辨識結果。")
        item = items[item_id]
        cutout, context = self._references(item)
        paths = {"cutout": cutout}
        origins = {"cutout": (item, item.get("cutout_url") or item.get("image_url"))}
        if context:
            paths["context"] = context
            origins["context"] = (item, item.get("image_url") or item.get("source_url"))
        for number, ident in enumerate(reference_item_ids, 1):
            source = items[ident]
            url = source.get("image_url") or source.get("source_url")
            key = f"extra_{number}"
            paths[key] = self._path(source, url)
            origins[key] = (source, url)
        sources, hashes = [], set()
        for key, path in paths.items():
            source, url = origins[key]
            fingerprint = self._file_hash(path)
            if key.startswith("extra_") and fingerprint in hashes:
                raise ReconstructionError(422, "所選參考照片內容重複，請選擇另一張原照。")
            hashes.add(fingerprint)
            sources.append(
                {
                    "id": key,
                    "item_id": source["id"],
                    "job_id": source["job_id"],
                    "role": "target_locator" if key == "cutout" else "original_context",
                    "media_url": url,
                    "source_url": source.get("source_url"),
                    "original_url": source.get("original_url"),
                    "file_sha256": fingerprint,
                    "reference_sha256": None,
                }
            )
        membership = {key: group[key] for key in ("id", "primary_item_id", "item_ids")}
        manifest = ReferenceManifest.model_validate(
            {
                "version": 1,
                "target_item_id": item_id,
                "group_id": group["id"],
                "membership_sha256": hashlib.sha256(
                    json.dumps(membership, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                "primary_source": "context" if context else "cutout",
                "sources": sources,
            }
        )
        return ReconstructionSourceBundle(manifest=manifest, paths=paths)

    def _ensure_sources_current(self, bundle):
        if bundle is None:
            return
        try:
            reference_ids = [s.item_id for s in bundle.manifest.sources if s.id.startswith("extra_")]
            current = self._source_bundle(bundle.manifest.target_item_id, reference_ids)
            if current.manifest != bundle.manifest or current.paths != bundle.paths:
                raise ReferencesChanged()
        except (ReconstructionError, OSError, ValueError):
            raise ReferencesChanged() from None

    @staticmethod
    def _report_manifest(report, bundle):
        """Bind a multi-photo result to exactly the originals actually requested."""
        if bundle is None:
            return None
        value = ReferenceManifest.model_validate((report.get("plan") or {}).get("reference_manifest"))
        raw = value.model_dump()
        if any(source["reference_sha256"] is None for source in raw["sources"]):
            raise ValueError("Missing normalized reference hashes")
        for source in raw["sources"]:
            source["reference_sha256"] = None
        if raw != bundle.manifest.model_dump():
            raise ValueError("Result does not match the requested original-photo bundle")
        return value.model_dump()

    def _state(self, item, bundle=None, reference_item_ids=(), *, mode="preview"):
        previous = item.get("reconstruction") or {}
        verified_previous = accepted_validation(previous.get("image_validation"))
        preview_previous = is_preview_ready(previous.get("preview_validation"))
        candidate_url = previous.get("candidate_url")
        candidate_validation = previous.get("candidate_validation")
        if not candidate_url:
            if previous.get("preview_url") and not preview_previous:
                candidate_url, candidate_validation = (
                    previous["preview_url"],
                    previous.get("preview_validation"),
                )
            elif previous.get("image_url") and not verified_previous:
                candidate_url, candidate_validation = previous["image_url"], previous.get("image_validation")
        previous_candidates = previous.get("preview_candidates")
        if not isinstance(previous_candidates, list) or not 1 <= len(previous_candidates) <= 3:
            previous_candidates = []
        else:
            previous_candidates = copy.deepcopy(previous_candidates)
        previous_selection = previous.get("selected_candidate_id")
        if not any(entry.get("id") == previous_selection for entry in previous_candidates):
            previous_selection = previous_candidates[0].get("id") if previous_candidates else None
        return {
            "id": uuid4().hex,
            "status": "queued",
            "image_url": previous.get("image_url") if verified_previous else None,
            "image_validation": previous.get("image_validation") if verified_previous else None,
            "preview_url": previous.get("preview_url") if preview_previous else None,
            "preview_validation": previous.get("preview_validation") if preview_previous else None,
            "image_reference_manifest": previous.get("image_reference_manifest")
            if verified_previous
            else None,
            "preview_reference_manifest": previous.get("preview_reference_manifest")
            if preview_previous
            else None,
            # Older images remain inspectable, but were never fidelity checked.
            "candidate_url": candidate_url,
            "candidate_validation": candidate_validation,
            "candidate_reference_manifest": previous.get("candidate_reference_manifest")
            if previous.get("candidate_url")
            else ((candidate_validation or {}).get("plan") or {}).get("reference_manifest"),
            "validation": None,
            "mode": mode,
            "preview_candidates": previous_candidates,
            "selected_candidate_id": previous_selection,
            "preview_candidates_reference_manifest": previous.get("preview_candidates_reference_manifest"),
            "error": None,
            "planning_diagnostics": [],
            "model": "muse-image-1.0",
            "is_synthetic": True,
            "note": NOTE,
            "created_at": now(),
            "updated_at": now(),
            "reference_urls": [source.media_url for source in bundle.manifest.sources]
            if bundle
            else [item.get("cutout_url") or item.get("image_url"), item.get("image_url")],
            "reference_record": copy.deepcopy(item.get("record") or {}),
            "reference_item_ids": list(reference_item_ids),
            "reference_manifest": bundle.manifest.model_dump() if bundle else None,
        }

    def enqueue(self, item_id, reference_item_ids=None, *, mode="preview"):
        selected = list(reference_item_ids or [])
        if (
            mode not in {"preview", "verified"}
            or len(selected) > 2
            or any(not isinstance(ident, str) for ident in selected)
            or len(set(selected)) != len(selected)
            or item_id in selected
        ):
            raise ReconstructionError(422, "請選擇最多兩張不同的其他參考照片。")
        selected.sort()
        item = self.store.item(item_id)
        if not item:
            raise ReconstructionError(404, "找不到這件衣物。")
        if self.deleting_items[item_id] or self.deleting_jobs[item["job_id"]]:
            raise ReconstructionError(409, "這件衣物正在刪除，無法開始重建。")
        task = self.tasks.get(item_id)
        if task is not None and not task.done():
            active = item.get("reconstruction") or {}
            if selected != active.get("reference_item_ids", []) or mode != active.get("mode", "verified"):
                raise ReconstructionError(409, "正在使用另一組參考照片生成，請先取消或等待完成。")
            return item
        if not self.available:
            raise ReconstructionError(503, "完整衣物圖需要已設定的 Meta 圖片模型。")
        if not eligible(item):
            raise ReconstructionError(422, "目前無法確認衣物種類，請先核對原圖並填寫、確認衣物資料。")
        if sum(not task.done() for task in self.tasks.values()) >= self.settings.max_queue or (
            self.work_budget is not None and not self.work_budget.has_capacity()
        ):
            raise ReconstructionError(429, "衣物重建佇列已滿，請等待目前圖片完成後重試。")
        self._references(item)
        try:
            bundle = self._source_bundle(item_id, selected)
        except (ValidationError, OSError):
            raise ReconstructionError(409, "參考照片資料不完整或已變更，請重新選擇。") from None
        try:
            with (
                self.storage_budget.reserve(kind="reconstruction")
                if self.storage_budget is not None
                else nullcontext()
            ):
                state = self._state(item, bundle, selected, mode=mode)
                item = self.store.update_reconstruction(item_id, state)
                create_task = self.work_budget.create_task if self.work_budget else asyncio.create_task
                task = create_task(self._run(item, state["id"], bundle), name=f"reconstruct-{item_id}")
                self.tasks[item_id] = task
                if bundle is not None:
                    self.source_dependencies[item_id] = bundle
                if self.storage_budget is not None:
                    self.storage_budget.track(task)
        except HTTPException as error:
            raise ReconstructionError(error.status_code, error.detail) from None

        def finished(done):
            if self.tasks.get(item_id) is done:
                self.tasks.pop(item_id, None)
                self.source_dependencies.pop(item_id, None)
            if done.cancelled():
                self.store.update_reconstruction(
                    item_id,
                    {"status": "cancelled", "error": None},
                    expected_id=state["id"],
                    active_only=True,
                )

        task.add_done_callback(finished)
        return item

    def enqueue_automatic(self, item):
        if not self.available or not eligible(item):
            return
        try:
            self.enqueue(item["id"], mode="preview")
        except ReconstructionError as error:
            state = self._state(item)
            state.update(status="failed", error=str(error))
            self.store.update_reconstruction(item["id"], state)

    def select_candidate(self, item_id, candidate_id):
        item = self.store.item(item_id)
        if item is None:
            raise ReconstructionError(404, "找不到這件衣物。")
        state = item.get("reconstruction") or {}
        if state.get("status") in {"queued", "running"}:
            raise ReconstructionError(409, "完整預覽仍在生成，請完成後再選擇。")
        candidates = state.get("preview_candidates")
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 3:
            raise ReconstructionError(409, "目前沒有可選擇的完整預覽。")
        selected = next(
            (
                entry
                for entry in candidates
                if isinstance(entry, dict)
                and entry.get("id") == candidate_id
                and isinstance(entry.get("variant_url"), str)
            ),
            None,
        )
        if selected is None:
            raise ReconstructionError(422, "找不到這張完整預覽候選。")
        prefix = f"/media/{item['job_id']}/reconstructions/"
        filename = selected["variant_url"].removeprefix(prefix)
        directory = self.settings.data_dir / "jobs" / item["job_id"]
        output = directory / "reconstructions"
        path = (output / filename).resolve()
        if (
            not selected["variant_url"].startswith(prefix)
            or not filename
            or "/" in filename
            or not filename.endswith(".png")
            or output.is_symlink()
            or not path.is_relative_to(output.resolve())
            or not self._valid_candidate(path)
        ):
            raise ReconstructionError(409, "完整預覽圖片已不存在，請重新生成。")
        updated = self.store.update_reconstruction(
            item_id,
            {
                "selected_candidate_id": candidate_id,
                "candidate_url": selected["variant_url"],
                "error": None,
            },
            expected_id=state.get("id"),
        )
        if updated is None:
            raise ReconstructionError(409, "完整預覽狀態已變更，請重新選擇。")
        return updated

    def cancel(self, item_id):
        task = self.tasks.get(item_id)
        if task is not None and not task.done() and not task.cancelling():
            task.cancel()

    def cancel_job(self, job_id):
        for ident in self._tasks_for_job(job_id):
            self.cancel(ident)

    def _tasks_for_job(self, job_id):
        targets = {item["id"] for item in self.store.items(job_id)}
        targets.update(
            ident
            for ident, bundle in self.source_dependencies.items()
            if any(source.job_id == job_id for source in bundle.manifest.sources)
        )
        # Removing an unselected group member also changes the confirmed group
        # snapshot. Cancel its multi-photo work before changing membership.
        group_ids = {group["id"] for group in self.store.groups() if targets.intersection(group["item_ids"])}
        targets.update(
            ident
            for ident, bundle in self.source_dependencies.items()
            if bundle.manifest.group_id in group_ids
        )
        return targets

    def active_for_job(self, job_id):
        return any(
            ident in self.tasks and not self.tasks[ident].done() for ident in self._tasks_for_job(job_id)
        )

    async def stop_item(self, item_id):
        task = self.tasks.get(item_id)
        self.cancel(item_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def stop_job(self, job_id):
        await asyncio.gather(*(self.stop_item(ident) for ident in self._tasks_for_job(job_id)))

    async def stop_membership(self, item_ids, group_ids=()):
        identities, groups = set(item_ids), set(group_ids)
        targets = [
            ident
            for ident, bundle in self.source_dependencies.items()
            if bundle.manifest.group_id in groups
            or any(source.item_id in identities for source in bundle.manifest.sources)
        ]
        await asyncio.gather(*(self.stop_item(ident) for ident in targets))

    async def stop_source_item(self, item_id):
        groups = [group["id"] for group in self.store.groups() if item_id in group["item_ids"]]
        await asyncio.gather(self.stop_item(item_id), self.stop_membership([item_id], groups))

    async def close(self):
        await asyncio.gather(*(self.stop_item(ident) for ident in list(self.tasks)))

    @staticmethod
    def _valid_candidate(path):
        if path is None or path.is_symlink() or not path.is_file():
            return False
        try:
            if path.stat().st_size > 20 * 1024 * 1024:
                return False
            with Image.open(path) as image:
                if image.width * image.height > 24_000_000:
                    return False
                image.verify()
            return True
        except (OSError, ValueError, Image.DecompressionBombError):
            return False

    def _retain_unverified(self, item, attempt_id, destination, reason, diagnostics=None, bundle=None):
        """Keep a valid late candidate on audit errors, without certifying it."""
        if not self._valid_candidate(destination):
            return False
        try:
            self._ensure_sources_current(bundle)
        except ReferencesChanged:
            return False
        report = {
            "version": 1,
            "status": "needs_review",
            "source_view": "unclear",
            "generated": True,
            "checks": {},
            "issues": ["圖片已保留，但核對未完成：" + reason],
            "preview": None,
        }
        updated = self.store.update_reconstruction(
            item["id"],
            {
                "status": "needs_review",
                "validation": report,
                "candidate_url": f"/media/{item['job_id']}/reconstructions/{attempt_id}.png",
                "candidate_validation": report,
                "candidate_reference_manifest": bundle.manifest.model_dump() if bundle else None,
                "error": reason,
                "planning_diagnostics": diagnostics or [],
            },
            expected_id=attempt_id,
        )
        return updated is not None

    async def _run(self, item, attempt_id, bundle=None):
        destination = None
        artifacts = []
        committed = False
        preview_attempt = False
        item_id = item["id"]
        usage_token = begin_usage_scope(entity_id=item_id, job_id=item["job_id"], feature="reconstruction")
        try:
            async with self.semaphore:
                current = self.store.item(item_id)
                if not current or (current.get("reconstruction") or {}).get("id") != attempt_id:
                    return
                self._ensure_sources_current(bundle)
                cutout, context = self._references(item)
                directory = self.settings.data_dir / "jobs" / item["job_id"]
                output = directory / "reconstructions"
                if output.is_symlink() or not output.resolve().is_relative_to(directory.resolve()):
                    raise ReconstructionError(409, "衣物圖儲存位置不正確，請重新上傳。")
                output.mkdir(exist_ok=True)
                destination = output / f"{attempt_id}.png"
                artifacts = [destination]
                self.store.update_reconstruction(item_id, {"status": "running"}, expected_id=attempt_id)
                mode = (current.get("reconstruction") or {}).get("mode", "verified")
                if mode == "preview" and callable(getattr(self.providers, "reconstruct_preview", None)):
                    preview_attempt = True
                    artifacts = [output / f"{attempt_id}-preview-{number}.png" for number in (1, 2, 3)]
                    destination = artifacts[0]
                    async with asyncio.timeout(self.settings.reconstruction_timeout + 10):
                        result = await self.providers.reconstruct_preview(
                            cutout,
                            context,
                            copy.deepcopy(item["record"]),
                            artifacts,
                            **({"source_bundle": bundle} if bundle is not None else {}),
                        )
                    self._ensure_sources_current(bundle)
                    if not isinstance(result, dict) or result.get("candidate_count") != len(artifacts):
                        raise ProviderError("圖片模型未產生完整的三張衣物預覽。")
                    if not all(self._valid_candidate(path) for path in artifacts):
                        raise ProviderError("圖片模型未產生有效的衣物預覽。")
                    image_manifest = self._report_manifest(
                        {"plan": {"reference_manifest": result.get("reference_manifest")}}, bundle
                    )
                    candidates = [
                        {
                            "id": str(number),
                            "variant_url": (f"/media/{item['job_id']}/reconstructions/{path.name}"),
                        }
                        for number, path in enumerate(artifacts, 1)
                    ]
                    report = {
                        "version": 2,
                        "mode": "preview",
                        "status": "needs_review",
                        "generated": True,
                        "issues": [
                            "已產生三張低成本 AI 完整預覽；請逐張對照原圖後選擇。"
                            "這些預覽未執行付費的逐項款式核對。"
                        ],
                    }
                    changes = {
                        "status": "needs_review",
                        "validation": report,
                        "candidate_url": candidates[0]["variant_url"],
                        "candidate_validation": report,
                        "candidate_reference_manifest": image_manifest,
                        "preview_candidates": candidates,
                        "selected_candidate_id": candidates[0]["id"],
                        "preview_candidates_reference_manifest": image_manifest,
                        "error": None,
                    }
                    if image_manifest is not None:
                        changes["reference_manifest"] = image_manifest
                    updated = self.store.update_reconstruction(
                        item_id,
                        changes,
                        expected_id=attempt_id,
                    )
                    committed = updated is not None
                    return
                async with asyncio.timeout(self.settings.reconstruction_timeout + 10):
                    report = await self.providers.reconstruct(
                        cutout,
                        context,
                        copy.deepcopy(item["record"]),
                        destination,
                        **({"source_bundle": bundle} if bundle is not None else {}),
                    )
                self._ensure_sources_current(bundle)
                generated = destination.is_file()
                if generated and not self._valid_candidate(destination):
                    raise ProviderError("圖片模型未產生有效的衣物圖。")
                try:
                    report = validate_report(report)
                    image_manifest = self._report_manifest(report, bundle)
                except (ValidationError, ValueError, TypeError):
                    image_manifest = bundle.manifest.model_dump() if bundle else None
                    report = {
                        "version": 1,
                        "status": "needs_review",
                        "source_view": "unclear",
                        "generated": generated,
                        "checks": {},
                        "issues": ["這張圖尚未完成原圖款式比對，請核對後再使用。"],
                        "preview": None,
                    }
                report = copy.deepcopy(report)
                if report.get("generated") is True and not generated:
                    raise ProviderError("圖片模型未產生有效的衣物圖。")
                accepted = accepted_validation(report) and generated
                preview_ready = is_preview_ready(report) and generated
                # Fail closed when summary and individual checks disagree.
                if report.get("status") == "accepted" and not accepted:
                    report["status"] = "needs_review"
                    report.setdefault("issues", []).append("款式比對尚未全部通過，請核對原圖。")
                url = f"/media/{item['job_id']}/reconstructions/{attempt_id}.png" if generated else None
                changes = {
                    "status": "completed" if accepted else report["status"],
                    "validation": report,
                    "error": None,
                }
                if image_manifest is not None:
                    changes["reference_manifest"] = image_manifest
                if preview_ready:
                    changes.update(
                        preview_url=url,
                        preview_validation=report,
                        preview_reference_manifest=image_manifest,
                    )
                if accepted:
                    changes.update(
                        image_url=url,
                        image_validation=report,
                        image_reference_manifest=image_manifest,
                        candidate_url=None,
                        candidate_validation=None,
                        candidate_reference_manifest=None,
                    )
                elif generated:
                    changes["candidate_url"] = url
                    changes["candidate_validation"] = report
                    changes["candidate_reference_manifest"] = image_manifest
                updated = self.store.update_reconstruction(
                    item_id,
                    changes,
                    expected_id=attempt_id,
                )
                committed = updated is not None
        except asyncio.CancelledError:
            self.store.update_reconstruction(
                item_id, {"status": "cancelled", "error": None}, expected_id=attempt_id
            )
            raise
        except ReferencesChanged as error:
            self.store.update_reconstruction(
                item_id, {"status": "cancelled", "error": str(error)}, expected_id=attempt_id
            )
        except (ProviderError, ReconstructionError) as error:
            try:
                diagnostics = validate_planning_diagnostics(getattr(error, "diagnostics", []))
            except (ValidationError, ValueError, TypeError):
                diagnostics = []
            committed = (
                False
                if preview_attempt
                else self._retain_unverified(item, attempt_id, destination, str(error), diagnostics, bundle)
            )
            if committed:
                return
            self.store.update_reconstruction(
                item_id,
                {"status": "failed", "error": str(error), "planning_diagnostics": diagnostics},
                expected_id=attempt_id,
            )
        except TimeoutError:
            committed = (
                False
                if preview_attempt
                else self._retain_unverified(
                    item, attempt_id, destination, "完整衣物圖核對逾時。", bundle=bundle
                )
            )
            if committed:
                return
            self.store.update_reconstruction(
                item_id,
                {"status": "failed", "error": "完整衣物圖產生逾時，原始辨識已保留，請稍後重試。"},
                expected_id=attempt_id,
            )
        except Exception:
            committed = (
                False
                if preview_attempt
                else self._retain_unverified(
                    item, attempt_id, destination, "完整衣物圖核對未能完成。", bundle=bundle
                )
            )
            if committed:
                return
            self.store.update_reconstruction(
                item_id,
                {"status": "failed", "error": "完整衣物圖未能產生，原始辨識已保留，請重試。"},
                expected_id=attempt_id,
            )
        finally:
            if not committed:
                for artifact in artifacts:
                    artifact.unlink(missing_ok=True)
            end_usage_scope(usage_token)
