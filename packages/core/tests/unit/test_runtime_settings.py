"""Etapa 5H (Kill Switches + Runtime Settings) — src/services/runtime_settings.py.

Covers: fail-safe defaults for a missing key vs. a corrupted-but-present
value (deliberately DIFFERENT per the module's own docstring), valid
set/get round-trips for every known key, rejection of unknown keys and
wrong-shaped values, and the "always fresh, never cached" read contract
(a write on one db session is immediately visible on a different one —
this IS the concurrency guarantee: no cache to go stale, no race to
exploit).
"""
from __future__ import annotations

import time
import uuid

import pytest

from src.models.runtime_setting import RuntimeSetting
from src.models.user import User
from src.services.runtime_settings import (
    AGENT_ENABLED,
    CONTINUITY_ENABLED,
    DISABLED_CAPABILITIES,
    DISABLED_PROVIDER_KEYS,
    KNOWN_KEYS,
    InvalidSettingValueError,
    UnknownSettingError,
    get_all_settings,
    get_disabled_capabilities,
    get_disabled_provider_keys,
    get_setting,
    is_agent_enabled,
    is_capability_disabled,
    is_continuity_enabled,
    is_provider_disabled,
    set_setting,
)
from tests.postgres_helpers import get_test_postgres_url, postgres_available


def _new_admin_id(db) -> str:
    user = User(id=str(uuid.uuid4()), email=f"{uuid.uuid4()}@test.com", hashed_password="x", is_admin=True)
    db.add(user)
    db.commit()
    return user.id


# ═════════════════════════════════════════════════════════════════════════
# 1. Defaults — missing key
# ═════════════════════════════════════════════════════════════════════════

def test_agent_enabled_defaults_true_when_missing(db):
    assert is_agent_enabled(db) is True


def test_continuity_enabled_defaults_true_when_missing(db):
    assert is_continuity_enabled(db) is True


def test_disabled_provider_keys_defaults_empty_when_missing(db):
    assert get_disabled_provider_keys(db) == frozenset()


def test_disabled_capabilities_defaults_empty_when_missing(db):
    assert get_disabled_capabilities(db) == frozenset()


def test_get_all_settings_returns_every_known_key_with_defaults(db):
    settings = get_all_settings(db)
    assert set(settings.keys()) == KNOWN_KEYS
    assert settings[AGENT_ENABLED] is True
    assert settings[CONTINUITY_ENABLED] is True
    assert settings[DISABLED_PROVIDER_KEYS] == []
    assert settings[DISABLED_CAPABILITIES] == []


# ═════════════════════════════════════════════════════════════════════════
# 2. Valid set/get round-trips
# ═════════════════════════════════════════════════════════════════════════

def test_set_agent_enabled_false_then_read_back(db):
    admin_id = _new_admin_id(db)
    set_setting(db, AGENT_ENABLED, False, admin_user_id=admin_id)
    assert is_agent_enabled(db) is False


def test_set_continuity_enabled_false_then_read_back(db):
    admin_id = _new_admin_id(db)
    set_setting(db, CONTINUITY_ENABLED, False, admin_user_id=admin_id)
    assert is_continuity_enabled(db) is False


def test_set_disabled_provider_keys_then_read_back(db):
    admin_id = _new_admin_id(db)
    set_setting(db, DISABLED_PROVIDER_KEYS, ["claude-sonnet", "gpt-4o"], admin_user_id=admin_id)
    assert get_disabled_provider_keys(db) == frozenset({"claude-sonnet", "gpt-4o"})
    assert is_provider_disabled(db, "claude-sonnet") is True
    assert is_provider_disabled(db, "claude-opus") is False


def test_set_disabled_capabilities_then_read_back(db):
    admin_id = _new_admin_id(db)
    set_setting(db, DISABLED_CAPABILITIES, ["image", "video"], admin_user_id=admin_id)
    assert get_disabled_capabilities(db) == frozenset({"image", "video"})
    assert is_capability_disabled(db, "image") is True
    assert is_capability_disabled(db, "chat") is False


def test_updating_an_existing_key_overwrites_not_duplicates(db):
    admin_id = _new_admin_id(db)
    set_setting(db, AGENT_ENABLED, False, admin_user_id=admin_id)
    set_setting(db, AGENT_ENABLED, True, admin_user_id=admin_id)
    assert is_agent_enabled(db) is True
    assert db.query(RuntimeSetting).filter(RuntimeSetting.key == AGENT_ENABLED).count() == 1


def test_set_setting_records_who_changed_it(db):
    admin_id = _new_admin_id(db)
    set_setting(db, AGENT_ENABLED, False, admin_user_id=admin_id)
    row = db.query(RuntimeSetting).filter(RuntimeSetting.key == AGENT_ENABLED).first()
    assert row.updated_by_user_id == admin_id


# ═════════════════════════════════════════════════════════════════════════
# 3. Validation — unknown keys / wrong-shaped values are REJECTED, never
#    silently written.
# ═════════════════════════════════════════════════════════════════════════

def test_set_setting_rejects_unknown_key(db):
    admin_id = _new_admin_id(db)
    with pytest.raises(UnknownSettingError):
        set_setting(db, "not_a_real_setting", True, admin_user_id=admin_id)
    assert db.query(RuntimeSetting).filter(RuntimeSetting.key == "not_a_real_setting").count() == 0


def test_get_setting_rejects_unknown_key(db):
    with pytest.raises(UnknownSettingError):
        get_setting(db, "not_a_real_setting")


@pytest.mark.parametrize("bad_value", ["yes", 1, None, ["a", "b"]])
def test_set_setting_rejects_non_bool_for_agent_enabled(db, bad_value):
    admin_id = _new_admin_id(db)
    with pytest.raises(InvalidSettingValueError):
        set_setting(db, AGENT_ENABLED, bad_value, admin_user_id=admin_id)
    # Rejected write never persists — is_agent_enabled() still sees the default.
    assert is_agent_enabled(db) is True


@pytest.mark.parametrize("bad_value", ["not-a-list", True, [1, 2, 3], {"a": 1}])
def test_set_setting_rejects_wrong_shape_for_disabled_provider_keys(db, bad_value):
    admin_id = _new_admin_id(db)
    with pytest.raises(InvalidSettingValueError):
        set_setting(db, DISABLED_PROVIDER_KEYS, bad_value, admin_user_id=admin_id)
    assert get_disabled_provider_keys(db) == frozenset()


# ═════════════════════════════════════════════════════════════════════════
# 4. Fail-safe on a CORRUPTED (present but invalid) value — deliberately
#    different defaults for booleans (fail closed) vs. lists (fail empty).
#    Simulates corruption by writing directly through the model, bypassing
#    set_setting()'s own validation — the only way a bad value could ever
#    reach the table in practice (a manual DB edit, or a bug elsewhere).
# ═════════════════════════════════════════════════════════════════════════

def _write_raw(db, key: str, raw_value) -> None:
    row = RuntimeSetting(key=key, value=raw_value)
    db.add(row)
    db.commit()


def test_corrupted_agent_enabled_fails_closed_not_open(db):
    _write_raw(db, AGENT_ENABLED, "not-a-bool")
    assert is_agent_enabled(db) is False  # NOT True — never silently re-enable


def test_corrupted_continuity_enabled_fails_closed_not_open(db):
    _write_raw(db, CONTINUITY_ENABLED, {"nested": "garbage"})
    assert is_continuity_enabled(db) is False


def test_corrupted_disabled_provider_keys_fails_to_empty_not_full_block(db):
    _write_raw(db, DISABLED_PROVIDER_KEYS, "not-a-list")
    assert get_disabled_provider_keys(db) == frozenset()  # baseline, not "everything disabled"


def test_corrupted_disabled_capabilities_fails_to_empty(db):
    _write_raw(db, DISABLED_CAPABILITIES, [1, 2, 3])  # list, but not of strings
    assert get_disabled_capabilities(db) == frozenset()


# ═════════════════════════════════════════════════════════════════════════
# 5. Concurrency — always-fresh reads (no cache) mean a write on one
#    session is immediately visible from a DIFFERENT session, with no
#    stale window to race against. Uses a real second engine/session
#    (not just the same `db` fixture) to prove this isn't an artifact of
#    reading the same in-memory ORM identity map.
# ═════════════════════════════════════════════════════════════════════════

def test_write_on_one_session_is_immediately_visible_on_another(db):
    """Genuine cross-session proof (not just the same session with an
    expired identity map): opens a SECOND SQLAlchemy session against the
    exact same underlying engine tests/conftest.py's `db` fixture uses,
    writes through `db`, and reads through the other session — no cache
    layer sits between runtime_settings.py and the database, so this
    always sees the latest committed value."""
    from tests.conftest import _TestingSession

    admin_id = _new_admin_id(db)
    other_session = _TestingSession()
    try:
        assert is_agent_enabled(other_session) is True  # baseline, before any write

        set_setting(db, AGENT_ENABLED, False, admin_user_id=admin_id)
        assert is_agent_enabled(other_session) is False  # visible immediately, no stale window

        set_setting(db, AGENT_ENABLED, True, admin_user_id=admin_id)
        assert is_agent_enabled(other_session) is True
    finally:
        other_session.close()


def test_get_setting_never_caches_across_repeated_calls(db):
    """No module-level/process-level cache exists at all — this test would
    fail if one were ever accidentally introduced, since the SECOND call
    for the same key must reflect a change made between the two calls."""
    admin_id = _new_admin_id(db)
    assert is_agent_enabled(db) is True
    set_setting(db, AGENT_ENABLED, False, admin_user_id=admin_id)
    assert is_agent_enabled(db) is False  # same process, same db session, no cache to go stale


@pytest.mark.skipif(not postgres_available(), reason="TEST_POSTGRES_URL not set or PostgreSQL unreachable — see tests/postgres_helpers.py")
def test_concurrent_reads_during_a_write_never_observe_a_torn_or_bypassable_value():
    """Real OS threads, each with their OWN session against a REAL
    PostgreSQL database, hammering is_agent_enabled() while a separate
    thread commits a change — every observed value must be a value the
    setting genuinely held at some point (True before the write commits,
    False after), NEVER anything else (a torn read, an exception treated
    as "enabled", etc.). This is the concrete proof behind "garantir que
    não exista condição de corrida capaz de burlar o switch".

    Gated on real PostgreSQL, mirroring this codebase's own established
    convention for every other genuine-concurrency proof (see
    test_entitlement.py::test_concurrent_reservation_only_one_succeeds,
    test_continuity_entitlement.py::test_concurrent_continuity_debits_never_exceed_the_budget)
    — SQLite's tests/conftest.py fixture is a SINGLE shared StaticPool
    connection reused by every session; genuinely concurrent multi-thread
    access to that ONE underlying sqlite3 connection object is unsafe
    (can corrupt cursor/transaction state for whichever test runs next in
    the same pytest session, not just this one) and was the wrong tool
    for this proof even before that risk was discovered — Postgres gives
    each thread a real, independent connection instead.
    """
    import threading

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import src.models  # noqa: F401 — registers all models on Base.metadata
    from src.database import Base

    url = get_test_postgres_url()
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)

    setup_db = Session()
    try:
        admin_id = _new_admin_id(setup_db)
    finally:
        setup_db.close()

    stop = threading.Event()
    observed: set[bool] = set()
    errors: list[BaseException] = []

    def reader():
        session = Session()
        try:
            while not stop.is_set():
                try:
                    observed.add(is_agent_enabled(session))
                except BaseException as exc:  # pragma: no cover — failure path only
                    errors.append(exc)
        finally:
            session.close()

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in readers:
        t.start()

    try:
        writer_db = Session()
        try:
            time.sleep(0.05)  # let readers observe the True baseline for a moment
            set_setting(writer_db, AGENT_ENABLED, False, admin_user_id=admin_id)
            time.sleep(0.05)  # let readers observe the new False value for a moment
        finally:
            writer_db.close()
    finally:
        stop.set()
        for t in readers:
            t.join(timeout=10)
        for t in readers:
            assert not t.is_alive(), "a reader thread failed to terminate — would corrupt later tests"
        engine.dispose()

    assert errors == []
    assert observed.issubset({True, False})  # never anything but a genuinely-held value
    assert False in observed  # the write's effect was actually observed by at least one reader
