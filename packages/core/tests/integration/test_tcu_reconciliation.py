"""
Financial reconciliation regression test (Beta hardening, 2026-09-10).

Confirms SUM(cost_usd)×100, SUM(tcu), and the daily/monthly totals
get_user_period_usage() reports are all mathematically consistent within
the documented rounding rule (TCU = round(cost_usd × 100, 4) — see
trinity_router/tcu.py's own calculate_tcu()) — the same single source of
truth (usage_records) feeds every one of these figures; this test is the
automated guard against a future change silently breaking that.

Not a dashboard, not a new reconciliation mechanism — a pytest regression
test, per the mission's own scope for this etapa.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import func

from src.models.usage import UsageRecord
from src.services.entitlement_service import (
    _day_window,
    _sum_active_tcu,
    check_and_reserve,
    get_user_period_usage,
)
from trinity_router.tcu import calculate_tcu


def _seed_completed_records(db, user_id, session_id, cost_values):
    now = datetime.now(timezone.utc)
    for i, cost in enumerate(cost_values):
        tcu = calculate_tcu(cost)
        db.add(UsageRecord(
            user_id=user_id, session_id=session_id, model=f"model-{i}",
            input_tokens=100 * (i + 1), output_tokens=50 * (i + 1), cached_tokens=0,
            cost_usd=cost, tcu=tcu, duration_ms=100, status="completed",
            created_at=now,
        ))
    db.commit()


def test_sum_cost_usd_and_sum_tcu_reconcile_within_rounding_tolerance(db, db2_user_factory):
    """SUM(tcu) must equal SUM(cost_usd) × 100 within the tolerance
    implied by calculate_tcu()'s own per-record round(..., 4) contract —
    at most 0.00005 of drift per record, never an unbounded/growing gap."""
    user = db2_user_factory("reconcile1@test.com", plan="pro")
    user_id, session_id = user.id, None

    cost_values = [0.0031, 0.1247, 0.00009, 2.5, 0.0000137, 0.4999, 0.01, 0.333333]
    _seed_completed_records(db, user_id, session_id, cost_values)

    sum_cost_usd = float(
        db.query(func.coalesce(func.sum(UsageRecord.cost_usd), 0.0))
        .filter(UsageRecord.user_id == user_id, UsageRecord.status == "completed")
        .scalar()
    )
    sum_tcu = float(
        db.query(func.coalesce(func.sum(UsageRecord.tcu), 0.0))
        .filter(UsageRecord.user_id == user_id, UsageRecord.status == "completed")
        .scalar()
    )

    max_drift = 0.0001 * len(cost_values)  # per-record rounding bound (round to 4 decimals)
    drift = abs(sum_tcu - sum_cost_usd * 100)
    assert drift <= max_drift, (
        f"SUM(tcu)={sum_tcu} vs SUM(cost_usd)*100={sum_cost_usd * 100} "
        f"drift={drift} exceeds tolerance {max_drift}"
    )


def test_period_usage_matches_direct_sum_tcu(db, db2_user_factory):
    """get_user_period_usage()'s daily/monthly 'used' figures must equal
    a direct SUM(tcu) over the same (user, status='completed', window) —
    same single source of truth, no second, independently-computed
    number that could silently drift from it."""
    user = db2_user_factory("reconcile2@test.com", plan="pro")
    user_id, session_id = user.id, None

    cost_values = [0.02, 0.15, 1.3333, 0.00042, 0.999999]
    _seed_completed_records(db, user_id, session_id, cost_values)

    sum_tcu = float(
        db.query(func.coalesce(func.sum(UsageRecord.tcu), 0.0))
        .filter(UsageRecord.user_id == user_id, UsageRecord.status == "completed")
        .scalar()
    )

    period = get_user_period_usage(db, user_id, "pro")
    assert period["daily"]["used"] == pytest.approx(round(sum_tcu, 4), abs=1e-9)
    assert period["monthly"]["used"] == pytest.approx(round(sum_tcu, 4), abs=1e-9)


def test_enforcement_sum_matches_display_sum_when_all_completed(db, db2_user_factory):
    """When every record is 'completed' (no pending/failed in flight),
    the ENFORCEMENT source (_sum_active_tcu, filters status != 'failed')
    and the DISPLAY source (get_user_period_usage, filters
    status == 'completed') must agree exactly — same table, same rows,
    different filters that happen to coincide in this specific case."""
    user = db2_user_factory("reconcile3@test.com", plan="start")
    user_id, session_id = user.id, None

    cost_values = [0.05, 0.03, 0.02]
    _seed_completed_records(db, user_id, session_id, cost_values)

    day_start, day_end = _day_window()
    enforcement_sum = round(_sum_active_tcu(db, user_id, day_start, day_end), 4)
    display_used = get_user_period_usage(db, user_id, "start")["daily"]["used"]

    assert enforcement_sum == pytest.approx(display_used, abs=1e-9)


def test_pending_reservation_included_in_enforcement_but_not_display(db, db2_user_factory):
    """Pins down WHY the two sums above are allowed to diverge in
    general: a 'pending' reservation counts toward the ENFORCEMENT sum
    (prevents over-allocation while a call is in flight) but never
    toward the user-facing DISPLAY sum (which only reflects real,
    finalized consumption) — documented behavior, proven here with real
    rows rather than asserted only in prose, so a future change can't
    quietly conflate the two."""
    user = db2_user_factory("reconcile4@test.com", plan="start")
    user_id, session_id = user.id, None
    _seed_completed_records(db, user_id, session_id, [0.01])  # 1.0 TCU completed

    reservation_id = check_and_reserve(db, user_id=user_id, session_id=session_id, plan="start")
    pending_record = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).first()
    assert pending_record.status == "pending"
    pending_tcu = float(pending_record.tcu)  # start's max_tcu_per_request

    day_start, day_end = _day_window()
    enforcement_sum = _sum_active_tcu(db, user_id, day_start, day_end)
    display_used = get_user_period_usage(db, user_id, "start")["daily"]["used"]

    assert enforcement_sum == pytest.approx(1.0 + pending_tcu, abs=1e-9)
    assert display_used == pytest.approx(1.0, abs=1e-9)  # pending never counted here
