from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

from pydantic import ValidationError

from . import media
from .config import Settings
from .duplicates import visual_signature
from .model_usage import begin_usage_scope, end_usage_scope
from .outfit import segment_outfit
from .providers import ProviderError, Providers
from .quality import QUALITY_VERSION, apply_inspection, unassessed_quality
from .schemas import unknown_garment
from .store import ACTIVE, Store

log = logging.getLogger(__name__)


def media_url(job_id: str, path: Path, directory: Path) -> str:
    return f"/media/{job_id}/{path.relative_to(directory).as_posix()}"


async def media_call(fn, *args, **kwargs):
    # Wait for a bounded media process to stop writing before declaring cancellation.
    task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception:
            pass
        raise


class JobManager:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        providers: Providers,
        reconstruction=None,
        discovery=None,
        *,
        work_budget=None,
        storage_budget=None,
    ):
        self.settings, self.store, self.providers = settings, store, providers
        self.reconstruction = reconstruction
        self.discovery = discovery
        self.tasks: dict[str, asyncio.Task] = {}
        self.work_budget = work_budget
        self.storage_budget = storage_budget
        self.semaphore = work_budget.semaphore("analysis") if work_budget else asyncio.Semaphore(1)

    def available(self) -> bool:
        return sum(job["status"] in ACTIVE for job in self.store.jobs()) < self.settings.max_queue and (
            self.work_budget is None or self.work_budget.has_capacity()
        )

    def enqueue(self, job_id: str):
        job = self.store.job(job_id)
        if not job or job["status"] != "queued" or job_id in self.tasks:
            return
        create_task = self.work_budget.create_task if self.work_budget else asyncio.create_task
        task = create_task(self._run(job_id), name=f"wardrobe-{job_id}")
        if self.storage_budget is not None:
            self.storage_budget.track(task)
        self.tasks[job_id] = task

        def finished(done):
            self.tasks.pop(job_id, None)
            if done.cancelled() and self.store.job(job_id)["status"] in ACTIVE:
                self.store.update_job(job_id, status="cancelled", stage="Cancelled")

        task.add_done_callback(finished)

    def cancel(self, job_id: str):
        self.cancel_reconstructions(job_id)
        task = self.tasks.get(job_id)
        if task:
            self.store.update_job(job_id, stage="Cancelling analysis")
            if not task.cancelling():
                task.cancel()
        else:
            self.store.update_job(job_id, status="cancelled", stage="Cancelled")

    def cancel_reconstructions(self, job_id: str):
        """Cancel supplemental item work, including on completed analysis jobs."""
        if self.reconstruction is not None:
            self.reconstruction.cancel_job(job_id)
        if self.discovery is not None:
            self.discovery.cancel_job(job_id)

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, job_id: str):
        usage_token = begin_usage_scope(entity_id=job_id, job_id=job_id, feature="analysis")
        try:
            async with self.semaphore:
                job = self.store.job(job_id)
                if not job or job["status"] not in ACTIVE:
                    return
                async with asyncio.timeout(self.settings.job_timeout):
                    self.store.update_job(job_id, status="running", stage="Preparing image", progress=2)
                    await self._analyse(job_id)
        except asyncio.CancelledError:
            self.store.update_job(job_id, status="cancelled", stage="Cancelled; completed items preserved")
        except TimeoutError:
            self.store.update_job(
                job_id,
                status="partial" if self.store.items(job_id) else "failed",
                stage="Analysis timed out",
                error="Analysis exceeded the time limit; completed items have been preserved. Please use a shorter video or retry.",
            )
        except (ProviderError, media.MediaError) as exc:
            self.store.update_job(
                job_id,
                status="partial" if self.store.items(job_id) else "failed",
                stage="Analysis incomplete",
                error=str(exc),
            )
        except Exception as exc:
            log.error("Job %s failed: %s", job_id, type(exc).__name__)
            self.store.update_job(
                job_id,
                status="partial" if self.store.items(job_id) else "failed",
                stage="Analysis incomplete",
                error="An error occurred while processing the image; completed items have been preserved. Please retry.",
            )
        finally:
            end_usage_scope(usage_token)

    async def _analyse(self, job_id: str):
        job = self.store.job(job_id)
        directory = self.settings.data_dir / "jobs" / job_id
        warnings: list[str] = []
        partial = False
        if job["media_type"] == "generated" and not job["input_name"]:
            source = directory / "generated.png"
            self.store.update_job(job_id, stage="Generating garment image", progress=5)
            await self.providers.generate(job["options"]["prompt"], source)
            self.store.update_job(
                job_id, input_name=source.name, source_url=media_url(job_id, source, directory)
            )
        else:
            source = directory / job["input_name"]

        preview = directory / "preview.jpg"
        is_video = job["media_type"] == "video"
        wearing = job["mode"] == "outfit"
        person_box = job.get("options", {}).get("person_box")
        if is_video and wearing:
            raise media.MediaError(
                "Lifestyle outfit mode currently supports photos only; please use a photo instead."
            )
        if person_box is not None and (not wearing or is_video):
            raise media.MediaError("Person selection can only be used with lifestyle outfit photos.")
        metadata = None
        if is_video:
            prepared = directory / "clip.mp4"
            metadata = await media_call(
                media.prepare_video, source, prepared, self.settings.max_video_seconds
            )
            await media_call(media.video_frame, prepared, preview, 0, metadata["fps"])
        else:
            prepared = preview
            await media_call(media.prepare_image, source, preview)
        self.store.update_job(job_id, preview_url=media_url(job_id, preview, directory), progress=15)

        selection = None
        if person_box is not None:
            prepared = directory / "selected.jpg"
            selection = await media_call(media.crop_selection, preview, person_box, prepared)
            self.store.update_job(
                job_id,
                selection_url=media_url(job_id, prepared, directory),
                selection_bbox=selection["bbox"],
            )

        records = []
        outfit_plan = None
        if is_video or job["mode"] in ("rack", "outfit"):
            self.store.update_job(job_id, stage="Identifying and segmenting garments", progress=22)
            try:
                if (
                    wearing
                    and job["concept"].strip().lower() == "garment"
                    and callable(getattr(self.providers, "_vision_request", None))
                ):
                    self.store.update_job(job_id, stage="Locating each garment in the photo", progress=22)
                    try:
                        localized = await segment_outfit(
                            self.providers,
                            prepared,
                            directory / "outfit",
                            provider=job["provider"],
                            max_targets=self.settings.max_items,
                            media_runner=media_call,
                        )
                    except ProviderError as exc:
                        warnings.append(str(exc))
                        partial = True
                        records = await self.providers.segment(prepared, job["concept"], video=False)
                    else:
                        records = localized.records
                        warnings.extend(localized.warnings)
                        partial = localized.partial
                    outfit_plan = "outfit/outfit-plan.json"
                else:
                    records = await self.providers.segment(prepared, job["concept"], video=is_video)
            except ProviderError as exc:
                records = getattr(exc, "records", None) or []
                if not records:
                    raise
                warnings.append(str(exc))
                partial = True
            (directory / "segments.json").write_text(
                json.dumps(
                    {
                        "model": "sam-3.1",
                        "concept": job["concept"],
                        "source_url": media_url(job_id, prepared, directory),
                        "selection": selection,
                        "outfit_plan": outfit_plan,
                        "record_count": len(records),
                        "records": records,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            self.store.update_job(job_id, progress=40)
            if is_video:
                candidates = await self._video_crops(job_id, prepared, records, directory, metadata)
            else:
                candidates = await media_call(media.crop_records, prepared, records, directory / "crops")
                for candidate in candidates:
                    candidate["source_path"] = prepared
                overlay = directory / "overlay.jpg"
                await media_call(media.render_overlay, prepared, records, overlay)
                self.store.update_job(job_id, overlay_url=media_url(job_id, overlay, directory))
            if not candidates:
                warnings.append(
                    "No garments matching the prompt were found. Try a clearer image or adjust the segmentation prompt."
                )
        else:
            crop = directory / "single.jpg"
            shutil.copy2(preview, crop)
            candidates = [
                {"path": crop, "source_path": preview, "object_id": "1", "frame": None, "bbox": None}
            ]

        if len(candidates) > self.settings.max_items:
            warnings.append(
                f"A maximum of {self.settings.max_items} garments can be analyzed at once; please upload the rest in separate batches."
            )
            candidates = candidates[: self.settings.max_items]
            partial = True
        failures = 0
        retakes = 0
        confirmations = 0
        context_reviews = 0
        for index, candidate in enumerate(candidates):
            self.store.update_job(
                job_id,
                stage=f"Processing garment {index + 1} / {len(candidates)}",
                progress=45 + int(42 * index / max(1, len(candidates))),
                warnings=warnings,
            )
            error = None
            try:
                inspect_options = {"context_path": candidate.get("context_path")}
                if wearing:
                    inspect_options["wearing"] = True
                assessment = await self.providers.inspect(
                    candidate["path"], job["provider"], **inspect_options
                )
                record, model_record, quality = apply_inspection(
                    assessment, candidate.get("metrics"), wearing=wearing
                )
                if (
                    wearing
                    and context_reviews < 3
                    and candidate.get("context_path")
                    and candidate.get("bbox")
                    and quality["status"] == "needs_retake"
                    and assessment["target"] == "one_garment"
                    and assessment["visibility"] in ("partial", "fragment", "unclear")
                ):
                    # A tight crop can hide the waist/layers needed to recognise
                    # a garment. Reinspect once with additional ORIGINAL pixels,
                    # never with an inventory label or the previous model answer.
                    context_reviews += 1
                    expanded = candidate["path"].with_name(f"expanded-{candidate['path'].stem}.jpg")
                    review = {"initial": assessment, "expanded": None, "selected": "initial"}
                    try:
                        await media_call(
                            media.context_crop, prepared, candidate["bbox"], expanded, padding=1.0
                        )
                        review["context_url"] = media_url(job_id, expanded, directory)
                        second = await self.providers.inspect(
                            candidate["path"], job["provider"], context_path=expanded, wearing=True
                        )
                        review["expanded"] = second
                        second_result = apply_inspection(second, candidate.get("metrics"), wearing=True)
                        # Recovery is only into explicit human confirmation.
                        # Unknowns, fragments and a claimed complete silhouette
                        # cannot silently override the initial uncertainty.
                        if second_result[2]["status"] == "needs_confirmation":
                            record, model_record, quality = second_result
                            review["selected"] = "expanded"
                    except (ProviderError, ValidationError, ValueError, media.MediaError, OSError) as exc:
                        review["error"] = (
                            str(exc)
                            if isinstance(exc, ProviderError)
                            else "Expanded context review incomplete; keeping initial result."
                        )
                    quality["context_review"] = review
            except (ProviderError, ValidationError, ValueError) as exc:
                error = (
                    str(exc)
                    if isinstance(exc, ProviderError)
                    else "The model returned incomplete fields; please fill in manually or retry."
                )
                record = unknown_garment()
                model_record = record
                quality = unassessed_quality(candidate.get("metrics"), wearing=wearing)
                failures += 1
                partial = True
            if quality["status"] == "needs_retake":
                retakes += 1
            if quality["status"] == "needs_confirmation":
                confirmations += 1
            provenance = (
                f"Ollama / {self.settings.ollama_model}"
                if job["provider"] == "local"
                else "Meta / muse-spark-1.3"
            )
            if records:
                provenance += " · SAM 3.1"
            item = self.store.add_item(
                job_id,
                image_url=media_url(job_id, candidate.get("context_path", candidate["path"]), directory),
                cutout_url=media_url(job_id, candidate["path"], directory) if candidate.get("bbox") else None,
                source_url=media_url(job_id, candidate.get("source_path", preview), directory),
                original_url=media_url(
                    job_id, candidate.get("source_path", preview) if is_video else preview, directory
                ),
                frame=candidate.get("frame"),
                object_id=str(candidate.get("object_id", "")),
                bbox=candidate.get("bbox"),
                record=record,
                model_record=model_record,
                quality=quality,
                visual_signature=await media_call(visual_signature, candidate["path"]),
                provenance=provenance,
                error=error,
            )
            if wearing and job.get("options", {}).get("reconstruct", True) and self.reconstruction:
                self.reconstruction.enqueue_automatic(item)
            if wearing and job.get("options", {}).get("discover", False) and self.discovery:
                self.discovery.enqueue_automatic(item, job["options"].get("discovery_market", "TW"))
        if failures:
            warnings.append(
                f"{failures} garment(s) could not be fully extracted; images saved and marked for confirmation."
            )
        if retakes:
            warnings.append(
                f"{retakes} area(s) show only partial or unclear garments; marked for retake, not included in wearable items."
            )
        if confirmations:
            warnings.append(
                f"{confirmations} worn garment(s) pending manual confirmation; visible attributes only, not included in wearable items until confirmed."
            )

        if is_video and records:
            self.store.update_job(job_id, stage="Generating tracking preview", progress=90)
            overlay = directory / "overlay.mp4"
            try:
                await media_call(media.render_video_overlay, prepared, records, overlay, metadata)
                self.store.update_job(job_id, overlay_url=media_url(job_id, overlay, directory))
            except media.MediaError as exc:
                warnings.append(str(exc))
                partial = True
        self.store.update_job(
            job_id,
            status="partial" if partial else "completed",
            progress=100,
            quality_version=QUALITY_VERSION if candidates else None,
            stage="Partially complete; please review"
            if partial
            else (
                f"Analysis complete; {retakes} area(s) need retake"
                if retakes
                else ("Analysis complete" if candidates else "No garments matching the prompt were found")
            ),
            warnings=warnings,
        )

    async def _video_crops(self, job_id, prepared, records, directory, metadata):
        best = {}
        for record in records:
            oid, frame = record.get("object_id"), record.get("frame")
            if oid is None or not isinstance(frame, int) or frame < 0 or frame >= metadata["frames"]:
                continue
            if record.get("kind") == "box":
                area = max(0, record["right"] - record["left"]) * max(0, record["bottom"] - record["top"])
            else:
                area = 0
            if oid not in best or area > best[oid][0]:
                best[oid] = (area, frame)
        candidates = []
        # One representative frame per tracked object, never one item per video frame.
        for oid, (_, frame) in list(best.items())[: self.settings.max_items + 1]:
            image = directory / f"frame-{frame}.jpg"
            if not image.exists():
                await media_call(media.video_frame, prepared, image, frame, metadata["fps"])
            selected = [r for r in records if r.get("object_id") == oid and r.get("frame") == frame]
            crops = await media_call(media.crop_records, image, selected, directory / "crops", frame=frame)
            for crop in crops:
                crop["source_path"] = image
            candidates.extend(crops[:1])
        return candidates
