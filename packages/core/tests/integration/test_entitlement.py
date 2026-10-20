"""
Fase 3 — Entitlement / TCU enforcement integration tests.

Most tests here run against SQLite (in-memory via the standard test
fixtures). SQLite serializes all writes (single-writer), so the write-skew
race condition documented in entitlement_service.py cannot be reproduced
there — that's what the two tests under "POSTGRESQL_INTEGRATION" below are
for: they run against a REAL PostgreSQL instance (gated by
tests/postgres_helpers.py::postgres_available(), i.e. TEST_POSTGRES_URL),
using their own engine/session, independent of the SQLite `db`/`client`
fixtures used by every other test in this file.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch, MagicMock
import pytest

from src.entitlements import PLAN_ENTITLEMENTS, get_plan_entitlement
from src.models.usage import UsageRecord
from src.services.entitlement_service import (
    cancel_reservation,
    check_agent_iteration_budget,
    check_and_reserve,
    finalize_usage,
    get_user_period_usage,
    _day_window,
    _month_window,
    _sum_active_tcu,
)
from tests.postgres_helpers import get_test_postgres_url, postgres_available


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def user_and_session(client, db):
    """Register a user, return (headers, user_id, session_id)."""
    resp = client.post("/auth/register", json={
        "email": "entitlement@test.com",
        "password": "TestPass123!",
        "full_name": "Entitlement Tester",
    })
    assert resp.status_code == 201
    token = resp.json()["access_token"]
    user_id = resp.json()["user_id"]
    headers = {"Authorization": f"Bearer {token}"}

    sess_resp = client.post("/sessions", json={"title": "test"}, headers=headers)
    assert sess_resp.status_code == 201
    session_id = sess_resp.json()["id"]
    return headers, user_id, session_id


@pytest.fixture
def plan_user(client, db):
    """Factory: create a user and return (headers, user_id, session_id, set_plan_fn)."""
    counter = {"n": 0}

    def _make(plan: str = "start"):
        counter["n"] += 1
        email = f"planuser{counter['n']}@test.com"
        resp = client.post("/auth/register", json={
            "email": email,
            "password": "TestPass123!",
            "full_name": "Plan User",
        })
        assert resp.status_code == 201
        token = resp.json()["access_token"]
        user_id = resp.json()["user_id"]
        headers = {"Authorization": f"Bearer {token}"}

        from src.models.user import User
        user = db.query(User).filter(User.id == user_id).first()
        user.plan = plan
        db.commit()

        sess_resp = client.post("/sessions", json={"title": "test"}, headers=headers)
        session_id = sess_resp.json()["id"]
        return headers, user_id, session_id

    return _make


# ── 1. Plan entitlements ───────────────────────────────────────────────────────

def test_entitlement_free():
    e = get_plan_entitlement("free")
    assert e.daily_tcu_limit == 10.0
    # PARTE 10 (2026-08-31): no operational monthly usage cap for any plan.
    assert e.monthly_tcu_limit is None
    assert e.max_tcu_per_request == 2.0


def test_entitlement_start():
    # Re-windowed 2026-09-11 from what $9.90 can actually pay for; the
    # derivation is in tests/unit/test_plan_economics.py.
    e = get_plan_entitlement("start")
    assert e.daily_tcu_limit == 45.0
    assert e.session_tcu_limit == 30.0
    assert e.monthly_tcu_limit is None
    # 10, not 4: the reservation is each call's budget and has to pay for the
    # standard band START is sold with (test_request_budget_invariants.py).
    assert e.max_tcu_per_request == 10.0


def test_entitlement_pro():
    e = get_plan_entitlement("pro")
    assert e.daily_tcu_limit == 110.0
    assert e.session_tcu_limit == 60.0
    assert e.monthly_tcu_limit is None
    # 11, not 8: Sonnet, PRO's standard fallback, is estimated at 8.6, and
    # PRO has to stay above START's 10.
    assert e.max_tcu_per_request == 11.0


def test_entitlement_max():
    """MAX is premium, not infinite. It used to carry no daily or weekly
    ceiling at all, which meant a single automated client could run up
    unbounded provider cost on a fixed-price plan."""
    e = get_plan_entitlement("max")
    assert e.session_tcu_limit is not None
    assert e.daily_tcu_limit is not None
    assert e.weekly_tcu_limit is not None
    # 60, not 30: one Opus call at MAX's output cap is estimated at 42, so 30
    # made PREMIUM unreachable in chat and agent mode.
    assert e.max_tcu_per_request == 60.0
    assert e.team_weekly_pool_tcu_limit is None, "MAX is an individual plan"
    # No monthly window: the weekly one is the ceiling that binds.
    assert e.monthly_tcu_limit is None


def test_entitlement_team():
    e = get_plan_entitlement("team")
    assert e.session_tcu_limit is not None
    assert e.daily_tcu_limit is not None
    assert e.weekly_tcu_limit is not None
    assert e.team_weekly_pool_tcu_limit is not None, "TEAM must have a shared pool"
    assert e.monthly_tcu_limit is None


def test_no_plan_is_truly_unlimited():
    """The product promise is 'no small artificial cap', not 'infinite'.
    Every plan must be bounded on at least one real window."""
    for plan in ("free", "start", "pro", "max", "team"):
        e = get_plan_entitlement(plan)
        bounded = any((e.session_tcu_limit, e.daily_tcu_limit, e.weekly_tcu_limit))
        assert bounded, f"{plan} has no usage ceiling at all"


def test_the_windows_are_ordered_so_each_one_binds():
    """A window nobody can reach is decoration. The 5-hour budget must be
    reachable inside a day, and a day's worth must be reachable inside a
    week — otherwise the longer windows would never trigger."""
    for plan in ("max", "team"):
        e = get_plan_entitlement(plan)
        assert e.session_tcu_limit < e.daily_tcu_limit
        assert e.daily_tcu_limit < e.weekly_tcu_limit
        # ~4.8 five-hour windows fit in a day, so the daily cap must bite
        # before the session cap could be spent that many times over.
        assert e.session_tcu_limit * 4.8 > e.daily_tcu_limit
        # And a week of daily maximums must exceed the weekly cap, so the
        # weekly window is what stops sustained heavy use.
        assert e.daily_tcu_limit * 7 > e.weekly_tcu_limit


def test_team_pool_bounds_the_team_without_stranding_one_member():
    """The pool protects the flat price against an open number of seats;
    the per-member weekly limit protects members from each other."""
    e = get_plan_entitlement("team")
    assert e.team_weekly_pool_tcu_limit > e.weekly_tcu_limit, (
        "a pool smaller than one member's limit would make the per-member "
        "limit unreachable and the pool the only thing that ever binds"
    )
    # No single member may drain the whole pool alone.
    assert e.weekly_tcu_limit < e.team_weekly_pool_tcu_limit


def test_max_and_team_are_more_generous_than_pro():
    """Whatever the numbers, the ordering has to hold or the pricing page
    is lying."""
    pro = get_plan_entitlement("pro")
    for plan in ("max", "team"):
        e = get_plan_entitlement(plan)
        assert e.daily_tcu_limit > pro.daily_tcu_limit
        assert e.weekly_tcu_limit > pro.weekly_tcu_limit
        assert e.max_tcu_per_request > pro.max_tcu_per_request


def test_entitlement_unknown_defaults_to_start():
    e = get_plan_entitlement("enterprise_xyz")
    assert e == get_plan_entitlement("start")


# ── 2. check_and_reserve — success paths ──────────────────────────────────────

def test_reserve_creates_pending_record(db, plan_user):
    headers, user_id, session_id = plan_user("start")

    reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="start")

    record = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).first()
    assert record is not None
    assert record.status == "pending"
    assert record.tcu == get_plan_entitlement("start").max_tcu_per_request
    assert record.user_id == user_id
    assert record.session_id == session_id


def test_reserve_returns_unique_ids(db, plan_user):
    headers, user_id, session_id = plan_user("pro")
    id1 = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="pro")
    id2 = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="pro")
    assert id1 != id2


def test_finalize_usage_ignores_already_completed_reservation(db, plan_user):
    """Security Hardening — idempotency guard: finalize_usage() called
    TWICE on the same reservation (e.g. a crash-recovery cancel racing a
    legitimate finalize, or any other double-invocation) must not
    overwrite the first, real result with different values the second
    time."""
    headers, user_id, session_id = plan_user("pro")
    reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="pro")

    finalize_usage(
        db, reservation_id, message_id="m1", model="claude-sonnet",
        input_tokens=10, output_tokens=20, cached_tokens=0,
        cost_usd=0.01, tcu=5.0, duration_ms=100,
    )
    # Second call, different values — must be a no-op, not an overwrite.
    finalize_usage(
        db, reservation_id, message_id="m2-should-be-ignored", model="different-model",
        input_tokens=999, output_tokens=999, cached_tokens=0,
        cost_usd=99.0, tcu=999.0, duration_ms=999,
    )

    record = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).first()
    assert record.message_id == "m1"
    assert record.tcu == 5.0
    assert record.status == "completed"


def test_cancel_reservation_ignores_already_completed_reservation(db, plan_user):
    """Mirror of the above for cancel_reservation() — must never zero out
    an already-completed record's real TCU/cost (silent under-count of
    legitimately billable usage)."""
    headers, user_id, session_id = plan_user("pro")
    reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="pro")

    finalize_usage(
        db, reservation_id, message_id="m1", model="claude-sonnet",
        input_tokens=10, output_tokens=20, cached_tokens=0,
        cost_usd=0.01, tcu=5.0, duration_ms=100,
    )
    cancel_reservation(db, reservation_id)  # arrives late — must be ignored

    record = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).first()
    assert record.status == "completed"
    assert record.tcu == 5.0


# ── 3. check_and_reserve — daily limit ────────────────────────────────────────

def test_daily_limit_exceeded_returns_429(db, plan_user):
    from fastapi import HTTPException
    headers, user_id, session_id = plan_user("free")

    # Seeded to FREE's daily ceiling. Under the ADMIT rule (2026-09-11) a
    # request is refused when the window has NO balance left, not when the
    # flat per-request estimate stops fitting — 9 of 10 now buys a clamped
    # 1 TCU request instead of a 429, which is the whole point of the change.
    # Reaching the limit exactly is what still refuses.
    day_start, _ = _day_window()
    record = UsageRecord(
        user_id=user_id, session_id=session_id, model="test",
        input_tokens=0, output_tokens=0, cached_tokens=0,
        cost_usd=0.10, tcu=10.0, duration_ms=0, status="completed",
        created_at=day_start + timedelta(hours=12),
    )
    db.add(record)
    db.commit()

    with pytest.raises(HTTPException) as exc_info:
        check_and_reserve(db, user_id=user_id, session_id=session_id, plan="free")

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail["error"] == "daily_limit_exceeded"


def test_daily_exactly_at_limit_is_accepted(db, plan_user):
    """Reserved TCU + usage == limit → still allowed (not exceeded)."""
    headers, user_id, session_id = plan_user("free")

    # free daily=10, max_per_request=2: seed 8 TCU → 8+2=10 exactly == limit → OK
    day_start, _ = _day_window()
    record = UsageRecord(
        user_id=user_id, session_id=session_id, model="test",
        input_tokens=0, output_tokens=0, cached_tokens=0,
        cost_usd=0.08, tcu=8.0, duration_ms=0, status="completed",
        created_at=day_start.replace(hour=12),
    )
    db.add(record)
    db.commit()

    reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="free")
    assert reservation_id is not None


def test_daily_limit_exceeded_blocks_provider(client, plan_user, db):
    """HTTP 429 must be returned without calling the provider."""
    from fastapi import HTTPException
    headers, user_id, session_id = plan_user("free")

    day_start, _ = _day_window()
    record = UsageRecord(
        user_id=user_id, session_id=session_id, model="test",
        input_tokens=0, output_tokens=0, cached_tokens=0,
        cost_usd=0.10, tcu=10.0, duration_ms=0, status="completed",
        created_at=day_start + timedelta(hours=12),
    )
    db.add(record)
    db.commit()

    with patch("src.services.chat_service._router") as mock_router:
        resp = client.post(
            f"/sessions/{session_id}/chat",
            json={"content": "hello"},
            headers=headers,
        )
    assert resp.status_code == 429
    mock_router.stream.assert_not_called()


# ── 4. check_and_reserve — monthly limit retired (PARTE 10, 2026-08-31) ────────
# There is no operational monthly usage cap anymore — monthly_tcu_limit is
# None for every plan (entitlements.py). The old test_monthly_limit_exceeded_
# returns_429 asserted behavior that no longer exists by product decision;
# replaced with a positive test confirming monthly usage is now unbounded.

def test_monthly_usage_no_longer_enforced(db, plan_user):
    """Seeding a huge monthly total must NOT raise 429 — only daily/weekly
    (both still active) can block a request.

    now is mocked to a fixed instant so the seed record's placement
    relative to the day/week windows is deterministic regardless of the
    real calendar date the suite runs on (see PARTE 12).
    """
    headers, user_id, session_id = plan_user("start")

    # Fixed "now": Wednesday 2026-08-19 12:00 UTC. The weekly window (UTC
    # fallback, no user timezone set) is Sun 2026-08-16 12:00 -> Sun
    # 2026-08-23 12:00. Seed on the 1st of the month at hour=3 — outside
    # both today's day window AND this week's window.
    fixed_now = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
    seed_at = datetime(2026, 8, 1, 3, 0, tzinfo=timezone.utc)
    assert not (datetime(2026, 8, 16, 12, tzinfo=timezone.utc) <= seed_at < datetime(2026, 8, 23, 12, tzinfo=timezone.utc))
    assert not (fixed_now.replace(hour=0, minute=0, second=0, microsecond=0) <= seed_at)

    record = UsageRecord(
        user_id=user_id, session_id=session_id, model="test",
        input_tokens=0, output_tokens=0, cached_tokens=0,
        cost_usd=99.95, tcu=9_995.0, duration_ms=0, status="completed",
        created_at=seed_at,
    )
    db.add(record)
    db.commit()

    # Would have raised 429 monthly_limit_exceeded under the old rule
    # (start monthly=1000 < 9995+10). Now it must succeed.
    with patch("src.services.entitlement_service._utcnow", return_value=fixed_now):
        reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="start")
    assert reservation_id is not None


def test_max_plan_daily_limit_is_real(db, plan_user):
    """Replaces the old "max has no daily cap" test. Seeding a day's worth
    of usage past the ceiling must now be refused — that is the whole point
    of the change, and the old behaviour was an unbounded cost exposure."""
    headers, user_id, session_id = plan_user("max")
    limit = get_plan_entitlement("max").daily_tcu_limit

    day_start, _ = _day_window()
    db.add(UsageRecord(
        user_id=user_id, session_id=session_id, model="test",
        input_tokens=0, output_tokens=0, cached_tokens=0,
        cost_usd=0.0, tcu=limit, duration_ms=0, status="completed",
        created_at=day_start.replace(hour=12),
    ))
    db.commit()

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        check_and_reserve(db, user_id=user_id, session_id=session_id, plan="max")
    assert exc.value.status_code == 429
    assert exc.value.detail["error"] in ("daily_limit_exceeded", "session_limit_exceeded")


def test_max_plan_allows_generous_normal_use(db, plan_user):
    """The other half of the same coin: well inside the ceiling must still
    sail through, or the limits are not 'premium' in any useful sense."""
    headers, user_id, session_id = plan_user("max")

    day_start, _ = _day_window()
    db.add(UsageRecord(
        user_id=user_id, session_id=session_id, model="test",
        input_tokens=0, output_tokens=0, cached_tokens=0,
        cost_usd=0.0, tcu=50.0, duration_ms=0, status="completed",
        created_at=day_start.replace(hour=12),
    ))
    db.commit()

    assert check_and_reserve(db, user_id=user_id, session_id=session_id, plan="max")


# ── 5. finalize_usage ─────────────────────────────────────────────────────────

def test_finalize_updates_record_with_real_values(db, plan_user):
    headers, user_id, session_id = plan_user("start")
    reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="start")

    record = finalize_usage(
        db, reservation_id,
        message_id="msg-001",
        model="claude-sonnet-5",
        input_tokens=500,
        output_tokens=200,
        cached_tokens=0,
        cost_usd=0.003,
        tcu=0.3,
        duration_ms=1200,
    )

    assert record.status == "completed"
    assert float(record.tcu) == pytest.approx(0.3, abs=1e-6)
    assert float(record.cost_usd) == pytest.approx(0.003, abs=1e-9)
    assert record.model == "claude-sonnet-5"
    assert record.input_tokens == 500
    assert record.output_tokens == 200
    assert record.message_id == "msg-001"
    assert record.duration_ms == 1200


def test_finalize_replaces_estimated_with_real_tcu(db, plan_user):
    """Real TCU (0.3) is much less than the flat per-request estimate."""
    estimate = get_plan_entitlement("start").max_tcu_per_request
    headers, user_id, session_id = plan_user("start")
    reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="start")

    pending = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).first()
    assert float(pending.tcu) == pytest.approx(estimate, abs=1e-6)

    finalize_usage(
        db, reservation_id,
        message_id=None, model="m", input_tokens=0, output_tokens=0,
        cached_tokens=0, cost_usd=0.003, tcu=0.3, duration_ms=0,
    )

    db.refresh(pending)
    assert float(pending.tcu) == pytest.approx(0.3, abs=1e-6)  # replaced by real


# ── Teste 2/3 (connection-retention follow-up) — finalize_usage no longer ──
# calls db.refresh(record); persisted values remain correct, verified via a
# genuinely independent Session (not the ORM identity map's lazy-reload).

def test_finalize_usage_does_not_call_db_refresh(db, plan_user, monkeypatch):
    """The removed db.refresh(record) served no purpose (return value never
    captured by any caller in the codebase) and kept a pool connection
    checked out until the whole streaming response closed. This asserts
    the BEHAVIOR (refresh is never invoked), not just that the source line
    is gone."""
    headers, user_id, session_id = plan_user("start")
    reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="start")

    refresh_calls = []
    orig_refresh = db.refresh

    def spy_refresh(*args, **kwargs):
        refresh_calls.append(args)
        return orig_refresh(*args, **kwargs)

    monkeypatch.setattr(db, "refresh", spy_refresh)

    finalize_usage(
        db, reservation_id,
        message_id="msg-002", model="claude-sonnet-5",
        input_tokens=10, output_tokens=5, cached_tokens=0,
        cost_usd=0.02, tcu=0.5, duration_ms=100,
    )

    assert refresh_calls == [], f"finalize_usage() must never call db.refresh(); got {refresh_calls}"


def test_finalize_usage_persists_completed_record_via_independent_session(db, plan_user):
    """Verifies the actual database row, through a SEPARATE Session bound
    to the same underlying connection/engine — bypasses the original
    Session's identity map entirely, so this proves the data is genuinely
    correct in the database, not merely correct via SQLAlchemy's
    lazy-reload-of-an-expired-object mechanism."""
    from sqlalchemy.orm import sessionmaker

    headers, user_id, session_id = plan_user("start")
    reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="start")

    finalize_usage(
        db, reservation_id,
        message_id="msg-003", model="claude-sonnet-5",
        input_tokens=42, output_tokens=7, cached_tokens=3,
        cost_usd=0.00123, tcu=1.23, duration_ms=250,
    )

    IndependentSession = sessionmaker(bind=db.get_bind())
    fresh_db = IndependentSession()
    try:
        record = fresh_db.query(UsageRecord).filter(UsageRecord.id == reservation_id).one()
        assert record.status == "completed"
        assert float(record.tcu) == pytest.approx(1.23, abs=1e-6)
        assert float(record.cost_usd) == pytest.approx(0.00123, abs=1e-9)
        assert record.message_id == "msg-003"
        assert record.input_tokens == 42
        assert record.output_tokens == 7
    finally:
        fresh_db.close()


# ── 6. cancel_reservation ─────────────────────────────────────────────────────

def test_cancel_sets_failed_and_zeroes_tcu(db, plan_user):
    headers, user_id, session_id = plan_user("start")
    reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="start")

    cancel_reservation(db, reservation_id)

    record = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).first()
    assert record.status == "failed"
    assert record.tcu == 0.0
    assert record.cost_usd == 0.0


def test_cancelled_reservation_does_not_count_against_quota(db, plan_user):
    """After cancel, the quota is freed and a new reservation is accepted."""
    headers, user_id, session_id = plan_user("free")

    # free daily=10, max_per_request=2: seed 8 TCU
    day_start, _ = _day_window()
    record = UsageRecord(
        user_id=user_id, session_id=session_id, model="test",
        input_tokens=0, output_tokens=0, cached_tokens=0,
        cost_usd=0.08, tcu=8.0, duration_ms=0, status="completed",
        created_at=day_start.replace(hour=12),
    )
    db.add(record)
    db.commit()

    # First reservation: 8+2=10 → accepted
    res1 = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="free")
    # Cancel it
    cancel_reservation(db, res1)
    # Now 8+0(failed)=8 → second reservation accepted
    res2 = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="free")
    assert res2 is not None
    assert res2 != res1


def test_cancel_nonexistent_reservation_is_safe(db):
    """cancel_reservation on a missing ID must not raise."""
    cancel_reservation(db, "00000000-0000-0000-0000-000000000000")


# ── 7. get_user_period_usage ──────────────────────────────────────────────────

def test_period_usage_zero_on_fresh_user(db, plan_user):
    headers, user_id, session_id = plan_user("start")
    result = get_user_period_usage(db, user_id=user_id, plan="start")

    assert result["plan"] == "start"
    daily_limit = get_plan_entitlement("start").daily_tcu_limit
    assert result["daily"]["used"] == 0.0
    assert result["daily"]["limit"] == daily_limit
    assert result["daily"]["remaining"] == daily_limit
    # PARTE 10 — no operational monthly usage cap; limit/remaining are null.
    assert result["monthly"]["used"] == 0.0
    assert result["monthly"]["limit"] is None
    assert result["monthly"]["remaining"] is None


def test_period_usage_excludes_pending_records(db, plan_user):
    """Pending (in-flight) records are excluded from display usage."""
    headers, user_id, session_id = plan_user("start")
    reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="start")

    result = get_user_period_usage(db, user_id=user_id, plan="start")
    assert result["daily"]["used"] == 0.0  # pending excluded from display


def test_period_usage_counts_completed(db, plan_user):
    headers, user_id, session_id = plan_user("start")
    reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="start")
    finalize_usage(
        db, reservation_id,
        message_id=None, model="m", input_tokens=100, output_tokens=50,
        cached_tokens=0, cost_usd=0.005, tcu=0.5, duration_ms=500,
    )

    result = get_user_period_usage(db, user_id=user_id, plan="start")
    assert result["daily"]["used"] == 0.5
    assert result["daily"]["remaining"] == get_plan_entitlement("start").daily_tcu_limit - 0.5


def test_period_usage_excludes_failed_records(db, plan_user):
    headers, user_id, session_id = plan_user("start")
    reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="start")
    cancel_reservation(db, reservation_id)

    result = get_user_period_usage(db, user_id=user_id, plan="start")
    assert result["daily"]["used"] == 0.0


def test_period_usage_remaining_never_negative(db, plan_user):
    """remaining is capped at 0 even if used > limit (agent overshoot scenario)."""
    headers, user_id, session_id = plan_user("free")

    # Directly insert a completed record exceeding daily limit
    day_start, _ = _day_window()
    record = UsageRecord(
        user_id=user_id, session_id=session_id, model="test",
        input_tokens=0, output_tokens=0, cached_tokens=0,
        cost_usd=0.15, tcu=15.0, duration_ms=0, status="completed",
        created_at=day_start.replace(hour=12),
    )
    db.add(record)
    db.commit()

    result = get_user_period_usage(db, user_id=user_id, plan="free")
    assert result["daily"]["remaining"] == 0.0  # capped, not negative


def test_period_usage_reports_the_real_max_limits(db, plan_user):
    headers, user_id, session_id = plan_user("max")
    result = get_user_period_usage(db, user_id=user_id, plan="max")
    assert result["daily"]["limit"] == get_plan_entitlement("max").daily_tcu_limit
    assert result["daily"]["remaining"] is not None
    # Monthly stays unbounded — the weekly window is the ceiling that binds.
    assert result["monthly"]["limit"] is None


# ── 8. GET /users/me/usage endpoint ──────────────────────────────────────────

def test_usage_endpoint_returns_new_shape(client, plan_user):
    headers, user_id, session_id = plan_user("start")

    resp = client.get("/users/me/usage", headers=headers)
    assert resp.status_code == 200
    data = resp.json()

    assert "plan" in data
    assert "daily" in data
    assert "monthly" in data
    assert "used" in data["daily"]
    assert "limit" in data["daily"]
    assert "remaining" in data["daily"]


def test_usage_endpoint_requires_auth(client):
    resp = client.get("/users/me/usage")
    assert resp.status_code == 401


def test_usage_endpoint_reflects_plan(client, plan_user):
    headers, user_id, session_id = plan_user("pro")
    resp = client.get("/users/me/usage", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["plan"] == "pro"
    assert resp.json()["daily"]["limit"] == get_plan_entitlement("pro").daily_tcu_limit
    assert resp.json()["monthly"]["limit"] is None  # PARTE 10


# ── 9. Provider not called when 429 ──────────────────────────────────────────

def test_agent_429_does_not_call_provider(client, plan_user, db):
    # "start", not "free" (2026-09-07): FREE's supports_agent_mode=False
    # is now actually enforced (sessions.py's _reject_if_agent_mode_not_
    # entitled), so a FREE user hitting /agent gets 403 agent_mode_not_
    # entitled before the TCU check this test exists to exercise ever
    # runs. "start" supports agent mode, so the daily-TCU-limit path this
    # test is actually about is still reachable — seeded amounts adjusted
    # to START's own numbers (daily_tcu_limit=100.0, max_tcu_per_request=
    # 10.0 — src/entitlements.py): 95 used + 10 estimated > 100 -> 429.
    headers, user_id, session_id = plan_user("start")

    day_start, _ = _day_window()
    record = UsageRecord(
        user_id=user_id, session_id=session_id, model="test",
        input_tokens=0, output_tokens=0, cached_tokens=0,
        cost_usd=0.95, tcu=95.0, duration_ms=0, status="completed",
        created_at=day_start.replace(hour=12),
    )
    db.add(record)
    db.commit()

    with patch("src.services.agent_service._router") as mock_router:
        resp = client.post(
            f"/sessions/{session_id}/agent",
            json={"content": "do something"},
            headers=headers,
        )

    assert resp.status_code == 429
    mock_router.complete.assert_not_called()


# ── 10. Pending records count against quota ───────────────────────────────────

def test_pending_reservation_counts_against_quota(db, plan_user):
    """A pending (in-flight) reservation counts in the quota check."""
    from fastapi import HTTPException
    headers, user_id, session_id = plan_user("free")

    # Seed 8 TCU completed (daily=10, max_per_request=2 → 8+2=10 exactly → OK)
    day_start, _ = _day_window()
    record = UsageRecord(
        user_id=user_id, session_id=session_id, model="test",
        input_tokens=0, output_tokens=0, cached_tokens=0,
        cost_usd=0.08, tcu=8.0, duration_ms=0, status="completed",
        created_at=day_start.replace(hour=12),
    )
    db.add(record)
    db.commit()

    # First reservation: 8+2=10 == limit → accepted
    res1 = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="free")
    assert res1 is not None

    # Second reservation: 8+2(pending)+2=12 > 10 → REJECTED
    with pytest.raises(Exception) as exc_info:
        check_and_reserve(db, user_id=user_id, session_id=session_id, plan="free")
    assert exc_info.value.status_code == 429


# ── 11. _sum_active_tcu helpers ──────────────────────────────────────────────

def test_sum_active_tcu_excludes_outside_period(db, plan_user):
    """Records outside the period window must not be counted."""
    headers, user_id, session_id = plan_user("start")

    from datetime import timedelta
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=2)

    record = UsageRecord(
        user_id=user_id, session_id=session_id, model="test",
        input_tokens=0, output_tokens=0, cached_tokens=0,
        cost_usd=5.0, tcu=500.0, duration_ms=0, status="completed",
        created_at=yesterday,
    )
    db.add(record)
    db.commit()

    day_start, day_end = _day_window()
    total = _sum_active_tcu(db, user_id, day_start, day_end)
    assert total == 0.0


# ── Beta hardening (2026-09-10) — Agent iteration budget ────────────────────

def test_agent_iteration_budget_stops_before_exceeding_daily_limit(db, plan_user):
    """
    check_agent_iteration_budget() must refuse to let the loop continue
    once one more max_tcu_per_request-worth of headroom would push the
    user's REAL running total over the daily limit — the AGENT OVERSHOOT
    gap this function closes.

    start plan: daily_tcu_limit=100, max_tcu_per_request=10. Simulating
    each iteration "really" costing exactly max_tcu_per_request (the
    documented worst case): iterations 1-9 (accumulated 10..90) must all
    be allowed (90+10=100 == limit, not exceeded); iteration 10
    (accumulated 100) must be refused (100+10=110 > 100).
    """
    headers, user_id, session_id = plan_user("start")
    reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="start")

    # Derived rather than hardcoded: the binding window for a loop inside a
    # single session is the narrower of the session and daily ceilings, less
    # what the reservation already holds. Writing the expectation this way
    # means re-windowing a plan changes one table, not this test.
    # Under the ADMIT rule the loop continues while the window still has
    # balance, so the last allowed iteration is the one that brings the total
    # to just under the ceiling — the per-request estimate no longer reserves
    # a slice that can never be spent.
    e = get_plan_entitlement("start")
    ceiling = min(e.session_tcu_limit, e.daily_tcu_limit)
    step = 1.0
    expected_iterations = int(ceiling / step) - 1

    accumulated = 0.0
    allowed_iterations = 0
    for _ in range(expected_iterations + 6):
        accumulated += step
        ok = check_agent_iteration_budget(db, reservation_id, user_id, "start", accumulated)
        if not ok:
            break
        allowed_iterations += 1

    assert allowed_iterations == expected_iterations, (
        f"expected exactly {expected_iterations} allowed iterations before the "
        f"budget was exceeded, got {allowed_iterations}"
    )

    # The reservation's own tcu must reflect the REAL accumulated total at
    # the moment it stopped — updated even on the call that refused to
    # continue, never left at the original flat estimate nor at the
    # last-allowed value.
    record = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).first()
    assert float(record.tcu) == pytest.approx((expected_iterations + 1) * step)
    assert record.status == "pending"  # still pending — finalize_usage() (not this function) closes it out


def test_agent_iteration_budget_allows_continuation_within_budget(db, plan_user):
    """Sanity check for the allowed side: well within budget, every check succeeds."""
    headers, user_id, session_id = plan_user("pro")  # session=28, max_tcu_per_request=8
    reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="pro")

    for accumulated in (5.0, 10.0, 15.0, 20.0):
        assert check_agent_iteration_budget(db, reservation_id, user_id, "pro", accumulated) is True


def test_agent_iteration_budget_binds_on_max_too(db, plan_user):
    """Replaces "unlimited plan never blocks". An agent loop is exactly the
    shape of workload that could run up unbounded cost, so MAX's ceiling has
    to apply to it as well — a budget a ReAct loop can walk around is not a
    budget."""
    from src.services.entitlement_service import check_agent_iteration_budget

    headers, user_id, session_id = plan_user("max")
    reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="max")

    # Well inside the ceiling: the loop keeps going.
    assert check_agent_iteration_budget(db, reservation_id, user_id, "max", 50.0) is True

    # Past it: the loop is stopped rather than allowed to keep spending.
    assert check_agent_iteration_budget(db, reservation_id, user_id, "max", 10_000.0) is False

def test_reclaim_expired_reservation_recovers_old_pending(db, plan_user):
    """A 'pending' reservation older than the TTL must be reclaimed:
    status -> 'failed', tcu/cost_usd zeroed — exactly cancel_reservation()'s
    own transition, so it stops counting against quota and never blocks
    the user indefinitely."""
    from src.services.entitlement_service import reclaim_expired_reservations
    from datetime import timedelta

    headers, user_id, session_id = plan_user("start")
    reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="start")

    # Backdate the reservation as if its owning request's process crashed
    # long ago and never reached finalize_usage()/cancel_reservation().
    record = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).first()
    record.created_at = datetime.now(timezone.utc) - timedelta(seconds=3600)
    db.commit()

    reclaimed_count = reclaim_expired_reservations(db, ttl_seconds=1800.0)
    assert reclaimed_count == 1

    db.refresh(record)
    assert record.status == "failed"
    assert float(record.tcu) == 0.0
    assert float(record.cost_usd) == 0.0


def test_reclaim_expired_reservation_never_touches_recent_pending(db, plan_user):
    """A reservation created moments ago (a genuinely in-flight request)
    must NEVER be reclaimed just because a sweep happens to run — only
    age past the TTL makes a reservation eligible."""
    from src.services.entitlement_service import reclaim_expired_reservations

    headers, user_id, session_id = plan_user("start")
    reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="start")

    reclaimed_count = reclaim_expired_reservations(db, ttl_seconds=1800.0)
    assert reclaimed_count == 0

    record = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).first()
    assert record.status == "pending"
    # untouched — still the original flat estimate
    assert float(record.tcu) == get_plan_entitlement("start").max_tcu_per_request


def test_reclaim_expired_reservations_never_touches_completed_records(db, plan_user):
    """A 'completed' record must NEVER be reclaimed, no matter how old —
    the sweep's WHERE clause only ever matches status='pending'."""
    from src.services.entitlement_service import reclaim_expired_reservations
    from datetime import timedelta

    headers, user_id, session_id = plan_user("start")
    old_completed = UsageRecord(
        user_id=user_id, session_id=session_id, model="gpt-real",
        input_tokens=100, output_tokens=50, cached_tokens=0,
        cost_usd=0.05, tcu=5.0, duration_ms=1200, status="completed",
        created_at=datetime.now(timezone.utc) - timedelta(days=30),
    )
    db.add(old_completed)
    db.commit()

    reclaimed_count = reclaim_expired_reservations(db, ttl_seconds=1800.0)
    assert reclaimed_count == 0

    db.refresh(old_completed)
    assert old_completed.status == "completed"
    assert float(old_completed.tcu) == 5.0
    assert float(old_completed.cost_usd) == 0.05


def test_reclaim_expired_reservations_is_idempotent(db, plan_user):
    """Running the sweep twice in a row must be safe — the second run
    reclaims zero (nothing left eligible), never double-applies the
    'failed'/zeroed transition or errors on an already-reclaimed row."""
    from src.services.entitlement_service import reclaim_expired_reservations
    from datetime import timedelta

    headers, user_id, session_id = plan_user("start")
    reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="start")
    record = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).first()
    record.created_at = datetime.now(timezone.utc) - timedelta(seconds=3600)
    db.commit()

    first_pass = reclaim_expired_reservations(db, ttl_seconds=1800.0)
    second_pass = reclaim_expired_reservations(db, ttl_seconds=1800.0)

    assert first_pass == 1
    assert second_pass == 0

    db.refresh(record)
    assert record.status == "failed"
    assert float(record.tcu) == 0.0


# ── POSTGRESQL_INTEGRATION — real PostgreSQL, gated by TEST_POSTGRES_URL ──────

@pytest.mark.skipif(not postgres_available(), reason="TEST_POSTGRES_URL not set or PostgreSQL unreachable — see tests/postgres_helpers.py")
def test_concurrent_reservation_only_one_succeeds():
    """
    Scenario: start plan's daily_tcu_limit=45 (see entitlements.py),
    used=35 seeded, two concurrent threads each attempt to reserve 10 TCU
    (35+10=45 OK, but a second concurrent 10 would push to 55 > 45).

    Fixture recalibrated 2026-09-23: an earlier revision seeded 90 against
    an assumed limit of 100 — stale after entitlements.py's economics
    hardening lowered start's daily_tcu_limit to 45. A second recalibration
    (seed=20 against session_tcu_limit=30) turned out to target the WRONG
    window: session_tcu_limit's window starts at this session's own first
    touch (resolve_or_start_session), which happens strictly after the seed
    row is inserted — so a seed can never fall inside it, and that revision
    never reached the race this test measures at all (both threads always
    read session_used=0 and both succeeded, sequentially or concurrently;
    confirmed with a standalone non-threaded probe). daily_tcu_limit's
    window is calendar-day-based and does include the seed, so it is the
    window this fixture now targets. The concurrency guarantee under test
    is unchanged; only the numbers that must straddle the currently-binding
    limit were updated.

    Third recalibration (2026-09-23, determinism): the seed's created_at is
    now `now - 60s`, not bare `now`. A live-Postgres reproduction (~1/10 to
    ~1/300, see the session's own investigation) showed the SAME instant
    used as `now` at seed-insertion time and as this session's own
    session_started_at (both threads' first touch of check_and_reserve())
    can occasionally collide or invert by a hair under real scheduling
    jitter, letting the seed land AT OR AFTER session_started_at and get
    counted toward session_tcu_limit=30 too — tripping BOTH threads before
    either ever reaches the daily-window race this test exists to measure.
    A 60s backdate makes that impossible by construction (far beyond any
    realistic scheduling jitter): the seed is unambiguously BEFORE any
    session window this test's own two threads could ever open, so only
    daily_tcu_limit governs, deterministically.

    Expected: exactly one request is authorized (gets a reservation_id),
    the other is blocked with HTTPException(429). Total committed TCU never
    exceeds the daily limit.

    Under READ COMMITTED with the advisory lock check_and_reserve() takes
    (pg_advisory_xact_lock(hashtext('tcu_' || user_id))): the second
    transaction blocks until the first commits, then re-reads the now-
    up-to-date SUM and correctly sees itself as over budget.

    Without the lock (or on a dialect where it's a no-op, e.g. SQLite),
    both could read the same stale SUM and both be authorized — this test
    is the acceptance criterion that the lock actually prevents that on
    real PostgreSQL. Uses its own engine/session, entirely independent of
    the SQLite `db`/`client` fixtures used by the rest of this file.
    """
    import threading
    import uuid as uuid_module

    from fastapi import HTTPException
    from sqlalchemy import create_engine, func
    from sqlalchemy.orm import sessionmaker

    import src.models  # noqa: F401 — registers all models on Base.metadata
    from src.database import Base
    from src.models.user import User

    url = get_test_postgres_url()
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)

    setup_db = Session()
    try:
        user = User(
            id=str(uuid_module.uuid4()),
            email=f"pgconcurrency_{uuid_module.uuid4().hex[:8]}@test.com",
            hashed_password="x",
            plan="start",
        )
        setup_db.add(user)
        setup_db.commit()
        user_id = user.id

        seed = UsageRecord(
            user_id=user_id, session_id=None, model="seed",
            input_tokens=0, output_tokens=0, cached_tokens=0,
            cost_usd=0.35, tcu=35.0, duration_ms=0, status="completed",
            # 60s in the past — unambiguously before any session_started_at
            # the threads below could ever open, and still well inside
            # today's calendar-day daily window. See this test's own
            # docstring, "Third recalibration", for why a bare `now()` seed
            # was non-deterministic.
            created_at=datetime.now(timezone.utc) - timedelta(seconds=60),
        )
        setup_db.add(seed)
        setup_db.commit()
    finally:
        setup_db.close()

    results: dict[str, tuple] = {}
    barrier = threading.Barrier(2)

    def attempt(label: str) -> None:
        session = Session()
        try:
            barrier.wait(timeout=10)  # maximize the chance both threads race together
            reservation_id = check_and_reserve(session, user_id=user_id, session_id=None, plan="start")
            results[label] = ("ok", reservation_id)
        except HTTPException as exc:
            results[label] = ("blocked", exc.status_code)
        except Exception as exc:  # capture anything unexpected instead of hanging the test
            results[label] = ("error", repr(exc))
        finally:
            session.close()

    t_a = threading.Thread(target=attempt, args=("A",))
    t_b = threading.Thread(target=attempt, args=("B",))
    t_a.start()
    t_b.start()
    t_a.join(timeout=20)
    t_b.join(timeout=20)

    try:
        outcomes = [results.get("A"), results.get("B")]
        assert None not in outcomes, f"a thread did not finish in time: {outcomes}"

        ok_count = sum(1 for o in outcomes if o[0] == "ok")
        blocked_count = sum(1 for o in outcomes if o[0] == "blocked")
        assert ok_count == 1, f"expected exactly one successful reservation, got: {outcomes}"
        assert blocked_count == 1, f"expected exactly one 429-blocked request, got: {outcomes}"
        blocked_status = next(o[1] for o in outcomes if o[0] == "blocked")
        assert blocked_status == 429

        verify_db = Session()
        try:
            total = verify_db.query(func.coalesce(func.sum(UsageRecord.tcu), 0.0)).filter(
                UsageRecord.user_id == user_id,
                UsageRecord.status != "failed",
            ).scalar()
            assert total <= 45.0, f"total committed TCU {total} exceeded the daily limit of 45"
        finally:
            verify_db.close()
    finally:
        engine.dispose()


@pytest.mark.skipif(not postgres_available(), reason="TEST_POSTGRES_URL not set or PostgreSQL unreachable — see tests/postgres_helpers.py")
def test_concurrent_agents_same_user_respect_daily_limit():
    """
    Beta hardening (2026-09-10) — the multi-agent counterpart of
    test_concurrent_reservation_only_one_succeeds() above, proving
    check_agent_iteration_budget() (not just check_and_reserve()) holds
    the SAME advisory lock for its full write-then-read sequence (see
    that function's own docstring on why an earlier draft that committed
    between the write and the read would have reopened the race).

    Scenario: start plan (daily=100, max_tcu_per_request=10). Two
    reservations for the SAME user already exist, each mid-ReAct-loop
    with 40 TCU genuinely accumulated so far (tcu=40 each, SUM=80).
    Exactly ONE more max_tcu_per_request-worth of headroom (10) remains
    before hitting 100. Both "agents" simultaneously call
    check_agent_iteration_budget() reporting their next accumulated total
    as 50 (i.e. each wants to record that its next real call brought its
    own running total to 50).

    Expected: at most one of the two may see its update land such that
    the OTHER's subsequent check sees a SUM that is still within budget
    — the write-skew-free guarantee is that the two `tcu` writes and
    their corresponding limit checks never interleave, so the total
    committed TCU across both reservations never exceeds the daily limit
    by more than what a single, correctly-serialized pair of updates
    would produce. Concretely: not both may finish with tcu=50 while
    also both returning True — if both raced to write 50 (SUM=100) and
    both then read SUM=100 (100+10=110>100), both should correctly see
    exceeded=False->True (return False) rather than one incorrectly
    seeing a stale SUM=90 and returning True. Whatever the actual
    interleaving, the FINAL committed total must never exceed 100.
    """
    import threading
    import uuid as uuid_module

    from sqlalchemy import create_engine, func
    from sqlalchemy.orm import sessionmaker

    import src.models  # noqa: F401 — registers all models on Base.metadata
    from src.database import Base
    from src.models.user import User

    url = get_test_postgres_url()
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)

    setup_db = Session()
    try:
        user = User(
            id=str(uuid_module.uuid4()),
            email=f"pgagentconcurrency_{uuid_module.uuid4().hex[:8]}@test.com",
            hashed_password="x",
            plan="start",
        )
        setup_db.add(user)
        setup_db.commit()
        user_id = user.id

        reservation_a = check_and_reserve(setup_db, user_id=user_id, session_id=None, plan="start")
        reservation_b = check_and_reserve(setup_db, user_id=user_id, session_id=None, plan="start")
        for rid in (reservation_a, reservation_b):
            rec = setup_db.query(UsageRecord).filter(UsageRecord.id == rid).first()
            rec.tcu = 40.0
        setup_db.commit()
    finally:
        setup_db.close()

    results: dict[str, bool] = {}
    barrier = threading.Barrier(2)

    def attempt(label: str, reservation_id: str) -> None:
        session = Session()
        try:
            barrier.wait(timeout=10)  # maximize the chance both threads race together
            results[label] = check_agent_iteration_budget(session, reservation_id, user_id, "start", 50.0)
        except Exception as exc:  # capture anything unexpected instead of hanging the test
            results[label] = repr(exc)
        finally:
            session.close()

    t_a = threading.Thread(target=attempt, args=("A", reservation_a))
    t_b = threading.Thread(target=attempt, args=("B", reservation_b))
    t_a.start()
    t_b.start()
    t_a.join(timeout=20)
    t_b.join(timeout=20)

    try:
        assert "A" in results and "B" in results, f"a thread did not finish in time: {results}"
        assert isinstance(results["A"], bool) and isinstance(results["B"], bool), (
            f"a thread raised instead of returning cleanly: {results}"
        )

        verify_db = Session()
        try:
            total = verify_db.query(func.coalesce(func.sum(UsageRecord.tcu), 0.0)).filter(
                UsageRecord.user_id == user_id,
                UsageRecord.status != "failed",
            ).scalar()
            assert total <= 100.0, f"total committed TCU {total} exceeded the daily limit of 100"
        finally:
            verify_db.close()
    finally:
        engine.dispose()


@pytest.mark.skipif(not postgres_available(), reason="TEST_POSTGRES_URL not set or PostgreSQL unreachable — see tests/postgres_helpers.py")
def test_migration_0007_applies_on_postgresql():
    """
    `alembic upgrade head` (the full current migration chain, which
    supersedes and includes migration 0007) must apply cleanly on a real
    PostgreSQL database. Checks that ix_usage_user_created and
    ix_usage_user_status (added in migration 0007) exist afterward.

    Runs the real `alembic` console script as a subprocess with DATABASE_URL
    pointed at TEST_POSTGRES_URL — alembic/env.py reads settings.database_url
    at import time (a process-wide singleton), so this cannot safely run
    in-process alongside the SQLite-backed main test suite. A plain
    `python -c "import alembic..."` also will not work here: with this
    package's own cwd on sys.path, `import alembic` resolves to the local
    packages/core/alembic/ migrations directory instead of the installed
    library — the console script avoids that by construction.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    from sqlalchemy import create_engine, inspect, text

    url = get_test_postgres_url()
    core_dir = Path(__file__).resolve().parents[2]  # packages/core
    alembic_exe = Path(sys.executable).parent / ("alembic.exe" if os.name == "nt" else "alembic")
    assert alembic_exe.exists(), f"alembic console script not found at {alembic_exe}"

    # Guarantee a genuinely clean database regardless of what any other test
    # in this shared, persistent TEST_POSTGRES_URL instance left behind
    # (e.g. test_concurrent_reservation_only_one_succeeds' Base.metadata.
    # create_all()). Base.metadata.drop_all() alone wouldn't remove
    # alembic's own version-tracking table — dropping/recreating the whole
    # public schema is the only way to match this test's own docstring
    # promise of "a clean PostgreSQL database".
    reset_engine = create_engine(url)
    try:
        with reset_engine.connect() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
            conn.commit()
    finally:
        reset_engine.dispose()

    env = os.environ.copy()
    env["DATABASE_URL"] = url

    result = subprocess.run(
        [str(alembic_exe), "upgrade", "head"],
        cwd=str(core_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"alembic upgrade head failed against PostgreSQL:\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )

    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        index_names = {idx["name"] for idx in inspector.get_indexes("usage_records")}
        assert "ix_usage_user_created" in index_names, f"indexes found: {index_names}"
        assert "ix_usage_user_status" in index_names, f"indexes found: {index_names}"
    finally:
        engine.dispose()
