"""
Entitlement service — TCU quota enforcement.

Flow:
  1. Route handler calls check_and_reserve() BEFORE returning StreamingResponse.
     If limit exceeded → HTTPException(429). Provider is NEVER called.
     On success → pending UsageRecord created, reservation_id returned.

  2. Stream generator calls finalize_usage() on success path.
     Pending record updated with real TCU/cost/tokens, status='completed'.

  3. Stream generator calls cancel_reservation() on any failure path.
     Pending record set to status='failed', tcu=0 (stops counting against quota).

CONCURRENCY GUARANTEE (READ COMMITTED + advisory lock):
  check_and_reserve() (and, since the Beta hardening pass below,
  check_agent_iteration_budget()) take pg_advisory_xact_lock(hashtext
  ('tcu_'||user_id)) — a BLOCKING, transaction-scoped PostgreSQL advisory
  lock keyed per user — BEFORE reading the SUM of non-failed records and
  inserting/updating a reservation. This closes the write-skew window
  that plain READ COMMITTED alone would leave open: a second concurrent
  call for the SAME user blocks until the first commits, then re-reads
  the now-up-to-date SUM and correctly sees itself as over budget.
  Verified against real PostgreSQL by test_concurrent_reservation_only_
  one_succeeds() (and, for the multi-agent case, test_concurrent_agents_
  same_user_respect_daily_limit()) in tests/integration/test_entitlement.py.

  Silently skipped (best-effort only) on any non-PostgreSQL dialect
  (SQLite in tests) via _acquire_tcu_advisory_lock()'s own dialect check,
  and also silently skipped if the lock acquisition itself raises for any
  OTHER reason (e.g. a transient connection error) — that second case
  reopens the write-skew window with no alert; see this function's own
  docstring for why that residual gap was deliberately not papered over
  in this pass.

  AGENT OVERSHOOT — CLOSED (Beta hardening, 2026-09-10):
    Previously, check_and_reserve() only ever reserved ONE
    max_tcu_per_request estimate up front, but agent_service.py's ReAct
    loop could make up to settings.agent_max_iterations model calls
    before finalize_usage() ever recorded the real total — real TCU
    could exceed the reservation by up to
    max_tcu_per_request × (agent_max_iterations - 1) with NO check in
    between. check_agent_iteration_budget() (below) closes this: called
    after every agent iteration that produced tool_calls (i.e. right
    before the loop would call the model again), it updates the
    reservation's own `tcu` to the REAL running total so far and re-runs
    the SAME daily/weekly/monthly comparisons check_and_reserve() makes.
    A budget-exceeded result stops the loop (agent_service.py's existing
    `stopped_early_reason` mechanism — the same one already used for the
    repeated-action/no-progress circuit breakers), and finalize_usage()
    still records whatever was genuinely, safely accumulated. See that
    function's own docstring for the full design.

  RESERVATION TTL / ORPHAN RECLAIM — CLOSED (Beta hardening, 2026-09-10):
    A 'pending' reservation whose owning request's process crashed
    before its own `finally` block could run (chat_service.py/
    agent_service.py's `_cancel_on_exit` pattern guarantees exactly one
    of finalize_usage()/cancel_reservation() runs on every NORMAL exit
    path, but cannot run at all if the process itself dies) would
    otherwise stay 'pending' forever, permanently counting against that
    user's quota. reclaim_expired_reservations() (below), run
    periodically by run_tcu_reservation_reclaim_loop() from main.py's
    lifespan (same in-process background-task pattern as every other
    sweep in this codebase — webhook_replay_service, execution_worker,
    payment_order_service), transitions any 'pending' row older than
    settings.tcu_reservation_ttl_seconds to 'failed' (tcu=0, cost_usd=0)
    — exactly cancel_reservation()'s own transition, via one bulk,
    race-safe UPDATE. Never touches a 'completed' row.
"""
from __future__ import annotations

import uuid
import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from decimal import Decimal

from sqlalchemy import func, text
from sqlalchemy.orm import Session as DBSession

from src.entitlements import get_plan_entitlement
from src.models.usage import UsageRecord
from src.models.user import User
from src.observability.metrics import core_metrics
from src.models.entitlement_session import EntitlementSession
from src.services.session_window_service import resolve_or_start_session
from src.timezones import day_window as _tz_day_window
from src.timezones import week_window as _tz_week_window

logger = logging.getLogger(__name__)

# Etapa 5G — additive alongside the existing logger calls throughout this
# module; does not change any reservation/finalize/cancel decision. Values
# observed are always the same real numbers already computed by the
# quota-enforcement logic below — never invented.
_reservations_total = core_metrics.counter(
    "entitlement_reservations_total", "TCU reservation outcomes, labeled by plan/outcome.",
)
_tcu_reserved = core_metrics.histogram(
    "entitlement_tcu_reserved", "Estimated TCU reserved per successful check_and_reserve(), labeled by plan.",
)
_tcu_finalized = core_metrics.histogram(
    "entitlement_tcu_finalized", "Real TCU recorded per finalize_usage() call, labeled by model.",
)
_cost_usd_finalized = core_metrics.histogram(
    "entitlement_cost_usd_finalized", "Real cost_usd recorded per finalize_usage() call, labeled by model.",
)
_reservations_reclaimed_total = core_metrics.counter(
    "entitlement_reservations_reclaimed_total",
    "Pending TCU reservations reclaimed as expired/orphaned by the background sweep.",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _day_window(user_timezone: str | None = None) -> tuple[datetime, datetime]:
    """Local midnight to local midnight, in the user's own timezone.

    This used to be UTC midnight while the weekly window was already Sunday
    12:00 LOCAL - two different notions of "a period ends" inside one
    product. The visible symptom was a Brazilian user's daily quota
    resetting at 21:00 with nothing in the interface able to explain it.
    Both rules now live in src/timezones.py and both are expressed in the
    user's own time; every call site here already had the timezone in hand
    and was forwarding it to the weekly window only.

    The 5-hour session window is deliberately untouched: it is a duration
    from an instant, not a calendar boundary, so it has no local midnight to
    be wrong about.
    """
    return _tz_day_window(_utcnow(), user_timezone)


def _week_window(user_timezone: str | None = None) -> tuple[datetime, datetime]:
    """Return (week_start_utc, week_end_utc) for the weekly reset rule:
    Sunday 12:00 in the user's LOCAL IANA timezone (see src/timezones.py).

    user_timezone=None (or invalid) computes against the documented
    FALLBACK_TIMEZONE — used by call sites that genuinely have no user
    context (e.g. tests exercising daily/monthly logic only). Every
    request-handling call site MUST pass the real current_user.timezone —
    see PARTE 8: check_and_reserve, get_user_week_usage, and
    get_user_period_usage all forward it consistently; there is exactly one
    weekly-reset rule in this codebase, implemented once in src/timezones.py.
    """
    return _tz_week_window(_utcnow(), user_timezone)


def _month_window() -> tuple[datetime, datetime]:
    now = _utcnow()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (start + timedelta(days=32)).replace(day=1)
    return start, next_month


def _acquire_tcu_advisory_lock(db: DBSession, user_id: str) -> None:
    """Best-effort, transaction-scoped PostgreSQL advisory lock
    serializing all TCU reservation/recheck operations for one user —
    see this module's own "CONCURRENCY GUARANTEE" docstring section.
    Shared by check_and_reserve() and check_agent_iteration_budget() so
    both take the EXACT SAME lock, never a second one. Silently skipped
    on any non-PostgreSQL dialect (SQLite in tests) and, best-effort,
    on any other failure acquiring it — see the module docstring for
    the residual risk that represents."""
    try:
        bind = db.get_bind()
        if getattr(getattr(bind, "dialect", None), "name", None) == "postgresql":
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"tcu_{user_id}"},
            )
    except Exception:
        logger.debug("Advisory lock unavailable, continuing without it | user=%s", user_id)


def _acquire_team_advisory_lock(db: DBSession, team_id: str) -> None:
    """Serialize the SHARED pool check across members of one team.

    The per-user lock above cannot do this: two members are two different
    keys, so both would read the same pool total and both would pass. This
    lock is keyed on the team, so concurrent members queue behind each
    other for the pool comparison. Taken IN ADDITION to the user lock, and
    always in the same order (user then team) so two requests can never
    deadlock by taking them in opposite orders.

    Same best-effort posture as the user lock: PostgreSQL only, never fatal.
    """
    try:
        bind = db.get_bind()
        if getattr(getattr(bind, "dialect", None), "name", None) == "postgresql":
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"tcu_team_{team_id}"},
            )
    except Exception:
        logger.debug("Team advisory lock unavailable, continuing | team=%s", team_id)


def _acquire_free_plan_advisory_lock(db: DBSession) -> None:
    """Serialize the plan-wide FREE budget check across every free account.

    Same reason as the team lock: per-user locks are different keys, so two
    free accounts would both read the same daily total and both pass. Taken
    after the user lock, always in that order. PostgreSQL only, never fatal.
    """
    try:
        bind = db.get_bind()
        if getattr(getattr(bind, "dialect", None), "name", None) == "postgresql":
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": "tcu_free_plan_global"},
            )
    except Exception:
        logger.debug("FREE plan advisory lock unavailable, continuing")


class MediaPoolExhausted(HTTPException):
    """The media pool (media/plans.py) has no room left, though chat's windows may.

    Raised under the same advisory lock as every other window check, so two
    concurrent creations can never both read a pool with room for one.
    """

    def __init__(self, resets_at: datetime) -> None:
        super().__init__(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                         detail={"error": "media_pool_exhausted", "resets_at": resets_at.isoformat()})
        self.resets_at = resets_at


MEDIA_MODEL_PREFIX = "media:"


def _sum_media_tcu(db: DBSession, user_id: str, period_start: datetime, period_end: datetime) -> float:
    """Pending + completed TCU of media work (records tagged media:*) in the period — the media pool's use."""
    result = (
        db.query(func.coalesce(func.sum(UsageRecord.tcu), 0.0))
        .filter(
            UsageRecord.user_id == user_id,
            UsageRecord.status != "failed",
            UsageRecord.model.like(f"{MEDIA_MODEL_PREFIX}%"),
            UsageRecord.created_at >= period_start,
            UsageRecord.created_at < period_end,
        )
        .scalar()
    )
    return float(result)


def media_tcu_used(db: DBSession, user_id: str, user_timezone: str | None = None) -> tuple[float, float]:
    """(today, this week) TCU held or spent by media for this user: what the media pool has used."""
    day_start, day_end = _day_window(user_timezone)
    week_start, week_end = _week_window(user_timezone)
    return (_sum_media_tcu(db, user_id, day_start, day_end), _sum_media_tcu(db, user_id, week_start, week_end))


def _utc_day(now: datetime) -> tuple[datetime, datetime]:
    start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _sum_free_plan_cost_usd(db: DBSession, period_start: datetime, period_end: datetime) -> float:
    """Provider spend of every free account together — the FREE plan budget.

    Same accounting rule as the per-user sums: pending reservations count (a
    reservation carries its own ceiling as cost_usd), failed ones do not.
    """
    result = (
        db.query(func.coalesce(func.sum(UsageRecord.cost_usd), 0.0))
        .join(User, User.id == UsageRecord.user_id)
        .filter(
            User.plan == "free",
            UsageRecord.status != "failed",
            UsageRecord.created_at >= period_start,
            UsageRecord.created_at < period_end,
        )
        .scalar()
    )
    return float(result or 0.0)


def _humanize_reset(now: datetime, resets_at: datetime) -> str:
    """"em 3 h 20 min" — what the user actually needs to know.

    Deliberately coarse: minutes below an hour, whole hours and minutes
    above. Never negative (a window that just expired reads as "agora").
    """
    delta = resets_at - now
    minutes = int(delta.total_seconds() // 60)
    if minutes <= 0:
        return "agora"
    if minutes < 60:
        return f"em {minutes} min"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"em {hours} h {mins} min" if mins else f"em {hours} h"
    days, rem_hours = divmod(hours, 24)
    return f"em {days} d {rem_hours} h" if rem_hours else f"em {days} d"


def _limit_exceeded(
    *,
    error: str,
    message: str,
    now: datetime,
    resets_at: datetime,
) -> HTTPException:
    """Build the 429 a user sees when a window is full.

    Carries WHEN it reopens and nothing else. No TCU, no used/limit
    figures: they are this platform's internal accounting unit, they mean
    nothing to the person reading them, and the only actionable fact is
    the reset time.
    """
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "error": error,
            "message": f"{message} Seu acesso será renovado {_humanize_reset(now, resets_at)}.",
            "resets_at": resets_at.isoformat(),
            "resets_in": _humanize_reset(now, resets_at),
        },
    )


def _sum_team_active_tcu(
    db: DBSession,
    team_id: str,
    period_start: datetime,
    period_end: datetime,
) -> float:
    """Sum the whole team's usage for the period — the shared pool.

    Same accounting rule as the per-user sum (pending + completed, never
    failed), widened from one user to every user carrying this team_id.
    """
    result = (
        db.query(func.coalesce(func.sum(UsageRecord.tcu), 0.0))
        .join(User, User.id == UsageRecord.user_id)
        .filter(
            User.team_id == team_id,
            UsageRecord.status != "failed",
            UsageRecord.created_at >= period_start,
            UsageRecord.created_at < period_end,
        )
        .scalar()
    )
    return float(result or 0.0)


def _sum_active_tcu(
    db: DBSession,
    user_id: str,
    period_start: datetime,
    period_end: datetime,
) -> float:
    """Sum pending + completed TCU for the period (excludes failed/cancelled)."""
    result = (
        db.query(func.coalesce(func.sum(UsageRecord.tcu), 0.0))
        .filter(
            UsageRecord.user_id == user_id,
            UsageRecord.status != "failed",
            UsageRecord.created_at >= period_start,
            UsageRecord.created_at < period_end,
        )
        .scalar()
    )
    return float(result)


def check_and_reserve(
    db: DBSession,
    user_id: str,
    session_id: str | None,
    plan: str,
    user_timezone: str | None = None,
    estimated_tcu: float | None = None,
    model: str = "",
    media_pool_tcu: tuple[float | None, float | None] | None = None,
) -> str:
    """
    Verify entitlement limits and INSERT a pending UsageRecord reservation.

    Returns reservation_id. Call finalize_usage() on success or
    cancel_reservation() on failure. Must be called BEFORE StreamingResponse
    is returned so HTTPException(429) can still be raised.

    user_timezone: the caller's IANA timezone (current_user.timezone),
    used to compute the weekly window's Sunday-12:00-local reset instant
    (src/timezones.py). Optional for callers with no user context (tests
    exercising daily/monthly logic) — defaults to the documented UTC
    fallback, which does NOT affect daily/monthly enforcement, only weekly.

    estimated_tcu: what this particular unit of work is expected to cost,
    when the caller can actually predict it. Chat and agent turns cannot —
    one turn's cost is not knowable until the model has answered — so they
    leave this None and get the flat per-request figure, unchanged.

    A presentation CAN predict it: the slide count determines the number of
    model calls and the image strategy determines the rest (see
    src/presentation/cost.py), and a deck costs multiples of the flat figure
    — roughly 17 TCU against a per-request estimate of 4 on START or 8 on
    PRO. Reserving 4 for something that will cost 17 is precisely the
    overshoot the ADMIT+CLAMP pass was written to remove, so a caller that
    knows better says so here rather than reserving a number it knows to be
    wrong.

    This never widens a limit. The estimate is what gets reserved and
    checked, and the CLAMP below still cuts it down to whatever balance
    genuinely remains — so a large estimate against a small balance reserves
    the balance, exactly as a flat one would.

    model / media_pool_tcu: a media creation tags its reservation "media:<kind>"
    from the moment it is written and passes its plan's (daily, weekly) media
    pool. The pool is then one more window, checked and clamped under the same
    lock as the others and summed over the media-tagged records — pending ones
    included — so a concurrent creation sees this one's reservation before any
    job row exists. Chat and agents pass neither and are unaffected.
    """
    entitlement = get_plan_entitlement(plan)
    if estimated_tcu is None:
        estimated_tcu = entitlement.max_tcu_per_request
    else:
        estimated_tcu = max(0.0, float(estimated_tcu))
    now = _utcnow()

    # Advisory lock: closes write-skew window on PostgreSQL. See
    # _acquire_tcu_advisory_lock()'s own docstring.
    _acquire_tcu_advisory_lock(db, user_id)

    # 5-hour rolling entitlement session — resolved/started under the same
    # lock as the TCU reservation below, so a concurrent "no active
    # session" race can't create two sessions. Separate dimension from
    # daily/weekly/monthly TCU and from credits/billing — see
    # src/services/session_window_service.py for the full rule. Only
    # touched here (the success-reserving path); cancel_reservation() and
    # finalize_usage() never prolong or restart it.
    #
    # This only flush()es — it becomes durable exactly when (and only when)
    # this function reaches its own db.commit() below, alongside the
    # reservation row. If a daily/weekly/monthly check raises 429 first,
    # nothing commits and this change is discarded when the request's DB
    # session closes without a commit (see database.py get_db). That's
    # intentional: the rule says a session starts on the first operation
    # that ACTUALLY reserves TCU — a request blocked by another limit never
    # reserved anything, so it must not start or touch a session either.
    # Looked up here, not passed in: a shared pool that depends on every
    # call site remembering to forward a team_id is a pool that silently
    # stops applying the first time someone forgets. Only queried for plans
    # that actually have a pool, so no extra read for anyone else.
    team_id: str | None = None
    if entitlement.team_weekly_pool_tcu_limit is not None:
        team_id = db.query(User.team_id).filter(User.id == user_id).scalar()

    # Starting (or continuing) the 5-hour window happens BEFORE the checks
    # below so that an expired window has already rolled over by the time
    # its budget is compared — that rollover is what makes the renewal
    # automatic, with no cron and no scheduled job anywhere.
    entitlement_session = resolve_or_start_session(db, user_id, now)

    day_start, day_end = _day_window(user_timezone)
    week_start, week_end = _week_window(user_timezone)
    month_start, month_end = _month_window()

    # Every window's remaining balance, collected as each one is checked.
    # The smallest of them is this request's effective budget - the most it
    # may spend before the next window would be breached.
    remaining_budgets: list[float] = []

    # Track consumed quota per window for audit logging (0.0 = unlimited or not yet computed)
    daily_used: float = 0.0
    weekly_used: float = 0.0
    monthly_used: float = 0.0

    try:
        # ── Rolling 5-hour session window ─────────────────────────────────
        # Checked first: it is the shortest window, the one a burst hits
        # soonest, and the one whose reset a user is happiest to hear about.
        #
        # ADMIT + CLAMP (2026-09-11). The test used to be
        # `used + max_tcu_per_request > limit`, which refused a request
        # whenever the FLAT per-request estimate no longer fitted - even
        # with plenty of real balance left. That permanently locked the last
        # max_tcu_per_request of every window: 33% of START, 29% of PRO,
        # 26% of MAX, unreachable by design rather than by spending.
        #
        # Now a request is admitted while any balance remains, and what gets
        # reserved is the balance itself when it is smaller than the flat
        # estimate. The reservation is therefore never larger than what the
        # user can actually spend, so a concurrent second request sees the
        # true remaining figure and cannot overshoot either. The same value
        # is handed to the router as this call's cost ceiling (see
        # get_reservation_budget_tcu), which is what stops the CALL from
        # exceeding what the RESERVATION allows.
        if entitlement.session_tcu_limit is not None:
            session_used = _sum_active_tcu(
                db, user_id,
                entitlement_session.session_started_at,
                entitlement_session.session_expires_at,
            )
            remaining_budgets.append(entitlement.session_tcu_limit - session_used)
            if session_used >= entitlement.session_tcu_limit:
                logger.info(
                    "entitlement session exceeded | user=%s plan=%s "
                    "used=%.4f estimated=%.4f limit=%.4f resets_at=%s",
                    user_id, plan, session_used, estimated_tcu,
                    entitlement.session_tcu_limit,
                    entitlement_session.session_expires_at.isoformat(),
                )
                _reservations_total.inc(plan=plan, outcome="session_limit_exceeded")
                raise _limit_exceeded(
                    error="session_limit_exceeded",
                    message="Você atingiu o limite de uso desta sessão.",
                    now=now,
                    resets_at=entitlement_session.session_expires_at,
                )

        # ── Shared team pool ──────────────────────────────────────────────
        # Only TEAM carries one. Checked before the per-member windows so a
        # team that has collectively run out says so, rather than telling
        # one member their personal limit is fine while the request fails.
        if entitlement.team_weekly_pool_tcu_limit is not None and team_id:
            _acquire_team_advisory_lock(db, team_id)
            pool_used = _sum_team_active_tcu(db, team_id, week_start, week_end)
            remaining_budgets.append(entitlement.team_weekly_pool_tcu_limit - pool_used)
            if pool_used >= entitlement.team_weekly_pool_tcu_limit:
                logger.info(
                    "entitlement team pool exceeded | team=%s user=%s "
                    "used=%.4f estimated=%.4f limit=%.4f",
                    team_id, user_id, pool_used, estimated_tcu,
                    entitlement.team_weekly_pool_tcu_limit,
                )
                _reservations_total.inc(plan=plan, outcome="team_pool_exceeded")
                raise _limit_exceeded(
                    error="team_pool_exceeded",
                    message="Sua equipe atingiu o limite de uso compartilhado desta semana.",
                    now=now,
                    resets_at=week_end,
                )

        # ── FREE: one daily budget for every free account together ───────
        # Per-account windows bound one account; they cannot bound how many
        # accounts exist. FREE earns nothing, so its total spend is a number
        # the business sets (config.free_plan_global_daily_budget_usd), and
        # this is where it holds — under its own lock, clamped like every
        # other window so the last cents of the day cannot be overshot.
        if entitlement.plan == "free":
            from src.config import settings

            free_budget_usd = settings.free_plan_global_daily_budget_usd
            if free_budget_usd is not None:
                _acquire_free_plan_advisory_lock(db)
                free_day_start, free_day_end = _utc_day(now)
                free_spent_usd = _sum_free_plan_cost_usd(db, free_day_start, free_day_end)
                remaining_budgets.append((free_budget_usd - free_spent_usd) * 100.0)
                if free_spent_usd >= free_budget_usd:
                    logger.warning(
                        "entitlement FREE plan daily budget exhausted | user=%s "
                        "spent_usd=%.4f budget_usd=%.4f",
                        user_id, free_spent_usd, free_budget_usd,
                    )
                    _reservations_total.inc(plan=plan, outcome="free_plan_daily_budget_exhausted")
                    raise _limit_exceeded(
                        error="free_capacity_exhausted",
                        message="A capacidade gratuita de hoje se esgotou.",
                        now=now,
                        resets_at=free_day_end,
                    )

        if entitlement.daily_tcu_limit is not None:
            daily_used = _sum_active_tcu(db, user_id, day_start, day_end)
            remaining_budgets.append(entitlement.daily_tcu_limit - daily_used)
            if daily_used >= entitlement.daily_tcu_limit:
                logger.info(
                    "entitlement daily exceeded | user=%s plan=%s "
                    "used=%.4f estimated=%.4f limit=%.4f",
                    user_id, plan, daily_used, estimated_tcu, entitlement.daily_tcu_limit,
                )
                _reservations_total.inc(plan=plan, outcome="daily_limit_exceeded")
                raise _limit_exceeded(
                    error="daily_limit_exceeded",
                    message="Você atingiu o limite de uso de hoje.",
                    now=now,
                    resets_at=day_end,
                )

        if entitlement.weekly_tcu_limit is not None:
            weekly_used = _sum_active_tcu(db, user_id, week_start, week_end)
            remaining_budgets.append(entitlement.weekly_tcu_limit - weekly_used)
            if weekly_used >= entitlement.weekly_tcu_limit:
                logger.info(
                    "entitlement weekly exceeded | user=%s plan=%s "
                    "used=%.4f estimated=%.4f limit=%.4f",
                    user_id, plan, weekly_used, estimated_tcu, entitlement.weekly_tcu_limit,
                )
                _reservations_total.inc(plan=plan, outcome="weekly_limit_exceeded")
                raise _limit_exceeded(
                    error="weekly_limit_exceeded",
                    message="Você atingiu o limite de uso desta semana.",
                    now=now,
                    resets_at=week_end,
                )

        if entitlement.monthly_tcu_limit is not None:
            monthly_used = _sum_active_tcu(db, user_id, month_start, month_end)
            remaining_budgets.append(entitlement.monthly_tcu_limit - monthly_used)
            if monthly_used >= entitlement.monthly_tcu_limit:
                logger.info(
                    "entitlement monthly exceeded | user=%s plan=%s "
                    "used=%.4f estimated=%.4f limit=%.4f",
                    user_id, plan, monthly_used, estimated_tcu, entitlement.monthly_tcu_limit,
                )
                _reservations_total.inc(plan=plan, outcome="monthly_limit_exceeded")
                raise _limit_exceeded(
                    error="monthly_limit_exceeded",
                    message="Você atingiu o limite de uso deste mês.",
                    now=now,
                    resets_at=month_end,
                )

        # ── Media pool (media creations only) ─────────────────────────────
        # The windows less the chat/agent floor (media/plans.py). Same lock,
        # same ADMIT + CLAMP as the windows above.
        if media_pool_tcu is not None:
            for limit, start, end in ((media_pool_tcu[0], day_start, day_end),
                                      (media_pool_tcu[1], week_start, week_end)):
                if limit is None:
                    continue
                media_used = _sum_media_tcu(db, user_id, start, end)
                remaining_budgets.append(limit - media_used)
                if media_used >= limit - 1e-9:
                    logger.info(
                        "entitlement media pool exhausted | user=%s plan=%s used=%.4f estimated=%.4f pool=%.4f",
                        user_id, plan, media_used, estimated_tcu, limit,
                    )
                    _reservations_total.inc(plan=plan, outcome="media_pool_exhausted")
                    raise MediaPoolExhausted(end)

        # The effective budget: the flat per-request estimate, or whatever
        # balance is actually left if that is smaller. Reserving the smaller
        # of the two is what makes the last slice of a window usable without
        # letting anything overshoot - the row itself carries the ceiling,
        # so a concurrent request reads it through _sum_active_tcu and a
        # provider call is capped by it through get_reservation_budget_tcu.
        effective_tcu = estimated_tcu
        if remaining_budgets:
            effective_tcu = min(estimated_tcu, max(0.0, min(remaining_budgets)))

        reservation = UsageRecord(
            id=str(uuid.uuid4()),
            user_id=user_id,
            session_id=session_id,
            message_id=None,
            model=model,
            input_tokens=0,
            output_tokens=0,
            cached_tokens=0,
            cost_usd=round(effective_tcu / 100.0, 8),
            tcu=effective_tcu,
            duration_ms=0,
            status="pending",
        )
        db.add(reservation)
        db.commit()

        # Audit: log quota state at reservation time so ops can track remaining budget.
        # daily/weekly/monthly_used are 0.0 when the plan has no limit for that window.
        logger.info(
            "entitlement reserved | user=%s session=%s plan=%s tcu_reserved=%.4f "
            "daily_used=%.4f daily_limit=%s weekly_used=%.4f weekly_limit=%s "
            "monthly_used=%.4f monthly_limit=%s id=%s",
            user_id, session_id, plan, effective_tcu,
            daily_used, entitlement.daily_tcu_limit,
            weekly_used, entitlement.weekly_tcu_limit,
            monthly_used, entitlement.monthly_tcu_limit,
            reservation.id,
        )
        _reservations_total.inc(plan=plan, outcome="reserved")
        _tcu_reserved.observe(effective_tcu, plan=plan)
        return reservation.id

    except HTTPException:
        raise
    except Exception:
        db.rollback()
        logger.exception("check_and_reserve error | user=%s", user_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Entitlement service temporarily unavailable.",
        )


def finalize_usage(
    db: DBSession,
    reservation_id: str,
    *,
    message_id: str | None,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    cost_usd: float,
    tcu: float,
    duration_ms: int,
) -> UsageRecord:
    """
    Finalize a pending reservation with real values from the provider.
    Replaces estimated TCU with actual TCU and sets status='completed'.
    """
    record = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).first()
    if record is None:
        logger.error("finalize_usage: reservation not found | id=%s", reservation_id)
        raise RuntimeError(f"Reservation {reservation_id} not found for finalize")

    # Idempotency guard (Security Hardening — narrow race with
    # execution_worker.py's reclaim_expired_leases(), which calls
    # cancel_reservation() on a lease it believes is dangling; if the SAME
    # job legitimately finishes and calls finalize_usage() around the
    # same moment, whichever call lands second must be a no-op, never
    # overwrite an already-terminal record with different values).
    if record.status != "pending":
        logger.warning(
            "finalize_usage: reservation already in terminal status, ignoring | id=%s status=%s",
            reservation_id, record.status,
        )
        return record

    record.message_id = message_id
    record.model = model
    record.input_tokens = input_tokens
    record.output_tokens = output_tokens
    record.cached_tokens = cached_tokens
    record.cost_usd = cost_usd
    record.tcu = tcu
    # What the provider really billed, when a free tier makes it differ from list price (TCU is unchanged).
    from src.billing.provider_billing import provider_cost_or_none

    record.provider_cost_usd = provider_cost_or_none(model, float(cost_usd or 0.0))
    record.duration_ms = duration_ms
    record.status = "completed"

    db.commit()
    # Connection-retention follow-up (see 9378494's own analysis): the
    # returned `record`'s refreshed attributes are never read again — this
    # function's own logging/metrics below use the ORIGINAL arguments, not
    # `record`, and _persist_chat_result() (the only caller) never captures
    # this function's return value at all. The refresh served no purpose
    # except reopening a connection that then stayed checked out until
    # get_db()'s db.close() at the very end of the whole streaming
    # response — removing it releases the connection right here, at
    # commit, exactly like every other write in this codebase already does.

    logger.info(
        "entitlement finalized | id=%s model=%s tcu=%.4f cost_usd=%.6f duration_ms=%d",
        reservation_id, model, tcu, cost_usd, duration_ms,
    )
    _tcu_finalized.observe(tcu, model=model or "unknown")
    _cost_usd_finalized.observe(cost_usd, model=model or "unknown")
    return record


def cancel_reservation(db: DBSession, reservation_id: str) -> None:
    """
    Cancel a pending reservation (provider failure, stream interrupt, client cancel, etc.).
    Sets status='failed' and zeroes TCU so it no longer counts against quota.
    """
    try:
        record = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).first()
        if record is None:
            logger.warning("cancel_reservation: not found | id=%s", reservation_id)
            return
        # Idempotency guard — see finalize_usage()'s own comment on the
        # same race. Never zero out an already-completed record's real
        # TCU/cost (that would silently under-count a legitimately
        # finished, billable usage).
        if record.status != "pending":
            logger.warning(
                "cancel_reservation: reservation already in terminal status, ignoring | id=%s status=%s",
                reservation_id, record.status,
            )
            return
        record.status = "failed"
        record.tcu = Decimal("0")
        record.cost_usd = Decimal("0")
        db.commit()
        logger.info("entitlement reservation cancelled | id=%s", reservation_id)
    except Exception:
        logger.exception("cancel_reservation error | id=%s", reservation_id)
        try:
            db.rollback()
        except Exception:
            pass


def get_reservation_budget_tcu(db: DBSession, reservation_id: str) -> float:
    """The most this reservation is allowed to spend, in TCU.

    check_and_reserve() already wrote it onto the reservation row as `tcu`,
    so this is a read of the decision that was made under the advisory lock
    rather than a second, independently-computed opinion about the same
    thing. Callers convert it to USD and pass it to the router as this
    call's cost ceiling (RoutingRequest.max_cost_usd_override), which is
    what keeps the CALL inside what the RESERVATION allows.

    Returns 0.0 for an unknown id — a caller that cannot find its own
    reservation should not be authorised to spend anything.
    """
    row = db.query(UsageRecord.tcu).filter(UsageRecord.id == reservation_id).scalar()
    return float(row) if row is not None else 0.0


def remaining_budget_tcu(
    db: DBSession, user_id: str, plan: str, user_timezone: str | None = None
) -> float | None:
    """How much this user could still spend right now, across every window.

    The smallest remaining balance among session/daily/weekly/monthly (and
    the team pool where one exists) — i.e. the same minimum check_and_reserve()
    clamps to, computed WITHOUT reserving anything.

    Deliberately counts pending reservations, via the same _sum_active_tcu()
    the enforcement path uses. get_user_period_usage() counts only completed
    records because it feeds a display, where showing money that might yet be
    refunded would be misleading. This one feeds a decision about whether to
    START expensive work, and there a reservation someone else's request is
    already holding is very much spent as far as this request is concerned.

    Returns None when the plan has no limits at all — no caller should read
    that as zero.

    Read-only: no advisory lock, no writes. The figure can therefore be stale
    by the time it is acted on, which is fine for its one purpose — deciding
    whether a deck is worth starting. check_and_reserve() remains the
    authority, under its lock, and would clamp or refuse anyway.
    """
    entitlement = get_plan_entitlement(plan)
    now = _utcnow()
    remaining: list[float] = []

    if entitlement.session_tcu_limit is not None:
        # Read-only on purpose: resolve_or_start_session() would START a
        # window, and merely asking "could I afford a deck?" must never do
        # that — the 5-hour clock begins when work is actually reserved.
        session = (
            db.query(EntitlementSession).filter(EntitlementSession.user_id == user_id).first()
        )
        if session is not None and now < session.session_expires_at:
            used = _sum_active_tcu(
                db, user_id, session.session_started_at, session.session_expires_at
            )
            remaining.append(entitlement.session_tcu_limit - used)
        else:
            # No open window, or one that has already lapsed: the next
            # reservation starts a fresh one, so the whole session
            # allowance is genuinely available.
            remaining.append(float(entitlement.session_tcu_limit))

    if entitlement.team_weekly_pool_tcu_limit is not None:
        team_id = db.query(User.team_id).filter(User.id == user_id).scalar()
        if team_id:
            week_start, week_end = _week_window(user_timezone)
            pool_used = _sum_team_active_tcu(db, team_id, week_start, week_end)
            remaining.append(entitlement.team_weekly_pool_tcu_limit - pool_used)

    if entitlement.daily_tcu_limit is not None:
        day_start, day_end = _day_window(user_timezone)
        remaining.append(entitlement.daily_tcu_limit - _sum_active_tcu(db, user_id, day_start, day_end))

    if entitlement.weekly_tcu_limit is not None:
        week_start, week_end = _week_window(user_timezone)
        remaining.append(entitlement.weekly_tcu_limit - _sum_active_tcu(db, user_id, week_start, week_end))

    if entitlement.monthly_tcu_limit is not None:
        month_start, month_end = _month_window()
        remaining.append(entitlement.monthly_tcu_limit - _sum_active_tcu(db, user_id, month_start, month_end))

    if not remaining:
        return None
    return max(0.0, min(remaining))


def check_agent_iteration_budget(
    db: DBSession,
    reservation_id: str,
    user_id: str,
    plan: str,
    accumulated_tcu: float,
    user_timezone: str | None = None,
) -> bool:
    """
    Beta hardening (2026-09-10) — closes the "AGENT OVERSHOOT" gap
    documented in this module's own docstring: check_and_reserve() only
    ever reserves ONE max_tcu_per_request estimate up front, but an
    agent's ReAct loop can make up to settings.agent_max_iterations
    model calls. Without a per-iteration recheck, a single agent run
    could consume up to max_tcu_per_request × agent_max_iterations
    before finalize_usage() ever records (and therefore before anything
    downstream could react to) the real total.

    Called by agent_service.py's ReAct loop AFTER an iteration that
    produced tool_calls (i.e. right before the loop would call the model
    again) — never before the FIRST call, which check_and_reserve()
    itself already covers.

    Reuses the EXACT SAME daily/weekly/monthly windows, the SAME
    _sum_active_tcu() helper, and the SAME advisory lock as
    check_and_reserve() — no parallel accounting, no second source of
    truth; this function lives in the entitlement service precisely so
    that stays true. Two things happen under that one lock — and,
    critically, in the SAME transaction: `pg_advisory_xact_lock` is
    transaction-scoped, so it releases the instant this function commits.
    The tcu update below therefore uses `flush()`, never an intermediate
    `commit()`, so the SUM comparisons that follow still run under the
    SAME lock/transaction as the write — exactly the shape
    check_and_reserve() itself uses (lock → reads/writes → ONE commit at
    the very end). An earlier draft of this function committed right
    after the update, which would have silently released the lock before
    the limit checks ran, reopening the exact write-skew window this
    function exists to close for a second concurrent agent iteration.

      1. The reservation's own `tcu` column is updated to
         `accumulated_tcu` — the REAL running total of the agent run so
         far, replacing the flat max_tcu_per_request placeholder
         check_and_reserve() started it at. This keeps _sum_active_tcu()
         (used here, by check_and_reserve() for a concurrent second
         request from the SAME user, and by the daily/weekly/monthly
         display endpoints) accurate in real time instead of only once
         finalize_usage() finally runs at the very end.
      2. The SAME three limit comparisons check_and_reserve() makes are
         repeated against the now-current SUM, using one more
         max_tcu_per_request as the conservative estimate for the NEXT
         call about to be made.

    The tcu update is committed unconditionally (whether or not the
    budget check below passes) — the real, safe total accumulated so far
    must be persisted either way, never lost because the NEXT call was
    refused.

    Returns True if the loop may continue, False if it must stop — NEVER
    raises: the caller is responsible for breaking its own loop (the
    same `stopped_early_reason` mechanism agent_service.py already uses
    for its repeated-action/no-progress circuit breakers, not a new
    control-flow shape) and finalizing with whatever was genuinely
    accumulated so far.
    """
    entitlement = get_plan_entitlement(plan)
    estimated_next_call = entitlement.max_tcu_per_request

    _acquire_tcu_advisory_lock(db, user_id)

    record = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).first()
    if record is not None and record.status == "pending":
        record.tcu = accumulated_tcu
        db.flush()  # visible to the SUM query below, still under this SAME lock/transaction

    day_start, day_end = _day_window(user_timezone)
    week_start, week_end = _week_window(user_timezone)
    month_start, month_end = _month_window()

    # The 5-hour window has to be re-checked here for the same reason the
    # others are: a ReAct loop is the workload most able to spend a budget
    # between one check and the next, and a window an agent can walk around
    # is not a window. Read (never started) here — starting a session is
    # check_and_reserve's job, and a loop must not be able to open a fresh
    # window for itself mid-run.
    windows: list[tuple[str, float | None, datetime, datetime]] = []
    if entitlement.session_tcu_limit is not None:
        active = (
            db.query(EntitlementSession)
            .filter(EntitlementSession.user_id == user_id)
            .first()
        )
        if active is not None:
            windows.append((
                "session", entitlement.session_tcu_limit,
                active.session_started_at, active.session_expires_at,
            ))
    windows += [
        ("daily", entitlement.daily_tcu_limit, day_start, day_end),
        ("weekly", entitlement.weekly_tcu_limit, week_start, week_end),
        ("monthly", entitlement.monthly_tcu_limit, month_start, month_end),
    ]

    exceeded = False

    # The shared pool binds a team's loops too, or one member's agent could
    # drain the week for everyone else.
    if entitlement.team_weekly_pool_tcu_limit is not None:
        team_id = db.query(User.team_id).filter(User.id == user_id).scalar()
        if team_id:
            _acquire_team_advisory_lock(db, team_id)
            pool_used = _sum_team_active_tcu(db, team_id, week_start, week_end)
            if pool_used >= entitlement.team_weekly_pool_tcu_limit:
                logger.info(
                    "agent iteration budget exceeded (team pool) | team=%s user=%s "
                    "used=%.4f estimated_next=%.4f limit=%.4f reservation=%s",
                    team_id, user_id, pool_used, estimated_next_call,
                    entitlement.team_weekly_pool_tcu_limit, reservation_id,
                )
                exceeded = True

    for label, limit, period_start, period_end in windows:
        if exceeded:
            break
        if limit is None:
            continue
        used = _sum_active_tcu(db, user_id, period_start, period_end)
        # Matches check_and_reserve's ADMIT rule: the loop continues while
        # the window still has balance, and the next call is clamped to that
        # balance by the same reservation-budget mechanism. Testing
        # `used + estimated_next_call > limit` here would have re-introduced
        # the locked tail this pass exists to remove, one iteration early.
        if used >= limit:
            logger.info(
                "agent iteration budget exceeded (%s) | user=%s plan=%s used=%.4f "
                "estimated_next=%.4f limit=%.4f accumulated_so_far=%.4f reservation=%s",
                label, user_id, plan, used, estimated_next_call, limit, accumulated_tcu, reservation_id,
            )
            exceeded = True
            break

    # ONE commit at the very end — releases the advisory lock only now,
    # after both the write and every read that must be serialized with it.
    db.commit()
    return not exceeded


def reclaim_expired_reservations(db: DBSession, *, ttl_seconds: float, now: datetime | None = None) -> int:
    """
    Beta hardening (2026-09-10) — recovers a 'pending' UsageRecord
    reservation whose owning request never reached finalize_usage()/
    cancel_reservation() (e.g. the server process crashed or was
    force-killed between check_and_reserve()'s own commit and its
    caller's `finally` block — see chat_service.py/agent_service.py's
    `_cancel_on_exit` pattern, which guarantees exactly one of those two
    runs on every NORMAL exit path, but cannot run at all if the process
    itself dies).

    Transitions status='pending' -> 'failed' (tcu=0, cost_usd=0) —
    EXACTLY cancel_reservation()'s own transition, reached via one bulk
    UPDATE instead of a per-row read/write so a single sweep call can
    reclaim every expired reservation for every user at once.

    Idempotent and race-safe by construction: the UPDATE's WHERE clause
    (status='pending' AND created_at < cutoff) is evaluated atomically
    per row by the database — a row a legitimate finalize_usage()/
    cancel_reservation() call already moved to 'completed'/'failed'
    simply no longer matches on the next sweep, and a row THIS sweep
    reclaims first is silently no-op'd by finalize_usage()'s own
    idempotency guard if that request's `finally` eventually does run
    late. NEVER touches a 'completed' row — the WHERE clause only ever
    matches 'pending'.

    ttl_seconds must be comfortably larger than the longest a legitimate
    reservation can stay pending — settings.tcu_reservation_ttl_seconds
    defaults to several times settings.agent_scheduler_execution_
    timeout_seconds's own hard per-request cutoff, so a genuinely
    still-running request is never reclaimed out from under itself.

    Returns the number of reservations reclaimed.
    """
    now = now or _utcnow()
    cutoff = now - timedelta(seconds=ttl_seconds)
    reclaimed = (
        db.query(UsageRecord)
        .filter(UsageRecord.status == "pending", UsageRecord.created_at < cutoff)
        .update(
            {
                UsageRecord.status: "failed",
                UsageRecord.tcu: Decimal("0"),
                UsageRecord.cost_usd: Decimal("0"),
            },
            synchronize_session=False,
        )
    )
    if reclaimed:
        db.commit()
        _reservations_reclaimed_total.inc(reclaimed)
        logger.warning(
            "entitlement: reclaimed %d expired pending reservation(s) | ttl_seconds=%.0f cutoff=%s",
            reclaimed, ttl_seconds, cutoff.isoformat(),
        )
    return reclaimed


async def run_tcu_reservation_reclaim_loop(*, stop_event) -> None:
    """Background task started from main.py's lifespan, stopped via
    `stop_event` on shutdown — same in-process asyncio-task pattern as
    every other background sweep in this codebase (webhook_replay_
    service.run_webhook_nonce_cleanup_loop(), execution_worker's
    recovery loop, payment_order_service's expiry loop) — not a new
    scheduling mechanism. See reclaim_expired_reservations()'s own
    docstring for what this reclaims and why it is safe to run
    periodically without ever touching a 'completed' row."""
    import asyncio

    from src.config import settings
    from src.database import SessionLocal

    logger.info(
        "entitlement: reservation reclaim loop started | interval=%.0fs ttl=%.0fs",
        settings.tcu_reservation_reclaim_interval_seconds, settings.tcu_reservation_ttl_seconds,
    )
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.tcu_reservation_reclaim_interval_seconds)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        db = SessionLocal()
        try:
            reclaim_expired_reservations(db, ttl_seconds=settings.tcu_reservation_ttl_seconds)
        except Exception:
            logger.exception("entitlement: reservation reclaim sweep failed")
            db.rollback()
        try:
            # Same cadence, same session: spend alerts need no loop of their own.
            from src.services import spend_guard

            spend_guard.run(db)
        except Exception:
            logger.exception("entitlement: spend guard sweep failed")
        finally:
            db.close()
    logger.info("entitlement: reservation reclaim loop stopped")


def get_user_week_usage(db: DBSession, user_id: str, plan: str, user_timezone: str | None = None) -> dict:
    """Return TCU usage for the current weekly window (used by /users/me/usage/week).

    user_timezone: current_user.timezone — the weekly window is Sunday
    12:00 in this IANA zone (src/timezones.py), not a fixed UTC rule.
    """
    entitlement = get_plan_entitlement(plan)
    week_start, week_end = _week_window(user_timezone)

    result = (
        db.query(func.coalesce(func.sum(UsageRecord.tcu), 0.0))
        .filter(
            UsageRecord.user_id == user_id,
            UsageRecord.status == "completed",
            UsageRecord.created_at >= week_start,
            UsageRecord.created_at < week_end,
        )
        .scalar()
    )
    used = round(float(result), 4)
    limit = entitlement.weekly_tcu_limit
    remaining = max(0.0, round(limit - used, 4)) if limit is not None else None

    return {
        "plan": plan,
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        # Same instant as week_end, named for the frontend countdown — the
        # backend computes it once so the client never re-derives the rule.
        "next_reset_at": week_end.isoformat(),
        "weekly": {
            "used": used,
            "limit": limit,
            "remaining": remaining,
        },
    }


def get_user_period_usage(db: DBSession, user_id: str, plan: str, user_timezone: str | None = None) -> dict:
    """
    Return daily and weekly TCU usage with limits for display via API.

    Uses only 'completed' records (not pending/failed) for the reported figures.
    The quota check (_sum_active_tcu) includes pending records to prevent
    concurrent over-allocation, but the user-facing display shows real consumption.

    remaining is never negative (capped at 0).
    limit/remaining are null for unlimited plans.

    user_timezone: current_user.timezone — see get_user_week_usage().

    Monthly usage is still returned for backward compatibility (some
    callers may not have migrated yet), but monthly_tcu_limit is None for
    every plan (see entitlements.py — there is no operational monthly
    usage cap by product decision). The panel no longer displays this as a
    third quota; it's a separate financial/budget concern if one is added
    later, not a usage entitlement.
    """
    entitlement = get_plan_entitlement(plan)
    day_start, day_end = _day_window(user_timezone)
    week_start, week_end = _week_window(user_timezone)
    month_start, month_end = _month_window()

    def _sum_completed(period_start: datetime, period_end: datetime) -> float:
        result = (
            db.query(func.coalesce(func.sum(UsageRecord.tcu), 0.0))
            .filter(
                UsageRecord.user_id == user_id,
                UsageRecord.status == "completed",
                UsageRecord.created_at >= period_start,
                UsageRecord.created_at < period_end,
            )
            .scalar()
        )
        return round(float(result), 4)

    daily_used = _sum_completed(day_start, day_end)
    weekly_used = _sum_completed(week_start, week_end)
    monthly_used = _sum_completed(month_start, month_end)

    def _remaining(used: float, limit: float | None) -> float | None:
        if limit is None:
            return None
        return max(0.0, round(limit - used, 4))

    return {
        "plan": plan,
        "daily": {
            "used": daily_used,
            "limit": entitlement.daily_tcu_limit,
            "remaining": _remaining(daily_used, entitlement.daily_tcu_limit),
        },
        "weekly": {
            "used": weekly_used,
            "limit": entitlement.weekly_tcu_limit,
            "remaining": _remaining(weekly_used, entitlement.weekly_tcu_limit),
            "week_start": week_start.isoformat(),
            "next_reset_at": week_end.isoformat(),
        },
        "monthly": {
            "used": monthly_used,
            "limit": entitlement.monthly_tcu_limit,
            "remaining": _remaining(monthly_used, entitlement.monthly_tcu_limit),
        },
    }


def get_admin_usage_windows(db: DBSession, user_id: str, plan: str,
                            user_timezone: str | None = None) -> dict:
    """Every usage window for one user, for operators.

    Deliberately separate from get_user_period_usage(): that one feeds the
    customer-facing panel and speaks in remaining allowance, while this one
    exists so an operator can answer "why was this person refused" without
    reading the database by hand. It therefore reports the raw internal
    figures — including TCU, which is exactly the audience that should see
    it — plus the reset instants.

    Counts pending alongside completed, matching what the CHECK actually
    compares against, so a diagnosis never disagrees with the enforcement.

    Contains no message content, no prompt, no PII beyond the user id the
    caller already had to know to ask.
    """
    entitlement = get_plan_entitlement(plan)
    now = _utcnow()
    day_start, day_end = _day_window(user_timezone)
    week_start, week_end = _week_window(user_timezone)

    session_row = (
        db.query(EntitlementSession)
        .filter(EntitlementSession.user_id == user_id)
        .first()
    )

    def _window(used: float, limit: float | None, resets_at: datetime | None) -> dict:
        return {
            "used_tcu": round(used, 4),
            "limit_tcu": limit,
            "remaining_tcu": None if limit is None else max(0.0, round(limit - used, 4)),
            "percent_used": None if not limit else round(100.0 * used / limit, 1),
            "resets_at": resets_at.isoformat() if resets_at else None,
            "resets_in": _humanize_reset(now, resets_at) if resets_at else None,
        }

    session_used = 0.0
    if session_row is not None:
        session_used = _sum_active_tcu(
            db, user_id, session_row.session_started_at, session_row.session_expires_at,
        )

    out = {
        "plan": plan,
        "now": now.isoformat(),
        "session": _window(
            session_used,
            entitlement.session_tcu_limit,
            session_row.session_expires_at if session_row else None,
        ),
        "daily": _window(_sum_active_tcu(db, user_id, day_start, day_end),
                         entitlement.daily_tcu_limit, day_end),
        "weekly": _window(_sum_active_tcu(db, user_id, week_start, week_end),
                          entitlement.weekly_tcu_limit, week_end),
        "per_request_tcu_limit": entitlement.max_tcu_per_request,
        "team": None,
    }

    if entitlement.team_weekly_pool_tcu_limit is not None:
        team_id = db.query(User.team_id).filter(User.id == user_id).scalar()
        pool_used = (
            _sum_team_active_tcu(db, team_id, week_start, week_end) if team_id else 0.0
        )
        out["team"] = {
            "team_id": team_id,
            "members": (
                db.query(func.count(User.id)).filter(User.team_id == team_id).scalar()
                if team_id else 1
            ),
            "pool": _window(pool_used, entitlement.team_weekly_pool_tcu_limit, week_end),
        }

    return out
