"""The media pool is checked under the same per-user lock as every TCU window.

A creation's reservation is tagged "media:<kind>" the moment it is written and
the pool is summed over those records, pending ones included. So a second,
concurrent creation sees the first one's reservation before any job row exists,
and two creations can never both read a pool that has room for one.

The race itself needs real PostgreSQL (the advisory lock is a no-op on SQLite):
it is gated by TEST_POSTGRES_URL, like tests/integration/test_entitlement.py.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException

from src.media import plans
from src.models.usage import UsageRecord
from src.models.user import User
from src.services import entitlement_service, media_service
from src.services.entitlement_service import MediaPoolExhausted, check_and_reserve
from tests.postgres_helpers import get_test_postgres_url, postgres_available
from tests.week_helpers import earlier_this_week_not_today

START_POOL = (plans.media_daily_pool_tcu("start"), plans.media_weekly_pool_tcu("start"))  # (36, 88)


def _seed(db, user_id: str, tcu: float, *, model: str, status: str = "completed", seconds_ago: int = 60) -> None:
    db.add(UsageRecord(user_id=user_id, model=model, cost_usd=Decimal(str(tcu / 100)), tcu=Decimal(str(tcu)),
                       status=status, created_at=datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)))
    db.commit()


def _start_user(db, registered_user) -> User:
    db.query(User).filter(User.id == registered_user["user_id"]).update({"plan": "start"})
    db.commit()
    return db.query(User).filter(User.id == registered_user["user_id"]).one()


# ── Sequential behaviour (SQLite) ───────────────────────────────────────────

def test_a_media_reservation_is_tagged_and_counted_by_the_pool_while_pending(db, registered_user):
    user = _start_user(db, registered_user)
    reservation = check_and_reserve(db, user.id, None, "start", user.timezone, estimated_tcu=5.0,
                                    model="media:video", media_pool_tcu=START_POOL)
    row = db.query(UsageRecord).filter(UsageRecord.id == reservation).one()
    assert row.model == "media:video" and row.status == "pending"
    # No job row exists, yet the pool already counts the reservation.
    assert media_service.media_pool(db, user).daily_used == pytest.approx(5.0)


def test_the_pool_refuses_under_the_lock_even_when_chat_windows_have_room(db, registered_user):
    user = _start_user(db, registered_user)
    _seed(db, user.id, 36.0, model="media:image")  # START's daily pool, used up; the daily window (45) is not
    with pytest.raises(MediaPoolExhausted) as refused:
        check_and_reserve(db, user.id, None, "start", user.timezone, estimated_tcu=1.0,
                          model="media:video", media_pool_tcu=START_POOL)
    assert refused.value.status_code == 429 and refused.value.detail["error"] == "media_pool_exhausted"
    # The chat/agent floor is untouched: an ordinary turn still reserves.
    chat = check_and_reserve(db, user.id, None, "start", user.timezone, estimated_tcu=1.0)
    assert entitlement_service.get_reservation_budget_tcu(db, chat) == pytest.approx(1.0)


def test_the_pool_clamps_a_reservation_to_what_is_left(db, registered_user):
    user = _start_user(db, registered_user)
    _seed(db, user.id, 34.0, model="media:video")
    reservation = check_and_reserve(db, user.id, None, "start", user.timezone, estimated_tcu=5.0,
                                    model="media:video", media_pool_tcu=START_POOL)
    assert entitlement_service.get_reservation_budget_tcu(db, reservation) == pytest.approx(2.0)


def test_failed_media_and_chat_records_do_not_count_toward_the_pool(db, registered_user):
    user = _start_user(db, registered_user)
    _seed(db, user.id, 30.0, model="media:video", status="failed")
    _seed(db, user.id, 30.0, model="chat")
    pool = media_service.media_pool(db, user)
    assert pool.daily_used == 0.0 and pool.remaining == pytest.approx(36.0)


def test_the_weekly_pool_counts_earlier_days(db, registered_user):
    user = _start_user(db, registered_user)
    earlier = earlier_this_week_not_today(user.timezone)
    db.add(UsageRecord(user_id=user.id, model="media:image", cost_usd=Decimal("0.875"), tcu=Decimal("87.5"),
                       status="completed", created_at=earlier))
    db.commit()
    # 0.5 of the weekly pool (88) is left: a reservation is clamped to it, though today's pool is untouched.
    reservation = check_and_reserve(db, user.id, None, "start", user.timezone, estimated_tcu=5.0,
                                    model="media:image", media_pool_tcu=START_POOL)
    assert entitlement_service.get_reservation_budget_tcu(db, reservation) == pytest.approx(0.5)
    with pytest.raises(MediaPoolExhausted):
        check_and_reserve(db, user.id, None, "start", user.timezone, estimated_tcu=1.0,
                          model="media:image", media_pool_tcu=START_POOL)


# ── The race (real PostgreSQL) ──────────────────────────────────────────────

def _race(pool: tuple[float, float] | None) -> tuple[list, float]:
    """Two threads reserve 1 TCU of media at once, with 1 TCU left in START's daily pool."""
    from sqlalchemy import create_engine, func
    from sqlalchemy.orm import sessionmaker

    import src.models  # noqa: F401 — registers all models on Base.metadata
    from src.database import Base

    engine = create_engine(get_test_postgres_url())
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    setup = Session()
    try:
        user = User(id=str(uuid.uuid4()), email=f"mediapool_{uuid.uuid4().hex[:8]}@test.com",
                    hashed_password="x", plan="start")
        setup.add(user)
        setup.commit()
        user_id = user.id
        # 35 TCU of media earlier today (60 s back: before any session window the threads open).
        _seed(setup, user_id, 35.0, model="media:image")
    finally:
        setup.close()

    results: dict[str, tuple] = {}
    barrier = threading.Barrier(2)

    def attempt(label: str) -> None:
        session = Session()
        try:
            barrier.wait(timeout=10)
            results[label] = ("ok", check_and_reserve(session, user_id, None, "start", estimated_tcu=1.0,
                                                      model="media:video", media_pool_tcu=pool))
        except HTTPException as exc:
            results[label] = ("blocked", exc.detail.get("error") if isinstance(exc.detail, dict) else exc.status_code)
        except Exception as exc:  # noqa: BLE001 — surface anything unexpected instead of hanging
            results[label] = ("error", repr(exc))
        finally:
            session.close()

    threads = [threading.Thread(target=attempt, args=(label,)) for label in ("A", "B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    verify = Session()
    try:
        media_total = float(verify.query(func.coalesce(func.sum(UsageRecord.tcu), 0.0)).filter(
            UsageRecord.user_id == user_id, UsageRecord.status != "failed",
            UsageRecord.model.like("media:%")).scalar())
    finally:
        verify.close()
        engine.dispose()
    return [results.get("A"), results.get("B")], media_total


@pytest.mark.skipif(not postgres_available(), reason="TEST_POSTGRES_URL not set or PostgreSQL unreachable — see tests/postgres_helpers.py")
def test_postgresql_two_concurrent_creations_never_both_take_the_last_of_the_pool():
    outcomes, media_total = _race(START_POOL)
    assert None not in outcomes, outcomes
    assert sorted(o[0] for o in outcomes) == ["blocked", "ok"], outcomes
    assert next(o[1] for o in outcomes if o[0] == "blocked") == "media_pool_exhausted"
    assert media_total <= START_POOL[0] + 1e-6


@pytest.mark.skipif(not postgres_available(), reason="TEST_POSTGRES_URL not set or PostgreSQL unreachable — see tests/postgres_helpers.py")
def test_postgresql_control_without_the_pool_both_would_pass():
    """The same race without the pool: the windows alone (45 daily) let both through — the pool is what binds."""
    outcomes, media_total = _race(None)
    assert [o[0] for o in outcomes] == ["ok", "ok"], outcomes
    assert media_total == pytest.approx(37.0)
