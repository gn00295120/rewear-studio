"""Isolated account lifecycle tests; no wardrobe data or external services."""

import base64
import hashlib
import json
import os
import sqlite3
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from uuid import UUID

import pytest
from argon2 import PasswordHasher

from wardrobe.accounts import APPLICATION_ID, SCHEMA_VERSION, AccountError, AccountStore

PASSWORD = "Original password 測試 123"
REPLACEMENT = "Replacement password 456"


class Clock:
    def __init__(self):
        self.value = 1_700_000_000.0

    def __call__(self):
        return self.value


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return AccountStore(tmp_path / "accounts", clock=clock)


@pytest.fixture
def registered(store):
    return store.register("Person+tag@EXAMPLE.test", PASSWORD, "衣櫃使用者 🧥")


def database_rows(store, sql, args=()):
    with sqlite3.connect(store.path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(sql, args)]


def assert_error(code, function, *args):
    with pytest.raises(AccountError) as caught:
        function(*args)
    assert caught.value.code == code
    return caught.value


def test_real_argon2_hash_and_safe_public_result(store, registered):
    user = registered["user"]
    assert set(user) == {"id", "username", "display_name", "created_at", "disabled"}
    assert UUID(user["id"]).version == 4
    assert len(user["id"]) == 32 and user["id"].isalnum()
    assert user["username"] == "person+tag@example.test"
    assert user["display_name"] == "衣櫃使用者 🧥"
    assert user["disabled"] is False
    assert store.authenticate("PERSON+TAG@example.TEST", PASSWORD) == user
    row = database_rows(store, "SELECT * FROM users")[0]
    assert row["password_hash"].startswith("$argon2id$")
    assert PasswordHasher().verify(row["password_hash"], PASSWORD)
    assert not PasswordHasher().check_needs_rehash(row["password_hash"])
    assert row["recovery_hash"] == hashlib.sha256(registered["recovery_code"].encode()).hexdigest()
    assert len(base64.urlsafe_b64decode(registered["recovery_code"] + "=")) == 32
    assert not any(key in user for key in ("password_hash", "recovery_hash", "credential_version"))


def test_password_hashes_have_independent_salts(store):
    store.register("person-one", PASSWORD)
    store.register("person-two", PASSWORD)
    rows = database_rows(store, "SELECT password_hash FROM users")
    assert rows[0]["password_hash"] != rows[1]["password_hash"]


@pytest.mark.parametrize(
    "password", ["測" * 12, "密碼🧥" * 42 + "界界", "12345678901\x00suffix", " pass word 12 "]
)
def test_unicode_passwords_are_not_normalized_or_truncated(store, password):
    store.register("unicode-user", password)
    assert store.authenticate("unicode-user", password)["username"] == "unicode-user"
    different = password[:-1] + ("X" if password[-1] != "X" else "Y")
    assert_error("authentication_failed", store.authenticate, "unicode-user", different)
    if "\x00" in password:
        assert_error("authentication_failed", store.authenticate, "unicode-user", password.split("\x00")[0])


@pytest.mark.parametrize(
    "username", ["ab", "a" * 81, " user", "user ", "用戶名稱", "line\nname", "name/name", "name:1", None, 12]
)
def test_registration_rejects_invalid_usernames(store, username):
    assert_error("invalid_input", store.register, username, PASSWORD)
    assert store.count_users() == 0


@pytest.mark.parametrize("password", ["a" * 11, "a" * 129, "\ud800" * 12, None, b"password bytes"])
def test_registration_rejects_invalid_password_without_partial_account(store, password):
    assert_error("invalid_input", store.register, "valid-name", password)
    assert store.count_users() == 0


def test_display_name_bound_and_username_fallback(store):
    short = store.register("abc", PASSWORD)
    assert short["user"]["display_name"] == "abc"
    long_name = "A" * 80
    long = store.register(long_name, PASSWORD)
    assert long["user"]["display_name"] == long_name.casefold()
    assert store.register("display-name", PASSWORD, "界" * 60)["user"]["display_name"] == "界" * 60
    assert_error("invalid_input", store.register, "bad-display", PASSWORD, "界" * 61)
    assert_error("invalid_input", store.register, "bad-surrogate", PASSWORD, "\ud800")


def test_duplicate_casefold_registration_is_not_overwritten(store, registered):
    assert_error("register_failed", store.register, "PERSON+TAG@EXAMPLE.TEST", REPLACEMENT)
    assert store.count_users() == 1
    assert store.authenticate("person+tag@example.test", PASSWORD) == registered["user"]
    assert_error("authentication_failed", store.authenticate, "person+tag@example.test", REPLACEMENT)


def test_authentication_errors_do_not_enumerate_or_leak_input(store, registered):
    user = registered["user"]
    errors = [
        assert_error("authentication_failed", store.authenticate, user["username"], "Wrong secret password"),
        assert_error("authentication_failed", store.authenticate, "absent-account", PASSWORD),
        assert_error("authentication_failed", store.authenticate, "bad/name", PASSWORD),
        assert_error("authentication_failed", store.authenticate, user["username"], "short"),
    ]
    store.disable_user(user["id"])
    errors.append(assert_error("authentication_failed", store.authenticate, user["username"], PASSWORD))
    assert len({(e.code, str(e), repr(e), e.status_code) for e in errors}) == 1
    assert errors[0].status == 401
    for error in errors:
        representation = str(error) + repr(error)
        assert PASSWORD not in representation
        assert user["username"] not in representation
        assert registered["recovery_code"] not in representation
    unknown = AccountError("private token not an allowed error code")
    assert "private token" not in repr(unknown) + str(unknown)


def test_unknown_account_runs_real_password_verification(store, monkeypatch):
    real_verify = store._verify
    calls = []

    def observe(encoded_hash, password):
        calls.append(encoded_hash)
        return real_verify(encoded_hash, password)

    monkeypatch.setattr(store, "_verify", observe)
    assert_error("authentication_failed", store.authenticate, "unknown-person", PASSWORD)
    assert len(calls) == 1 and calls[0].startswith("$argon2id$")


def test_sessions_are_opaque_hash_only_fixed_eight_hours(store, registered):
    session = store.create_session(registered["user"]["id"])
    assert len(base64.urlsafe_b64decode(session["token"] + "=")) == 32
    assert len(base64.urlsafe_b64decode(session["csrf_token"] + "=")) == 32
    assert session["token"] != session["csrf_token"]
    loaded = store.get_session(session["token"])
    assert set(loaded) == {"user", "csrf_token", "expires_at"}
    assert loaded == {key: value for key, value in session.items() if key != "token"}
    assert (
        datetime.fromisoformat(session["expires_at"])
        - datetime.fromisoformat(registered["user"]["created_at"])
    ).total_seconds() == 8 * 3600
    rows = database_rows(store, "SELECT * FROM sessions")
    assert rows[0]["token_hash"] == hashlib.sha256(session["token"].encode()).hexdigest()
    assert "token" not in rows[0]
    for path in store.path.parent.iterdir():
        if path.is_file():
            contents = path.read_bytes()
            for secret in (PASSWORD, registered["recovery_code"], session["token"]):
                assert secret.encode() not in contents


def test_secret_result_repr_is_redacted_but_json_contract_is_preserved(store, registered):
    session = store.create_session(registered["user"]["id"])
    for result in (registered, session, store.get_session(session["token"])):
        assert isinstance(result, dict)
        assert repr(result) == str(result) == "<AccountResult redacted>"
        assert json.loads(json.dumps(result)) == result
    assert str(store.path) not in repr(store)


def test_expiry_boundary_cleans_storage_and_does_not_slide(tmp_path, clock):
    store = AccountStore(tmp_path, clock=clock, session_ttl=120)
    user = store.register("expiry-test", PASSWORD)["user"]
    session = store.create_session(user["id"])
    clock.value += 119.9
    assert store.get_session(session["token"])["expires_at"] == session["expires_at"]
    clock.value += 0.1
    assert store.get_session(session["token"]) is None
    assert database_rows(store, "SELECT * FROM sessions") == []


def test_session_revocation_is_idempotent_and_does_not_affect_other_session(store, registered):
    user_id = registered["user"]["id"]
    first, second = store.create_session(user_id), store.create_session(user_id)
    store.revoke_session(first["token"])
    store.revoke_session(first["token"])
    store.revoke_session("invalid-value")
    assert store.get_session(first["token"]) is None
    assert store.get_session(second["token"]) is not None
    for token in (None, "", "not-a-session", "x" * 10000, {"token": first["token"]}):
        assert store.get_session(token) is None


def test_session_cap_revokes_oldest_even_with_equal_clock(tmp_path, clock):
    store = AccountStore(tmp_path, clock=clock, max_sessions_per_user=2)
    user_id = store.register("session-limit", PASSWORD)["user"]["id"]
    first, second, third = [store.create_session(user_id) for _ in range(3)]
    assert store.get_session(first["token"]) is None
    assert store.get_session(second["token"]) is not None
    assert store.get_session(third["token"]) is not None
    assert len(database_rows(store, "SELECT * FROM sessions")) == 2


def test_password_change_revokes_all_sessions_but_preserves_other_users(store, registered):
    user = registered["user"]
    sessions = [store.create_session(user["id"]) for _ in range(2)]
    other = store.register("unrelated-person", PASSWORD)
    unrelated = store.create_session(other["user"]["id"])
    assert_error("authentication_failed", store.change_password, user["id"], "wrong password 12", REPLACEMENT)
    assert all(store.get_session(s["token"]) is not None for s in sessions)
    assert store.change_password(user["id"], PASSWORD, REPLACEMENT) is None
    assert all(store.get_session(s["token"]) is None for s in sessions)
    assert store.get_session(unrelated["token"]) is not None
    assert_error("authentication_failed", store.authenticate, user["username"], PASSWORD)
    assert store.authenticate(user["username"], REPLACEMENT) == user


def test_change_password_session_leaves_only_the_new_session(store, registered):
    user = registered["user"]
    original = store.authenticate_session(user["username"], PASSWORD)
    replacement = store.change_password_session(user["id"], PASSWORD, REPLACEMENT)
    assert store.get_session(original["token"]) is None
    assert store.get_session(replacement["token"])["user"] == user
    assert len(database_rows(store, "SELECT * FROM sessions")) == 1


def test_recovery_rotates_code_and_password_and_invalidates_sessions(store, registered):
    user = registered["user"]
    session = store.create_session(user["id"])
    assert_error("invalid_input", store.recover, user["username"], registered["recovery_code"], "short")
    recovered = store.recover(user["username"].upper(), registered["recovery_code"], REPLACEMENT)
    assert recovered["user"] == user
    assert recovered["recovery_code"] != registered["recovery_code"]
    assert len(base64.urlsafe_b64decode(recovered["recovery_code"] + "=")) == 32
    assert store.get_session(session["token"]) is None
    assert_error("authentication_failed", store.authenticate, user["username"], PASSWORD)
    assert store.authenticate(user["username"], REPLACEMENT) == user
    assert_error(
        "authentication_failed", store.recover, user["username"], registered["recovery_code"], PASSWORD
    )
    again = store.recover(user["username"], recovered["recovery_code"], PASSWORD)
    assert again["recovery_code"] not in (registered["recovery_code"], recovered["recovery_code"])
    assert_error(
        "authentication_failed", store.recover, user["username"], recovered["recovery_code"], PASSWORD
    )


def test_recovery_error_is_same_for_wrong_code_unknown_and_disabled_user(store, registered):
    user = registered["user"]
    errors = [
        assert_error("authentication_failed", store.recover, user["username"], "bad-code", REPLACEMENT),
        assert_error(
            "authentication_failed",
            store.recover,
            "missing-account",
            registered["recovery_code"],
            REPLACEMENT,
        ),
    ]
    store.disable_user(user["id"])
    errors.append(
        assert_error(
            "authentication_failed", store.recover, user["username"], registered["recovery_code"], REPLACEMENT
        )
    )
    assert len({(e.code, str(e), repr(e)) for e in errors}) == 1


def test_atomic_recovery_session_only_keeps_replacement_session(store, registered):
    user = registered["user"]
    old = store.create_session(user["id"])
    result = store.recover_session(user["username"], registered["recovery_code"], REPLACEMENT)
    assert set(result) == {"session", "recovery_code"}
    assert store.get_session(old["token"]) is None
    assert store.get_session(result["session"]["token"])["user"] == user
    assert len(database_rows(store, "SELECT * FROM sessions")) == 1


def test_disable_is_persistent_and_blocks_all_auth_paths(store, registered, clock):
    user = registered["user"]
    session = store.create_session(user["id"])
    store.disable_user(user["id"])
    store.disable_user(user["id"])
    store.disable_user("missing-id")
    assert store.get_user(user["id"])["disabled"] is True
    assert store.get_session(session["token"]) is None
    assert_error("authentication_failed", store.create_session, user["id"])
    assert_error("authentication_failed", store.change_password, user["id"], PASSWORD, REPLACEMENT)
    assert_error("authentication_failed", store.authenticate_session, user["username"], PASSWORD)
    reopened = AccountStore(store.path.parent, clock=clock)
    assert reopened.get_user(user["id"])["disabled"] is True
    assert reopened.count_users() == 1


def test_database_reopens_with_password_recovery_and_session(store, registered, clock):
    user = registered["user"]
    session = store.create_session(user["id"])
    reopened = AccountStore(store.path.parent, clock=clock)
    assert reopened.authenticate(user["username"], PASSWORD) == user
    assert reopened.get_user(user["id"]) == user
    assert reopened.get_user("nonexistent") is None
    assert reopened.get_session(session["token"])["csrf_token"] == session["csrf_token"]
    reopened.recover(user["username"], registered["recovery_code"], REPLACEMENT)
    assert store.get_session(session["token"]) is None
    assert store.authenticate(user["username"], REPLACEMENT) == user


def test_reopening_cleans_expired_sessions(tmp_path, clock):
    store = AccountStore(tmp_path, clock=clock, session_ttl=1)
    user = store.register("cleanup-test", PASSWORD)["user"]
    store.create_session(user["id"])
    clock.value += 2
    AccountStore(tmp_path, clock=clock)
    assert database_rows(store, "SELECT * FROM sessions") == []


def test_successful_login_upgrades_real_older_argon2_parameters(store, registered):
    old = PasswordHasher(time_cost=1).hash(PASSWORD)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE users SET password_hash=?", (old,))
    assert store.authenticate(registered["user"]["username"], PASSWORD) == registered["user"]
    upgraded = database_rows(store, "SELECT password_hash FROM users")[0]["password_hash"]
    assert upgraded != old
    assert PasswordHasher().verify(upgraded, PASSWORD)
    assert not PasswordHasher().check_needs_rehash(upgraded)


def test_schema_is_versioned_private_and_enforces_foreign_keys(store, registered):
    with store._transaction() as db:
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert db.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    if os.name != "nt":
        assert store.path.stat().st_mode & 0o777 == 0o600
    session = store.create_session(registered["user"]["id"])
    with store._transaction() as db:
        db.execute("DELETE FROM users WHERE id=?", (registered["user"]["id"],))
    assert store.get_session(session["token"]) is None
    assert database_rows(store, "SELECT * FROM sessions") == []


def test_unknown_database_version_fails_without_migration(store, registered, clock):
    with sqlite3.connect(store.path) as db:
        db.execute("PRAGMA user_version=999")
    assert_error("storage_unavailable", AccountStore, store.path.parent)
    assert database_rows(store, "PRAGMA user_version")[0]["user_version"] == 999
    assert store.get_user(registered["user"]["id"]) == registered["user"]


def test_existing_unrelated_database_is_never_adopted(tmp_path):
    path = tmp_path / "accounts.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE private_data(value TEXT)")
        db.execute("INSERT INTO private_data VALUES('preserve-me')")
    assert_error("storage_unavailable", AccountStore, tmp_path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM private_data").fetchone()[0] == "preserve-me"
        assert db.execute("SELECT name FROM sqlite_master WHERE name='users'").fetchone() is None


@pytest.mark.parametrize("operation", ["change", "recover", "disable"])
def test_credential_change_rolls_back_if_session_revocation_fails(store, registered, operation):
    user = registered["user"]
    session = store.create_session(user["id"])
    with sqlite3.connect(store.path) as db:
        db.execute("""CREATE TRIGGER fail_revoke BEFORE DELETE ON sessions
                      BEGIN SELECT RAISE(ABORT, 'private injected failure'); END""")
    action = {
        "change": lambda: store.change_password(user["id"], PASSWORD, REPLACEMENT),
        "recover": lambda: store.recover(user["username"], registered["recovery_code"], REPLACEMENT),
        "disable": lambda: store.disable_user(user["id"]),
    }[operation]
    error = assert_error("storage_unavailable", action)
    assert "private injected failure" not in "".join(traceback.format_exception(error))
    assert store.authenticate(user["username"], PASSWORD) == user
    assert store.get_session(session["token"]) is not None
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TRIGGER fail_revoke")
    assert store.recover(user["username"], registered["recovery_code"], REPLACEMENT)["user"] == user


def test_failed_session_cap_eviction_does_not_leave_new_session(tmp_path, clock):
    store = AccountStore(tmp_path, clock=clock, max_sessions_per_user=1)
    user = store.register("cap-rollback", PASSWORD)["user"]
    original = store.create_session(user["id"])
    with sqlite3.connect(store.path) as db:
        db.execute("""CREATE TRIGGER fail_evict BEFORE DELETE ON sessions
                      BEGIN SELECT RAISE(ABORT, 'private eviction failure'); END""")
    assert_error("storage_unavailable", store.create_session, user["id"])
    assert len(database_rows(store, "SELECT * FROM sessions")) == 1
    assert store.get_session(original["token"]) is not None


def test_concurrent_recovery_code_can_be_consumed_only_once(store, registered, clock):
    second = AccountStore(store.path.parent, clock=clock)
    barrier = threading.Barrier(2)

    def run(instance, password):
        barrier.wait(timeout=5)
        try:
            result = instance.recover(registered["user"]["username"], registered["recovery_code"], password)
            return {"result": result, "password": password}
        except AccountError as error:
            return {"error": error.code}

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(run, instance, password)
            for instance, password in ((store, REPLACEMENT), (second, "Another replacement 789"))
        ]
        results = [future.result(timeout=10) for future in futures]
    successes = [result for result in results if "result" in result]
    assert len(successes) == 1
    assert [result["error"] for result in results if "error" in result] == ["authentication_failed"]
    assert store.authenticate(registered["user"]["username"], successes[0]["password"])


def test_concurrent_capacity_is_checked_in_registration_transaction(tmp_path, clock):
    stores = [AccountStore(tmp_path, clock=clock, max_accounts=1) for _ in range(2)]
    barrier = threading.Barrier(2)

    def run(index):
        barrier.wait(timeout=5)
        try:
            return stores[index].register(f"person-{index}", PASSWORD)
        except AccountError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [future.result(timeout=10) for future in [executor.submit(run, i) for i in range(2)]]
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("capacity_reached") == 1
    assert stores[0].count_users() == 1
    user = next(result["user"] for result in results if isinstance(result, dict))
    stores[0].disable_user(user["id"])
    assert_error("capacity_reached", stores[1].register, "new-person", PASSWORD)


def test_concurrent_session_cap_applies_across_store_instances(tmp_path, clock):
    stores = [AccountStore(tmp_path, clock=clock, max_sessions_per_user=2) for _ in range(2)]
    user = stores[0].register("session-concurrency", PASSWORD)["user"]
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(stores[i % 2].create_session, user["id"]) for i in range(6)]
        sessions = [future.result(timeout=10) for future in futures]
    assert sum(stores[0].get_session(session["token"]) is not None for session in sessions) == 2
    assert len(database_rows(stores[0], "SELECT * FROM sessions")) == 2


@pytest.mark.parametrize("change", ["password", "disable", "recovery"])
def test_inflight_old_password_login_cannot_issue_session_after_invalidation(
    store, registered, monkeypatch, change
):
    user = registered["user"]
    verified, proceed = threading.Event(), threading.Event()
    original = store._authentication_snapshot

    def pause_after_verification(*args):
        snapshot = original(*args)
        verified.set()
        assert proceed.wait(5)
        return snapshot

    monkeypatch.setattr(store, "_authentication_snapshot", pause_after_verification)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(store.authenticate_session, user["username"], PASSWORD)
        try:
            assert verified.wait(5)
            if change == "password":
                store.change_password(user["id"], PASSWORD, REPLACEMENT)
            elif change == "recovery":
                store.recover(user["username"], registered["recovery_code"], REPLACEMENT)
            else:
                store.disable_user(user["id"])
        finally:
            proceed.set()
        assert_error("authentication_failed", pending.result, 5)
    assert database_rows(store, "SELECT * FROM sessions") == []


@pytest.mark.parametrize("issue_session", [False, True])
def test_inflight_password_change_cannot_overwrite_completed_recovery(
    store, registered, monkeypatch, issue_session
):
    user = registered["user"]
    previous = store.create_session(user["id"])
    hashed, proceed = threading.Event(), threading.Event()
    original = store._hash_password

    def pause_after_replacement_hash(password):
        encoded = original(password)
        if password == REPLACEMENT:
            hashed.set()
            assert proceed.wait(5)
        return encoded

    monkeypatch.setattr(store, "_hash_password", pause_after_replacement_hash)
    action = store.change_password_session if issue_session else store.change_password
    recovery_password = "Emergency recovery password 890"
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(action, user["id"], PASSWORD, REPLACEMENT)
        try:
            assert hashed.wait(5)
            store.recover(user["username"], registered["recovery_code"], recovery_password)
        finally:
            proceed.set()
        assert_error("authentication_failed", pending.result, 5)
    assert store.authenticate(user["username"], recovery_password) == user
    assert_error("authentication_failed", store.authenticate, user["username"], REPLACEMENT)
    assert store.get_session(previous["token"]) is None
    assert database_rows(store, "SELECT * FROM sessions") == []


def test_atomic_password_session_rolls_back_if_new_session_creation_fails(store, registered):
    user = registered["user"]
    previous = store.create_session(user["id"])
    with sqlite3.connect(store.path) as db:
        db.execute("""CREATE TRIGGER fail_new_session BEFORE INSERT ON sessions
                      BEGIN SELECT RAISE(ABORT, 'private insertion failure'); END""")
    assert_error("storage_unavailable", store.change_password_session, user["id"], PASSWORD, REPLACEMENT)
    assert store.authenticate(user["username"], PASSWORD) == user
    assert store.get_session(previous["token"]) is not None
    assert len(database_rows(store, "SELECT * FROM sessions")) == 1


def test_registration_session_and_recovery_are_issued_together(store):
    result = store.register_session("new-person", PASSWORD, "新衣櫃")
    assert store.get_session(result["token"])["user"] == result["user"]
    assert result["csrf_token"]
    assert "recovery_code" not in repr(result)
    recovered = store.recover("new-person", result["recovery_code"], REPLACEMENT)
    assert recovered["recovery_code"] != result["recovery_code"]
    assert store.get_session(result["token"]) is None
    assert store.authenticate("new-person", REPLACEMENT) == result["user"]


def test_registration_session_failure_does_not_leave_unrecoverable_account(store):
    with sqlite3.connect(store.path) as db:
        db.execute("""CREATE TRIGGER fail_registration_session BEFORE INSERT ON sessions
                      BEGIN SELECT RAISE(ABORT, 'private insertion failure'); END""")
    assert_error("storage_unavailable", store.register_session, "new-person", PASSWORD)
    assert store.count_users() == 0
    assert database_rows(store, "SELECT * FROM sessions") == []
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TRIGGER fail_registration_session")
    result = store.register_session("new-person", PASSWORD)
    assert store.get_session(result["token"])["user"]["username"] == "new-person"
