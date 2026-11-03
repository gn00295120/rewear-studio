import asyncio
import json
import sqlite3

from PIL import Image
import pytest

from scripts.reassess_catalogue import media_path, reassess
from wardrobe.config import Settings
from wardrobe.providers import ProviderError
from wardrobe.schemas import unknown_garment
from wardrobe.store import Store


class Inspector:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    async def inspect(self, path, provider, *, context_path=None):
        self.calls += 1
        assert path.is_file() and context_path.is_file()
        if self.fail:
            raise ProviderError("模型暫時無法連線。")
        return {
            "garment": unknown_garment(),
            "target": "one_garment",
            "visibility": "fragment",
            "reason": "衣物被遮擋",
        }


def seed(tmp_path):
    store = Store(tmp_path)
    job = store.create_job(status="completed", provider="meta", mode="rack")
    directory = tmp_path / "jobs" / job["id"]
    directory.mkdir(parents=True)
    for name in ("preview.jpg", "crop.jpg"):
        Image.new("RGB", (100, 100), "blue").save(directory / name)
    original = {**unknown_garment(), "category": "accessory", "subtype": "scarf"}
    item = store.add_item(
        job["id"],
        image_url=f"/media/{job['id']}/crop.jpg",
        bbox=[10, 20, 70, 100],
        record=original,
        model_record=original,
    )
    return store, job, item


async def test_reassessment_backs_up_audits_and_skips_human_changes(tmp_path):
    store, job, untouched = seed(tmp_path)
    for reviewed in (True, False):
        edited = store.add_item(job["id"], record=unknown_garment())
        store.update_item(edited["id"], reviewed=reviewed, record={**unknown_garment(), "brand": "user"})
    original_crop = media_path(untouched["image_url"], job["id"], tmp_path / "jobs" / job["id"])
    original_bytes = original_crop.read_bytes()
    provider = Inspector()
    report_path = tmp_path / "report.json"
    result = await reassess(Settings(data_dir=tmp_path), provider, report_path)
    assert provider.calls == 1 and result["skipped"] == 2
    assert original_crop.read_bytes() == original_bytes
    with sqlite3.connect(result["backup"]) as backup:
        prior = json.loads(
            backup.execute("SELECT payload FROM items WHERE id=?", (untouched["id"],)).fetchone()[0]
        )
    assert prior == untouched
    changed = store.item(untouched["id"])
    assert changed["model_record"] == untouched["model_record"]
    assert changed["record"]["subtype"] is None
    assert changed["quality"]["status"] == "needs_retake"
    assert changed["quality"]["version"] == 3 and changed["quality"]["model_target"] == "one_garment"
    assert store.job(job["id"])["retake_count"] == 1
    with store.connect() as db:
        audit = json.loads(
            db.execute("SELECT payload FROM audit WHERE entity_id=?", (untouched["id"],)).fetchone()[0]
        )
    assert audit == untouched
    repeated = await reassess(Settings(data_dir=tmp_path), provider, report_path)
    assert repeated["backup"] is None and repeated["results"] == []
    assert provider.calls == 1


async def test_failed_reassessment_keeps_model_evidence_out_of_catalogue_and_can_retry(tmp_path):
    store, _, untouched = seed(tmp_path)
    provider = Inspector(fail=True)
    result = await reassess(Settings(data_dir=tmp_path), provider, tmp_path / "report.json")
    assert result["results"][0]["error"] == "模型暫時無法連線。"
    failed = store.item(untouched["id"])
    assert failed["model_record"] == untouched["model_record"]
    assert failed["record"] == unknown_garment()
    assert failed["quality"]["status"] == "unassessed"
    assert failed["quality"]["version"] == 3 and failed["quality"]["model_target"] is None
    assert store.job(untouched["job_id"])["usable_count"] == 0
    provider.fail = False
    result = await reassess(Settings(data_dir=tmp_path), provider, tmp_path / "retry.json")
    assert result["results"][0]["quality"]["status"] == "needs_retake"
    assert provider.calls == 2


async def test_reassessment_cannot_send_source_symlink_outside_its_job(tmp_path):
    store, job, item = seed(tmp_path)
    source = tmp_path / "jobs" / job["id"] / "preview.jpg"
    outside = tmp_path / "private.jpg"
    source.rename(outside)
    source.symlink_to(outside)
    provider = Inspector()
    result = await reassess(Settings(data_dir=tmp_path), provider, tmp_path / "report.json")
    assert provider.calls == 0
    assert "error" in result["results"][0]
    assert store.item(item["id"])["quality"]["status"] == "unassessed"


async def test_interrupted_reassessment_quarantines_all_targets_and_can_resume(tmp_path):
    store, job, item = seed(tmp_path)
    store.add_item(job["id"], **{k: v for k, v in item.items() if k not in ("id", "job_id")})

    class CancelledInspector:
        async def inspect(self, *args, **kwargs):
            raise asyncio.CancelledError

    output = tmp_path / "report.json"
    with pytest.raises(asyncio.CancelledError):
        await reassess(Settings(data_dir=tmp_path), CancelledInspector(), output)
    assert store.job(job["id"])["usable_count"] == 0
    assert store.job(job["id"])["unassessed_count"] == 2
    assert json.loads(output.read_text())["backup"]
    provider = Inspector()
    result = await reassess(Settings(data_dir=tmp_path), provider, tmp_path / "resumed.json")
    assert provider.calls == 2
    assert all(row["quality"]["status"] == "needs_retake" for row in result["results"])
