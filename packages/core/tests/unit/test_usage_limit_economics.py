"""The limits have to survive contact with the pricing page.

Every number here comes from the code, never from a guess:
  1 TCU = $0.01                     trinity_router.tcu.TCU_EXCHANGE_RATE
  plan prices                        src.billing.pricing.PLAN_PRICES
  window ceilings                    src.entitlements.PLAN_ENTITLEMENTS

These tests are the margin review, executable. If someone later raises a
ceiling without thinking about what it costs to honour it, this is what
says so.

A ceiling is the point at which the platform stops, not the expected bill:
reaching it every window of every week takes an automated client, and three
other layers (max_tcu_per_request, the per-iteration agent budget re-check,
and Request Control's rate/concurrency limits) stop automation long before
the ceiling does.

These tests hold the ceilings to a bounded multiple of revenue. The
stricter statement — that the worst case is actually PROFITABLE, per plan,
after payment fees — lives in test_plan_economics.py, together with the
model the ceilings are now derived from. Both are kept: this file is the
coarse guardrail that fails loudly for any plan, that one is the derivation.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.billing.pricing import PLAN_PRICES
from src.entitlements import get_plan_entitlement
from trinity_router.tcu import TCU_EXCHANGE_RATE

USD_PER_TCU = 1.0 / TCU_EXCHANGE_RATE          # 1 TCU = $0.01
WEEKS_PER_MONTH = 52 / 12                       # 4.333…

# Worst case a plan may expose the business to, as a multiple of its price.
# Not a margin target — the ceiling is the stop, not the forecast.
MAX_EXPOSURE_MULTIPLE = 5.0


def _price_usd(plan: str) -> float:
    if plan == "team":
        # Flat TEAM is no longer sold (TEAM is by seat); accounts already on it
        # keep its entitlement, so its last price still bounds that exposure.
        from src.billing.team_pricing import LEGACY_FLAT_TEAM_USD

        return float(LEGACY_FLAT_TEAM_USD)
    return float(PLAN_PRICES[plan]["USD"])


def _binding_weekly_tcu(plan: str) -> float:
    """The weekly ceiling that actually caps what a PLAN can cost.

    For TEAM that is the shared pool, not the per-member limit: the pool is
    what the subscription buys, and members draw from it together.
    """
    e = get_plan_entitlement(plan)
    if e.team_weekly_pool_tcu_limit is not None:
        return e.team_weekly_pool_tcu_limit
    assert e.weekly_tcu_limit is not None
    return e.weekly_tcu_limit


def _monthly_ceiling_usd(plan: str) -> float:
    """Cost of a month spent entirely at the ceiling.

    The weekly window is what binds over a month — nobody can spend more
    than the weekly ceiling per week however many daily windows they open —
    so this is weekly x weeks, not daily x 30.
    """
    return _binding_weekly_tcu(plan) * USD_PER_TCU * WEEKS_PER_MONTH


@pytest.mark.parametrize("plan", ["start", "pro", "max", "team"])
def test_the_worst_case_is_bounded_and_known(plan):
    exposure = _monthly_ceiling_usd(plan) / _price_usd(plan)
    assert exposure <= MAX_EXPOSURE_MULTIPLE, (
        f"{plan}: a month at the ceiling costs {exposure:.1f}x its price "
        f"(${_monthly_ceiling_usd(plan):.0f} vs ${_price_usd(plan):.2f}). "
        "Lower the weekly window or raise the price."
    )


def test_the_team_pool_is_what_bounds_team_not_the_seat_count():
    """TEAM is a flat price with an open number of members. Without a pool,
    cost would scale with headcount while revenue did not."""
    e = get_plan_entitlement("team")
    pool_monthly = e.team_weekly_pool_tcu_limit * USD_PER_TCU * WEEKS_PER_MONTH
    exposure = pool_monthly / _price_usd("team")
    assert exposure <= MAX_EXPOSURE_MULTIPLE, (
        f"TEAM pool exposes {exposure:.1f}x its price (${pool_monthly:.0f})"
    )
    # Whatever the headcount, the pool is the ceiling: ten members cannot
    # cost ten times what two do.
    assert e.team_weekly_pool_tcu_limit < e.weekly_tcu_limit * 10


def test_typical_heavy_use_is_comfortably_profitable():
    """The ceiling is the tail. What matters commercially is the ordinary
    heavy user — modelled here at a fifth of the ceiling, which is already
    far more than a person types in a week."""
    for plan in ("max", "team"):
        cost = _monthly_ceiling_usd(plan) * 0.20
        margin = (_price_usd(plan) - cost) / _price_usd(plan)
        assert margin > 0.20, (
            f"{plan}: a heavy-but-ordinary month leaves {margin:.0%} margin"
        )


def test_a_single_request_cannot_move_the_needle():
    """Per-request cap as a share of the session window: one call must never
    be able to consume a meaningful slice of the 5-hour budget, or a handful
    of requests would lock a user out."""
    for plan in ("max", "team"):
        e = get_plan_entitlement(plan)
        share = e.max_tcu_per_request / e.session_tcu_limit
        assert share <= 0.30, f"{plan}: one request can take {share:.0%} of a session"


def test_the_cheapest_capable_model_is_what_makes_the_ceilings_generous():
    """The router picks the cheapest model that can do the job, so a TCU
    buys far more work here than it would on a fixed premium model. This
    asserts the relationship the limits were sized against, using the real
    catalogue rather than a remembered price."""
    from trinity_router.providers import PROVIDER_CATALOG

    costs = [
        (cfg.cost_per_1m_input_usd + cfg.cost_per_1m_output_usd) / 2
        for cfg in PROVIDER_CATALOG.values()
    ]
    cheapest, dearest = min(costs), max(costs)
    assert cheapest > 0 and dearest > cheapest
    # An order of magnitude or more between the cheapest and dearest option
    # is what the Adaptive Router converts into usable headroom.
    assert dearest / cheapest >= 10, (
        "the spread between cheapest and dearest collapsed — the limits were "
        "sized assuming the router has somewhere cheap to route"
    )


def test_the_daily_windows_are_the_ones_that_were_decided():
    """A blunt pin on the daily ceilings.

    Every other test here reasons about the numbers; this one just states
    them, so that changing a window is never something that happens as a
    side effect of changing something else. If you meant it, update the
    line — after re-running the derivation in test_plan_economics.py."""
    assert get_plan_entitlement("free").daily_tcu_limit == 10.0
    assert get_plan_entitlement("start").daily_tcu_limit == 45.0
    assert get_plan_entitlement("pro").daily_tcu_limit == 110.0
    assert get_plan_entitlement("max").daily_tcu_limit == 350.0
    assert get_plan_entitlement("team").daily_tcu_limit == 350.0


def test_price_ordering_matches_allowance_ordering():
    """Paying more must buy more, on every window, or the plans are
    incoherent regardless of what the copy says."""
    ordered = ["start", "pro", "max"]
    for lower, higher in zip(ordered, ordered[1:]):
        assert _price_usd(lower) < _price_usd(higher)
        assert (get_plan_entitlement(lower).daily_tcu_limit
                < get_plan_entitlement(higher).daily_tcu_limit)
        assert (get_plan_entitlement(lower).weekly_tcu_limit
                < get_plan_entitlement(higher).weekly_tcu_limit)


def test_report_the_numbers(capsys):
    """Not an assertion — a readable record of what the chosen limits imply,
    printed with -s so the margin review is reproducible."""
    lines = ["", "plan   price     weekly ceiling   month at ceiling   exposure"]
    for plan in ("start", "pro", "max", "team"):
        monthly = _monthly_ceiling_usd(plan)
        lines.append(
            f"{plan:6} ${_price_usd(plan):>7.2f}   "
            f"{_binding_weekly_tcu(plan):>8,.0f} TCU   "
            f"${monthly:>10,.0f}   "
            f"{monthly / _price_usd(plan):>5.1f}x"
        )
    print("\n".join(lines))
    assert True


# PRO used to be carved out here: its ceiling exposed 7.2x its price, the
# worst of any plan, and lowering an allowance customers already paid for
# was left as a pricing decision rather than a refactor. That decision has
# since been taken — PRO was repriced to $19.90 and re-windowed to 235
# TCU/week — so the carve-out is gone and PRO is held to the same bar as
# everything else, in test_the_worst_case_is_bounded_and_known above.
