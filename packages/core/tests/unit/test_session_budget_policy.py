"""The session window as a product promise, not just a number.

The 2026-09-11 audit found the 5-hour window was sized so tightly that
ordinary use hit it inside the first two hours — a normal working window
measures ~24 TCU and START's usable budget was 8, PRO's 20. Every existing
test passed throughout, because they all checked that the windows were
*coherent* (daily binds inside a week, session inside a day) and none
checked that a real person could get anything done inside one.

That is the gap these tests close. They assert the commercial shape the
limits exist to produce:

    NORMAL   abundant       — comfortably inside one window
    HEAVY    noticeable     — starts to meet the limit
    EXTREME  contained      — stopped well before it costs money

plus the invariant that makes it affordable: WEEKLY, not session or daily,
is what caps a month.
"""
from __future__ import annotations

import pytest

from src.billing import economics as ec
from src.billing.pricing import PLAN_PRICES
from src.billing.team_pricing import LEGACY_FLAT_TEAM_USD
from src.entitlements import get_plan_entitlement
from trinity_router.limits import get_plan_limits

BANDS = ("ultra_low", "low", "standard", "premium")
WORKLOADS = {
    w.name: w for w in (ec.CHAT_TURN, ec.LONG_CHAT_TURN, ec.AGENT_TASK, ec.BROWSER_TASK)
}

# How one task's calls spread across quality bands once the router has picked
# the cheapest model clearing each step's floor. An assumption about task
# difficulty, deliberately pessimistic — it puts more weight on the expensive
# bands than a real mix would, so a plan that passes here has margin to spare.
ROUTING_MIX = {
    "chat turn": {"low": 0.60, "standard": 0.35, "premium": 0.05},
    "long chat turn": {"low": 0.35, "standard": 0.55, "premium": 0.10},
    "agent task": {"low": 0.10, "standard": 0.70, "premium": 0.20},
    "browser task": {"low": 0.10, "standard": 0.70, "premium": 0.20},
}

# What a person does inside ONE five-hour window.
PROFILES = {
    "light": {"chat turn": 12, "long chat turn": 2, "agent task": 0, "browser task": 0},
    "normal": {"chat turn": 30, "long chat turn": 6, "agent task": 1, "browser task": 0},
    "heavy": {"chat turn": 60, "long chat turn": 15, "agent task": 4, "browser task": 2},
    "extreme": {"chat turn": 150, "long chat turn": 40, "agent task": 12, "browser task": 6},
}


def task_cost(name: str, plan: str) -> float:
    """One task's TCU on this plan, after its band ceiling clamps the mix."""
    cap = get_plan_limits(plan).max_quality_class
    cap_i = BANDS.index(cap)
    return sum(
        ec.workload_tcu(WORKLOADS[name], band if BANDS.index(band) <= cap_i else cap) * share
        for band, share in ROUTING_MIX[name].items()
    )


def profile_cost(profile: str, plan: str) -> float:
    return sum(task_cost(n, plan) * c for n, c in PROFILES[profile].items())


def window_budget(plan: str) -> float:
    """What one 5-hour window can actually spend.

    The whole limit, not limit-minus-one-reservation: under the ADMIT rule a
    request is refused only when the window has no balance left, and the
    final request is clamped to whatever remains.
    """
    e = get_plan_entitlement(plan)
    return e.session_tcu_limit if e.session_tcu_limit is not None else e.daily_tcu_limit


# ── The commercial shape ─────────────────────────────────────────────────

@pytest.mark.parametrize("plan", ["start", "pro", "max"])
def test_normal_use_is_abundant_inside_one_window(plan):
    """The failure this suite exists for. Before the session pass a normal
    window blocked START at ~100 minutes and PRO at ~249 — on the two plans
    that carry the conversion."""
    cost = profile_cost("normal", plan)
    budget = window_budget(plan)
    assert cost <= budget * 0.85, (
        f"{plan}: a normal window costs {cost:.1f} TCU against a {budget:.0f} budget "
        f"({100 * cost / budget:.0f}%) — normal use should feel abundant, not metered"
    )


@pytest.mark.parametrize("plan", ["start", "pro", "max"])
def test_light_use_never_comes_close(plan):
    assert profile_cost("light", plan) <= window_budget(plan) * 0.35


def test_heavy_use_starts_to_meet_the_limit():
    """Heavy is where the limit should become visible — fitting on the top
    plan and pinching below it. If heavy fitted everywhere the limits would
    not be doing anything."""
    assert profile_cost("heavy", "max") <= window_budget("max")
    for plan in ("start", "pro"):
        assert profile_cost("heavy", plan) > window_budget(plan), (
            f"{plan} absorbs a heavy window; either it is underpriced or MAX is unsellable"
        )


@pytest.mark.parametrize("plan", ["start", "pro", "max"])
def test_extreme_use_is_contained_on_every_plan(plan):
    assert profile_cost("extreme", plan) > window_budget(plan)


def test_start_can_finish_a_real_agent_task():
    """START could not complete ONE agent task before this pass: the task
    costs ~10.6 TCU routed and the usable window was 8."""
    per_task = task_cost("agent task", "start")
    fits = window_budget("start") / per_task
    assert fits >= 2, (
        f"START fits only {fits:.1f} agent tasks per window ({per_task:.1f} TCU each) — "
        "the cheapest paid plan has to be able to finish the thing it is sold for"
    )


def test_pro_carries_a_working_session():
    assert window_budget("pro") / task_cost("agent task", "pro") >= 4
    assert profile_cost("normal", "pro") <= window_budget("pro") * 0.5


def test_max_is_clearly_superior():
    """Not marginally better — visibly a different plan."""
    assert window_budget("max") >= window_budget("pro") * 3
    assert get_plan_limits("max").max_quality_class == "premium"
    assert get_plan_limits("pro").max_quality_class == "standard"
    # And it is the only plan a heavy window fits inside.
    assert profile_cost("heavy", "max") <= window_budget("max")


def test_team_stays_protected_by_its_pool():
    team = get_plan_entitlement("team")
    assert team.team_weekly_pool_tcu_limit == 2400.0
    # A member gets MAX's own windows...
    assert team.session_tcu_limit == get_plan_entitlement("max").session_tcu_limit
    # ...but no one member can drain the shared pool alone.
    assert team.weekly_tcu_limit <= team.team_weekly_pool_tcu_limit * 0.5


# ── The economics the shape rests on ─────────────────────────────────────

@pytest.mark.parametrize("plan", ["start", "pro", "max", "team"])
def test_weekly_is_what_caps_a_month(plan):
    """Session and daily shape experience; weekly sets the bill. This is the
    invariant that let the session windows be raised at zero margin cost, so
    it is the one to break loudly if anyone changes a daily limit."""
    e = get_plan_entitlement(plan)
    assert e.daily_tcu_limit * 7 > e.weekly_tcu_limit, (
        f"{plan}: seven full days no longer exceed the week — DAILY would now cap "
        f"the month instead of WEEKLY, and the margin model assumes otherwise"
    )
    assert e.session_tcu_limit < e.daily_tcu_limit
    assert e.session_tcu_limit * 4.8 > e.daily_tcu_limit


@pytest.mark.parametrize(
    "plan,expected",
    [("start", 48.8), ("pro", 46.5), ("max", 46.7), ("team", 46.3)],
)
def test_margin_at_the_ceiling_is_unchanged_by_the_session_pass(plan, expected):
    e = get_plan_entitlement(plan)
    binding = e.team_weekly_pool_tcu_limit or e.weekly_tcu_limit
    # The flat TEAM entitlement still exists for legacy accounts, priced at its old flat fee.
    price = float(LEGACY_FLAT_TEAM_USD) if plan == "team" else float(PLAN_PRICES[plan]["USD"])
    margin = ec.gross_margin_at_ceiling(price, binding) * 100
    assert margin == pytest.approx(expected, abs=0.3), (
        f"{plan}: margin moved to {margin:.1f}% — the session pass was supposed to "
        f"leave every weekly ceiling alone"
    )


# ── The two per-request caps must agree ──────────────────────────────────

@pytest.mark.parametrize("plan", ["start", "pro", "max", "team"])
def test_no_single_call_may_be_authorised_beyond_one_window(plan):
    """The router used to allow a PRO call costing 50 TCU against a 28 TCU
    session — 1.8x the whole window, discovered only on the NEXT request."""
    router_cap_tcu = get_plan_limits(plan).max_estimated_cost_per_request_usd * 100
    session = get_plan_entitlement(plan).session_tcu_limit
    assert router_cap_tcu <= session, (
        f"{plan}: one call may cost {router_cap_tcu:.0f} TCU but the whole window "
        f"holds {session:.0f}"
    )


@pytest.mark.parametrize("plan", ["free", "start", "pro", "max", "team"])
def test_the_reservation_estimate_never_exceeds_the_router_cap(plan):
    """The flat reservation is meant to be a conservative estimate of one
    call, so it cannot be larger than the most one call may cost."""
    e = get_plan_entitlement(plan)
    router_cap_tcu = get_plan_limits(plan).max_estimated_cost_per_request_usd * 100
    assert e.max_tcu_per_request <= router_cap_tcu


# ── Period boundaries are expressed in the user's own time ───────────────

def test_the_daily_window_resets_at_local_midnight():
    """Until 2026-09-11 the daily window was UTC midnight while the weekly
    one was already Sunday 12:00 LOCAL — so a user in São Paulo watched
    their day roll over at 21:00 and nothing in the product could explain
    it. Both boundaries are now in the user's own time."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from src.timezones import day_window

    # 23:30 UTC is 20:30 the SAME day in São Paulo, so the local day has not
    # turned over yet — under the old UTC rule it was 30 minutes from doing so.
    now = datetime(2026, 9, 11, 23, 30, tzinfo=timezone.utc)
    start, end = day_window(now, "America/Sao_Paulo")

    tz = ZoneInfo("America/Sao_Paulo")
    assert start.astimezone(tz).hour == 0
    assert end.astimezone(tz).hour == 0
    assert start <= now < end
    assert (end - start).total_seconds() == 24 * 3600


def test_the_daily_window_follows_the_zone_it_is_given():
    from datetime import datetime, timezone

    from src.timezones import day_window

    now = datetime(2026, 9, 11, 23, 30, tzinfo=timezone.utc)
    sp_start, _ = day_window(now, "America/Sao_Paulo")
    utc_start, _ = day_window(now, "UTC")
    tokyo_start, _ = day_window(now, "Asia/Tokyo")
    # Three zones, three different instants — the point of the fix.
    assert len({sp_start, utc_start, tokyo_start}) == 3


def test_an_unknown_timezone_still_produces_a_window():
    from datetime import datetime, timezone

    from src.timezones import day_window

    now = datetime(2026, 9, 11, 23, 30, tzinfo=timezone.utc)
    start, end = day_window(now, "Not/AZone")
    assert start < now < end


def test_the_five_hour_window_is_not_a_calendar_boundary():
    """The session window is a duration from an instant, so the timezone fix
    must not have touched it — it has no local midnight to be wrong about."""
    from src.services.session_window_service import SESSION_DURATION

    assert SESSION_DURATION.total_seconds() == 5 * 3600
