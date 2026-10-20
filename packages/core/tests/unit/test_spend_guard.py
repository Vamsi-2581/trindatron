"""Spend guard: persisted spend, grouped the way the questions are asked.

"How much does an average FREE account cost?", "which provider is spending?",
"how much went to Opus?" — each needs spend grouped by plan, provider or
model, read from UsageRecord so the answer survives restarts. And a leak has
to show as an alert before it shows on an invoice.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database import Base
from src.models.usage import UsageRecord
from src.models.user import User
from src.services import spend_guard as sg


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


NOW = datetime(2026, 9, 16, 15, 0, tzinfo=timezone.utc)   # a Wednesday


def _user(db, email, plan):
    user = User(email=email, hashed_password="x", full_name="x", plan=plan)
    db.add(user)
    db.commit()
    return user


def _spend(db, user, model, cost, *, when=NOW - timedelta(hours=1), status="completed"):
    db.add(UsageRecord(user_id=user.id, model=model, cost_usd=cost, tcu=cost * 100,
                       status=status, created_at=when))
    db.commit()


def test_summary_groups_by_provider_plan_and_model(db):
    free = _user(db, "f@t.com", "free")
    maxu = _user(db, "m@t.com", "max")
    _spend(db, free, "groq/openai/gpt-oss-20b", 0.10)
    _spend(db, maxu, "claude-opus-4-7", 2.00)
    _spend(db, maxu, "gpt-4o-mini", 0.05)

    day = sg.spend_summary(db, now=NOW)["day"]
    assert day["total_usd"] == pytest.approx(2.15)
    assert day["by_provider_usd"] == pytest.approx({"anthropic": 2.00, "groq": 0.10, "openai": 0.05})
    assert day["by_plan_usd"] == pytest.approx({"free": 0.10, "max": 2.05})
    assert day["by_model_usd"]["claude-opus-4-7"] == pytest.approx(2.00)


def test_failed_calls_and_older_windows_are_excluded(db):
    u = _user(db, "u@t.com", "pro")
    _spend(db, u, "gpt-4o-mini", 1.0, status="failed")
    _spend(db, u, "gpt-4o-mini", 3.0, when=NOW - timedelta(days=40))
    _spend(db, u, "gpt-4o-mini", 0.5, when=NOW - timedelta(days=2))   # this week, not today

    s = sg.spend_summary(db, now=NOW)
    assert s["day"]["total_usd"] == pytest.approx(0.0)
    assert s["week"]["total_usd"] == pytest.approx(0.5)
    assert s["month"]["total_usd"] == pytest.approx(0.5)


def test_thresholds_warn_before_they_go_critical(db):
    u = _user(db, "u@t.com", "pro")
    thresholds = sg.SpendThresholds(daily_usd=10.0, weekly_usd=1_000, monthly_usd=1_000,
                                    provider_daily_usd=1_000, free_plan_daily_usd=1_000)
    _spend(db, u, "gpt-4o-mini", 8.5)
    [alert] = sg.evaluate(db, now=NOW, thresholds=thresholds)
    assert (alert.level, alert.scope, alert.window) == ("warning", "total", "day")

    _spend(db, u, "gpt-4o-mini", 2.0)
    [alert] = sg.evaluate(db, now=NOW, thresholds=thresholds)
    assert alert.level == "critical"


def test_a_single_provider_jump_is_caught_even_when_the_total_is_fine(db):
    u = _user(db, "u@t.com", "max")
    thresholds = sg.SpendThresholds(daily_usd=1_000, weekly_usd=1_000, monthly_usd=1_000,
                                    provider_daily_usd=5.0, free_plan_daily_usd=1_000)
    _spend(db, u, "claude-opus-4-7", 6.0)
    alerts = sg.evaluate(db, now=NOW, thresholds=thresholds)
    assert [(a.level, a.scope) for a in alerts] == [("critical", "provider:anthropic")]


def test_free_plan_spend_has_its_own_alert(db):
    f = _user(db, "f@t.com", "free")
    thresholds = sg.SpendThresholds(daily_usd=1_000, weekly_usd=1_000, monthly_usd=1_000,
                                    provider_daily_usd=1_000, free_plan_daily_usd=1.0)
    _spend(db, f, "groq/openai/gpt-oss-20b", 0.9)
    [alert] = sg.evaluate(db, now=NOW, thresholds=thresholds)
    assert (alert.level, alert.scope) == ("warning", "plan:free")


def test_the_sweep_reports_each_alert_once_per_window(db):
    sg._reported.clear()
    u = _user(db, "u@t.com", "pro")
    thresholds = sg.SpendThresholds(daily_usd=1.0, weekly_usd=1_000, monthly_usd=1_000,
                                    provider_daily_usd=1_000, free_plan_daily_usd=1_000)
    _spend(db, u, "gpt-4o-mini", 2.0)

    assert len(sg.run(db, now=NOW, thresholds=thresholds)) == 1
    assert sg.run(db, now=NOW + timedelta(minutes=5), thresholds=thresholds) == []

    tomorrow = NOW + timedelta(days=1)
    _spend(db, u, "gpt-4o-mini", 2.0, when=tomorrow - timedelta(hours=1))
    assert len(sg.run(db, now=tomorrow, thresholds=thresholds)) == 1


def test_escalation_from_warning_to_critical_is_reported(db):
    sg._reported.clear()
    u = _user(db, "u@t.com", "pro")
    thresholds = sg.SpendThresholds(daily_usd=10.0, weekly_usd=1_000, monthly_usd=1_000,
                                    provider_daily_usd=1_000, free_plan_daily_usd=1_000)
    _spend(db, u, "gpt-4o-mini", 8.5)
    assert [a.level for a in sg.run(db, now=NOW, thresholds=thresholds)] == ["warning"]
    _spend(db, u, "gpt-4o-mini", 2.0)
    assert [a.level for a in sg.run(db, now=NOW, thresholds=thresholds)] == ["critical"]


def test_average_month_cost_per_account(db):
    a = _user(db, "a@t.com", "free")
    _user(db, "b@t.com", "free")
    _spend(db, a, "groq/openai/gpt-oss-20b", 0.40)
    assert sg.month_cost_per_account_usd(db, now=NOW)["free"] == pytest.approx(0.20)


def test_unknown_models_are_attributed_not_dropped():
    assert sg.provider_for_model("claude-sonnet-5") == "anthropic"
    assert sg.provider_for_model("mistral/mistral-small-latest") == "mistral"
    assert sg.provider_for_model("") == "unknown"
