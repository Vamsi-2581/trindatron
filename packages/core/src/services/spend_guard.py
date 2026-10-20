"""Financial guardrails: how much is being spent, by whom, and when to worry.

The economic audit of 2026-09-13 separated spend into two kinds, and the
guard treats them differently on purpose:

* Spend backed by revenue. Every paid plan's worst case is profitable by
  construction (src/billing/unit_economics.py, tests/unit/test_unit_economics.py),
  and every request is bounded by its reservation. A paying customer spending
  heavily is a customer using what they bought; cutting them off automatically
  would turn a success into an outage. So this spend is WATCHED, never
  contained here.

* Spend not backed by revenue. FREE, and whatever can still escape TCU. FREE
  is CONTAINED structurally, before any provider call, by the plan-wide daily
  budget in entitlement_service.check_and_reserve. Escaping spend is what the
  thresholds below exist to catch early.

evaluate() reads persisted usage (UsageRecord), so it answers across restarts
and replicas, unlike the in-process metrics. It is read-only, runs from the
existing reservation-reclaim loop and from GET /admin/economics, and reports
through logs and metrics.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session as DBSession

from src.models.usage import UsageRecord
from src.models.user import User
from src.observability.metrics import core_metrics

logger = logging.getLogger("trinity.spend_guard")

_alerts_total = core_metrics.counter(
    "spend_guard_alerts_total", "Spend guard alerts, labeled by level/scope/window.",
)

WARNING_SHARE = 0.80


@dataclass(frozen=True)
class SpendThresholds:
    """Provider spend, in USD, at which the guard raises CRITICAL. WARNING
    fires at WARNING_SHARE of each. Scaled for today's size (unit economics
    report: ~$68/month of infrastructure, tens of subscriptions); raise them
    as revenue grows."""
    daily_usd: float = 50.0
    weekly_usd: float = 250.0
    monthly_usd: float = 800.0
    #: Per provider, per day. A single provider jumping is how a leak or a
    #: failover storm usually shows first.
    provider_daily_usd: float = 30.0
    #: FREE is contained by its own budget; this is the alert on top of it.
    free_plan_daily_usd: float = 5.0


@dataclass(frozen=True)
class SpendAlert:
    level: str        # "warning" | "critical"
    scope: str        # "total" | "provider:<name>" | "plan:<name>"
    window: str       # "day" | "week" | "month"
    spent_usd: float
    threshold_usd: float


def provider_for_model(model: str) -> str:
    """Map a recorded model string to its provider, via the live catalog."""
    from trinity_router.providers import PROVIDER_CATALOG

    for cfg in PROVIDER_CATALOG.values():
        if cfg.model_id == model:
            return cfg.provider
    if model.startswith("claude"):
        return "anthropic"
    # Usage rows recorded before 2026-10-20 carry "gpt-4o-mini"/"gpt-4o", which
    # the catalog no longer lists (the aliases now hold GPT-6 models).
    if model.startswith("gpt-"):
        return "openai"
    if "/" in model:
        return model.split("/", 1)[0]
    return "unknown" if not model else model


def _windows(now: datetime) -> dict[str, tuple[datetime, datetime]]:
    """Each window's (start, end): end is always `now` itself, inclusive — a record
    created in this exact instant is spend that already happened, not spend from
    the future. Excluding it (a strict `<`) is wrong on principle and, in practice,
    was reachable: a UsageRecord's own created_at and a request's own `now` can be
    the identical value (client clock resolution — Windows' datetime.now() can
    return the same value hundreds of times in a row in a tight loop), which
    silently dropped that record from every window below (found as a
    non-deterministic KeyError on GET /admin/economics)."""
    now = now.astimezone(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - timedelta(days=day.weekday())
    month = day.replace(day=1)
    return {"day": (day, now), "week": (week, now), "month": (month, now)}


def _billed():
    """Real spend: what providers billed (provider_cost_usd), or list price where they bill it."""
    return func.coalesce(UsageRecord.provider_cost_usd, UsageRecord.cost_usd)


def spend_by_model(db: DBSession, start: datetime, end: datetime) -> dict[str, float]:
    rows = (
        db.query(UsageRecord.model, func.coalesce(func.sum(_billed()), 0.0))
        .filter(UsageRecord.status != "failed", UsageRecord.created_at >= start, UsageRecord.created_at <= end)
        .group_by(UsageRecord.model)
        .all()
    )
    return {model or "": float(total) for model, total in rows}


def spend_by_plan(db: DBSession, start: datetime, end: datetime) -> dict[str, float]:
    """Grouped by each account's CURRENT plan — a user who upgraded mid-period
    counts entirely under the new plan. Exact enough to see where money goes."""
    rows = (
        db.query(User.plan, func.coalesce(func.sum(_billed()), 0.0))
        .join(User, User.id == UsageRecord.user_id)
        .filter(UsageRecord.status != "failed", UsageRecord.created_at >= start, UsageRecord.created_at <= end)
        .group_by(User.plan)
        .all()
    )
    return {plan: float(total) for plan, total in rows}


def spend_summary(db: DBSession, *, now: datetime | None = None) -> dict:
    """Spend per window: total, by provider, by plan, by model."""
    now = now or datetime.now(timezone.utc)
    out: dict = {}
    for window, (start, end) in _windows(now).items():
        by_model = spend_by_model(db, start, end)
        by_provider: dict[str, float] = {}
        for model, total in by_model.items():
            provider = provider_for_model(model)
            by_provider[provider] = by_provider.get(provider, 0.0) + total
        out[window] = {
            "total_usd": round(sum(by_model.values()), 6),
            "by_provider_usd": {k: round(v, 6) for k, v in sorted(by_provider.items())},
            "by_plan_usd": {k: round(v, 6) for k, v in sorted(spend_by_plan(db, start, end).items())},
            "by_model_usd": {k: round(v, 6) for k, v in sorted(by_model.items())},
        }
    return out


def _check(alerts: list[SpendAlert], scope: str, window: str, spent: float, threshold: float) -> None:
    if threshold <= 0:
        return
    if spent >= threshold:
        alerts.append(SpendAlert("critical", scope, window, spent, threshold))
    elif spent >= threshold * WARNING_SHARE:
        alerts.append(SpendAlert("warning", scope, window, spent, threshold))


def evaluate(db: DBSession, *, now: datetime | None = None,
             thresholds: SpendThresholds = SpendThresholds()) -> list[SpendAlert]:
    """Every threshold currently crossed. Read-only; logging is the caller's choice."""
    summary = spend_summary(db, now=now)
    alerts: list[SpendAlert] = []
    _check(alerts, "total", "day", summary["day"]["total_usd"], thresholds.daily_usd)
    _check(alerts, "total", "week", summary["week"]["total_usd"], thresholds.weekly_usd)
    _check(alerts, "total", "month", summary["month"]["total_usd"], thresholds.monthly_usd)
    for provider, spent in summary["day"]["by_provider_usd"].items():
        _check(alerts, f"provider:{provider}", "day", spent, thresholds.provider_daily_usd)
    _check(alerts, "plan:free", "day", summary["day"]["by_plan_usd"].get("free", 0.0),
           thresholds.free_plan_daily_usd)
    return alerts


def report_alerts(alerts: list[SpendAlert]) -> None:
    """Log and count. CRITICAL logs at ERROR so any log-based alerting picks it up."""
    for alert in alerts:
        _alerts_total.inc(level=alert.level, scope=alert.scope, window=alert.window)
        log = logger.error if alert.level == "critical" else logger.warning
        log(
            "spend_guard %s | scope=%s window=%s spent_usd=%.4f threshold_usd=%.2f",
            alert.level, alert.scope, alert.window, alert.spent_usd, alert.threshold_usd,
        )


#: (level, scope, window, window start) already reported by this process.
_reported: set[tuple[str, str, str, str]] = set()


def run(db: DBSession, *, now: datetime | None = None,
        thresholds: SpendThresholds = SpendThresholds()) -> list[SpendAlert]:
    """The periodic sweep: evaluate, then report each alert once per window.

    The sweep runs every few minutes; a daily threshold crossed at 10:00
    should page once that day, not 150 times. A warning that escalates to
    critical is a new alert and is reported. Per process, so each replica
    reports once — acceptable for the replica counts in the unit economics
    model. Returns only the alerts reported on this call.
    """
    now = now or datetime.now(timezone.utc)
    windows = _windows(now)
    horizon = (now - timedelta(days=40)).isoformat()
    for key in [k for k in _reported if k[3] < horizon]:
        _reported.discard(key)

    fresh: list[SpendAlert] = []
    for alert in evaluate(db, now=now, thresholds=thresholds):
        key = (alert.level, alert.scope, alert.window, windows[alert.window][0].isoformat())
        if key not in _reported:
            _reported.add(key)
            fresh.append(alert)
    report_alerts(fresh)
    return fresh


def accounts_by_plan(db: DBSession) -> dict[str, int]:
    rows = db.query(User.plan, func.count(User.id)).group_by(User.plan).all()
    return {plan: int(count) for plan, count in rows}


def month_cost_per_account_usd(db: DBSession, *, now: datetime | None = None) -> dict[str, float]:
    """Month-to-date provider spend divided by accounts on each plan — the
    figure to hold against unit_economics' per-plan ceilings, e.g. what an
    average FREE account really costs."""
    spent = spend_summary(db, now=now)["month"]["by_plan_usd"]
    return {
        plan: round(spent.get(plan, 0.0) / count, 6)
        for plan, count in sorted(accounts_by_plan(db).items()) if count
    }
