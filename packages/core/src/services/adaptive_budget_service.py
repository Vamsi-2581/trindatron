"""
AdaptiveBudgetService — capacity/tier computation for Execution Runtime V1
(mission section 10 "ADAPTIVE CONSUMPTION" and section 11 "USER-FACING USAGE").

Reuses the EXISTING TCU/entitlement engine — computes NOTHING new
financially, only reads already-existing UsageRecord sums via
entitlement_service.py and the existing EntitlementSession window via
session_window_service.py. No new reservation, no new ledger, no double
accounting: this module never calls check_and_reserve/finalize_usage/
cancel_reservation, it only READS what those already wrote.

TCU never crosses this module's public boundary
--------------------------------------------------
Every function here returns percentages/tiers/booleans — never a raw TCU
number — so a caller (the user-facing /users/me/capacity route, or the
execution admission gate) cannot leak TCU even by accident. The one
raw-TCU read happens internally, from entitlement_service's own functions,
and is immediately converted to a ratio before this module returns anything.

"Sessão atual" percentage — a documented design choice, not a new limit
---------------------------------------------------------------------------
PlanEntitlement (src/entitlements.py) has NO field for a session-scoped TCU
limit — only daily/weekly/monthly. The mission's own UX mockup nonetheless
wants a "Sessão atual: 62% usado" figure, and the only session concept that
exists is EntitlementSession (session_window_service.py), a pure 5-hour
TIME window with no budget of its own. Rather than inventing a new plan
field (a new business rule the mission explicitly forbids without one),
this module computes the session's "% used" as (TCU consumed since
session_started_at) / daily_tcu_limit — using the EXISTING daily limit as
the session window's effective ceiling, since a session is bounded well
under 24h and the daily dimension is the nearest existing governance
concept for that timescale. This is a presentation heuristic combining two
already-real numbers, not a new limit — see docs/EXECUTION_RUNTIME.md for
the same note, and reconsider if product ever defines a real session quota.
UPDATE: product defined that quota, first for MAX/TEAM and then, in the
2026-09-11 economics pass, for every paid plan. So the percentage is now
measured against the ceiling actually enforced, and the heuristic above
survives only for FREE, which has no session budget of its own. A plan
with neither a session nor a daily ceiling would still report None, but no
such plan exists.

Capacity tiers (mission section 10's table, applied literally)
--------------------------------------------------------------------
  capacity_remaining_pct = min(session_remaining_pct, weekly_remaining_pct)
  (the more constrained of the two dimensions — deliberately conservative)

  100-76%: NORMAL              — every priority admitted
   75-51%: NORMAL_OPTIMIZATION — every priority admitted (optimization is a
                                  signal for callers to prefer cheaper
                                  models/skip redundant LLM calls — see
                                  should_skip_llm() below — not an admission
                                  change)
   50-26%: REDUCE_NON_CRITICAL — "low" priority jobs refused
   25-11%: PRIORITY_ONLY       — only "critical"/"high" admitted
   10-1% : PAUSE_LOW_PRIORITY  — only "critical" admitted
       0%: PAUSE_EXECUTION     — only "critical" admitted (never fully zero
                                  — a monitoring/safety agent must still be
                                  able to run something critical; the
                                  mission's own example never explicitly
                                  says critical work stops at 0%, and
                                  refusing ALL execution including safety
                                  work is a worse failure mode than
                                  continuing to allow it)
A plan with unlimited daily/weekly quota (max/team) is always capacity
100% / NORMAL — there is no ceiling to divide against.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session as DBSession

from src.entitlements import get_plan_entitlement
from src.models.usage import UsageRecord
from src.services.session_window_service import get_session_status

TIER_NORMAL = "normal"
TIER_NORMAL_OPTIMIZATION = "normal_optimization"
TIER_REDUCE_NON_CRITICAL = "reduce_non_critical"
TIER_PRIORITY_ONLY = "priority_only"
TIER_PAUSE_LOW_PRIORITY = "pause_low_priority"
TIER_PAUSE_EXECUTION = "pause_execution"

# priority -> minimum tier (inclusive) at which that priority is still admitted.
_TIER_ORDER = [
    TIER_PAUSE_EXECUTION, TIER_PAUSE_LOW_PRIORITY, TIER_PRIORITY_ONLY,
    TIER_REDUCE_NON_CRITICAL, TIER_NORMAL_OPTIMIZATION, TIER_NORMAL,
]
_MIN_TIER_FOR_PRIORITY: dict[str, str] = {
    "critical": TIER_PAUSE_EXECUTION,       # admitted at every tier, including 0%
    "high": TIER_PRIORITY_ONLY,
    "normal": TIER_REDUCE_NON_CRITICAL,
    "low": TIER_NORMAL_OPTIMIZATION,
}


@dataclass(frozen=True)
class CapacitySnapshot:
    tier: str
    capacity_remaining_pct: float
    session_pct_used: float | None
    session_resets_at: str | None
    session_active: bool
    weekly_pct_used: float | None
    weekly_resets_at: str | None


def _tier_for_pct(pct_remaining: float) -> str:
    if pct_remaining <= 0:
        return TIER_PAUSE_EXECUTION
    if pct_remaining <= 10:
        return TIER_PAUSE_LOW_PRIORITY
    if pct_remaining <= 25:
        return TIER_PRIORITY_ONLY
    if pct_remaining <= 50:
        return TIER_REDUCE_NON_CRITICAL
    if pct_remaining <= 75:
        return TIER_NORMAL_OPTIMIZATION
    return TIER_NORMAL


def _sum_tcu_since(db: DBSession, user_id: str, since: datetime) -> float:
    result = (
        db.query(func.coalesce(func.sum(UsageRecord.tcu), 0.0))
        .filter(
            UsageRecord.user_id == user_id,
            UsageRecord.status == "completed",
            UsageRecord.created_at >= since,
        )
        .scalar()
    )
    return float(result)


def compute_capacity(
    db: DBSession, *, user_id: str, plan: str, user_timezone: str | None = None,
) -> CapacitySnapshot:
    """The one function both the user-facing route and the execution
    admission gate call — a single source of truth for "how much capacity
    is left right now"."""
    from src.services.entitlement_service import get_user_period_usage

    entitlement = get_plan_entitlement(plan)
    now = datetime.now(timezone.utc)

    session = get_session_status(db, user_id=user_id, now=now)
    # The session ceiling: a real one when the plan has it, otherwise the
    # daily limit standing in as the nearest existing governance concept.
    # See this module's docstring — it flagged the heuristic and asked for
    # it to be reconsidered once product defined a real session quota. MAX
    # and TEAM now have one, so they use it and the percentage stops being
    # an approximation for exactly the plans that most needed it to be real.
    session_ceiling = entitlement.session_tcu_limit or entitlement.daily_tcu_limit

    session_pct_used: float | None = None
    if session["active"] and session_ceiling:
        session_started = datetime.fromisoformat(session["session_started_at"])
        used_in_session = _sum_tcu_since(db, user_id, session_started)
        session_pct_used = min(100.0, round(used_in_session / session_ceiling * 100, 1))

    period = get_user_period_usage(db, user_id=user_id, plan=plan, user_timezone=user_timezone)
    weekly = period["weekly"]
    weekly_pct_used: float | None = None
    if weekly["limit"]:
        weekly_pct_used = min(100.0, round(weekly["used"] / weekly["limit"] * 100, 1))

    session_remaining = 100.0 - session_pct_used if session_pct_used is not None else 100.0
    weekly_remaining = 100.0 - weekly_pct_used if weekly_pct_used is not None else 100.0
    capacity_remaining = round(min(session_remaining, weekly_remaining), 1)

    return CapacitySnapshot(
        tier=_tier_for_pct(capacity_remaining),
        capacity_remaining_pct=capacity_remaining,
        session_pct_used=session_pct_used,
        session_resets_at=session.get("session_expires_at"),
        session_active=session["active"],
        weekly_pct_used=weekly_pct_used,
        weekly_resets_at=weekly.get("next_reset_at"),
    )


def priority_admitted(tier: str, priority: str) -> bool:
    """Whether a job of `priority` may be admitted at the current `tier`.
    "critical" is always True (see module docstring's rationale)."""
    min_tier = _MIN_TIER_FOR_PRIORITY.get(priority, TIER_NORMAL)
    return _TIER_ORDER.index(tier) >= _TIER_ORDER.index(min_tier)


def should_skip_llm(*, has_state_changed: bool) -> bool:
    """Mission section 10's "API check -> sem LLM / detectou alteração ->
    chama modelo" rule, expressed as the one bit of information a caller
    (a monitoring-style job) actually has: did the cheap, non-LLM check
    detect a change? If not, skip the model call entirely. Deliberately a
    pure function with no capacity/tier dependency — this optimization
    applies REGARDLESS of current capacity, it's just good sense, not a
    budget-driven throttle.

    Bug fix (Persistent Agent Runtime V1, Tier 1): this function was
    unused by any caller since it was added, and its body returned
    `has_state_changed` verbatim — the OPPOSITE of what its name and the
    docstring above both say ("if not changed, skip"). The one existing
    test (test_B07) asserted that inverted behavior as intentional
    "passthrough", which was itself wrong — no caller ever depended on
    the old behavior (grep confirms this function had zero call sites
    before Persistent Agent Runtime V1 wired it into
    agent_schedule_service.py's DECIDE step), so fixing it here carries
    no regression risk. Left as-is, it would have made every persistent
    agent's DECIDE step skip acting exactly when something changed, and
    act only when nothing did — silently inverted from day one."""
    return not has_state_changed
