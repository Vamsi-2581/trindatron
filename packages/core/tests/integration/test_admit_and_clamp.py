"""ADMIT + CLAMP — the last slice of a window is spendable, and nothing overruns.

Before this, a request was refused when `used + max_tcu_per_request > limit`.
That flat estimate is the most a call MIGHT cost, so the rule locked the last
per-request slice of every window out of reach permanently: 33% of START's
5-hour budget, 29% of PRO's, 26% of MAX's — quota the customer paid for and
could never spend.

The rule is now:

  admit   while the window still has any balance
  clamp   the reservation, and the call, to that remaining balance

Both halves matter. Admitting without clamping would let one call overrun a
window by up to the plan's whole static cap; clamping without admitting would
leave the tail locked. These tests hold both.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from src.entitlements import get_plan_entitlement
from src.models.entitlement_session import EntitlementSession
from src.models.usage import UsageRecord
from src.models.user import User
from src.services.entitlement_service import (
    check_agent_iteration_budget,
    check_and_reserve,
    get_reservation_budget_tcu,
)


@pytest.fixture
def plan_user(client, db):
    counter = {"n": 0}

    def _make(plan: str = "pro"):
        counter["n"] += 1
        resp = client.post("/auth/register", json={
            "email": f"clamp{counter['n']}@test.com",
            "password": "TestPass123!",
            "full_name": "Clamp User",
        })
        assert resp.status_code == 201
        token, user_id = resp.json()["access_token"], resp.json()["user_id"]
        headers = {"Authorization": f"Bearer {token}"}
        user = db.query(User).filter(User.id == user_id).first()
        user.plan = plan
        db.commit()
        session_id = client.post("/sessions", json={"title": "t"}, headers=headers).json()["id"]
        return headers, user_id, session_id

    return _make


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _seed(db, user_id: str, tcu: float, *, when: datetime | None = None) -> None:
    db.add(UsageRecord(
        user_id=user_id, session_id=None, model="test",
        input_tokens=0, output_tokens=0, cached_tokens=0,
        cost_usd=tcu / 100.0, tcu=tcu, duration_ms=0, status="completed",
        created_at=when or _utcnow(),
    ))
    db.commit()


def _session_of(db, user_id: str) -> EntitlementSession:
    return db.query(EntitlementSession).filter(EntitlementSession.user_id == user_id).one()


def _clear_pending(db, user_id: str) -> None:
    """Drop the pending reservation left by the call that opened the window.

    Opening a 5-hour window requires a real reservation (that is the rule —
    the window starts on the first operation that actually reserves), and
    that reservation counts against the budget like any other. These tests
    are about what happens at a KNOWN level of usage, so the setup call's
    own reservation is cleared before the level is seeded."""
    db.query(UsageRecord).filter(
        UsageRecord.user_id == user_id, UsageRecord.status == "pending"
    ).delete()
    db.commit()


# ── The tail is spendable ────────────────────────────────────────────────

def test_a_request_is_admitted_while_any_balance_remains(db, plan_user):
    _, user_id, session_id = plan_user("pro")
    e = get_plan_entitlement("pro")

    # Open the window, then fill it to just under the ceiling.
    check_and_reserve(db, user_id=user_id, session_id=session_id, plan="pro")
    window = _session_of(db, user_id)
    _clear_pending(db, user_id)
    _seed(db, user_id, e.session_tcu_limit - 5.0,
          when=window.session_started_at + timedelta(minutes=1))

    # 5 TCU left, 8 TCU flat estimate: the old rule refused this outright.
    reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="pro")
    assert reservation_id

    budget = get_reservation_budget_tcu(db, reservation_id)
    assert 0 < budget <= 5.0, f"budget {budget} should be clamped to the remaining balance"
    assert budget < e.max_tcu_per_request


def test_a_full_window_is_still_refused(db, plan_user):
    _, user_id, session_id = plan_user("pro")
    e = get_plan_entitlement("pro")

    check_and_reserve(db, user_id=user_id, session_id=session_id, plan="pro")
    window = _session_of(db, user_id)
    _clear_pending(db, user_id)
    _seed(db, user_id, e.session_tcu_limit,
          when=window.session_started_at + timedelta(minutes=1))

    with pytest.raises(HTTPException) as exc:
        check_and_reserve(db, user_id=user_id, session_id=session_id, plan="pro")
    assert exc.value.status_code == 429
    assert exc.value.detail["error"] == "session_limit_exceeded"
    # And the refusal still tells the user when it comes back.
    assert exc.value.detail["resets_at"]


# ── No overshoot ─────────────────────────────────────────────────────────

def test_a_reservation_never_exceeds_the_remaining_balance(db, plan_user):
    """The property that makes ADMIT safe: whatever is reserved must be
    spendable, so the sum of reservations can never pass the ceiling."""
    _, user_id, session_id = plan_user("pro")
    e = get_plan_entitlement("pro")
    check_and_reserve(db, user_id=user_id, session_id=session_id, plan="pro")
    window = _session_of(db, user_id)
    _clear_pending(db, user_id)

    for used in (0.0, 10.0, 30.0, 55.0, 59.5):
        db.query(UsageRecord).filter(
            UsageRecord.user_id == user_id, UsageRecord.status == "completed"
        ).delete()
        db.commit()
        _seed(db, user_id, used, when=window.session_started_at + timedelta(minutes=1))

        remaining = e.session_tcu_limit - used
        rid = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="pro")
        budget = get_reservation_budget_tcu(db, rid)
        assert budget <= remaining + 1e-9, (
            f"with {used} used ({remaining} left) the reservation took {budget}"
        )
        assert budget <= e.max_tcu_per_request + 1e-9
        # Clear the pending reservation before the next round.
        db.query(UsageRecord).filter(
            UsageRecord.user_id == user_id, UsageRecord.status == "pending"
        ).delete()
        db.commit()


def test_the_agent_loop_stops_at_the_ceiling_not_before_it(db, plan_user):
    """The ReAct loop follows the same ADMIT rule, so it spends the window
    down to the ceiling rather than stopping one flat estimate short."""
    _, user_id, session_id = plan_user("pro")
    e = get_plan_entitlement("pro")
    reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="pro")

    ceiling = min(e.session_tcu_limit, e.daily_tcu_limit)
    # Just under: the loop may continue.
    assert check_agent_iteration_budget(
        db, reservation_id, user_id, "pro", ceiling - 0.5) is True
    # At the ceiling: it must stop.
    assert check_agent_iteration_budget(
        db, reservation_id, user_id, "pro", ceiling) is False


# ── The router cannot be handed more than the reservation allows ─────────

def test_the_call_budget_is_the_reservation_budget(db, plan_user):
    """The clamp reaches the provider call through this value — chat_service
    and agent_service pass it as RoutingRequest.max_cost_usd_override."""
    _, user_id, session_id = plan_user("max")
    rid = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="max")
    budget_tcu = get_reservation_budget_tcu(db, rid)
    assert budget_tcu == get_plan_entitlement("max").max_tcu_per_request

    from trinity_router.limits import get_plan_limits
    from trinity_router.router import RoutingRequest, _effective_limits
    from trinity_router import TaskType

    req = RoutingRequest(
        messages=[], task_type=TaskType.CHAT, user_plan="max", session_id="s",
        max_cost_usd_override=budget_tcu / 100.0,
    )
    effective = _effective_limits(req).max_estimated_cost_per_request_usd
    assert effective <= budget_tcu / 100.0 + 1e-9
    assert effective <= get_plan_limits("max").max_estimated_cost_per_request_usd


def test_an_unknown_reservation_authorises_nothing(db):
    assert get_reservation_budget_tcu(db, str(uuid.uuid4())) == 0.0
