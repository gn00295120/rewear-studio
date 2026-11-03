"""Batch and garment-group integration tests against real temporary SQLite/media.

Only model responses are simulated; these checks make no accuracy claim.
"""

import asyncio
import csv
import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from wardrobe.app import create_app
from wardrobe.config import Settings
from wardrobe.providers import ProviderError
from wardrobe.schemas import unknown_garment


def photo(colour="blue"):
    output = io.BytesIO()
    Image.new("RGB", (100, 120), colour).save(output, "JPEG")
    return output.getvalue()


class Models:
    def __init__(self):
        self.calls = 0
        self.delay_after = None
        self.fail = False
        self.partial = False

    async def health(self):
        return {"local": {"available": True}, "meta": {"configured": True}}

    async def close(self):
        pass

    async def segment(self, path, concept, video=False):
        with Image.open(path) as image:
            width, height = image.size
        return [
            {
                "kind": "box",
                "object_id": "shirt",
                "frame": None,
                "left": 2,
                "top": 2,
                "right": width - 2,
                "bottom": height - 2,
            }
        ]

    async def inspect(self, path, provider, *, context_path=None, wearing=False):
        self.calls += 1
        if self.delay_after is not None and self.calls > self.delay_after:
            await asyncio.sleep(10)
        if self.fail:
            raise ProviderError("本測試模擬模型暫時離線。")
        return {
            "target": "one_garment",
            "visibility": "partial" if self.partial else "complete",
            "reason": "測試用的部分遮擋。" if self.partial else None,
            "garment": {
                **unknown_garment(),
                "category": "top",
                "subtype": "shirt",
                "brand": "Example",
                "colors": ["blue"],
                "confidence": 0.9,
            },
        }


@pytest.fixture
def setup(tmp_path):
    model = Models()
    app = create_app(Settings(data_dir=tmp_path, api_key="test-only"), model)
    with TestClient(app) as client:
        yield client, app, model


def batch(client, files=None, **data):
    files = files if files is not None else [("a.jpg", photo()), ("b.jpg", photo())]
    return client.post(
        "/api/batches", files=[("files", item) for item in files], data={"mode": "single", **data}
    )


def wait_batch(client, ident, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/batches/{ident}")
        assert response.status_code == 200
        result = response.json()
        if result["status"] not in {"queued", "running"}:
            return result
        time.sleep(0.01)
    raise AssertionError(f"batch still active: {result}")


def test_mixed_batch_records_each_error_and_keeps_successful_files(setup):
    client, app, model = setup
    result = batch(client, [("../a.jpg", photo()), ("x.txt", b"no"), ("empty.png", b"")])
    assert result.status_code == 202
    done = wait_batch(client, result.json()["id"])
    assert done["status"] == "partial" and done["progress"] == 100
    assert done["file_count"] == 3 and done["job_count"] == done["item_count"] == 1
    assert done["jobs"][0]["filename"] == "a.jpg" and done["jobs"][0]["batch_index"] == 0
    assert [error["index"] for error in done["errors"]] == [1, 2]
    assert len(list((app.state.settings.data_dir / "jobs").iterdir())) == 1
    assert model.calls == 1
    assert "request_hash" not in done and "client_id" not in done
    assert "input_name" not in done["jobs"][0]
    assert client.get("/api/batches").json()["batches"] == [done]
    assert client.post(f"/api/batches/{done['id']}/retry").status_code == 409


def test_all_rejected_files_have_durable_failed_batch_with_no_jobs(setup):
    client, app, model = setup
    result = batch(client, [("readme.txt", b"invalid"), ("empty.jpg", b"")]).json()
    assert result["status"] == "failed" and len(result["errors"]) == 2
    assert result["job_count"] == 0 and result["progress"] == 100
    assert app.state.store.jobs() == [] and model.calls == 0
    assert client.get(f"/api/batches/{result['id']}").json() == result


def test_replay_key_is_bound_to_bytes_and_person_options(setup):
    client, app, model = setup
    options = {"mode": "outfit", "client_id": "stable-id", "selections": "[[0,0,0.5,1],null]"}
    first = batch(client, **options)
    assert first.status_code == 202
    ident = first.json()["id"]
    wait_batch(client, ident)
    assert batch(client, **options).json()["id"] == ident
    assert len(app.state.store.jobs()) == 2 and model.calls == 2
    changed = batch(client, [("a.jpg", photo("red")), ("b.jpg", photo())], **options)
    assert changed.status_code == 409
    assert batch(client, **{**options, "selections": "[null,null]"}).status_code == 409
    assert len(app.state.store.batches()) == 1 and len(app.state.store.jobs()) == 2
    assert len(list((app.state.settings.data_dir / "jobs").iterdir())) == 2


@pytest.mark.parametrize(
    "selections",
    [
        "{}",
        "[]",
        "[null]",
        "[null,null,null]",
        "garbage",
        "[[0,0,0,1],null]",
        "[[0,0,1.1,1],null]",
        "[[0,0,true,1],null]",
        "[[0,0,NaN,1],null]",
    ],
)
def test_invalid_batch_selections_fail_before_jobs_are_created(setup, selections):
    client, app, _ = setup
    assert batch(client, mode="outfit", selections=selections).status_code == 422
    assert app.state.store.jobs() == app.state.store.batches() == []
    assert list((app.state.settings.data_dir / "jobs").iterdir()) == []


def test_outfit_selection_quality_and_original_sources_round_trip(setup):
    client, app, model = setup
    model.partial = True
    options = {"mode": "outfit", "selections": "[[0,0,0.5,1]]"}
    result = batch(client, [("portrait.jpg", photo())], **options)
    assert result.status_code == 202
    done = wait_batch(client, result.json()["id"])
    job = client.get(f"/api/jobs/{done['job_ids'][0]}").json()
    item = job["items"][0]
    assert done["confirmation_count"] == 1 and done["usable_count"] == 0
    assert item["quality"]["status"] == "needs_confirmation" and item["record"]["category"] == "top"
    assert item["record"]["needs_review"] is True and item["reviewed"] is False
    assert app.state.store.job(job["id"])["options"]["person_box"] == [0, 0, 0.5, 1]
    assert job["selection_bbox"] == [0, 0, 50, 120]
    assert item["source_url"] == job["selection_url"] != item["original_url"]
    assert item["original_url"] == job["preview_url"]
    for field in ("source_url", "selection_url", "preview_url", "overlay_url"):
        assert client.get(job[field]).status_code == 200
    assert len(client.get("/api/items?quality=needs_confirmation").json()["items"]) == 1
    assert client.get("/api/items?quality=ready").json()["items"] == []
    edited = client.patch(f"/api/items/{item['id']}", json={"reviewed": True}).json()
    assert edited["quality"] == item["quality"] and edited["model_record"] == item["model_record"]
    current = client.get(f"/api/batches/{done['id']}").json()
    assert current["confirmation_count"] == 0 and current["usable_count"] == 1
    assert app.state.store.item(item["id"])["reviewed"] is True


def test_single_route_checks_selection_mode_and_outfit_video(setup):
    client, app, _ = setup
    files = {"file": ("a.jpg", photo())}
    assert client.post("/api/jobs", files=files, data={"person_box": "[0,0,1,1]"}).status_code == 422
    assert (
        client.post("/api/jobs", files=files, data={"mode": "outfit", "person_box": "[1,0,0,1]"}).status_code
        == 422
    )
    assert (
        client.post("/api/jobs", files={"file": ("a.mp4", b"video")}, data={"mode": "outfit"}).status_code
        == 422
    )
    assert app.state.store.jobs() == []
    result = client.post("/api/jobs", files=files, data={"mode": "outfit", "person_box": "[0,0,1,1]"})
    assert result.status_code == 202
    assert app.state.store.job(result.json()["id"])["options"]["person_box"] == [0, 0, 1, 1]


def test_batch_video_rejection_is_per_file_in_outfit_mode(setup):
    client, _, model = setup
    result = batch(client, [("photo.jpg", photo()), ("movie.mov", b"video")], mode="outfit").json()
    done = wait_batch(client, result["id"])
    assert done["status"] == "partial" and done["job_count"] == 1
    assert done["errors"][0]["filename"] == "movie.mov" and model.calls == 1


def test_upload_limits_are_per_file_per_batch_and_queue_atomic(tmp_path):
    model = Models()
    settings = Settings(
        data_dir=tmp_path,
        api_key="test",
        max_upload_mb=1,
        max_batch_upload_mb=3,
        max_batch_files=3,
        max_queue=1,
    )
    with TestClient(create_app(settings, model)) as client:
        assert batch(client, [(f"{i}.jpg", photo()) for i in range(4)]).status_code == 422
        assert batch(client, [("huge.jpg", b"x" * (3 * 1024 * 1024 + 1))]).status_code == 413
        assert batch(client).status_code == 429
        assert list((tmp_path / "jobs").iterdir()) == []
        result = batch(client, [("large.jpg", b"x" * (1024 * 1024 + 1)), ("good.jpg", photo())])
        assert result.status_code == 202
        done = wait_batch(client, result.json()["id"])
        assert done["job_count"] == 1 and done["errors"][0]["index"] == 0
        assert len(list((tmp_path / "jobs").iterdir())) == 1
        limits = client.get("/api/health").json()["limits"]
        assert limits["batch_files"] == 3 and limits["batch_upload_mb"] == 3 and limits["max_queue"] == 1


def test_outfit_requires_configured_meta_but_single_local_batch_does_not(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_key=None), Models())
    with TestClient(app) as client:
        assert batch(client, mode="outfit").status_code == 503
        assert batch(client, mode="rack").status_code == 503
        assert batch(client, provider="meta").status_code == 503
        assert app.state.store.jobs() == []
        result = batch(client)
        assert result.status_code == 202
        assert wait_batch(client, result.json()["id"])["usable_count"] == 2


def test_cancel_preserves_completed_work_and_retry_only_unfinished_job(setup):
    client, app, model = setup
    model.delay_after = 1
    old = batch(client).json()
    assert client.post(f"/api/batches/{old['id']}/retry").status_code == 409
    deadline = time.monotonic() + 5
    while model.calls < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert model.calls == 2
    assert client.post(f"/api/batches/{old['id']}/cancel").status_code == 200
    done = wait_batch(client, old["id"])
    assert done["status"] == "partial" and done["counts"]["completed"] == done["counts"]["cancelled"] == 1
    assert done["item_count"] == 1
    completed = next(job for job in done["jobs"] if job["status"] == "completed")
    before_item = app.state.store.items(completed["id"])[0]
    assert client.post(f"/api/batches/{old['id']}/cancel").status_code == 200
    model.delay_after = None
    retry = client.post(f"/api/batches/{old['id']}/retry")
    assert retry.status_code == 202
    redone = wait_batch(client, retry.json()["id"])
    assert redone["retry_of"] == old["id"] and redone["status"] == "completed" and redone["job_count"] == 1
    assert redone["jobs"][0]["retry_of"] != completed["id"]
    assert app.state.store.items(completed["id"])[0] == before_item
    assert client.get(f"/api/batches/{old['id']}").json() == done


def test_partial_retry_preserves_selection_bytes_and_original_evidence(setup):
    client, app, model = setup
    model.fail = True
    old = batch(client, [("photo.jpg", photo())], mode="outfit", selections="[[0,0,0.6,1]]").json()
    done = wait_batch(client, old["id"])
    assert done["status"] == "partial" and done["counts"]["partial"] == 1
    first = done["jobs"][0]
    evidence = app.state.store.items(first["id"])
    model.fail = False
    retried = client.post(f"/api/batches/{old['id']}/retry").json()
    redone = wait_batch(client, retried["id"])
    second = redone["jobs"][0]
    assert redone["status"] == "completed" and redone["usable_count"] == 1
    assert app.state.store.job(second["id"])["options"] == app.state.store.job(first["id"])["options"]
    assert app.state.store.job(second["id"])["options"] == {
        "person_box": [0, 0, 0.6, 1],
        "reconstruct": True,
        "discover": False,
        "discovery_market": "TW",
    }
    assert client.get(second["source_url"]).content == client.get(first["source_url"]).content
    assert app.state.store.items(first["id"]) == evidence


def test_retry_missing_source_cleans_staging_and_preserves_batch(setup):
    client, app, model = setup
    model.fail = True
    old = wait_batch(client, batch(client).json()["id"])
    jobs_dir = app.state.settings.data_dir / "jobs"
    second = app.state.store.job(old["job_ids"][1])
    (jobs_dir / second["id"] / second["input_name"]).unlink()
    before = set(jobs_dir.iterdir())
    assert client.post(f"/api/batches/{old['id']}/retry").status_code == 409
    assert set(jobs_dir.iterdir()) == before and len(app.state.store.batches()) == 1
    assert len(app.state.store.jobs()) == 2


@pytest.mark.parametrize("batch_retry", [False, True])
def test_retry_cannot_publish_a_symlink_outside_the_job(setup, batch_retry):
    client, app, model = setup
    model.fail = True
    old = wait_batch(client, batch(client, [("photo.jpg", photo())]).json()["id"])
    job = app.state.store.job(old["job_ids"][0])
    data_dir = app.state.settings.data_dir
    source = data_dir / "jobs" / job["id"] / job["input_name"]
    private = data_dir / "private.jpg"
    private.write_bytes(b"must never be published")
    source.unlink()
    source.symlink_to(private)
    assert client.get(job["source_url"]).status_code == 404
    route = f"/api/batches/{old['id']}/retry" if batch_retry else f"/api/jobs/{job['id']}/retry"
    before = set((data_dir / "jobs").iterdir())
    assert client.post(route).status_code == 409
    assert set((data_dir / "jobs").iterdir()) == before
    assert len(app.state.store.jobs()) == len(app.state.store.batches()) == 1
    assert private.read_bytes() == b"must never be published"


def test_batch_survives_restart_and_reports_deleted_members(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, api_key="test"), Models())
    with TestClient(app) as client:
        old = wait_batch(client, batch(client).json()["id"])
    with TestClient(create_app(app.state.settings, Models())) as restarted:
        assert restarted.get(f"/api/batches/{old['id']}").json() == old
        assert restarted.delete(f"/api/jobs/{old['job_ids'][0]}").status_code == 204
        current = restarted.get(f"/api/batches/{old['id']}").json()
        assert current["deleted_count"] == 1 and current["job_count"] == 1 and current["status"] == "partial"
        assert current["item_count"] == 1


def sources(app, statuses):
    rows = []
    for status in statuses:
        job = app.state.store.create_job(status="completed")
        rows.append(
            app.state.store.add_item(
                job["id"],
                record={
                    **unknown_garment(),
                    "category": "top",
                    "subtype": "shirt",
                    "brand": "Example",
                    "colors": ["blue"],
                },
                model_record=unknown_garment(),
                quality={"version": 3, "status": status},
                source_url=f"/media/{job['id']}/source.jpg",
            )
        )
    return rows


def merge(client, rows, primary=None):
    ids = [row["id"] for row in rows]
    return client.post("/api/garment-groups", json={"item_ids": ids, "primary_item_id": primary or ids[0]})


def test_human_merge_keeps_sources_confirmation_state_and_exports_then_unmerges(setup):
    client, app, _ = setup
    rows = sources(app, ["ready", "needs_confirmation"])
    group_response = merge(client, rows, primary=rows[1]["id"])
    assert group_response.status_code == 201
    group = group_response.json()
    assert group["primary_item_id"] == rows[1]["id"]
    assert set(group["item_ids"]) == {row["id"] for row in rows}
    assert client.get("/api/garment-groups").json()["groups"] == [group]
    for row in rows:
        assert app.state.store.item(row["id"]) == row
    assert len(client.get("/api/items?quality=needs_confirmation").json()["items"]) == 1
    exported = client.get("/api/export").json()
    assert exported["groups"] == [group] and len(exported["items"]) == 2
    csv_rows = list(
        csv.DictReader(io.StringIO(client.get("/api/export?format=csv").content.decode("utf-8-sig")))
    )
    assert {row["garment_group_id"] for row in csv_rows} == {group["id"]}
    assert {row["primary_item_id"] for row in csv_rows} == {rows[1]["id"]}
    assert {row["quality_status"] for row in csv_rows} == {"ready", "needs_confirmation"}
    assert client.delete(f"/api/garment-groups/{group['id']}").status_code == 204
    assert client.get("/api/garment-groups").json()["groups"] == []
    assert client.get("/api/export").json()["groups"] == []
    assert {row["id"] for row in client.get("/api/items").json()["items"]} == set(group["item_ids"])
    with app.state.store.connect() as db:
        audits = db.execute("SELECT action,payload FROM audit ORDER BY id").fetchall()
    assert [row[0] for row in audits] == ["merge_garments", "unmerge_garments"]
    assert json.loads(audits[1][1]) == group


def test_joining_existing_groups_preserves_all_sources_and_repairs_deleted_primary(setup):
    client, app, _ = setup
    rows = sources(app, ["ready"] * 4)
    group1 = merge(client, rows[:2]).json()
    group2 = merge(client, rows[2:]).json()
    joined = merge(client, [rows[0], rows[2]]).json()
    assert len(joined["item_ids"]) == 4 and len(app.state.store.groups()) == 1
    with app.state.store.connect() as db:
        audit = json.loads(db.execute("SELECT payload FROM audit ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert {group["id"] for group in audit["previous_groups"]} == {group1["id"], group2["id"]}
    assert client.delete(f"/api/items/{joined['primary_item_id']}").status_code == 204
    repaired = app.state.store.groups()[0]
    assert len(repaired["item_ids"]) == 3 and repaired["primary_item_id"] in repaired["item_ids"]
    assert joined["primary_item_id"] not in repaired["item_ids"]
    assert client.delete(f"/api/jobs/{rows[1]['job_id']}").status_code == 204
    assert len(app.state.store.groups()[0]["item_ids"]) == 2
    assert client.delete(f"/api/jobs/{rows[2]['job_id']}").status_code == 204
    assert app.state.store.groups() == [] and len(app.state.store.items()) == 1


@pytest.mark.parametrize("status", ["needs_retake", "unassessed"])
def test_unusable_source_merge_rejected_until_manual_review(setup, status):
    client, app, _ = setup
    rows = sources(app, ["ready", status])
    assert merge(client, rows).status_code == 422
    assert app.state.store.groups() == []
    assert client.patch(f"/api/items/{rows[1]['id']}", json={"reviewed": True}).status_code == 200
    assert merge(client, rows).status_code == 201
    assert app.state.store.item(rows[1]["id"])["quality"]["status"] == status


def test_invalid_merge_is_atomic_and_suggestions_never_modify_data(setup):
    client, app, _ = setup
    rows = sources(app, ["ready", "ready"])
    assert merge(client, [rows[0], rows[0]]).status_code == 422
    assert merge(client, rows, primary="missing").status_code == 422
    assert merge(client, [rows[0], {"id": "missing"}]).status_code == 404
    assert app.state.store.groups() == []
    first = client.get("/api/duplicates").json()
    assert len(first["suggestions"]) == 1 and first["suggestions"][0]["human_required"] is True
    assert client.get("/api/duplicates").json() == first
    assert app.state.store.groups() == []
    group = merge(client, rows).json()
    assert client.get("/api/duplicates").json()["suggestions"] == []
    assert len(app.state.store.items()) == 2
    assert client.delete(f"/api/garment-groups/{group['id']}").status_code == 204
    assert client.get("/api/duplicates").json() == first


def test_batch_and_group_unknown_ids_are_not_successes(setup):
    client, _, _ = setup
    assert client.get("/api/batches/missing").status_code == 404
    assert client.post("/api/batches/missing/cancel").status_code == 404
    assert client.post("/api/batches/missing/retry").status_code == 404
    assert client.delete("/api/garment-groups/missing").status_code == 404
