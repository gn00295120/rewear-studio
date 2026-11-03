from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .quality import result_counts, result_status, summarize_results
from .model_usage import summarize as summarize_model_usage

ACTIVE = {"queued", "running"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """Short SQLite transactions; an audit copy precedes every edit/deletion."""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "wardrobe.sqlite3"
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS items_by_job ON items(job_id);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, entity_id TEXT NOT NULL,
                    action TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, client_id TEXT UNIQUE, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS garment_groups (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS garment_members (
                    item_id TEXT PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
                    group_id TEXT NOT NULL REFERENCES garment_groups(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS members_by_group ON garment_members(group_id);
            """)

    @contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.execute("PRAGMA foreign_keys=ON")
            try:
                with db:
                    yield db
            finally:
                db.close()

    @staticmethod
    def _audit(db, entity_id, action, payload):
        db.execute(
            "INSERT INTO audit(entity_id,action,created_at,payload) VALUES(?,?,?,?)",
            (entity_id, action, now(), json.dumps(payload, ensure_ascii=False)),
        )

    @staticmethod
    def _job_payload(values) -> dict:
        values = dict(values)
        return {
            "id": values.pop("id", uuid4().hex),
            "filename": "",
            "media_type": "image",
            "provider": "local",
            "mode": "single",
            "concept": "garment",
            "status": "queued",
            "stage": "Queued",
            "progress": 0,
            "created_at": now(),
            "updated_at": now(),
            "source_url": None,
            "preview_url": None,
            "overlay_url": None,
            "warnings": [],
            "error": None,
            "is_demo": False,
            "input_name": None,
            "options": {},
            **values,
        }

    def create_job(self, **values) -> dict:
        job = self._job_payload(values)
        with self.connect() as db:
            db.execute("INSERT INTO jobs VALUES(?,?)", (job["id"], json.dumps(job, ensure_ascii=False)))
        return {**job, **result_counts([])}

    def record_model_usage(self, event: dict) -> None:
        """Append privacy-safe provider counters to the existing audit ledger."""
        entity_id = event.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id or len(entity_id) > 200:
            raise ValueError("Invalid model usage entity")
        payload = dict(event)
        payload.pop("entity_id", None)
        with self.connect() as db:
            self._audit(db, entity_id, "model_usage", payload)

    def model_usage(self, *, entity_id: str | None = None) -> dict:
        query = "SELECT entity_id,payload FROM audit WHERE action='model_usage'"
        parameters = ()
        if entity_id is not None:
            query += " AND entity_id=?"
            parameters = (entity_id,)
        with self.connect() as db:
            rows = db.execute(query + " ORDER BY id", parameters).fetchall()
        events = []
        for stored_entity, payload in rows:
            try:
                value = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(value, dict):
                events.append({"entity_id": stored_entity, **value})
        return summarize_model_usage(events)

    def model_spend_since(self, since: str) -> float:
        """Estimated model spend (USD) recorded at or after an ISO-8601 UTC timestamp."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT payload FROM audit WHERE action='model_usage' AND created_at>=?", (since,)
            ).fetchall()
        total = 0.0
        for (payload,) in rows:
            try:
                value = float(json.loads(payload).get("estimated_usd", 0))
            except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
                continue
            if value > 0:
                total += value
        return total

    def create_batch(self, jobs: list[dict], **values) -> dict:
        batch_id = uuid4().hex
        payloads = [self._job_payload({**job, "batch_id": batch_id}) for job in jobs]
        batch = {
            "id": batch_id,
            "created_at": now(),
            "job_ids": [j["id"] for j in payloads],
            "errors": [],
            **values,
        }
        with self.connect() as db:
            db.execute(
                "INSERT INTO batches VALUES(?,?,?)",
                (batch_id, batch.get("client_id"), json.dumps(batch, ensure_ascii=False)),
            )
            db.executemany(
                "INSERT INTO jobs VALUES(?,?)",
                [(j["id"], json.dumps(j, ensure_ascii=False)) for j in payloads],
            )
        return self.batch(batch_id)

    def batch_by_client(self, client_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT id FROM batches WHERE client_id=?", (client_id,)).fetchone()
        return self.batch(row[0]) if row else None

    def batch(self, batch_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not row:
            return None
        batch = json.loads(row[0])
        jobs = [job for ident in batch["job_ids"] if (job := self.job(ident))]
        counts = {
            status: sum(j["status"] == status for j in jobs)
            for status in ("queued", "running", "completed", "partial", "failed", "cancelled")
        }
        missing = len(batch["job_ids"]) - len(jobs)
        if counts["running"]:
            status = "running"
        elif counts["queued"]:
            status = "queued"
        elif jobs and counts["cancelled"] == len(jobs):
            status = "cancelled"
        elif counts["partial"] or (
            counts["completed"] and (counts["failed"] or counts["cancelled"] or batch["errors"] or missing)
        ):
            status = "partial"
        elif counts["failed"] or (not jobs and batch["errors"]):
            status = "failed"
        elif missing:
            status = "partial"
        else:
            status = "completed"
        progress = (
            round(sum(j["progress"] if j["status"] in ACTIVE else 100 for j in jobs) / len(jobs))
            if jobs
            else 100
        )
        return {
            **batch,
            "jobs": jobs,
            "job_count": len(jobs),
            "deleted_count": missing,
            "counts": counts,
            "status": status,
            "progress": progress,
            "item_count": sum(j["item_count"] for j in jobs),
            "usable_count": sum(j["usable_count"] for j in jobs),
            "confirmation_count": sum(j.get("confirmation_count", 0) for j in jobs),
        }

    def batches(self) -> list[dict]:
        with self.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM batches ORDER BY rowid DESC")]
        return [self.batch(ident) for ident in ids]

    @staticmethod
    def _groups(db) -> list[dict]:
        groups = []
        for ident, payload in db.execute("SELECT id,payload FROM garment_groups ORDER BY rowid DESC"):
            item_ids = [
                r[0]
                for r in db.execute(
                    "SELECT item_id FROM garment_members WHERE group_id=? ORDER BY item_id", (ident,)
                )
            ]
            groups.append({**json.loads(payload), "item_ids": item_ids})
        return groups

    def groups(self) -> list[dict]:
        with self.connect() as db:
            return self._groups(db)

    def reconstruction_snapshot(self, item_id: str, reference_item_ids: list[str]) -> dict:
        """Resolve confirmed membership and originals under one tenant-store lock."""
        with self.connect() as db:
            items, jobs = {}, {}
            for ident in (item_id, *reference_item_ids):
                row = db.execute("SELECT payload FROM items WHERE id=?", (ident,)).fetchone()
                if row is None:
                    raise KeyError("Reference garment not found.")
                item = json.loads(row[0])
                items[ident] = item
                row = db.execute("SELECT payload FROM jobs WHERE id=?", (item["job_id"],)).fetchone()
                if row is None:
                    raise KeyError("Source photo job not found.")
                jobs[item["job_id"]] = json.loads(row[0])
            group = next((g for g in self._groups(db) if item_id in g["item_ids"]), None)
            return {"items": items, "jobs": jobs, "group": group}

    def merge_scope(self, item_ids: list[str], primary_item_id: str) -> dict:
        """Validate before cancelling work; the mutation validates again afterwards."""
        with self.connect() as db:
            members, previous = self._merge_candidates(db, item_ids, primary_item_id)
            return {"item_ids": sorted(members), "group_ids": [g["id"] for g in previous]}

    def _merge_candidates(self, db, item_ids, primary_item_id):
        requested = set(item_ids)
        if len(requested) < 2 or len(requested) != len(item_ids) or primary_item_id not in requested:
            raise ValueError("Select at least two different sources and designate one as primary.")
        all_items = {r[0]: json.loads(r[1]) for r in db.execute("SELECT id,payload FROM items")}
        if not requested <= all_items.keys():
            raise KeyError("Garment to merge not found.")
        if any(result_status(all_items[ident]) not in {"ready", "needs_confirmation"} for ident in requested):
            raise ValueError("Review items that need retake or are unassessed before merging.")
        previous = [g for g in self._groups(db) if requested.intersection(g["item_ids"])]
        members = requested.union(*(set(g["item_ids"]) for g in previous))
        if len(members) > 128:
            raise ValueError("Each garment can have at most 128 merged sources.")
        return members, previous

    def merge_items(self, item_ids: list[str], primary_item_id: str) -> dict:
        with self.connect() as db:
            members, previous = self._merge_candidates(db, item_ids, primary_item_id)
            ident = uuid4().hex
            group = {
                "id": ident,
                "primary_item_id": primary_item_id,
                "created_at": now(),
                "updated_at": now(),
            }
            self._audit(
                db,
                ident,
                "merge_garments",
                {
                    "previous_groups": previous,
                    "requested_item_ids": item_ids,
                    "primary_item_id": primary_item_id,
                },
            )
            for old in previous:
                db.execute("DELETE FROM garment_groups WHERE id=?", (old["id"],))
            db.execute(
                "INSERT INTO garment_groups VALUES(?,?)", (ident, json.dumps(group, ensure_ascii=False))
            )
            db.executemany(
                "INSERT INTO garment_members VALUES(?,?)", [(member, ident) for member in sorted(members)]
            )
        return {**group, "item_ids": sorted(members)}

    def unmerge(self, group_id: str) -> bool:
        with self.connect() as db:
            group = next((g for g in self._groups(db) if g["id"] == group_id), None)
            if not group:
                return False
            self._audit(db, group_id, "unmerge_garments", group)
            db.execute("DELETE FROM garment_groups WHERE id=?", (group_id,))
        return True

    def _audit_membership(self, db, item_ids):
        for group in self._groups(db):
            if set(item_ids).intersection(group["item_ids"]):
                self._audit(db, group["id"], "remove_garment_source", group)

    def _repair_groups(self, db):
        for group in self._groups(db):
            if len(group["item_ids"]) < 2:
                db.execute("DELETE FROM garment_groups WHERE id=?", (group["id"],))
            elif group["primary_item_id"] not in group["item_ids"]:
                group.update(primary_item_id=group["item_ids"][0], updated_at=now())
                group.pop("item_ids")
                db.execute(
                    "UPDATE garment_groups SET payload=? WHERE id=?",
                    (json.dumps(group, ensure_ascii=False), group["id"]),
                )

    def job(self, job_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            job = json.loads(row[0])
            entries = db.execute("SELECT payload FROM items WHERE job_id=?", (job_id,)).fetchall()
            return summarize_results(job, [json.loads(entry[0]) for entry in entries])

    def jobs(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT payload FROM jobs ORDER BY json_extract(payload,'$.created_at') DESC
            """).fetchall()
            items_by_job = {}
            for job_id, payload in db.execute("SELECT job_id,payload FROM items"):
                items_by_job.setdefault(job_id, []).append(json.loads(payload))
        jobs = [json.loads(row[0]) for row in rows]
        return [summarize_results(job, items_by_job.get(job["id"], [])) for job in jobs]

    def update_job(self, job_id: str, **changes) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError(job_id)
            job = {**json.loads(row[0]), **changes, "updated_at": now()}
            db.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(job, ensure_ascii=False), job_id))
        return self.job(job_id)

    def add_item(self, job_id: str, **values) -> dict:
        item = {
            "id": uuid4().hex,
            "job_id": job_id,
            "created_at": now(),
            "image_url": None,
            "frame": None,
            "object_id": None,
            "bbox": None,
            "reviewed": False,
            "provenance": "",
            "error": None,
            **values,
        }
        with self.connect() as db:
            db.execute(
                "INSERT INTO items VALUES(?,?,?)",
                (item["id"], job_id, json.dumps(item, ensure_ascii=False)),
            )
        return item

    def item(self, item_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM items WHERE id=?", (item_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def items(self, job_id=None) -> list[dict]:
        with self.connect() as db:
            if job_id:
                rows = db.execute(
                    "SELECT payload FROM items WHERE job_id=? ORDER BY rowid", (job_id,)
                ).fetchall()
            else:
                rows = db.execute("SELECT payload FROM items ORDER BY rowid DESC").fetchall()
        return [json.loads(row[0]) for row in rows]

    def update_item(self, item_id, *, audit_action="edit_item", **changes) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                raise KeyError(item_id)
            item = json.loads(row[0])
            self._audit(db, item_id, audit_action, item)
            item.update(changes)
            db.execute(
                "UPDATE items SET payload=? WHERE id=?", (json.dumps(item, ensure_ascii=False), item_id)
            )
        return item

    def update_reconstruction(
        self, item_id: str, changes: dict, *, expected_id: str | None = None, active_only=False
    ) -> dict | None:
        """Update only synthetic presentation state, guarded against stale attempts."""
        return self._update_item_state(
            item_id,
            "reconstruction",
            "reconstruct_image",
            changes,
            expected_id=expected_id,
            active_only=active_only,
        )

    def update_discovery(
        self, item_id: str, changes: dict, *, expected_id: str | None = None, active_only=False
    ) -> dict | None:
        """Keep brand/search suggestions separate from observed or human-edited data."""
        return self._update_item_state(
            item_id,
            "discovery",
            "discover_shopping",
            changes,
            expected_id=expected_id,
            active_only=active_only,
        )

    def _update_item_state(self, item_id, field, action, changes, *, expected_id, active_only):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                return None
            item = json.loads(row[0])
            previous = item.get(field) or {}
            if expected_id is not None and previous.get("id") != expected_id:
                return None
            if active_only and previous.get("status") not in ACTIVE:
                return None
            self._audit(db, item_id, action, previous)
            item[field] = {
                **(previous if expected_id is not None else {}),
                **changes,
                "updated_at": now(),
            }
            db.execute(
                "UPDATE items SET payload=? WHERE id=?", (json.dumps(item, ensure_ascii=False), item_id)
            )
        return item

    def delete_item(self, item_id: str) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM items WHERE id=?", (item_id,)).fetchone()
            if not row:
                return False
            self._audit(db, item_id, "delete_item", json.loads(row[0]))
            self._audit_membership(db, [item_id])
            db.execute("DELETE FROM items WHERE id=?", (item_id,))
            self._repair_groups(db)
        return True

    def delete_job(self, job_id: str) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return False
            items = [
                json.loads(r[0]) for r in db.execute("SELECT payload FROM items WHERE job_id=?", (job_id,))
            ]
            self._audit(db, job_id, "delete_job", {**json.loads(row[0]), "items": items})
            self._audit_membership(db, [item["id"] for item in items])
            db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            self._repair_groups(db)
        return True

    def recover(self):
        for job in self.jobs():
            if job["status"] in ACTIVE:
                self.update_job(
                    job["id"],
                    status="partial" if job["item_count"] else "failed",
                    stage="Analysis interrupted",
                    error="Analysis was interrupted by a server stop. Completed items are preserved — please retry.",
                )
        for item in self.items():
            state = item.get("reconstruction") or {}
            if state.get("status") in ACTIVE:
                self._remove_interrupted_reconstruction(item, state)
                self.update_reconstruction(
                    item["id"],
                    {
                        "status": "failed",
                        "error": "Preview generation was interrupted by a server stop. Please regenerate.",
                    },
                    expected_id=state.get("id"),
                    active_only=True,
                )
            discovery = item.get("discovery") or {}
            if discovery.get("status") in ACTIVE:
                self._remove_interrupted_discovery(item, discovery)
                self.update_discovery(
                    item["id"],
                    {
                        "status": "failed",
                        "stage": "interrupted",
                        "error": "Brand & shopping search was interrupted by a server stop. Please search again.",
                    },
                    expected_id=discovery.get("id"),
                    active_only=True,
                )

    def _remove_interrupted_discovery(self, item, state):
        """Remove this interrupted attempt's media, preserving any retained result."""
        job_id, attempt_id = item.get("job_id"), state.get("id")
        if not all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) for value in (job_id, attempt_id)
        ):
            return
        jobs = self.path.parent / "jobs"
        directory = jobs / job_id
        parent = directory / "shopping"
        output = parent / attempt_id
        prefix = f"/media/{job_id}/shopping/{attempt_id}/"
        result = state.get("result") or {}
        offers = (result.get("shopping") or {}).get("offers") or []
        if (
            any(path.is_symlink() for path in (jobs, directory, parent, output))
            or not output.resolve().is_relative_to(jobs.resolve())
            or any(
                str((offer.get("verification") or {}).get("image_url", "")).startswith(prefix)
                for offer in offers
            )
        ):
            return
        if output.is_dir():
            shutil.rmtree(output)

    def _remove_interrupted_reconstruction(self, item, state):
        """Remove only this attempt's uncommitted output, never a retained image."""
        job_id, attempt_id = item.get("job_id"), state.get("id")
        if not all(
            isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}", value) for value in (job_id, attempt_id)
        ):
            return
        jobs = self.path.parent / "jobs"
        directory = jobs / job_id
        output = directory / "reconstructions"
        path = output / f"{attempt_id}.png"
        if (
            jobs.is_symlink()
            or directory.is_symlink()
            or output.is_symlink()
            or path.is_symlink()
            or not path.resolve().is_relative_to(jobs.resolve())
            or any(
                state.get(key) == f"/media/{job_id}/reconstructions/{attempt_id}.png"
                for key in ("image_url", "preview_url", "candidate_url")
            )
        ):
            return
        path.unlink(missing_ok=True)
        for candidate in output.glob(f"{attempt_id}-preview-*.png"):
            if candidate.is_symlink() or not candidate.resolve().is_relative_to(output.resolve()):
                continue
            candidate.unlink(missing_ok=True)
