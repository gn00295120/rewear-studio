"""On-demand brand evidence and sourced shopping candidates, separate from records."""

from __future__ import annotations

import asyncio
import copy
import re
import shutil
from collections import Counter
from contextlib import contextmanager, nullcontext
from pathlib import Path
from urllib.parse import urlencode
from uuid import uuid4

from PIL import Image
from fastapi import HTTPException

from .identity_schemas import normalize_identity, normalize_shopping
from .model_usage import begin_usage_scope, end_usage_scope
from .providers import ProviderError
from .reconstruction import eligible
from .store import now


class DiscoveryError(ValueError):
    def __init__(self, status_code, message):
        self.status_code = status_code
        super().__init__(message)


def fallback_query(identity, record, market):
    # Stored brand fields and free-form search suggestions are not visual evidence.
    profile = identity.get("visual_profile") or {}
    parts = [
        profile.get("type") or record.get("subtype"),
        profile.get("primary_color"),
        profile.get("pattern"),
        profile.get("sleeve_length"),
    ]
    if identity.get("status") == "observed":
        parts.insert(0, identity.get("brand"))
    if identity.get("status") == "observed":
        parts.insert(1, identity.get("product_code"))
    query = " ".join(str(part).strip() for part in parts if part)[:300] or "garment"
    return query + (" Taiwan buy" if market == "TW" else " clothing shop")


def query_links(query, market):
    params = {"q": query, "hl": "zh-TW"}
    if market == "TW":
        params["gl"] = "tw"
    shopping = {**params, "tbm": "shop"}
    bing = {"q": query}
    if market == "TW":
        bing.update({"cc": "tw", "setlang": "zh-hant"})
    return [
        {
            "label": "Google Shopping",
            "url": "https://www.google.com/search?" + urlencode(shopping),
        },
        {
            "label": "Bing Shopping",
            "url": "https://www.bing.com/shop?" + urlencode(bing),
        },
        {
            "label": "Google Web Search",
            "url": "https://www.google.com/search?" + urlencode(params),
        },
    ]


class DiscoveryManager:
    def __init__(self, settings, store, providers, *, work_budget=None, storage_budget=None):
        self.settings, self.store, self.providers = settings, store, providers
        self.tasks: dict[str, asyncio.Task] = {}
        self.work_budget = work_budget
        self.storage_budget = storage_budget
        self.semaphore = work_budget.semaphore("discovery") if work_budget else asyncio.Semaphore(1)
        self.deleting_items, self.deleting_jobs = Counter(), Counter()

    @property
    def available(self):
        return bool(self.settings.api_key) and all(
            callable(getattr(self.providers, name, None))
            for name in ("identify_brand", "search_shopping", "verify_shopping")
        )

    @contextmanager
    def deletion(self, *, item_id=None, job_id=None):
        guards, ident = (self.deleting_items, item_id) if item_id else (self.deleting_jobs, job_id)
        guards[ident] += 1
        try:
            yield
        finally:
            guards[ident] -= 1
            if not guards[ident]:
                del guards[ident]

    def _references(self, item):
        job = self.store.job(item["job_id"])
        if not job:
            raise DiscoveryError(409, "Original analysis not found. Please re-upload.")
        if job.get("media_type") == "generated" or item.get("provenance") == "generated":
            raise DiscoveryError(
                422,
                "AI-generated images cannot serve as brand evidence. Please upload the original garment photo.",
            )
        jobs = self.settings.data_dir / "jobs"
        directory = jobs / item["job_id"]
        if jobs.is_symlink() or directory.is_symlink() or not directory.is_dir():
            raise DiscoveryError(409, "Original garment photo not found. Please re-upload.")
        prefix = f"/media/{item['job_id']}/"

        def resolve(url):
            if not isinstance(url, str) or not url.startswith(prefix):
                raise DiscoveryError(409, "Original image path is invalid. Please re-upload.")
            relative = url[len(prefix) :]
            path = (directory / relative).resolve()
            if (
                not path.is_relative_to(directory.resolve())
                or not path.is_file()
                or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}
                or {"reconstructions", "shopping"}.intersection(Path(relative).parts)
                or {"reconstructions", "shopping"}.intersection(path.relative_to(directory.resolve()).parts)
            ):
                raise DiscoveryError(
                    409, "Search must use the original photo, not reconstructions or other sources."
                )
            return path

        first_url = item.get("cutout_url") or item.get("image_url")
        second_url = item.get("image_url") or item.get("source_url")
        first, second = resolve(first_url), resolve(second_url) if second_url else None
        return first, second if second != first else None

    def enqueue(self, item_id, market="TW"):
        if market not in {"TW", "global"}:
            raise DiscoveryError(422, "Please select Taiwan or Global region.")
        item = self.store.item(item_id)
        if not item:
            raise DiscoveryError(404, "Garment not found.")
        if self.deleting_items[item_id] or self.deleting_jobs[item["job_id"]]:
            raise DiscoveryError(409, "This garment is being deleted. Cannot start search.")
        previous = item.get("discovery") or {}
        task = self.tasks.get(item_id)
        if task is not None and not task.done():
            if previous.get("market") != market:
                raise DiscoveryError(
                    409, "A search for another region is in progress. Cancel or wait for it to finish."
                )
            return item
        if not self.available:
            raise DiscoveryError(503, "Brand & shopping search requires a configured Meta model.")
        if not eligible(item):
            raise DiscoveryError(
                422,
                "Please confirm garment category from the original photo and fill in garment details first.",
            )
        if sum(not task.done() for task in self.tasks.values()) >= self.settings.max_queue or (
            self.work_budget is not None and not self.work_budget.has_capacity()
        ):
            raise DiscoveryError(429, "Search queue is full. Please wait for current searches to finish.")
        self._references(item)
        try:
            with (
                self.storage_budget.reserve(kind="discovery")
                if self.storage_budget is not None
                else nullcontext()
            ):
                state = {
                    "id": uuid4().hex,
                    "status": "queued",
                    "stage": "queued",
                    "market": market,
                    "error": None,
                    "result": previous.get("result"),
                    "model": "muse-spark-1.3",
                    "created_at": now(),
                    "updated_at": now(),
                    "reference_record": copy.deepcopy(item.get("record") or {}),
                }
                item = self.store.update_discovery(item_id, state)
                create_task = self.work_budget.create_task if self.work_budget else asyncio.create_task
                task = create_task(self._run(item, state["id"]), name=f"discover-{item_id}")
                self.tasks[item_id] = task
                if self.storage_budget is not None:
                    self.storage_budget.track(task)
        except HTTPException as error:
            raise DiscoveryError(error.status_code, error.detail) from None

        def finished(done):
            if self.tasks.get(item_id) is done:
                self.tasks.pop(item_id, None)
            if done.cancelled():
                self.store.update_discovery(
                    item_id,
                    {"status": "cancelled", "stage": "cancelled", "error": None},
                    expected_id=state["id"],
                    active_only=True,
                )

        task.add_done_callback(finished)
        return item

    def enqueue_automatic(self, item, market="TW"):
        """Start once for a new eligible original item; never silently retry a billable job."""
        current = self.store.item(item["id"])
        if not current or current.get("discovery") or not self.available or not eligible(current):
            return
        job = self.store.job(current["job_id"])
        if not job or job.get("media_type") == "generated" or current.get("provenance") == "generated":
            return
        try:
            return self.enqueue(current["id"], market=market)
        except DiscoveryError as error:
            state = {
                "id": uuid4().hex,
                "status": "failed",
                "stage": "failed",
                "market": market,
                "error": str(error),
                "result": None,
                "model": "muse-spark-1.3",
                "created_at": now(),
                "updated_at": now(),
                "reference_record": copy.deepcopy(current.get("record") or {}),
            }
            return self.store.update_discovery(current["id"], state)

    def cancel(self, item_id):
        task = self.tasks.get(item_id)
        if task is not None and not task.done() and not task.cancelling():
            task.cancel()

    def cancel_job(self, job_id):
        for item in self.store.items(job_id):
            self.cancel(item["id"])

    def active_for_job(self, job_id):
        return any(
            item["id"] in self.tasks and not self.tasks[item["id"]].done()
            for item in self.store.items(job_id)
        )

    async def stop_item(self, item_id):
        task = self.tasks.get(item_id)
        self.cancel(item_id)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def stop_job(self, job_id):
        await asyncio.gather(*(self.stop_item(item["id"]) for item in self.store.items(job_id)))

    async def close(self):
        await asyncio.gather(*(self.stop_item(ident) for ident in list(self.tasks)))

    def _result(self, item, identity, shopping):
        market = item["discovery"]["market"]
        return {
            "identity": identity,
            "shopping": shopping,
            "market": market,
            "identified_at": now(),
            "reference_record": copy.deepcopy(item["record"]),
            "search_links": query_links(shopping["query"], market),
            "source_urls": list(
                dict.fromkeys(
                    url
                    for url in (
                        item.get("cutout_url") or item.get("image_url"),
                        item.get("image_url"),
                    )
                    if url
                )
            ),
        }

    def _prepare_output(self, item, attempt_id):
        jobs = self.settings.data_dir / "jobs"
        directory = jobs / item["job_id"]
        parent = directory / "shopping"
        if (
            not re.fullmatch(r"[0-9a-f]{32}", attempt_id)
            or jobs.is_symlink()
            or directory.is_symlink()
            or not directory.is_dir()
            or parent.is_symlink()
        ):
            raise DiscoveryError(409, "Product image storage path is invalid. Please search again.")
        parent.mkdir(exist_ok=True)
        output = parent / attempt_id
        output.mkdir(exist_ok=False)
        return output

    def _attach_media(self, shopping, item, attempt_id, output):
        """Only publish validated local images belonging to this exact attempt."""
        retained = set()
        for offer in shopping["offers"]:
            verification = offer.get("verification") or {}
            filename = verification.get("image_url", "")
            if not re.fullmatch(r"[1-3]\.(?:jpg|png)", filename):
                raise DiscoveryError(502, "Product image failed validation. Please search again.")
            path = output / filename
            if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 10 * 1024 * 1024:
                raise DiscoveryError(502, "Product image failed validation. Please search again.")
            with Image.open(path) as picture:
                if picture.format not in {"JPEG", "PNG"} or picture.width * picture.height > 24_000_000:
                    raise DiscoveryError(502, "Product image format or size does not meet requirements.")
                picture.verify()
            retained.add(filename)
            verification["image_url"] = f"/media/{item['job_id']}/shopping/{attempt_id}/{filename}"
        for path in output.iterdir():
            if path.name not in retained:
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)
        return bool(retained)

    async def _run(self, item, attempt_id):
        identity = None
        output, committed = None, False
        item_id, market = item["id"], item["discovery"]["market"]
        usage_token = begin_usage_scope(entity_id=item_id, job_id=item["job_id"], feature="shopping")

        def update(**changes):
            return self.store.update_discovery(item_id, changes, expected_id=attempt_id)

        try:
            async with self.semaphore:
                current = self.store.item(item_id)
                if not current or (current.get("discovery") or {}).get("id") != attempt_id:
                    return
                first, second = self._references(current)
                if not update(status="running", stage="identifying"):
                    return
                async with asyncio.timeout(self.settings.request_timeout * 3 + 20):
                    identity = normalize_identity(
                        await self.providers.identify_brand(first, second, copy.deepcopy(item["record"]))
                    )
                    if not update(stage="searching"):
                        return
                    search_options = {"market": market}
                    if self.settings.image_search_enabled:
                        search_options["image_path"] = first
                    shopping = normalize_shopping(
                        await self.providers.search_shopping(
                            copy.deepcopy(identity), copy.deepcopy(item["record"]), **search_options
                        ),
                        identity,
                    )
                    if not update(stage="verifying"):
                        return
                    output = self._prepare_output(item, attempt_id)
                    shopping = normalize_shopping(
                        await self.providers.verify_shopping(
                            copy.deepcopy(identity),
                            copy.deepcopy(item["record"]),
                            shopping,
                            first,
                            second,
                            output,
                            market=market,
                        ),
                        identity,
                    )
                    if shopping.get("version") != 2:
                        raise DiscoveryError(502, "Product verification incomplete. Please search again.")
                    has_media = self._attach_media(shopping, item, attempt_id, output)
                status = (
                    "partial"
                    if shopping["status"] == "unavailable"
                    or any(attempt.get("error") for attempt in shopping.get("search_attempts", []))
                    else "completed"
                )
                saved = update(
                    status=status, stage=status, error=None, result=self._result(item, identity, shopping)
                )
                committed = saved is not None and has_media
        except asyncio.CancelledError:
            update(status="cancelled", stage="cancelled", error=None)
            raise
        except Exception as error:
            if isinstance(error, (ProviderError, DiscoveryError)):
                message = str(error)
            elif isinstance(error, TimeoutError):
                message = "Brand & shopping search timed out. Please try again later."
            else:
                message = "Search could not be completed. Original data preserved — please try again later."
            if identity is None:
                update(status="failed", stage="failed", error=message)
            else:
                shopping = {
                    "version": 2,
                    "status": "unavailable",
                    "query": fallback_query(identity, item["record"], market),
                    "offers": [],
                    "checked_candidates": [],
                    "sources": [],
                    "searched_at": now(),
                    "notice": "Brand clues saved. Could not retrieve online shopping candidates — use the search links below.",
                }
                update(
                    status="partial",
                    stage="partial",
                    error=message,
                    result=self._result(item, identity, shopping),
                )
        finally:
            if output is not None and not committed:
                shutil.rmtree(output, ignore_errors=True)
            end_usage_scope(usage_token)
