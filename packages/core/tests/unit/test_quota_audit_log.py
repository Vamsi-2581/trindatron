"""
Unit tests — Fase F criterion 4: quota metrics in audit log.

Verifies that check_and_reserve() emits a structured log message containing
daily/weekly/monthly quota metrics so ops tooling can track remaining budget.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_db_mock(tcu_sum: float = 0.0):
    """Return a SQLAlchemy session mock that returns tcu_sum from any scalar()."""
    scalar_mock = MagicMock(return_value=tcu_sum)
    filter_mock = MagicMock()
    filter_mock.scalar.return_value = tcu_sum

    chain = MagicMock()
    chain.filter.return_value = chain
    chain.scalar.return_value = tcu_sum
    # check_and_reserve() also resolves/starts a 5h EntitlementSession via
    # db.query(EntitlementSession).filter(...).first() — None means "no
    # active session", so resolve_or_start_session() takes its "create new"
    # branch (db.add/db.flush, both no-ops on this MagicMock db).
    chain.first.return_value = None

    db = MagicMock()
    db.query.return_value = chain
    db.add = MagicMock()
    db.commit = MagicMock()
    return db


# ── tests ─────────────────────────────────────────────────────────────────────

def test_reservation_log_contains_daily_quota(caplog):
    """Reserved log message must include daily_used and daily_limit fields."""
    import logging
    from src.services.entitlement_service import check_and_reserve

    # "max" rather than "start": what is under test is the shape of the audit
    # line, so the plan only needs windows wide enough that the seeded usage
    # does not trip one. START's 5-hour budget is 12 TCU — seeding 10 into it
    # would make this a limit test by accident.
    db = _make_db_mock(tcu_sum=10.0)

    with caplog.at_level(logging.INFO, logger="src.services.entitlement_service"):
        reservation_id = check_and_reserve(db, "user-1", "sess-1", "max")

    reserved_msgs = [r.message for r in caplog.records if "entitlement reserved" in r.message]
    assert reserved_msgs, "No 'entitlement reserved' log message found"
    msg = reserved_msgs[0]

    assert "daily_used=" in msg, f"daily_used missing: {msg}"
    assert "daily_limit=" in msg, f"daily_limit missing: {msg}"
    assert "weekly_used=" in msg, f"weekly_used missing: {msg}"
    assert "weekly_limit=" in msg, f"weekly_limit missing: {msg}"
    assert "monthly_used=" in msg, f"monthly_used missing: {msg}"
    assert "monthly_limit=" in msg, f"monthly_limit missing: {msg}"
    assert "tcu_reserved=" in msg, f"tcu_reserved missing: {msg}"


def test_reservation_log_contains_correct_used_values(caplog):
    """Quota consumed values in the log must match what _sum_active_tcu returned."""
    import logging
    from src.services.entitlement_service import check_and_reserve

    # Simulate 42 TCU already used in all windows, on a plan whose windows
    # comfortably contain that — the assertion here is about what gets
    # logged, not about whether 42 is allowed.
    db = _make_db_mock(tcu_sum=42.0)

    with caplog.at_level(logging.INFO, logger="src.services.entitlement_service"):
        check_and_reserve(db, "user-2", "sess-2", "max")

    reserved_msgs = [r.message for r in caplog.records if "entitlement reserved" in r.message]
    msg = reserved_msgs[0]
    assert "daily_used=42." in msg, f"Expected daily_used=42.x in: {msg}"
    assert "weekly_used=42." in msg, f"Expected weekly_used=42.x in: {msg}"
    # PARTE 10 (2026-08-31): monthly_tcu_limit is None for every plan now —
    # check_and_reserve skips the (guarded) monthly _sum_active_tcu() call
    # entirely when there's no cap to enforce, so monthly_used stays at its
    # initialized 0.0 regardless of what the mock would have returned.
    assert "monthly_used=0." in msg, f"Expected monthly_used=0.x (not computed — no cap) in: {msg}"


def test_reservation_log_records_the_real_max_limits(caplog):
    """Renamed from "unlimited plan shows none". The audit line exists so an
    operator can reconstruct a decision; with MAX now bounded, logging
    daily_limit=None would misrepresent what was actually compared."""
    import logging
    from src.entitlements import get_plan_entitlement
    from src.services.entitlement_service import check_and_reserve

    db = _make_db_mock(tcu_sum=0.0)
    with caplog.at_level(logging.INFO, logger="src.services.entitlement_service"):
        check_and_reserve(db, "user-3", "sess-3", "max")

    msg = [r.message for r in caplog.records if "entitlement reserved" in r.message][0]
    e = get_plan_entitlement("max")
    assert "plan=max" in msg, msg
    assert f"daily_limit={e.daily_tcu_limit}" in msg, msg
    assert f"weekly_limit={e.weekly_tcu_limit}" in msg, msg
    # Monthly stays unbounded — the weekly window is the ceiling that binds.
    assert "monthly_limit=None" in msg, msg
