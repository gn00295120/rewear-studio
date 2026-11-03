"""Real SQLite/media round trips and hostile archive rejection in temp directories."""

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import struct
import warnings
import zipfile
from contextlib import contextmanager
from pathlib import Path

import pytest
from PIL import Image

from wardrobe import backup
from wardrobe.accounts import AccountStore
from wardrobe.backup import BackupError, backup_data, restore_data
from wardrobe.store import Store

PASSWORD = "Backup test password 123"


def seed(directory, *, color="blue"):
    store = Store(directory)
    job = store.create_job(filename="source.jpg", status="completed")
    path = directory / "jobs" / job["id"]
    (path / "crops").mkdir(parents=True)
    (path / "reconstructions").mkdir()
    (path / "shopping" / "attempt").mkdir(parents=True)
    for name in (
        "source.jpg",
        "crops/cutout.png",
        "reconstructions/candidate.png",
        "shopping/attempt/product.png",
    ):
        Image.new("RGB", (24, 32), color).save(path / name)
    (path / "segments.json").write_text(json.dumps({"fixture": "local metadata"}))
    (path / "outfit").mkdir()
    (path / "outfit" / "outfit-plan.json").write_text('{"fixture": true}')
    prefix = f"/media/{job['id']}/"
    store.update_job(job["id"], source_url=prefix + "source.jpg")
    item = store.add_item(
        job["id"],
        record={"category": "top", "colors": [color]},
        image_url=prefix + "crops/cutout.png",
        source_url=prefix + "source.jpg",
        reconstruction={"candidate_url": prefix + "reconstructions/candidate.png"},
        discovery={
            "result": {
                "shopping": {
                    "offers": [{"verification": {"image_url": prefix + "shopping/attempt/product.png"}}]
                }
            }
        },
    )
    store.update_item(item["id"], reviewed=True)
    return store, job, store.item(item["id"])


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def local(tmp_path):
    directory = tmp_path / "local"
    store, job, item = seed(directory)
    return directory, store, job, item


@contextmanager
def lock(directory):
    with (directory / ".server.lock").open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def read_archive(path):
    with zipfile.ZipFile(path) as bundle:
        return [(info.filename, bundle.read(info.filename)) for info in bundle.infolist()]


def rewrite(path, entries, *, modes=None, compression=zipfile.ZIP_STORED):
    with zipfile.ZipFile(path, "w", compression=compression) as bundle:
        for name, content in entries:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (modes or {}).get(name, stat.S_IFREG | 0o600) << 16
            info.compress_type = compression
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                bundle.writestr(info, content)


def mutate_manifest(entries, action):
    manifest = json.loads(next(value for key, value in entries if key == "manifest.json"))
    action(manifest)
    return [
        (name, json.dumps(manifest).encode() if name == "manifest.json" else data) for name, data in entries
    ]


def assert_failure(code, function, *args, **kwargs):
    with pytest.raises(BackupError) as caught:
        function(*args, **kwargs)
    if code is not None:
        assert caught.value.code == code
    return caught.value


def test_local_real_sqlite_media_metadata_and_audit_roundtrip(local, tmp_path):
    source, store, job, item = local
    original_db = digest(store.path)
    before_jobs, before_items = store.jobs(), store.items()
    archive, destination = tmp_path / "snapshot.zip", tmp_path / "restored"
    manifest = backup_data(source, archive, mode="local")
    assert manifest["format"] == backup.FORMAT and manifest["version"] == 1
    assert manifest["mode"] == "local"
    assert restore_data(archive, destination) == manifest
    for record in manifest["files"]:
        target = destination / record["path"]
        assert target.stat().st_size == record["size"] and digest(target) == record["sha256"]
        if record["path"] != "wardrobe.sqlite3":
            assert target.read_bytes() == (source / record["path"]).read_bytes()
    # Starting Store deliberately switches the restored SQLite snapshot back to
    # WAL, changing header bytes. Check archived bytes before starting it.
    restored = Store(destination)
    assert restored.jobs() == before_jobs and restored.items() == before_items
    assert restored.item(item["id"])["reviewed"] is True
    with sqlite3.connect(restored.path) as db:
        assert db.execute("SELECT count(*) FROM audit").fetchone()[0] == 1
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert digest(store.path) == original_db
    assert store.jobs() == before_jobs and store.items() == before_items
    assert (destination / "jobs" / job["id"] / "segments.json").exists()
    assert archive.stat().st_mode & 0o777 == 0o600


def test_hosted_roundtrip_keeps_accounts_recovery_and_distinct_tenant_media(tmp_path):
    source = tmp_path / "hosted"
    accounts = AccountStore(source / "control")
    first = accounts.register("first-person", PASSWORD)
    second = accounts.register("second-person", PASSWORD)
    account_only = accounts.register("no-wardrobe-yet", PASSWORD)
    session = accounts.create_session(first["user"]["id"])
    tenants = source / "tenants"
    first_store, _, first_item = seed(tenants / first["user"]["id"], color="blue")
    second_store, _, second_item = seed(tenants / second["user"]["id"], color="red")
    archive, destination = tmp_path / "hosted.zip", tmp_path / "restored-hosted"
    manifest = backup_data(source, archive, mode="hosted")
    assert len(manifest["databases"]) == 3
    restore_data(archive, destination)
    recovered = AccountStore(destination / "control")
    assert recovered.authenticate("first-person", PASSWORD) == first["user"]
    assert recovered.authenticate("second-person", PASSWORD) == second["user"]
    assert recovered.get_user(account_only["user"]["id"]) == account_only["user"]
    assert recovered.get_session(session["token"])["user"] == first["user"]
    assert Store(destination / "tenants" / first["user"]["id"]).item(first_item["id"]) == first_item
    assert Store(destination / "tenants" / second["user"]["id"]).item(second_item["id"]) == second_item
    recovered.recover("first-person", first["recovery_code"], "New recovered password 456")
    assert accounts.authenticate("first-person", PASSWORD) == first["user"]
    assert accounts.get_session(session["token"]) is not None
    assert first_store.item(first_item["id"]) == first_item
    assert second_store.item(second_item["id"]) == second_item


def test_sqlite_backup_includes_committed_wal(local, tmp_path):
    source, store, _, _ = local
    db = sqlite3.connect(store.path)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("INSERT INTO batches VALUES(?,?,?)", ("wal-batch", None, '{"id":"wal-batch"}'))
        db.commit()
        assert Path(str(store.path) + "-wal").stat().st_size > 0
        archive = tmp_path / "wal.zip"
        backup_data(source, archive, mode="local")
        restore_data(archive, tmp_path / "result")
        with sqlite3.connect(tmp_path / "result" / "wardrobe.sqlite3") as restored:
            assert restored.execute("SELECT id FROM batches").fetchone()[0] == "wal-batch"
        with zipfile.ZipFile(archive) as bundle:
            assert not any(name.endswith(("-wal", "-shm")) for name in bundle.namelist())
    finally:
        db.close()


def test_private_config_locks_temporary_and_trash_are_excluded(local, tmp_path):
    source, _, job, _ = local
    for name in (".env", "api-credentials.json", ".server.lock"):
        (source / name).write_text("private-do-not-copy")
    for name in (
        ".trash/old/source.jpg",
        "tmp/source.jpg",
        f"jobs/{job['id']}/.env",
        f"jobs/{job['id']}/tmp/pending.png",
        f"jobs/{job['id']}/pending.part",
    ):
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("private-do-not-copy")
    archive = tmp_path / "safe.zip"
    manifest = backup_data(source, archive, mode="local")
    assert all(
        not any(part in record["path"] for part in (".env", "trash", "tmp", "part", "lock"))
        for record in manifest["files"]
    )
    with zipfile.ZipFile(archive) as bundle:
        assert not any(b"private-do-not-copy" in bundle.read(name) for name in bundle.namelist())


def test_online_local_and_second_backup_are_rejected(local, tmp_path):
    source, _, _, _ = local
    archive = tmp_path / "locked.zip"
    with lock(source):
        assert_failure("online", backup_data, source, archive, mode="local")
    assert not archive.exists()


@pytest.mark.parametrize("held", ["root", "tenant"])
def test_online_hosted_or_independently_running_tenant_is_rejected(tmp_path, held):
    source = tmp_path / "hosted"
    account = AccountStore(source / "control").register("hosted-person", PASSWORD)
    tenant = source / "tenants" / account["user"]["id"]
    seed(tenant)
    with lock(source if held == "root" else tenant):
        assert_failure("online", backup_data, source, tmp_path / "locked.zip", mode="hosted")
    assert not (tmp_path / "locked.zip").exists()


def test_backup_output_is_exclusive_and_cannot_live_inside_source(local, tmp_path):
    source, _, _, _ = local
    target = tmp_path / "existing.zip"
    target.write_bytes(b"preserve original archive")
    assert_failure("destination_exists", backup_data, source, target, mode="local")
    assert target.read_bytes() == b"preserve original archive"
    assert_failure("invalid_path", backup_data, source, source / "nested.zip", mode="local")


@pytest.mark.parametrize("target_kind", ["nonempty", "file", "symlink"])
def test_restore_never_overwrites_existing_data(local, tmp_path, target_kind):
    source, _, _, _ = local
    archive = tmp_path / "valid.zip"
    backup_data(source, archive, mode="local")
    target = tmp_path / "existing"
    original = tmp_path / "private"
    original.mkdir()
    (original / "sentinel").write_text("keep")
    if target_kind == "nonempty":
        target.mkdir()
        (target / "sentinel").write_text("keep")
    elif target_kind == "file":
        target.write_text("keep")
    else:
        target.symlink_to(original, target_is_directory=True)
    assert_failure(None, restore_data, archive, target)
    assert (original / "sentinel").read_text() == "keep"
    if target_kind == "nonempty":
        assert (target / "sentinel").read_text() == "keep"
    elif target_kind == "file":
        assert target.read_text() == "keep"
    else:
        assert target.is_symlink()


def test_restore_into_empty_directory_and_empty_catalogue(tmp_path):
    source, destination = tmp_path / "empty-source", tmp_path / "empty-target"
    Store(source)
    destination.mkdir()
    manifest = backup_data(source, tmp_path / "empty.zip", mode="local")
    assert restore_data(tmp_path / "empty.zip", destination) == manifest
    assert (destination / "jobs").is_dir()
    assert Store(destination).jobs() == []


@pytest.mark.parametrize(
    "path",
    [
        "data/../../escape",
        "/absolute",
        "data/jobs/a/../../escape",
        "data\\jobs\\escape",
        "data/C:/escape",
        "data//escape",
    ],
)
def test_archive_path_attacks_rejected_before_publication(local, tmp_path, path):
    source, _, _, _ = local
    archive = tmp_path / "attack.zip"
    backup_data(source, archive, mode="local")
    rewrite(archive, read_archive(archive) + [(path, b"attack")])
    destination = tmp_path / "destination"
    destination.mkdir()
    assert_failure(None, restore_data, archive, destination)
    assert list(destination.iterdir()) == []
    assert not (tmp_path / "escape").exists()
    assert not list(tmp_path.glob(".rewear-restore-*"))


@pytest.mark.parametrize("attack", ["duplicate", "case-collision", "symlink", "fifo", "compressed"])
def test_archive_duplicate_special_entry_and_compression_rejected(local, tmp_path, attack):
    source, _, _, _ = local
    archive = tmp_path / "attack.zip"
    backup_data(source, archive, mode="local")
    entries = read_archive(archive)
    name, contents = entries[-1]
    modes = None
    compression = zipfile.ZIP_STORED
    if attack == "duplicate":
        entries.append((name, contents))
    elif attack == "case-collision":
        entries.append((name.upper(), contents))
    elif attack in {"symlink", "fifo"}:
        modes = {name: (stat.S_IFLNK if attack == "symlink" else stat.S_IFIFO) | 0o600}
    else:
        compression = zipfile.ZIP_DEFLATED
    rewrite(archive, entries, modes=modes, compression=compression)
    assert_failure("unsafe_entry", restore_data, archive, tmp_path / "destination")
    assert not (tmp_path / "destination").exists()


@pytest.mark.parametrize("attack", ["bytes", "hash", "version", "unlisted", "size"])
def test_corruption_and_unknown_archive_schema_rejected(local, tmp_path, attack):
    source, _, _, _ = local
    archive = tmp_path / "attack.zip"
    backup_data(source, archive, mode="local")
    entries = read_archive(archive)
    if attack == "bytes":
        name, value = entries[-1]
        entries[-1] = (name, value[:-1] + bytes([value[-1] ^ 1]))
    elif attack == "hash":
        entries = mutate_manifest(entries, lambda manifest: manifest["files"][0].update(sha256="0" * 64))
    elif attack == "version":
        entries = mutate_manifest(entries, lambda manifest: manifest.update(version=999))
    elif attack == "unlisted":
        entries.append(("data/jobs/unlisted/source.jpg", b"unlisted"))
    else:
        entries = mutate_manifest(entries, lambda manifest: manifest["files"][0].update(size=1))
    rewrite(archive, entries)
    assert_failure(None, restore_data, archive, tmp_path / "destination")
    assert not (tmp_path / "destination").exists()


@pytest.mark.parametrize("attack", ["version", "new-column", "trigger", "foreign-key", "missing-media"])
def test_invalid_database_or_broken_media_fails_backup_without_output(local, tmp_path, attack):
    source, store, job, item = local
    with sqlite3.connect(store.path) as db:
        if attack == "version":
            db.execute("PRAGMA user_version=42")
        elif attack == "new-column":
            db.execute("ALTER TABLE jobs ADD COLUMN unknown TEXT")
        elif attack == "trigger":
            db.execute("CREATE TRIGGER unexpected AFTER DELETE ON items BEGIN DELETE FROM jobs; END")
        elif attack == "foreign-key":
            db.execute("UPDATE items SET job_id='missing-job'")
    if attack == "missing-media":
        (source / "jobs" / job["id"] / "crops" / "cutout.png").unlink()
    archive = tmp_path / "invalid.zip"
    assert_failure(None, backup_data, source, archive, mode="local")
    assert not archive.exists()
    assert store.item(item["id"]) is not None
    assert not list(tmp_path.glob(".rewear-backup-*"))


def test_restore_rechecks_database_even_when_archive_hashes_were_recomputed(local, tmp_path):
    source, _, _, _ = local
    archive = tmp_path / "attack.zip"
    backup_data(source, archive, mode="local")
    entries = read_archive(archive)
    modified = tmp_path / "modified.sqlite3"
    modified.write_bytes(next(data for name, data in entries if name == "data/wardrobe.sqlite3"))
    with sqlite3.connect(modified) as db:
        db.execute("PRAGMA user_version=88")
    payload = modified.read_bytes()
    entries = [(name, payload if name == "data/wardrobe.sqlite3" else value) for name, value in entries]

    def update(manifest):
        for record in manifest["files"]:
            if record["path"] == "wardrobe.sqlite3":
                record["size"] = len(payload)
                record["sha256"] = hashlib.sha256(payload).hexdigest()
        manifest["total_size"] = sum(record["size"] for record in manifest["files"])

    rewrite(archive, mutate_manifest(entries, update))
    assert_failure("unsupported_data", restore_data, archive, tmp_path / "destination")
    assert not (tmp_path / "destination").exists()


@pytest.mark.parametrize("link_kind", ["file", "directory", "hardlink", "database-wal", "server-lock"])
def test_source_links_cannot_include_external_content(local, tmp_path, link_kind):
    source, store, job, _ = local
    outside = tmp_path / "outside"
    outside.mkdir()
    private = outside / "private.txt"
    private.write_bytes(b"private unrelated data")
    parent = source / "jobs" / job["id"]
    if link_kind == "file":
        (parent / "linked.jpg").symlink_to(private)
    elif link_kind == "directory":
        (parent / "linked").symlink_to(outside, target_is_directory=True)
    elif link_kind == "hardlink":
        os.link(private, parent / "linked.jpg")
    elif link_kind == "database-wal":
        Path(str(store.path) + "-wal").symlink_to(private)
    else:
        (source / ".server.lock").symlink_to(private)
    assert_failure(None, backup_data, source, tmp_path / "unsafe.zip", mode="local")
    assert private.read_bytes() == b"private unrelated data"
    assert not (tmp_path / "unsafe.zip").exists()


def test_source_change_during_copy_is_detected(local, tmp_path, monkeypatch):
    source, _, job, _ = local
    original = backup._media_inventory
    calls = 0

    def mutate_after_copy(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            (source / "jobs" / job["id"] / "source.jpg").write_bytes(b"changed source")
        return original(*args)

    monkeypatch.setattr(backup, "_media_inventory", mutate_after_copy)
    assert_failure("source_changed", backup_data, source, tmp_path / "changed.zip", mode="local")
    assert not (tmp_path / "changed.zip").exists()


def test_restore_publish_failure_leaves_existing_empty_destination(local, tmp_path, monkeypatch):
    source, _, _, _ = local
    archive, destination = tmp_path / "valid.zip", tmp_path / "destination"
    backup_data(source, archive, mode="local")
    destination.mkdir()

    def fail(*args):
        raise OSError("private filesystem details")

    monkeypatch.setattr(backup.os, "rename", fail)
    error = assert_failure(None, restore_data, archive, destination)
    assert "private" not in repr(error) + str(error)
    assert destination.is_dir() and list(destination.iterdir()) == []
    assert not list(tmp_path.glob(".rewear-restore-*"))


@pytest.mark.parametrize("limit", ["MAX_FILE_BYTES", "MAX_TOTAL_BYTES", "MAX_FILES", "MAX_MANIFEST_BYTES"])
def test_restore_capacity_bounds_reject_without_materializing_target(local, tmp_path, monkeypatch, limit):
    source, _, _, _ = local
    archive = tmp_path / "valid.zip"
    backup_data(source, archive, mode="local")
    monkeypatch.setattr(backup, limit, 1)
    assert_failure(None, restore_data, archive, tmp_path / "destination")
    assert not (tmp_path / "destination").exists()


def test_central_directory_count_is_bounded_before_zipfile_parsing(local, tmp_path, monkeypatch):
    source, _, _, _ = local
    archive = tmp_path / "valid.zip"
    backup_data(source, archive, mode="local")
    content = bytearray(archive.read_bytes())
    fields = list(struct.unpack("<4s4H2LH", content[-22:]))
    fields[3] = fields[4] = backup.MAX_FILES + 2
    content[-22:] = struct.pack("<4s4H2LH", *fields)
    archive.write_bytes(content)

    def must_not_parse(*args, **kwargs):
        pytest.fail("unbounded central directory reached ZipFile")

    monkeypatch.setattr(backup.zipfile, "ZipFile", must_not_parse)
    assert_failure("limit_exceeded", restore_data, archive, tmp_path / "destination")


def test_zip64_end_record_with_non_sentinel_classic_offsets_roundtrips(local, tmp_path, monkeypatch):
    source, _, _, _ = local
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 64)
    archive, destination = tmp_path / "zip64.zip", tmp_path / "result"
    manifest = backup_data(source, archive, mode="local")
    assert b"PK\x06\x06" in archive.read_bytes()[-100:]
    assert restore_data(archive, destination) == manifest


def test_orphan_hosted_tenant_is_rejected(tmp_path):
    source = tmp_path / "hosted"
    AccountStore(source / "control")
    seed(source / "tenants" / ("f" * 32))
    assert_failure("integrity_failed", backup_data, source, tmp_path / "orphan.zip", mode="hosted")
    assert not (tmp_path / "orphan.zip").exists()
