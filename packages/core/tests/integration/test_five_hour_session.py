"""
5-hour rolling entitlement session window — see
src/services/session_window_service.py for the full product rule and
src/services/entitlement_service.py::check_and_reserve for the integration
point (resolve_or_start_session is called there, under the same lock used
for the TCU reservation itself).

This is a SEPARATE dimension from daily/weekly TCU entitlement and from
credits/billing — nothing here touches those, and nothing in those touches
this. The weekly-reset rule (Sunday 12:00 local) and the retired monthly
cap are covered in test_weekly_entitlement.py / test_entitlement.py and
are NOT re-tested here.

All timestamps are fixed, explicit UTC instants — never the real "now" —
so every test is deterministic regardless of when the suite runs.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import sessionmaker

from src.models.entitlement_session import EntitlementSession
from src.models.user import User
from src.services.entitlement_service import cancel_reservation, check_and_reserve
from src.services.session_window_service import SESSION_DURATION, get_session_status, resolve_or_start_session


# ── helpers ──────────────────────────────────────────────────────────────

def _make_user(db, *, plan: str = "start", tz: str | None = None) -> str:
    user = User(
        id=str(uuid.uuid4()),
        email=f"5h_{uuid.uuid4().hex[:8]}@test.com",
        hashed_password="x",
        plan=plan,
        timezone=tz,
    )
    db.add(user)
    db.commit()
    return user.id


T0 = datetime(2026, 8, 31, 14, 37, 22, tzinfo=timezone.utc)  # spec's own example instant


# ── 1. First operation creates a session ────────────────────────────────

def test_first_operation_creates_session(db):
    user_id = _make_user(db)

    session = resolve_or_start_session(db, user_id, T0)
    db.commit()

    assert session.session_started_at == T0
    assert session.session_expires_at == T0 + timedelta(hours=5)


# ── 2. Second operation within 5h reuses the session ────────────────────

def test_second_operation_within_5h_reuses_session(db):
    user_id = _make_user(db)
    first = resolve_or_start_session(db, user_id, T0)
    db.commit()

    later = T0 + timedelta(hours=2, minutes=23)  # spec's own example: 17:00 case, generalized
    second = resolve_or_start_session(db, user_id, later)
    db.commit()

    assert second.id == first.id
    assert second.session_started_at == T0
    assert second.session_expires_at == T0 + timedelta(hours=5)


# ── 3. Exactly at the 5h boundary is expired (now >= expires_at) ───────

def test_exactly_at_5h_boundary_is_expired(db):
    user_id = _make_user(db)
    first = resolve_or_start_session(db, user_id, T0)
    db.commit()

    exact_expiry = T0 + timedelta(hours=5)
    second = resolve_or_start_session(db, user_id, exact_expiry)
    db.commit()

    assert second.id == first.id  # same row, updated in place
    assert second.session_started_at == exact_expiry  # NEW session, not the old one
    assert second.session_expires_at == exact_expiry + timedelta(hours=5)


def test_one_microsecond_before_expiry_still_active(db):
    """Sanity boundary check on the other side of test 3."""
    user_id = _make_user(db)
    first = resolve_or_start_session(db, user_id, T0)
    db.commit()

    almost_expired = T0 + timedelta(hours=5) - timedelta(microseconds=1)
    second = resolve_or_start_session(db, user_id, almost_expired)
    db.commit()

    assert second.session_started_at == T0  # still the original session


# ── 4. Operation after 5h creates a new session ─────────────────────────

def test_operation_after_5h_creates_new_session(db):
    user_id = _make_user(db)
    first = resolve_or_start_session(db, user_id, T0)
    db.commit()
    first_started_at = first.session_started_at  # capture the value — `first`
    # and the row resolve_or_start_session returns next are the SAME
    # identity-mapped Python object, mutated in place on the second call.

    after = T0 + timedelta(hours=5, seconds=1)  # spec's own example: 19:37:23 case
    second = resolve_or_start_session(db, user_id, after)
    db.commit()

    assert second.session_started_at == after
    assert second.session_expires_at == after + timedelta(hours=5)
    assert second.session_started_at != first_started_at


# ── 5. A new request never extends session_expires_at ──────────────────

def test_new_request_does_not_extend_expiry(db):
    """An operation at 18:00 inside a session started at 14:37:22 must NOT
    push expiry to 23:00 — the clock is absolute, never renewed."""
    user_id = _make_user(db)
    resolve_or_start_session(db, user_id, T0)
    db.commit()
    original_expiry = T0 + timedelta(hours=5)

    for offset in (timedelta(minutes=23), timedelta(hours=3, minutes=8), timedelta(hours=4, minutes=52)):
        result = resolve_or_start_session(db, user_id, T0 + offset)
        db.commit()
        assert result.session_expires_at == original_expiry, f"expiry drifted at +{offset}"


# ── 6. A cancelled/failed reservation does not (re)start a session ─────

def test_cancelled_reservation_does_not_restart_session(db):
    """cancel_reservation() must never touch EntitlementSession — a
    provider failure after a successful reservation must not prolong or
    restart the session."""
    user_id = _make_user(db)

    reservation_id = check_and_reserve(db, user_id=user_id, session_id=None, plan="start")
    sessions_after_reserve = db.query(EntitlementSession).filter_by(user_id=user_id).all()
    assert len(sessions_after_reserve) == 1
    original = sessions_after_reserve[0]
    original_started_at = original.session_started_at

    cancel_reservation(db, reservation_id)

    # A second reservation shortly after cancellation must reuse the SAME
    # session, not start a new one.
    reservation_id_2 = check_and_reserve(db, user_id=user_id, session_id=None, plan="start")
    cancel_reservation(db, reservation_id_2)

    sessions_after_cancel = db.query(EntitlementSession).filter_by(user_id=user_id).all()
    assert len(sessions_after_cancel) == 1
    assert sessions_after_cancel[0].session_started_at == original_started_at


def test_429_blocked_request_does_not_start_a_session(db):
    """A request blocked by the daily/weekly gate never actually reserved
    TCU, so per the rule ('primeira operação que efetivamente reserva
    TCU') it must not start or touch a session either. check_and_reserve
    only commits the session change alongside the reservation row — see
    the comment at its call site."""
    from fastapi import HTTPException

    from src.entitlements import get_plan_entitlement
    from src.models.usage import UsageRecord
    from src.services.entitlement_service import _day_window

    user_id = _make_user(db, plan="free")
    entitlement = get_plan_entitlement("free")

    day_start, _ = _day_window()
    seed = UsageRecord(
        user_id=user_id, session_id=None, model="test",
        input_tokens=0, output_tokens=0, cached_tokens=0,
        # Seeded to the daily ceiling itself. Under the ADMIT rule
        # (2026-09-11) a window with balance left admits a clamped request
        # instead of refusing it, so 'one per-request estimate short of the
        # cap' no longer blocks — reaching the cap is what blocks. The
        # invariant under test is unchanged.
        cost_usd=0.0, tcu=entitlement.daily_tcu_limit,
        duration_ms=0, status="completed", created_at=day_start.replace(hour=12),
    )
    db.add(seed)
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        check_and_reserve(db, user_id=user_id, session_id=None, plan="free")
    assert exc_info.value.status_code == 429

    # The session row was flush()ed (visible within this same still-open
    # transaction) but never committed. Roll back to reproduce exactly what
    # happens in production when the request's DB session closes without a
    # commit (see database.py::get_db) — only then is "was it ever
    # persisted" a meaningful question.
    db.rollback()
    assert db.query(EntitlementSession).filter_by(user_id=user_id).count() == 0


# ── 7. Concurrent "no active session" requests produce ONE session ─────

def test_concurrent_session_start_produces_exactly_one_session(db):
    """Simulates two requests racing when no session exists: both read
    'no active session' before either writes, then both attempt to start
    one. The UniqueConstraint(user_id) + IntegrityError fallback in
    resolve_or_start_session must ensure only one session ever persists —
    this is the safety net for dialects without the PostgreSQL advisory
    lock check_and_reserve() otherwise relies on (see module docstring)."""
    user_id = _make_user(db)
    engine = db.get_bind()
    Session = sessionmaker(bind=engine)
    session_a = Session()
    session_b = Session()

    try:
        # Both see no active session — the genuine race precondition.
        assert session_a.query(EntitlementSession).filter_by(user_id=user_id).first() is None
        assert session_b.query(EntitlementSession).filter_by(user_id=user_id).first() is None

        # B "wins": resolves (creates) and commits first. Capture the
        # values immediately — commit() expires attributes by default, and
        # we close this session before the outer asserts run.
        winner = resolve_or_start_session(session_b, user_id, T0)
        winner_id = winner.id
        winner_started_at = winner.session_started_at
        session_b.commit()

        # A is still working off its earlier stale read — force it down the
        # same "no existing session" branch resolve_or_start_session took
        # for B, so its INSERT genuinely collides with what B already
        # committed. This reproduces the exact interleaving a real
        # concurrent request would hit under snapshot/read-committed
        # isolation, without relying on OS thread-scheduling timing.
        session_a.query = MagicMock(side_effect=session_a.query)
        first_call = {"done": False}
        real_query = session_a.query

        def query_once_empty(model):
            q = real_query(model)
            if model is EntitlementSession and not first_call["done"]:
                first_call["done"] = True
                empty = MagicMock()
                empty.filter.return_value.first.return_value = None
                return empty
            return q

        session_a.query = query_once_empty
        loser = resolve_or_start_session(session_a, user_id, T0)
        loser_id = loser.id
        loser_started_at = loser.session_started_at
        session_a.commit()
    finally:
        session_a.close()
        session_b.close()

    all_rows = db.query(EntitlementSession).filter_by(user_id=user_id).all()
    assert len(all_rows) == 1, "exactly one EntitlementSession row must exist after the race"
    assert loser_id == winner_id
    assert loser_started_at == winner_started_at == T0


# ── 8. Timestamps are persisted correctly in UTC ────────────────────────

def test_timestamps_persisted_in_utc(db):
    user_id = _make_user(db)
    resolve_or_start_session(db, user_id, T0)
    db.commit()
    db.expire_all()  # force a fresh read from the DB, not the identity map

    row = db.query(EntitlementSession).filter_by(user_id=user_id).first()
    assert row.session_started_at.tzinfo is not None
    assert row.session_started_at.utcoffset() == timedelta(0)
    assert row.session_expires_at.tzinfo is not None
    assert row.session_expires_at.utcoffset() == timedelta(0)
    assert row.session_started_at == T0
    assert row.session_expires_at == T0 + timedelta(hours=5)


# ── 9. User timezone never changes the 5h duration ──────────────────────

@pytest.mark.parametrize("tz_name", [
    None, "UTC", "America/Sao_Paulo", "America/New_York", "Europe/London", "Asia/Tokyo", "Australia/Sydney",
])
def test_user_timezone_does_not_change_session_duration(db, tz_name):
    user_id = _make_user(db, tz=tz_name)
    session = resolve_or_start_session(db, user_id, T0)
    db.commit()

    assert session.session_expires_at - session.session_started_at == SESSION_DURATION
    assert session.session_expires_at == T0 + timedelta(hours=5)


# ── 10. Expired session is correctly treated as inactive ───────────────

def test_expired_session_reported_inactive(db):
    user_id = _make_user(db)
    resolve_or_start_session(db, user_id, T0)
    db.commit()

    still_active = get_session_status(db, user_id, T0 + timedelta(hours=1))
    assert still_active["active"] is True

    expired = get_session_status(db, user_id, T0 + timedelta(hours=5))  # now == expires_at
    assert expired["active"] is False
    # get_session_status is read-only — it must NOT have created/changed anything.
    row = db.query(EntitlementSession).filter_by(user_id=user_id).first()
    assert row.session_started_at == T0


def test_no_session_yet_reported_inactive_with_null_timestamps(db):
    user_id = _make_user(db)
    status = get_session_status(db, user_id, T0)
    assert status == {"active": False, "session_started_at": None, "session_expires_at": None}


# ── 11. Frontend receives session_expires_at via the API ───────────────

def test_session_endpoint_returns_expected_shape(client, db, auth_headers, registered_user):
    user_id = registered_user["user_id"]

    resolve_or_start_session(db, user_id, T0)
    db.commit()

    resp = client.get("/users/me/session", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "active" in data
    assert "session_started_at" in data
    assert "session_expires_at" in data
    assert data["session_started_at"] == T0.isoformat()
    assert data["session_expires_at"] == (T0 + timedelta(hours=5)).isoformat()


def test_session_endpoint_requires_auth(client):
    resp = client.get("/users/me/session")
    assert resp.status_code == 401


def test_session_endpoint_never_starts_a_session(client, db, auth_headers, registered_user):
    """GET /users/me/session is read-only — merely calling it must not
    create an EntitlementSession row."""
    resp = client.get("/users/me/session", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["active"] is False
    assert db.query(EntitlementSession).filter_by(user_id=registered_user["user_id"]).count() == 0


# ── 12. Countdown is a display concern only — backend stays authoritative ─

def test_backend_ignores_any_client_supplied_time_hint(client, db, auth_headers, registered_user):
    """The session endpoint takes no request body/query params at all —
    there is no channel for the frontend's clock to influence backend
    state. Extra/bogus query params must have zero effect."""
    user_id = registered_user["user_id"]
    resolve_or_start_session(db, user_id, T0)
    db.commit()

    resp = client.get(
        "/users/me/session?now=2099-01-01T00:00:00Z&session_expires_at=2000-01-01T00:00:00Z",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    # Server-computed values, completely unaffected by the bogus query params.
    assert data["session_started_at"] == T0.isoformat()
    assert data["session_expires_at"] == (T0 + timedelta(hours=5)).isoformat()
