"""
TRINITY Compute Units — plan entitlements (server-side authority).

This module is the SINGLE SOURCE OF TRUTH for per-plan TCU limits and feature flags.
The frontend MUST NOT maintain a local copy of these limits; it must
consume the /users/me/usage and /users/me/entitlement endpoints.

Approved limits — 2026-08-24:
  max_tcu_per_request is derived from ModelRouter PLAN_LIMITS:
    free=0.02 USD, start=0.10, pro=0.50, max/team=2.00 — each × 100 = TCU.

Product decision — 2026-08-31:
  monthly_tcu_limit is None for every plan — there is no operational
  monthly USAGE quota anymore. This field is kept (not deleted) because
  removing it would be a structural change with no benefit — the "None =
  no cap" convention already exists for daily/weekly, so setting it here
  is a pure data change with zero code-path removal. A monthly financial
  budget or spend cap, if ever introduced, is a DIFFERENT concept (belongs
  with the credits/billing layer in src/billing/, not here) and must not
  be conflated with this usage-entitlement quota.
  See PARTE 10 of the Voice/Entitlement implementation task.

Product decision — Continuity Mode (Etapa 5F, 2026-09-02):
  continuity_enabled: mechanically derived from the existing plan
  hierarchy — False for "free" (the only plan with no paid tier behind
  it), True for every other existing plan (start/pro/max/team — all
  already paid tiers per this file's own daily_tcu_limit/feature-flag
  progression). Not a new taxonomy — no plan name was invented.

  continuity_daily_tcu_limit: a SEPARATE budget dimension from
  daily_tcu_limit/weekly_tcu_limit above — those represent PREMIUM quota;
  this represents how much a plan may spend specifically while in
  Continuity Mode (Etapa 5F), financed through the existing
  CreditBalance/CreditTransaction ledger (src/services/credit_service.py),
  never through the premium quota path. "free" is 0.0 (continuity_enabled
  is False for free anyway, so this value is never read on that plan's
  path — kept at 0.0 rather than None specifically so "0 budget" always
  means "cannot fund Continuity", never "unlimited", regardless of which
  field is consulted).

Product decision — Continuity Mode budget values (Etapa 5F, provisioned
2026-09-03, official product values — do not alter, round, redistribute,
or invent other numbers without an explicit new product decision):
  start = 10.0 TCU/day
  pro   = 30.0 TCU/day
  max   = 75.0 TCU/day
  team  = 150.0 TCU/day
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlanEntitlement:
    plan: str
    # TCU quotas
    daily_tcu_limit: float | None    # None = no daily cap
    weekly_tcu_limit: float | None   # None = no weekly cap (Mon–Sun UTC)
    monthly_tcu_limit: float | None  # None = no monthly cap
    # Reservation per request, and — through max_cost_usd_override — the most a
    # single model call may cost. It must pay for every model the plan is
    # entitled to at the plan's output cap: until the 2026-09-13 pre-launch
    # audit MAX reserved 30 TCU while one Opus call is estimated at 42, so
    # PREMIUM could never be served in chat or agent mode, and PRO's Sonnet
    # fallback (8.6) did not fit 8. Now: START 10 (its router cap), PRO 11
    # (above START, as the plan ladder requires; pays for Sonnet and gpt-4o at
    # PRO's 8,192 output cap; stays below a 10-slide presentation deck, ~12.3,
    # which reserves its own estimate), MAX and TEAM 60
    # (30% of their 5-hour session, the most test_usage_limit_economics allows;
    # leaves Opus room for ~38k input tokens at 16,384 output). Held by
    # tests/unit/test_request_budget_invariants.py.
    # Weekly and daily limits — what a plan costs — are unchanged by this.
    max_tcu_per_request: float
    # Feature flags
    supports_agent_mode: bool        # ReAct agent loop (multi-step tool use)
    supports_trinity_code: bool      # TRINITY CODE workspace (filesystem + shell + AI tasks)
    supports_trinity_work: bool      # TRINITY WORK workspace (research + approval gates)
    supports_image_generation: bool  # IMAGE capability (future provider integration)
    # Resource limits
    max_projects: int | None         # None = unlimited
    max_agents: int | None           # None = unlimited
    # Continuity Mode (Etapa 5F) — a budget SEPARATE from daily/weekly_tcu_limit
    # above (premium quota). See this module's "Product decision — Continuity
    # Mode budget values" note for the official per-plan figures.
    continuity_enabled: bool
    continuity_daily_tcu_limit: float

    # Rolling 5-hour session window (src/services/session_window_service.py
    # already tracks the window itself; this is the budget inside it).
    # None = the window carries no budget of its own and only daily/weekly
    # bind — which is how FREE/START/PRO behave, unchanged.
    session_tcu_limit: float | None = None
    # TEAM only: one budget shared by every member of a team, on the same
    # weekly window as weekly_tcu_limit. None = no shared pool (every other
    # plan). See entitlement_service._sum_team_active_tcu.
    team_weekly_pool_tcu_limit: float | None = None
    # TEAM only: how many accounts may share one pool. The pool bounds AI
    # spend, but every member is still an account the infrastructure serves,
    # and src/billing/unit_economics.py prices TEAM at this headcount.
    team_max_members: int | None = None


PLAN_ENTITLEMENTS: dict[str, PlanEntitlement] = {
    # FREE earns nothing, so its ceiling is sized to a person evaluating the
    # product, not to what a price can afford (economic audit, 2026-09-13).
    # FREE routes to the cheapest band: a short chat turn costs ~0.03 TCU on
    # groq and ~0.06 on gpt-4o-mini, so 20 TCU is roughly 330-660 turns a
    # week — far more than anyone needs to decide whether to pay. The old 50
    # changed nothing for a human and let one account cost $2.17 a month; at
    # 20 the worst single account is $0.87. Across accounts, the plan-wide
    # daily budget in entitlement_service bounds the total regardless of how
    # many accounts exist.
    "free": PlanEntitlement(
        "free",
        daily_tcu_limit=10.0,
        weekly_tcu_limit=20.0,
        monthly_tcu_limit=None,  # PARTE 10 — no operational monthly usage cap
        max_tcu_per_request=2.0,
        supports_agent_mode=False,
        supports_trinity_code=False,
        supports_trinity_work=False,
        supports_image_generation=False,
        max_projects=3,
        max_agents=0,
        continuity_enabled=False,  # free plan — no Continuity, per Etapa 5F product decision
        continuity_daily_tcu_limit=0.0,
    ),
    # Repriced and re-windowed 2026-09-11. Every ceiling below is derived
    # from what its price can afford — src/billing/economics.py,
    # affordable_weekly_tcu — and never chosen for how it reads. The WEEKLY
    # window is the one that caps a month, so it is derived first and the
    # shorter windows are fitted underneath it:
    #
    #   daily   ~ weekly / 3.4   about three and a half heavy days exhaust a
    #                            week, so weekly stays the real macro stop
    #   session ~ daily  / 2.5   a 5-hour budget that intense use can spend
    #                            in roughly two hours: the window is a
    #                            budget that renews, not five hours of
    #                            airtime to be paced out
    #   request ~ session / 4    no single call can take a meaningful slice
    #                            of a session
    #
    # The previous figures (100/500 here, 1.000/5.000 on PRO) predated
    # anyone modelling what a plan costs to serve at its own ceiling. At
    # them every paid plan lost money on a user who broke no rule.
    # Re-windowed 2026-09-11 (session pass). The weekly ceilings below are
    # unchanged and remain the economic wall; what moved is the SESSION
    # window, which was sized so tightly that ordinary use hit it inside the
    # first two hours. The audit that led here measured a normal working
    # window at ~24 TCU against a usable session budget of 8 on START and 20
    # on PRO - so "normal" blocked at 100 and 249 minutes respectively.
    #
    # Raising a session costs nothing at the ceiling: monthly worst case is
    # the WEEKLY allowance x weeks-per-month (daily*7 > weekly on every plan,
    # so weekly is what binds a month). Session and daily shape experience;
    # weekly sets the bill. Margins are therefore identical before and after
    # - see tests/unit/test_plan_economics.py, which asserts exactly that.
    #
    #   session  holds a normal working window with room to spare
    #   daily    the second barrier, ~2 full sessions
    #   weekly   unchanged, and still the economic ceiling
    "start": PlanEntitlement(
        "start",
        # $9.90 affords 118 TCU/week; 110 leaves air and is unchanged.
        # The session was 12 (8 usable), which could not finish a single
        # agent task — one costs ~10.6 TCU routed. At 30 a normal window
        # lands at 80% and roughly three agent tasks fit.
        daily_tcu_limit=45.0,
        weekly_tcu_limit=110.0,
        monthly_tcu_limit=None,  # PARTE 10 — no operational monthly usage cap
        max_tcu_per_request=10.0,
        session_tcu_limit=30.0,
        supports_agent_mode=True,
        supports_trinity_code=True,
        supports_trinity_work=False,
        supports_image_generation=False,
        max_projects=10,
        max_agents=5,
        continuity_enabled=True,  # paid plan
        continuity_daily_tcu_limit=4.0,  # rescaled with this plan's own windows
    ),
    "pro": PlanEntitlement(
        "pro",
        # $19.90 affords 241 TCU/week; 235 leaves air. The same steady week
        # that fills START routes to ~110 TCU here, so PRO carries it with
        # room to spare; a genuinely intense week routes to ~530 and does
        # not fit, which is the plan boundary doing its job rather than a
        # number failing. PRO is capped at the standard quality band
        # (model-router limits.py) — that cap, not the ceiling, is what
        # makes the price possible.
        daily_tcu_limit=110.0,
        weekly_tcu_limit=235.0,
        monthly_tcu_limit=None,  # PARTE 10 — no operational monthly usage cap
        max_tcu_per_request=11.0,
        session_tcu_limit=60.0,
        supports_agent_mode=True,
        supports_trinity_code=True,
        supports_trinity_work=True,
        supports_image_generation=False,
        max_projects=50,
        max_agents=20,
        continuity_enabled=True,  # paid plan
        continuity_daily_tcu_limit=8.0,  # rescaled with this plan's own windows
    ),
    # MAX is premium, not infinite. $79.90 affords 981 TCU/week; 950 leaves
    # air. What MAX buys is the premium quality band, and 950 is sized so
    # that the intense profile — the one PRO cannot hold — actually fits:
    # it routes to ~914 TCU/week. That is deliberately close to the ceiling.
    # MAX is the plan where reaching for the best available model stops
    # being a budgeting decision, and it is the last one, so it should sit
    # near its limit rather than leave headroom nobody paid for.
    #
    # Three layers stop automation long before the ceiling does:
    # max_tcu_per_request, the per-iteration agent budget re-check, and
    # Request Control's rate/concurrency limits.
    #
    # session_tcu_limit exists so a burst is absorbed by a window that
    # renews in five hours rather than by a daily cap that locks the user
    # out until midnight. 115 x 4.8 windows/day > the daily cap, so daily
    # still binds within a day and weekly still binds within a week — each
    # window is a real constraint, not decoration.
    "max": PlanEntitlement(
        "max",
        daily_tcu_limit=350.0,
        weekly_tcu_limit=950.0,
        monthly_tcu_limit=None,  # PARTE 10 — no operational monthly usage cap
        max_tcu_per_request=60.0,
        session_tcu_limit=200.0,
        supports_agent_mode=True,
        supports_trinity_code=True,
        supports_trinity_work=True,
        supports_image_generation=False,
        max_projects=None,
        max_agents=None,
        continuity_enabled=True,  # paid plan
        continuity_daily_tcu_limit=30.0,  # rescaled with this plan's own windows
    ),
    # TEAM is MAX's per-member shape plus a pool the whole team draws from.
    # The pool is what makes a flat $199.90/month safe with an open number of
    # seats — without it, a flat price with unbounded members is trivially
    # abusable. $199.90 affords 2.460 TCU/week; 2.400 leaves air.
    #
    # Members carry MAX's own windows, so no individual window is smaller
    # than MAX's. The pool is a second, shared constraint on top of them:
    # about 2.5 members running flat out exhaust it, and the rest of the
    # team is then held until the pool resets. That is the honest shape of
    # a flat price — the per-member limit (950, ~40% of the pool) exists so
    # one person cannot be the one who does it.
    "team": PlanEntitlement(
        "team",
        daily_tcu_limit=350.0,
        weekly_tcu_limit=950.0,
        monthly_tcu_limit=None,  # PARTE 10 — no operational monthly usage cap
        max_tcu_per_request=60.0,
        session_tcu_limit=200.0,
        team_weekly_pool_tcu_limit=2_400.0,
        team_max_members=150,
        supports_agent_mode=True,
        supports_trinity_code=True,
        supports_trinity_work=True,
        supports_image_generation=False,
        max_projects=None,
        max_agents=None,
        continuity_enabled=True,  # paid plan
        continuity_daily_tcu_limit=30.0,  # rescaled with this plan's own windows
    ),
}

_DEFAULT_ENTITLEMENT = PLAN_ENTITLEMENTS["start"]


def get_plan_entitlement(plan: str) -> PlanEntitlement:
    """Return entitlement for plan; defaults to 'start' for unknown plan strings."""
    return PLAN_ENTITLEMENTS.get(plan.lower(), _DEFAULT_ENTITLEMENT)


def entitlement_to_dict(e: PlanEntitlement) -> dict:
    """Serialize a PlanEntitlement to a JSON-safe dict for the /users/me/entitlement endpoint."""
    return {
        "plan": e.plan,
        "quota": {
            "session_tcu_limit": e.session_tcu_limit,
            "team_weekly_pool_tcu_limit": e.team_weekly_pool_tcu_limit,
            "daily_tcu_limit": e.daily_tcu_limit,
            "weekly_tcu_limit": e.weekly_tcu_limit,
            "monthly_tcu_limit": e.monthly_tcu_limit,
            "max_tcu_per_request": e.max_tcu_per_request,
        },
        "features": {
            "supports_agent_mode": e.supports_agent_mode,
            "supports_trinity_code": e.supports_trinity_code,
            "supports_trinity_work": e.supports_trinity_work,
            "supports_image_generation": e.supports_image_generation,
        },
        "limits": {
            "max_projects": e.max_projects,
            "max_agents": e.max_agents,
        },
        "continuity": {
            "enabled": e.continuity_enabled,
            "daily_tcu_limit": e.continuity_daily_tcu_limit,
        },
    }
