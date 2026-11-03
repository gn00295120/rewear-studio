"""Reassess untouched pre-quality results; stop Rewear before running this repair.

Never changes reviewed or previously edited results. A SQLite backup and an
item-level audit preserve old guesses. Original photos and cutouts stay intact.
Only visibility/attribute inspection is rerun; SAM is not called again.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from wardrobe import media
from wardrobe.config import Settings
from wardrobe.pipeline import media_url
from wardrobe.providers import ProviderError, Providers
from wardrobe.quality import apply_inspection, unassessed_quality
from wardrobe.schemas import unknown_garment
from wardrobe.store import ACTIVE, Store


def media_path(url: str, job_id: str, directory: Path) -> Path:
    prefix = f"/media/{job_id}/"
    if not isinstance(url, str) or not url.startswith(prefix):
        raise ValueError("Unexpected saved image URL")
    path = (directory / url[len(prefix) :]).resolve()
    if not path.is_relative_to(directory.resolve()) or not path.is_file():
        raise ValueError("Saved image is missing or outside its job")
    return path


async def reassess(settings: Settings, provider, report_path: Path) -> dict:
    if not (settings.data_dir / "wardrobe.sqlite3").is_file():
        raise ValueError("Catalogue database does not exist")
    store = Store(settings.data_dir)
    jobs = {job["id"]: job for job in store.jobs()}
    if any(job["status"] in ACTIVE for job in jobs.values()):
        raise ValueError("Finish or cancel active analyses before reassessment")
    with store.connect() as db:
        edited = {
            row[0] for row in db.execute("SELECT DISTINCT entity_id FROM audit WHERE action='edit_item'")
        }
    targets = [
        item
        for item in store.items()
        if not item.get("reviewed")
        and (not item.get("quality") or item["quality"].get("reassessment_pending"))
        and item["id"] not in edited
        and not jobs[item["job_id"]].get("is_demo")
    ]
    report = {"backup": None, "results": [], "skipped": len(store.items()) - len(targets)}
    if targets:
        backup_dir = settings.data_dir / "backups"
        backup_dir.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = backup_dir / f"pre-quality-{stamp}.sqlite3"
        with store.connect() as source, sqlite3.connect(backup) as destination:
            source.backup(destination)
        report["backup"] = str(backup)
        # One transaction makes every target non-cataloguable before any remote
        # request. Cancellation or process termination can safely resume them.
        with store.connect() as db:
            for item in targets:
                store._audit(db, item["id"], "reassess_item", item)
                pending = {
                    **item,
                    "record": unknown_garment(),
                    "quality": {**unassessed_quality(), "reassessment_pending": True},
                }
                db.execute(
                    "UPDATE items SET payload=? WHERE id=?",
                    (json.dumps(pending, ensure_ascii=False), item["id"]),
                )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    for index, item in enumerate(targets):
        job = jobs[item["job_id"]]
        directory = settings.data_dir / "jobs" / job["id"]
        try:
            cutout = media_path(item["image_url"], job["id"], directory)
            frame = item.get("frame")
            source_name = f"frame-{frame}.jpg" if frame is not None else "preview.jpg"
            source = media_path(f"/media/{job['id']}/{source_name}", job["id"], directory)
            context = None
            metrics = {}
            if item.get("bbox"):
                context = directory / "quality" / f"context-{item['id']}.jpg"
                await asyncio.to_thread(media.context_crop, source, item["bbox"], context)
                metrics = await asyncio.to_thread(media.crop_metrics, source, item["bbox"])
            assessment = await provider.inspect(cutout, job["provider"], context_path=context)
            record, model_record, quality = apply_inspection(assessment, metrics)
            # The server lock excludes concurrent UI changes. Retain the exact
            # first model output separately from this new model assessment.
            store.update_item(
                item["id"],
                audit_action="reassess_item",
                image_url=media_url(job["id"], context or cutout, directory),
                cutout_url=media_url(job["id"], cutout, directory) if context else None,
                source_url=media_url(job["id"], source, directory),
                record=record,
                quality=quality,
                reassessed_model_record=model_record,
                error=None,
            )
            result = {"id": item["id"], "job_id": job["id"], "quality": quality, "record": record}
        except (ProviderError, media.MediaError, ValidationError, ValueError) as error:
            store.update_item(
                item["id"],
                audit_action="reassess_item",
                quality={**unassessed_quality(), "reassessment_pending": True},
                record=unknown_garment(),
                error=str(error),
            )
            result = {"id": item["id"], "job_id": job["id"], "error": str(error)}
        report["results"].append(result)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(
            json.dumps({"progress": f"{index + 1}/{len(targets)}", **result}, ensure_ascii=False), flush=True
        )
    # Reconcile from persisted results, including a previous run interrupted
    # after committing an item but before updating its job summary.
    for job_id in {item["job_id"] for item in store.items() if item.get("quality")}:
        updated = store.job(job_id)
        warnings = [
            w
            for w in updated["warnings"]
            if "未列入可整理衣物" not in w and "個區域尚未完成重新判讀" not in w
        ]
        if updated["retake_count"]:
            warnings.append(
                f"{updated['retake_count']} 個區域只顯示局部或不清楚的衣物，已分開標記需補拍，未列入可整理衣物。"
            )
        if updated["unassessed_count"]:
            warnings.append(f"{updated['unassessed_count']} 個區域尚未完成重新判讀，請重試或人工確認。")
        stage = updated["stage"]
        if updated["status"] == "completed":
            stage = (
                f"分析完成，{updated['retake_count']} 個區域需補拍" if updated["retake_count"] else "分析完成"
            )
        store.update_job(job_id, warnings=warnings, stage=stage)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


async def run(args):
    settings = Settings(data_dir=args.data_dir)
    with (settings.data_dir / ".server.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("請先停止 Rewear 服務；重評期間需獨佔資料目錄。") from None
        provider = Providers(settings)
        try:
            report = await reassess(settings, provider, args.output)
        finally:
            await provider.close()
    if any("error" in result for result in report["results"]):
        raise SystemExit("部分結果未能重評；原始結果已保留，詳見報告。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("out/verification/quality-reassessment.json"))
    asyncio.run(run(parser.parse_args()))
