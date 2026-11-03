"""Versioned, offline backups. Archives contain private user data, not configuration.

The cooperative server lock is mandatory; this is not an online snapshot tool.
ZIP_STORED is intentional: image/video data is already compressed and accepting
only this format removes decompression amplification. Checksums detect damage,
not a malicious archive author who can replace both data and the manifest.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import struct
import tempfile
import time
import zipfile
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .accounts import APPLICATION_ID, SCHEMA_VERSION

FORMAT = "rewear-offline-backup"
VERSION = 1
MAX_FILES = 50_000
MAX_TOTAL_BYTES = 20 * 1024**3
MAX_FILE_BYTES = 2 * 1024**3
MAX_MANIFEST_BYTES = 8 * 1024**2
MAX_CENTRAL_BYTES = 64 * 1024**2
SQLITE_TIMEOUT = 2
SQLITE_BACKUP_SECONDS = 120
_CHUNK = 1024 * 1024
_ID = re.compile(r"[a-f0-9]{32}\Z")
_HEX = re.compile(r"[a-f0-9]{64}\Z")
_PATH = re.compile(r"[A-Za-z0-9._/\-]+\Z")
_MEDIA = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".webm", ".m4v"}
_METADATA = {"segments.json", "outfit-plan.json"}
_EXCLUDED = {".server.lock", ".trash", "trash", ".tmp", "tmp", "temp", "__pycache__"}
_WARDROBE_COLUMNS = {
    "jobs": ("id", "payload"),
    "items": ("id", "job_id", "payload"),
    "audit": ("id", "entity_id", "action", "created_at", "payload"),
    "batches": ("id", "client_id", "payload"),
    "garment_groups": ("id", "payload"),
    "garment_members": ("item_id", "group_id"),
}
_ACCOUNT_COLUMNS = {
    "users": (
        "id",
        "username",
        "display_name",
        "password_hash",
        "recovery_hash",
        "credential_version",
        "disabled",
        "created_at",
        "updated_at",
    ),
    "sessions": ("token_hash", "user_id", "credential_version", "csrf_token", "created_at", "expires_at"),
}
_DDL = {
    "wardrobe": [
        "CREATE TABLE jobs(id TEXT PRIMARY KEY,payload TEXT NOT NULL)",
        "CREATE TABLE items(id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,payload TEXT NOT NULL)",
        "CREATE TABLE audit(id INTEGER PRIMARY KEY,entity_id TEXT NOT NULL,action TEXT NOT NULL,created_at TEXT NOT NULL,payload TEXT NOT NULL)",
        "CREATE TABLE batches(id TEXT PRIMARY KEY,client_id TEXT UNIQUE,payload TEXT NOT NULL)",
        "CREATE TABLE garment_groups(id TEXT PRIMARY KEY,payload TEXT NOT NULL)",
        "CREATE TABLE garment_members(item_id TEXT PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,group_id TEXT NOT NULL REFERENCES garment_groups(id) ON DELETE CASCADE)",
        "CREATE INDEX items_by_job ON items(job_id)",
        "CREATE INDEX members_by_group ON garment_members(group_id)",
    ],
    "accounts": [
        """CREATE TABLE users(id TEXT PRIMARY KEY,username TEXT NOT NULL UNIQUE,
           display_name TEXT NOT NULL,password_hash TEXT NOT NULL,recovery_hash TEXT NOT NULL,
           credential_version INTEGER NOT NULL DEFAULT 1,
           disabled INTEGER NOT NULL DEFAULT 0 CHECK(disabled IN (0,1)),
           created_at REAL NOT NULL,updated_at REAL NOT NULL)""",
        """CREATE TABLE sessions(token_hash TEXT PRIMARY KEY,
           user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
           credential_version INTEGER NOT NULL,csrf_token TEXT NOT NULL,
           created_at REAL NOT NULL,expires_at REAL NOT NULL)""",
        "CREATE INDEX sessions_by_user ON sessions(user_id,created_at)",
        "CREATE INDEX sessions_by_expiry ON sessions(expires_at)",
    ],
}
_ERRORS = {
    "invalid_path": "The backup or restore path does not meet requirements.",
    "online": "The data directory is still in use by a service or another backup operation. Please stop the service first.",
    "destination_exists": "The destination already exists or is not an empty directory. Existing data was not overwritten.",
    "unsupported_data": "The data contains unsupported structures or database versions.",
    "unsafe_entry": "The data contains unsafe paths, links, or file types.",
    "limit_exceeded": "The backup data exceeds the format capacity limit.",
    "invalid_archive": "The backup archive format is invalid or corrupted.",
    "integrity_failed": "Data verification, database integrity, or media reference check failed.",
    "source_changed": "Source data changed during backup. No backup was created.",
    "storage_error": "Unable to complete offline backup or restore.",
}


class BackupError(Exception):
    def __init__(self, code="storage_error"):
        self.code = code if isinstance(code, str) and code in _ERRORS else "storage_error"
        self.message = _ERRORS[self.code]
        super().__init__(self.message)

    def __repr__(self):
        return f"BackupError({self.code!r})"


def _relative(value: str) -> PurePosixPath:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or not _PATH.fullmatch(value)
        or value.startswith("/")
        or any(part in {"", ".", ".."} or len(part) > 255 for part in value.split("/"))
    ):
        raise BackupError("unsafe_entry")
    return PurePosixPath(value)


def _kind(value: str, mode: str) -> str:
    path = _relative(value)
    parts = path.parts
    if mode == "local":
        if parts == ("wardrobe.sqlite3",):
            return "wardrobe"
        media = parts
    elif mode == "hosted":
        if parts == ("control", "accounts.sqlite3"):
            return "accounts"
        if len(parts) < 3 or parts[0] != "tenants" or not _ID.fullmatch(parts[1]):
            raise BackupError("unsupported_data")
        if parts[2:] == ("wardrobe.sqlite3",):
            return "wardrobe"
        media = parts[2:]
    else:
        raise BackupError("unsupported_data")
    if (
        len(media) < 3
        or media[0] != "jobs"
        or any(part.startswith(".") or part in _EXCLUDED for part in media)
        or not (path.suffix.lower() in _MEDIA or path.name in _METADATA)
    ):
        raise BackupError("unsupported_data")
    return "media"


def _fingerprint(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _regular(path: Path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise BackupError("unsafe_entry")
    return info


@contextmanager
def _lock(directory: Path):
    if directory.is_symlink() or not directory.is_dir():
        raise BackupError("unsafe_entry")
    fd = os.open(directory / ".server.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
            raise BackupError("unsafe_entry")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("online") from None
        yield
    finally:
        os.close(fd)


def _databases(source: Path, mode: str):
    if mode == "local":
        return [("wardrobe.sqlite3", "wardrobe")]
    if mode != "hosted":
        raise BackupError("unsupported_data")
    control, tenants = source / "control", source / "tenants"
    if any(path.is_symlink() or not path.is_dir() for path in (control, tenants)):
        raise BackupError("unsafe_entry")
    databases = [("control/accounts.sqlite3", "accounts")]
    for path in sorted(tenants.iterdir()):
        if not _ID.fullmatch(path.name) or path.is_symlink() or not path.is_dir():
            raise BackupError("unsupported_data")
        databases.append((f"tenants/{path.name}/wardrobe.sqlite3", "wardrobe"))
        if len(databases) > MAX_FILES:
            raise BackupError("limit_exceeded")
    return databases


def _media_inventory(source: Path, databases):
    files, seen, visited, total = {}, set(), 0, 0
    for name, kind in databases:
        if kind != "wardrobe":
            continue
        jobs = source / Path(name).parent / "jobs"
        if not jobs.exists() and not jobs.is_symlink():
            continue
        pending = [jobs]
        while pending:
            directory = pending.pop()
            if directory.is_symlink() or not directory.is_dir():
                raise BackupError("unsafe_entry")
            for entry in directory.iterdir():
                visited += 1
                if visited > MAX_FILES * 2:
                    raise BackupError("limit_exceeded")
                if entry.name in _EXCLUDED or entry.name.endswith((".tmp", ".part", ".lock")):
                    continue
                if entry.is_symlink():
                    raise BackupError("unsafe_entry")
                relative = entry.relative_to(source).as_posix()
                _relative(relative)
                if entry.is_dir():
                    if len(entry.relative_to(source).parts) > 24:
                        raise BackupError("limit_exceeded")
                    if entry.name.startswith("."):
                        continue
                    pending.append(entry)
                    continue
                if entry.name.startswith("."):
                    continue
                _kind(relative, "local" if name == "wardrobe.sqlite3" else "hosted")
                info = _regular(entry)
                if relative.casefold() in seen:
                    raise BackupError("unsafe_entry")
                seen.add(relative.casefold())
                files[relative] = _fingerprint(info)
                total += info.st_size
                if (
                    len(files) + len(databases) > MAX_FILES
                    or info.st_size > MAX_FILE_BYTES
                    or total > MAX_TOTAL_BYTES
                ):
                    raise BackupError("limit_exceeded")
    return files


def _readonly(path: Path):
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=SQLITE_TIMEOUT)
    db.execute("PRAGMA query_only=ON")
    db.execute("PRAGMA trusted_schema=OFF")
    return db


def _check_database(path: Path, kind: str):
    with closing(_readonly(path)) as db:
        try:
            expected = _ACCOUNT_COLUMNS if kind == "accounts" else _WARDROBE_COLUMNS
            expected_version = SCHEMA_VERSION if kind == "accounts" else 0
            expected_id = APPLICATION_ID if kind == "accounts" else 0
            if (
                db.execute("PRAGMA user_version").fetchone()[0] != expected_version
                or db.execute("PRAGMA application_id").fetchone()[0] != expected_id
            ):
                raise BackupError("unsupported_data")
            definitions = db.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
            tables = {name for typ, name, _ in definitions if typ == "table"}
            if tables != set(expected) or any(typ not in {"table", "index"} for typ, _, _ in definitions):
                raise BackupError("unsupported_data")
            if any(
                typ == "table" and not sql.upper().startswith("CREATE TABLE ") for typ, _, sql in definitions
            ):
                raise BackupError("unsupported_data")

            def normalize(sql):
                return re.sub(r"\s+", "", sql).casefold()

            if {normalize(sql) for _, _, sql in definitions} != {normalize(sql) for sql in _DDL[kind]}:
                raise BackupError("unsupported_data")
            for table, columns in expected.items():
                actual = tuple(row[1] for row in db.execute(f'PRAGMA table_info("{table}")'))
                if actual != columns:
                    raise BackupError("unsupported_data")
            if (
                db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
                or db.execute("PRAGMA foreign_key_check").fetchone() is not None
            ):
                raise BackupError("integrity_failed")
            if kind == "accounts":
                if db.execute("SELECT count(*) FROM users").fetchone()[0] > MAX_FILES:
                    raise BackupError("limit_exceeded")
                ids = {row[0] for row in db.execute("SELECT id FROM users")}
                if any(not isinstance(ident, str) or not _ID.fullmatch(ident) for ident in ids):
                    raise BackupError("integrity_failed")
                return ids
            known_jobs = {row[0] for row in db.execute("SELECT id FROM jobs")}
            for table in ("jobs", "items"):
                for (payload,) in db.execute(f"SELECT payload FROM {table}"):
                    if not isinstance(payload, str) or len(payload) > MAX_MANIFEST_BYTES:
                        raise BackupError("limit_exceeded")
                    value = json.loads(payload)
                    if not isinstance(value, dict):
                        raise BackupError("integrity_failed")
                    _check_media_urls(value, path.parent, known_jobs=known_jobs)
            return set()
        except (json.JSONDecodeError, RecursionError):
            raise BackupError("integrity_failed") from None


def _check_media_urls(value, root: Path, depth=0, *, known_jobs=None, deleted_reference=False):
    if depth > 64:
        raise BackupError("limit_exceeded")
    if isinstance(value, dict):
        for key, child in value.items():
            if (
                key
                in {
                    "reference_manifest",
                    "image_reference_manifest",
                    "preview_reference_manifest",
                    "candidate_reference_manifest",
                    "preview_candidates_reference_manifest",
                }
                and child is not None
            ):
                from .reconstruction_sources import ReferenceManifest

                try:
                    manifest = ReferenceManifest.model_validate(child)
                except (ValueError, TypeError):
                    raise BackupError("integrity_failed") from None
                for source in manifest.sources:
                    # A completed image keeps its original provenance after a
                    # separate reference job is deliberately deleted. This is
                    # not permission to ignore broken media of existing jobs.
                    _check_media_urls(
                        source.model_dump(),
                        root,
                        depth + 1,
                        known_jobs=known_jobs,
                        deleted_reference=known_jobs is not None and source.job_id not in known_jobs,
                    )
                continue
            if key.endswith("_url") and isinstance(child, str) and child.startswith("/media/"):
                relative = _relative(child.removeprefix("/media/"))
                target = root / "jobs" / relative
                if target.is_symlink() or (
                    not target.is_file() and not (deleted_reference and not target.exists())
                ):
                    raise BackupError("integrity_failed")
            _check_media_urls(child, root, depth + 1, known_jobs=known_jobs)
    elif isinstance(value, list):
        for child in value:
            _check_media_urls(child, root, depth + 1, known_jobs=known_jobs)


def _check_bundle(root: Path, databases, mode):
    accounts = None
    for name, kind in databases:
        result = _check_database(root / name, kind)
        if kind == "accounts":
            accounts = result
    if mode == "hosted" and (
        accounts is None
        or any(PurePosixPath(name).parts[1] not in accounts for name, kind in databases if kind == "wardrobe")
    ):
        raise BackupError("integrity_failed")


def _backup_sqlite(source: Path, destination: Path, *, max_bytes=MAX_FILE_BYTES):
    if _regular(source).st_size > max_bytes:
        raise BackupError("limit_exceeded")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = source.with_name(source.name + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            _regular(sidecar)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    deadline = time.monotonic() + SQLITE_BACKUP_SECONDS

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise BackupError("storage_error")
        if total * page_size > max_bytes:
            raise BackupError("limit_exceeded")

    with closing(_readonly(source)) as original, closing(sqlite3.connect(destination)) as copied:
        page_size = original.execute("PRAGMA page_size").fetchone()[0]
        if original.execute("PRAGMA page_count").fetchone()[0] * page_size > max_bytes:
            raise BackupError("limit_exceeded")
        original.backup(copied, pages=256, progress=progress, sleep=0.05)
        copied.execute("PRAGMA journal_mode=DELETE")


@contextmanager
def _open_source(root: Path, name: str):
    # Open every ancestor relative to directory descriptors, never follow links.
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    file_fd = None
    try:
        parts = _relative(name).parts
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BackupError("unsafe_entry")
        with os.fdopen(file_fd, "rb") as stream:
            file_fd = None
            yield stream, info
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory)


def _hash_file(path: Path):
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _zip_info(name):
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.compress_type = zipfile.ZIP_STORED
    return info


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def backup_data(source: Path, archive: Path, *, mode: str) -> dict:
    """Snapshot an offline local or hosted root; never replace an archive."""
    try:
        source, archive = Path(source).expanduser().absolute(), Path(archive).expanduser().absolute()
        if source.is_symlink() or not source.is_dir() or archive.is_symlink():
            raise BackupError("invalid_path")
        source = source.resolve()
        archive = archive.parent.resolve() / archive.name
        if archive.is_relative_to(source) or not archive.parent.is_dir():
            raise BackupError("invalid_path")
        if archive.exists():
            raise BackupError("destination_exists")
        with ExitStack() as stack:
            stack.enter_context(_lock(source))
            databases = _databases(source, mode)
            if mode == "hosted":
                for name, kind in databases:
                    if kind == "wardrobe":
                        stack.enter_context(_lock((source / name).parent))
            inventory = _media_inventory(source, databases)
            temporary = stack.enter_context(
                tempfile.TemporaryDirectory(prefix=".rewear-backup-", dir=archive.parent)
            )
            staging = Path(temporary) / "data"
            staging.mkdir(mode=0o700)
            staged_total = sum(fingerprint[2] for fingerprint in inventory.values())
            for name, kind in databases:
                _backup_sqlite(
                    source / name,
                    staging / name,
                    max_bytes=min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - staged_total),
                )
                staged_total += (staging / name).stat().st_size
            for name, fingerprint in inventory.items():
                target = staging / name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with _open_source(source, name) as (stream, info):
                    if _fingerprint(info) != fingerprint:
                        raise BackupError("source_changed")
                    with target.open("xb") as output:
                        os.chmod(target, 0o600)
                        remaining = info.st_size
                        while remaining:
                            chunk = stream.read(min(_CHUNK, remaining))
                            if not chunk:
                                raise BackupError("source_changed")
                            output.write(chunk)
                            remaining -= len(chunk)
                        if stream.read(1):
                            raise BackupError("source_changed")
                    if _fingerprint(os.fstat(stream.fileno())) != fingerprint:
                        raise BackupError("source_changed")
            if inventory != _media_inventory(source, databases) or databases != _databases(source, mode):
                raise BackupError("source_changed")
            _check_bundle(staging, databases, mode)
            files, total = [], 0
            for name in sorted([name for name, _ in databases] + list(inventory)):
                size, digest = _hash_file(staging / name)
                total += size
                if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                    raise BackupError("limit_exceeded")
                files.append({"path": name, "size": size, "sha256": digest})
            manifest = {
                "format": FORMAT,
                "version": VERSION,
                "mode": mode,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "total_size": total,
                "files": files,
                "databases": [
                    {
                        "path": name,
                        "kind": kind,
                        "schema_version": SCHEMA_VERSION if kind == "accounts" else 0,
                    }
                    for name, kind in databases
                ],
            }
            encoded = json.dumps(manifest, ensure_ascii=True, separators=(",", ":")).encode()
            if len(encoded) > MAX_MANIFEST_BYTES:
                raise BackupError("limit_exceeded")
            pending = Path(temporary) / "archive.zip"
            with pending.open("xb") as output:
                os.chmod(pending, 0o600)
                with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as bundle:
                    bundle.writestr(_zip_info("manifest.json"), encoded)
                    for record in files:
                        with (
                            (staging / record["path"]).open("rb") as stream,
                            bundle.open(_zip_info("data/" + record["path"]), "w", force_zip64=True) as target,
                        ):
                            shutil.copyfileobj(stream, target, _CHUNK)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(pending, archive)
            except FileExistsError:
                raise BackupError("destination_exists") from None
            _sync_directory(archive.parent)
            return manifest
    except BackupError:
        raise
    except (OSError, sqlite3.Error, ValueError, zipfile.BadZipFile, RuntimeError):
        raise BackupError("storage_error") from None


def _zip_bounds(stream):
    """Bound the central directory before ZipFile allocates/parses its records."""
    size = os.fstat(stream.fileno()).st_size
    if size < 22 or size > MAX_TOTAL_BYTES + MAX_MANIFEST_BYTES + MAX_CENTRAL_BYTES * 2:
        raise BackupError("limit_exceeded")
    stream.seek(-22, os.SEEK_END)
    signature, disk, central_disk, entries_disk, entries, length, offset, comment = struct.unpack(
        "<4s4H2LH", stream.read(22)
    )
    end = size - 22
    if signature != b"PK\x05\x06" or disk or central_disk or comment:
        raise BackupError("invalid_archive")
    stream.seek(max(0, end - 20))
    has_zip64 = stream.read(4) == b"PK\x06\x07"
    if has_zip64 or entries == 65535 or entries_disk == 65535 or length == 0xFFFFFFFF or offset == 0xFFFFFFFF:
        if end < 20:
            raise BackupError("invalid_archive")
        stream.seek(end - 20)
        locator, disk, zip64_offset, disks = struct.unpack("<4sLQL", stream.read(20))
        if locator != b"PK\x06\x07" or disk or disks != 1 or zip64_offset + 56 != end - 20:
            raise BackupError("invalid_archive")
        stream.seek(zip64_offset)
        fields = struct.unpack("<4sQ2H2L4Q", stream.read(56))
        sig, record_size, _, _, disk, central_disk, entries_disk, entries, length, offset = fields
        if sig != b"PK\x06\x06" or record_size != 44 or disk or central_disk:
            raise BackupError("invalid_archive")
        end = zip64_offset
    if entries != entries_disk or entries > MAX_FILES + 1 or length > MAX_CENTRAL_BYTES:
        raise BackupError("limit_exceeded")
    if offset + length != end:
        raise BackupError("invalid_archive")
    stream.seek(0)


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise BackupError("invalid_archive")
        value[key] = item
    return value


def _read_manifest(bundle):
    entries, names, total = bundle.infolist(), set(), 0
    if len(entries) > MAX_FILES + 1:
        raise BackupError("limit_exceeded")
    for entry in entries:
        name = entry.filename
        if name != "manifest.json":
            _relative(name)
            if not name.startswith("data/"):
                raise BackupError("unsafe_entry")
        if name.casefold() in names or entry.orig_filename != name:
            raise BackupError("unsafe_entry")
        names.add(name.casefold())
        if (
            entry.compress_type != zipfile.ZIP_STORED
            or entry.flag_bits & 1
            or stat.S_IFMT(entry.external_attr >> 16) != stat.S_IFREG
            or entry.file_size != entry.compress_size
        ):
            raise BackupError("unsafe_entry")
        limit = MAX_MANIFEST_BYTES if name == "manifest.json" else MAX_FILE_BYTES
        if entry.file_size > limit:
            raise BackupError("limit_exceeded")
        total += entry.file_size
    if total > MAX_TOTAL_BYTES + MAX_MANIFEST_BYTES or "manifest.json" not in names:
        raise BackupError("limit_exceeded")
    manifest = json.loads(bundle.read("manifest.json"), object_pairs_hook=_unique_object)
    if not isinstance(manifest, dict) or set(manifest) != {
        "format",
        "version",
        "mode",
        "created_at",
        "total_size",
        "files",
        "databases",
    }:
        raise BackupError("invalid_archive")
    if manifest["format"] != FORMAT or type(manifest["version"]) is not int or manifest["version"] != VERSION:
        raise BackupError("unsupported_data")
    mode = manifest["mode"]
    if mode not in {"local", "hosted"} or not isinstance(manifest["created_at"], str):
        raise BackupError("unsupported_data")
    records, databases = manifest["files"], manifest["databases"]
    if not isinstance(records, list) or not isinstance(databases, list) or len(records) > MAX_FILES:
        raise BackupError("limit_exceeded")
    expected_names, paths, expected_databases, expected_size = {"manifest.json"}, set(), [], 0
    for record in records:
        if not isinstance(record, dict) or set(record) != {"path", "size", "sha256"}:
            raise BackupError("invalid_archive")
        path, size, digest = record["path"], record["size"], record["sha256"]
        kind = _kind(path, mode)
        if path.casefold() in paths:
            raise BackupError("unsafe_entry")
        paths.add(path.casefold())
        if (
            type(size) is not int
            or not 0 <= size <= MAX_FILE_BYTES
            or not isinstance(digest, str)
            or not _HEX.fullmatch(digest)
        ):
            raise BackupError("invalid_archive")
        expected_size += size
        expected_names.add("data/" + path)
        if kind != "media":
            expected_databases.append(
                {"path": path, "kind": kind, "schema_version": SCHEMA_VERSION if kind == "accounts" else 0}
            )
        if bundle.getinfo("data/" + path).file_size != size:
            raise BackupError("integrity_failed")
    for record in records:
        if _kind(record["path"], mode) == "media":
            parts = PurePosixPath(record["path"]).parts
            owner = "wardrobe.sqlite3" if mode == "local" else "/".join(parts[:2]) + "/wardrobe.sqlite3"
            if owner.casefold() not in paths:
                raise BackupError("integrity_failed")
    if (
        expected_names != {entry.filename for entry in entries}
        or type(manifest["total_size"]) is not int
        or manifest["total_size"] != expected_size
        or expected_size > MAX_TOTAL_BYTES
        or sorted(databases, key=lambda item: item["path"])
        != sorted(expected_databases, key=lambda item: item["path"])
        or (
            mode == "local"
            and expected_databases != [{"path": "wardrobe.sqlite3", "kind": "wardrobe", "schema_version": 0}]
        )
        or (mode == "hosted" and not any(record["kind"] == "accounts" for record in expected_databases))
    ):
        raise BackupError("invalid_archive")
    return manifest


def restore_data(archive: Path, destination: Path) -> dict:
    """Verify everything in private staging, then rename into an absent/empty root."""
    try:
        archive = Path(archive).expanduser().absolute()
        destination = Path(destination).expanduser().absolute()
        if archive.is_symlink() or destination.is_symlink() or not destination.parent.is_dir():
            raise BackupError("invalid_path")
        destination = destination.parent.resolve() / destination.name
        existed = destination.exists()
        initial = destination.stat() if existed else None
        if existed and (not destination.is_dir() or any(destination.iterdir())):
            raise BackupError("destination_exists")
        _regular(archive)
        with (
            archive.open("rb") as stream,
            tempfile.TemporaryDirectory(prefix=".rewear-restore-", dir=destination.parent) as temporary,
        ):
            _zip_bounds(stream)
            staging = Path(temporary) / "data"
            staging.mkdir(mode=0o700)
            with zipfile.ZipFile(stream) as bundle:
                manifest = _read_manifest(bundle)
                for record in manifest["files"]:
                    target = staging / record["path"]
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    digest, size = hashlib.sha256(), 0
                    with bundle.open("data/" + record["path"]) as incoming, target.open("xb") as output:
                        os.chmod(target, 0o600)
                        while chunk := incoming.read(_CHUNK):
                            size += len(chunk)
                            if size > record["size"]:
                                raise BackupError("integrity_failed")
                            digest.update(chunk)
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if size != record["size"] or digest.hexdigest() != record["sha256"]:
                        raise BackupError("integrity_failed")
            databases = [(record["path"], record["kind"]) for record in manifest["databases"]]
            _check_bundle(staging, databases, manifest["mode"])
            if manifest["mode"] == "hosted":
                (staging / "tenants").mkdir(mode=0o700, exist_ok=True)
            for name, kind in databases:
                if kind == "wardrobe":
                    (staging / name).parent.joinpath("jobs").mkdir(mode=0o700, exist_ok=True)
            if destination.is_symlink():
                raise BackupError("destination_exists")
            if existed:
                current = destination.stat()
                if (current.st_dev, current.st_ino) != (initial.st_dev, initial.st_ino) or any(
                    destination.iterdir()
                ):
                    raise BackupError("destination_exists")
            elif destination.exists():
                raise BackupError("destination_exists")
            for directory, _, _ in os.walk(staging, topdown=False):
                _sync_directory(directory)
            os.rename(staging, destination)
            _sync_directory(destination.parent)
            return manifest
    except BackupError:
        raise
    except (
        OSError,
        sqlite3.Error,
        ValueError,
        KeyError,
        TypeError,
        struct.error,
        zipfile.BadZipFile,
        RuntimeError,
    ):
        raise BackupError("invalid_archive") from None
