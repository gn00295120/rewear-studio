"""Opt-in server-side accounts, separate from every wardrobe database.

All methods are synchronous; an HTTP caller should run them in a worker thread.
Passwords use the official argon2-cffi PasswordHasher defaults. Session and
recovery secrets contain 256 random bits and only their SHA-256 digests persist.
This module does not send email or assert that an email-like name is verified.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import HashingError, InvalidHashError, VerificationError

SCHEMA_VERSION = 1
APPLICATION_ID = 0x52574143
DEFAULT_SESSION_TTL = 8 * 60 * 60
_USERNAME = re.compile(r"[A-Za-z0-9._@+\-]{3,80}\Z", re.ASCII)
_SECRET = re.compile(r"[A-Za-z0-9_\-]{43}\Z", re.ASCII)
_ERRORS = {
    "invalid_input": (422, "帳號資料格式不符。"),
    "authentication_failed": (401, "無法驗證提供的登入或復原資料。"),
    "register_failed": (409, "無法建立帳號，請檢查資料或使用登入／復原功能。"),
    "capacity_reached": (409, "目前無法建立更多帳號。"),
    "storage_unavailable": (503, "帳號服務暫時無法使用。"),
    "account_error": (400, "無法完成帳號操作。"),
}


class AccountError(Exception):
    """Only allowlisted, non-identifying error text may reach the API or logs."""

    def __init__(self, code: str = "authentication_failed"):
        self.code = code if isinstance(code, str) and code in _ERRORS else "account_error"
        self.status_code, self.message = _ERRORS[self.code]
        self.status = self.status_code
        super().__init__(self.message)

    def __repr__(self) -> str:
        return f"AccountError({self.code!r})"


class _SensitiveResult(dict):
    """Still JSON-serializable, but accidental repr/print does not log secrets."""

    def __repr__(self) -> str:
        return "<AccountResult redacted>"

    __str__ = __repr__


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _username(value) -> str | None:
    return value.casefold() if isinstance(value, str) and _USERNAME.fullmatch(value) else None


def _valid_password(value) -> bool:
    if not isinstance(value, str) or not 12 <= len(value) <= 128:
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


class AccountStore:
    """Store at ``directory/accounts.sqlite3``; callers own the private directory.

    ``clock`` returns UNIX seconds. TTL is fixed, not renewed by access. At the
    per-user session limit, creating a session revokes the oldest issued session.
    ``max_accounts`` counts disabled accounts too, since their data remains.
    Explicit display names are 1–60 Unicode characters; an omitted/empty name
    falls back to the complete normalized username (which can be 80 characters).
    """

    def __init__(
        self,
        directory: Path,
        *,
        clock: Callable[[], float] = time.time,
        session_ttl: float = DEFAULT_SESSION_TTL,
        max_sessions_per_user: int = 10,
        timeout: float = 10,
        max_accounts: int | None = None,
    ):
        if (
            not callable(clock)
            or isinstance(session_ttl, bool)
            or not isinstance(session_ttl, (int, float))
            or not math.isfinite(session_ttl)
            or session_ttl <= 0
            or type(max_sessions_per_user) is not int
            or max_sessions_per_user < 1
            or isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
            or (max_accounts is not None and (type(max_accounts) is not int or max_accounts < 1))
        ):
            raise AccountError("invalid_input")
        self.path = Path(directory) / "accounts.sqlite3"
        self._clock = clock
        self._ttl = float(session_ttl)
        self._max_sessions = max_sessions_per_user
        self._timeout = float(timeout)
        self._max_accounts = max_accounts
        self._hasher = PasswordHasher()
        self._dummy_hash = self._hash_password(secrets.token_urlsafe(32))
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.path.is_symlink():
                raise AccountError("storage_unavailable")
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        except OSError:
            raise AccountError("storage_unavailable") from None
        with self._transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            application = db.execute("PRAGMA application_id").fetchone()[0]
            if version == 0 and application == 0:
                existing = db.execute(
                    "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                ).fetchone()
                if existing:
                    raise AccountError("storage_unavailable")
                for statement in (
                    """CREATE TABLE users (
                        id TEXT PRIMARY KEY,
                        username TEXT NOT NULL UNIQUE,
                        display_name TEXT NOT NULL,
                        password_hash TEXT NOT NULL,
                        recovery_hash TEXT NOT NULL,
                        credential_version INTEGER NOT NULL DEFAULT 1,
                        disabled INTEGER NOT NULL DEFAULT 0 CHECK(disabled IN (0,1)),
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )""",
                    """CREATE TABLE sessions (
                        token_hash TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        credential_version INTEGER NOT NULL,
                        csrf_token TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        expires_at REAL NOT NULL
                    )""",
                    "CREATE INDEX sessions_by_user ON sessions(user_id,created_at)",
                    "CREATE INDEX sessions_by_expiry ON sessions(expires_at)",
                ):
                    db.execute(statement)
                db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION or application != APPLICATION_ID:
                raise AccountError("storage_unavailable")
            self._cleanup(db, self._now())
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")

    def __repr__(self) -> str:
        return "<AccountStore>"

    @contextmanager
    def _connection(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=self._timeout, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            yield db
        except (sqlite3.Error, OSError):
            raise AccountError("storage_unavailable") from None
        finally:
            if db is not None:
                db.close()

    @contextmanager
    def _transaction(self, *, write: bool = True):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.rollback()
                raise

    def _now(self) -> float:
        try:
            value = float(self._clock())
            if not math.isfinite(value):
                raise ValueError
            _iso(value + self._ttl)
            return value
        except Exception:
            raise AccountError("storage_unavailable") from None

    def _hash_password(self, password: str) -> str:
        try:
            return self._hasher.hash(password)
        except (HashingError, UnicodeError):
            raise AccountError("storage_unavailable") from None

    def _verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError, UnicodeError):
            return False

    @staticmethod
    def _public(user: sqlite3.Row) -> dict:
        return {
            "id": user["id"],
            "username": user["username"],
            "display_name": user["display_name"],
            "created_at": _iso(user["created_at"]),
            "disabled": bool(user["disabled"]),
        }

    @staticmethod
    def _cleanup(db, timestamp: float):
        db.execute("DELETE FROM sessions WHERE expires_at<=?", (timestamp,))

    def _register(self, username: str, password: str, display_name: str, *, issue_session: bool) -> dict:
        normalized = _username(username)
        if normalized is None or not _valid_password(password) or not isinstance(display_name, str):
            raise AccountError("invalid_input")
        if display_name:
            if not 1 <= len(display_name) <= 60:
                raise AccountError("invalid_input")
            try:
                display_name.encode("utf-8")
            except UnicodeError:
                raise AccountError("invalid_input") from None
        else:
            display_name = normalized
        password_hash = self._hash_password(password)
        recovery_code = secrets.token_urlsafe(32)
        user_id = uuid4().hex
        with self._transaction() as db:
            if self._max_accounts is not None:
                count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                if count >= self._max_accounts:
                    raise AccountError("capacity_reached")
            timestamp = self._now()
            try:
                db.execute(
                    """INSERT INTO users
                       (id,username,display_name,password_hash,recovery_hash,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        user_id,
                        normalized,
                        display_name,
                        password_hash,
                        _digest(recovery_code),
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError:
                raise AccountError("register_failed") from None
            user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if issue_session:
                return _SensitiveResult(**self._insert_session(db, user), recovery_code=recovery_code)
        return _SensitiveResult(user=self._public(user), recovery_code=recovery_code)

    def register(self, username: str, password: str, display_name: str = "") -> dict:
        return self._register(username, password, display_name, issue_session=False)

    def register_session(self, username: str, password: str, display_name: str = "") -> dict:
        """Create the account, recovery code and initial session in one transaction."""
        return self._register(username, password, display_name, issue_session=True)

    def _authentication_snapshot(self, username, password):
        normalized = _username(username)
        with self._transaction(write=False) as db:
            user = db.execute("SELECT * FROM users WHERE username=?", (normalized,)).fetchone()
        valid_password = _valid_password(password)
        password_hash = user["password_hash"] if user is not None and valid_password else self._dummy_hash
        verified = self._verify(password_hash, password if valid_password else "invalid-password")
        if not verified or user is None or user["disabled"] or not valid_password:
            raise AccountError("authentication_failed")
        return user

    def _authenticate(self, username, password, *, issue_session: bool):
        snapshot = self._authentication_snapshot(username, password)
        replacement = (
            self._hash_password(password)
            if self._hasher.check_needs_rehash(snapshot["password_hash"])
            else None
        )
        with self._transaction() as db:
            user = db.execute("SELECT * FROM users WHERE id=?", (snapshot["id"],)).fetchone()
            self._require_version(user, snapshot)
            if replacement is not None:
                db.execute(
                    "UPDATE users SET password_hash=? WHERE id=? AND password_hash=?",
                    (replacement, user["id"], snapshot["password_hash"]),
                )
            return self._insert_session(db, user) if issue_session else self._public(user)

    @staticmethod
    def _require_version(user, snapshot):
        if user is None or user["disabled"] or user["credential_version"] != snapshot["credential_version"]:
            raise AccountError("authentication_failed")

    def authenticate(self, username: str, password: str) -> dict:
        return self._authenticate(username, password, issue_session=False)

    def authenticate_session(self, username: str, password: str) -> dict:
        """Authenticate and issue a session atomically against credential changes."""
        return self._authenticate(username, password, issue_session=True)

    def _insert_session(self, db, user) -> dict:
        if user is None or user["disabled"]:
            raise AccountError("authentication_failed")
        timestamp = self._now()
        self._cleanup(db, timestamp)
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = timestamp + self._ttl
        db.execute(
            """INSERT INTO sessions
               (token_hash,user_id,credential_version,csrf_token,created_at,expires_at)
               VALUES(?,?,?,?,?,?)""",
            (_digest(token), user["id"], user["credential_version"], csrf_token, timestamp, expires_at),
        )
        db.execute(
            """DELETE FROM sessions WHERE token_hash IN (
                SELECT token_hash FROM sessions WHERE user_id=?
                ORDER BY rowid DESC LIMIT -1 OFFSET ?
            )""",
            (user["id"], self._max_sessions),
        )
        return _SensitiveResult(
            token=token, csrf_token=csrf_token, user=self._public(user), expires_at=_iso(expires_at)
        )

    def create_session(self, user_id: str) -> dict:
        """For trusted callers; password-based login should use authenticate_session."""
        with self._transaction() as db:
            user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return self._insert_session(db, user)

    def get_session(self, token: str) -> dict | None:
        if not isinstance(token, str) or not _SECRET.fullmatch(token):
            return None
        with self._transaction() as db:
            self._cleanup(db, self._now())
            session = db.execute("SELECT * FROM sessions WHERE token_hash=?", (_digest(token),)).fetchone()
            if session is None:
                return None
            user = db.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
            if (
                user is None
                or user["disabled"]
                or user["credential_version"] != session["credential_version"]
            ):
                db.execute("DELETE FROM sessions WHERE token_hash=?", (_digest(token),))
                return None
            return _SensitiveResult(
                csrf_token=session["csrf_token"],
                user=self._public(user),
                expires_at=_iso(session["expires_at"]),
            )

    def revoke_session(self, token: str) -> None:
        if not isinstance(token, str) or not _SECRET.fullmatch(token):
            return
        with self._transaction() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (_digest(token),))

    def _change_password(self, user_id, current_password, new_password, *, issue_session):
        if not _valid_password(new_password):
            raise AccountError("invalid_input")
        with self._transaction(write=False) as db:
            snapshot = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        valid_current = _valid_password(current_password)
        password_hash = (
            snapshot["password_hash"] if snapshot is not None and valid_current else self._dummy_hash
        )
        verified = self._verify(password_hash, current_password if valid_current else "invalid-password")
        if not verified or snapshot is None or snapshot["disabled"] or not valid_current:
            raise AccountError("authentication_failed")
        replacement = self._hash_password(new_password)
        with self._transaction() as db:
            user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            self._require_version(user, snapshot)
            db.execute(
                """UPDATE users SET password_hash=?,credential_version=credential_version+1,
                   updated_at=? WHERE id=?""",
                (replacement, self._now(), user_id),
            )
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            if issue_session:
                updated = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
                return self._insert_session(db, updated)
        return None

    def change_password(self, user_id: str, current_password: str, new_password: str) -> None:
        self._change_password(user_id, current_password, new_password, issue_session=False)

    def change_password_session(self, user_id: str, current_password: str, new_password: str) -> dict:
        """Replace all sessions with one new session in the same password transaction."""
        return self._change_password(user_id, current_password, new_password, issue_session=True)

    def _recover(self, username, recovery_code, new_password, *, issue_session):
        if not _valid_password(new_password):
            raise AccountError("invalid_input")
        normalized = _username(username)
        valid_code = isinstance(recovery_code, str) and _SECRET.fullmatch(recovery_code)
        code_hash = _digest(recovery_code) if valid_code else "0" * 64
        replacement = self._hash_password(new_password)
        new_code = secrets.token_urlsafe(32)
        with self._transaction() as db:
            user = db.execute("SELECT * FROM users WHERE username=?", (normalized,)).fetchone()
            expected = user["recovery_hash"] if user is not None else "f" * 64
            matches = secrets.compare_digest(code_hash, expected)
            if not matches or not valid_code or user is None or user["disabled"]:
                raise AccountError("authentication_failed")
            db.execute(
                """UPDATE users SET password_hash=?,recovery_hash=?,
                   credential_version=credential_version+1,updated_at=? WHERE id=?""",
                (replacement, _digest(new_code), self._now(), user["id"]),
            )
            db.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
            updated = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
            if issue_session:
                return _SensitiveResult(session=self._insert_session(db, updated), recovery_code=new_code)
            return _SensitiveResult(user=self._public(updated), recovery_code=new_code)

    def recover(self, username: str, recovery_code: str, new_password: str) -> dict:
        return self._recover(username, recovery_code, new_password, issue_session=False)

    def recover_session(self, username: str, recovery_code: str, new_password: str) -> dict:
        return self._recover(username, recovery_code, new_password, issue_session=True)

    def get_user(self, user_id: str) -> dict | None:
        with self._transaction(write=False) as db:
            user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return self._public(user) if user is not None else None

    def count_users(self) -> int:
        with self._transaction(write=False) as db:
            return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def disable_user(self, user_id: str) -> None:
        with self._transaction() as db:
            db.execute(
                """UPDATE users SET disabled=1,credential_version=credential_version+1,
                   updated_at=? WHERE id=? AND disabled=0""",
                (self._now(), user_id),
            )
            db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
