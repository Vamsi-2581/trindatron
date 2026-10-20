"""Unit tests for adaptive_budget_service — Execution Runtime V1's capacity/
tier computation. Reuses the SAME UsageRecord/EntitlementSession tables the
existing TCU engine writes to — no new financial table, no new limit field.

Test matrix
-----------
B01) No usage at all -> capacity 100%, tier normal
B02) Weekly usage pushes remaining below 50% -> tier reduce_non_critical
B03) MAX -> capacity reflects its real session/weekly windows (it is no
     longer unlimited); B03b) light use on MAX still reads as full
B04) Session inactive -> session_pct_used is None, only weekly constrains capacity
B05) priority_admitted table matches mission section 10 exactly, tier by tier
B06) capacity_remaining_pct is the MIN of session/weekly remaining (the more
     constrained dimension), not an average
B07) should_skip_llm returns True (skip) exactly when NOT changed — the
     inverse of has_state_changed, matching its own name/docstring (fixed
     as part of Persistent Agent Runtime V1, Tier 1 — this function had
     zero callers before that work wired it into the DECIDE step, so the
     old inverted "passthrough" behavior this test used to assert was
     itself the bug, not a real contract anything depended on)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base
from src.models import EntitlementSession, UsageRecord  # noqa: F401 — registers tables
from src.entitlements import get_plan_entitlement
from src.models.user import User
from src.services import adaptive_budget_service as abs_


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def user(db):
    u = User(email=f"{uuid.uuid4()}@test.com", hashed_password="x", full_name="T", plan="start", timezone="UTC")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _add_usage(db, user_id: str, tcu: float, *, created_at=None):
    rec = UsageRecord(
        user_id=user_id, model="claude", tcu=Decimal(str(tcu)), cost_usd=Decimal("0.01"),
        status="completed", created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(rec)
    db.commit()


def _start_session(db, user_id: str, *, started_at=None):
    started_at = started_at or datetime.now(timezone.utc)
    db.add(EntitlementSession(user_id=user_id, session_started_at=started_at, session_expires_at=started_at + timedelta(hours=5)))
    db.commit()


def test_B01_no_usage_full_capacity(db, user):
    snapshot = abs_.compute_capacity(db, user_id=user.id, plan=user.plan)
    assert snapshot.capacity_remaining_pct == 100.0
    assert snapshot.tier == abs_.TIER_NORMAL


def test_B02_weekly_usage_reduces_tier(db, user):
    # start plan: weekly_tcu_limit=110.0 -> 57.2 used = 52% used = 48% left.
    # No session started (session_pct_used stays None) so only the weekly
    # dimension constrains capacity here — see test_B06 for the session case.
    _add_usage(db, user.id, 57.2)
    snapshot = abs_.compute_capacity(db, user_id=user.id, plan=user.plan)
    assert snapshot.weekly_pct_used == 52.0
    assert snapshot.tier == abs_.TIER_REDUCE_NON_CRITICAL


def test_B03_max_plan_capacity_reflects_its_real_windows(db, user):
    """Replaces "unlimited plan always full capacity". MAX is no longer
    unlimited — it carries a real 5-hour session budget and a weekly one —
    so a user who has consumed everything must read as exhausted rather
    than as permanently at 100%. Reporting full capacity to someone who is
    about to be refused is the worst of both worlds."""
    user.plan = "max"
    db.commit()
    _start_session(db, user.id)
    _add_usage(db, user.id, 999999.0)
    snapshot = abs_.compute_capacity(db, user_id=user.id, plan=user.plan)
    assert snapshot.capacity_remaining_pct == 0.0
    assert snapshot.session_pct_used == 100.0
    assert snapshot.tier != abs_.TIER_NORMAL


def test_B03b_max_plan_with_light_use_is_at_full_capacity(db, user):
    """The other half: MAX must still feel unconstrained in normal use."""
    user.plan = "max"
    db.commit()
    _start_session(db, user.id)
    _add_usage(db, user.id, 3.0)  # 3 of MAX's 115-TCU session budget
    snapshot = abs_.compute_capacity(db, user_id=user.id, plan=user.plan)
    assert snapshot.capacity_remaining_pct > 95.0
    assert snapshot.tier == abs_.TIER_NORMAL


def test_B04_no_active_session_only_weekly_constrains(db, user):
    _add_usage(db, user.id, 57.2)  # weekly usage exists, but no session started
    snapshot = abs_.compute_capacity(db, user_id=user.id, plan=user.plan)
    assert snapshot.session_active is False
    assert snapshot.session_pct_used is None
    assert snapshot.weekly_pct_used == 52.0
    assert snapshot.capacity_remaining_pct == 48.0  # weekly is the only constraint


@pytest.mark.parametrize(
    "tier,priority,expected",
    [
        (abs_.TIER_NORMAL, "low", True),
        (abs_.TIER_NORMAL_OPTIMIZATION, "low", True),
        (abs_.TIER_REDUCE_NON_CRITICAL, "low", False),
        (abs_.TIER_REDUCE_NON_CRITICAL, "normal", True),
        (abs_.TIER_PRIORITY_ONLY, "normal", False),
        (abs_.TIER_PRIORITY_ONLY, "high", True),
        (abs_.TIER_PAUSE_LOW_PRIORITY, "high", False),
        (abs_.TIER_PAUSE_LOW_PRIORITY, "critical", True),
        (abs_.TIER_PAUSE_EXECUTION, "critical", True),
        (abs_.TIER_PAUSE_EXECUTION, "high", False),
    ],
)
def test_B05_priority_admitted_matches_mission_table(tier, priority, expected):
    assert abs_.priority_admitted(tier, priority) is expected


def test_B06_capacity_is_min_not_average(db, user):
    # Session heavily used (90%), weekly barely used (5%) -> capacity should
    # reflect the WORSE (session) dimension, not an average of the two.
    started = datetime.now(timezone.utc) - timedelta(hours=1)
    _start_session(db, user.id, started_at=started)
    # 27 of START's 30-TCU session budget = 90% used, while the same 27 is
    # under a quarter of its 110-TCU week. Derived from the entitlement so a
    # re-window changes one table, not this assertion.
    session_budget = get_plan_entitlement("start").session_tcu_limit
    _add_usage(db, user.id, session_budget * 0.9, created_at=started + timedelta(minutes=1))
    snapshot = abs_.compute_capacity(db, user_id=user.id, plan=user.plan)
    assert snapshot.session_pct_used == 90.0
    assert snapshot.capacity_remaining_pct == 10.0  # min(10, ~90) = 10, not the average


def test_B07_should_skip_llm_inverse_of_has_state_changed():
    assert abs_.should_skip_llm(has_state_changed=False) is True   # nothing changed -> skip the LLM
    assert abs_.should_skip_llm(has_state_changed=True) is False   # something changed -> don't skip
