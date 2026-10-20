"""The plan ladder, proved rather than asserted.

Prices and usage ceilings are one decision, and they used to be made in two
places by two different arguments: the ceilings assumed compute was nearly
free, the prices assumed nobody would reach the ceilings. Modelled together,
every paid plan could be pushed into a loss by a user who broke no rule.

These tests are the thing that stops that returning. They read the live
tables — PLAN_PRICES, PLAN_ENTITLEMENTS, the router's per-plan quality
bands, the provider catalogue's real token costs — and fail if the numbers
stop agreeing with each other. Nothing here is a fixture of expected
values; raising a ceiling without re-running the arithmetic breaks the
build, which is the point.

What is proved
--------------
E01) The worst case the product PERMITS is not a loss. For every paid plan,
     a user who consumes every unit the plan allows, every week, still
     leaves a positive gross margin.
E02) The same for TEAM's shared pool, which is what actually bounds a flat
     price with an open number of seats.
E03) FREE's exposure is bounded and small — it is an acquisition cost, and
     it should be legible as one.
E04) Every window actually binds. A window that can never be reached before
     a wider one stops you is decoration, and decoration in a limit system
     is how users end up surprised.
E05) The ladder is monotonic: no plan is worse than a cheaper plan in any
     dimension.
E06) The prices stay at or below the comparable products they were set
     against. Entering a market above the incumbents is the one commercial
     rule that does not bend.
E07) The ladder separates real profiles: steady use fits the cheap plans,
     intense use fits MAX and only MAX. This is what makes the boundary
     between plans a fact about work rather than a marketing choice.
E08) PRO's quality band is what makes PRO's price possible — the test
     records the size of that lever so nobody widens the band without
     seeing the cost.
"""
from __future__ import annotations

import pytest

from src.billing import economics as ec
from src.billing.pricing import PLAN_PRICES
from src.entitlements import PLAN_ENTITLEMENTS
from trinity_router.limits import get_plan_limits

PAID_PLANS = ("start", "pro", "max", "team")
QUALITY_ORDER = ("ultra_low", "low", "standard", "premium")


def price_of(plan: str) -> float:
    if plan == "team":
        # Flat TEAM is no longer sold (TEAM is by seat — tests/unit/test_team_economics.py),
        # but accounts an operator placed on it keep its entitlement, so its last price
        # still has to pay for that entitlement.
        from src.billing.team_pricing import LEGACY_FLAT_TEAM_USD

        return float(LEGACY_FLAT_TEAM_USD)
    return float(PLAN_PRICES[plan]["USD"])


# ── E01/E02: the permitted worst case is not a loss ──────────────────────


@pytest.mark.parametrize("plan", PAID_PLANS)
def test_E01_no_paid_plan_loses_money_at_its_own_ceiling(plan):
    """A user consuming every unit the plan allows must still be profitable.

    Not "unlikely to lose money" — the ceiling is the promise, so the
    ceiling is what has to be affordable. If this fails the fix is to lower
    the ceiling or raise the price, never to widen the tolerance.
    """
    price = price_of(plan)
    weekly = PLAN_ENTITLEMENTS[plan].weekly_tcu_limit
    assert weekly is not None, f"{plan} has no weekly ceiling — cost is unbounded"

    cost = ec.monthly_ai_cost_at_ceiling_usd(weekly)
    net = ec.net_revenue_usd(price)
    margin = ec.gross_margin_at_ceiling(price, weekly)

    assert cost < net, (
        f"{plan}: at its own ceiling it costs ${cost:.2f}/month against "
        f"${net:.2f} of net revenue — a user who broke no rule loses money"
    )
    assert margin >= ec.MIN_GROSS_MARGIN_AT_CEILING, (
        f"{plan}: {margin:.1%} gross margin at the ceiling, below the "
        f"{ec.MIN_GROSS_MARGIN_AT_CEILING:.0%} floor that has to cover "
        f"everything which is not a provider bill"
    )


@pytest.mark.parametrize("plan", PAID_PLANS)
def test_E01b_ceiling_is_within_what_the_price_affords(plan):
    """The same fact stated from the other side, so a failure names the
    number to change: the weekly ceiling must not exceed what the price can
    buy."""
    weekly = PLAN_ENTITLEMENTS[plan].weekly_tcu_limit
    affordable = ec.affordable_weekly_tcu(price_of(plan))
    assert weekly <= affordable, (
        f"{plan}: ceiling {weekly:.0f} TCU/week exceeds the {affordable:.0f} "
        f"its price can honour — lower the ceiling or raise the price"
    )


def test_E02_team_pool_is_what_bounds_the_subscription():
    """TEAM is flat-priced with an open number of seats, so the per-member
    windows cannot be what limits it — the pool has to, and the pool has to
    be affordable on its own."""
    team = PLAN_ENTITLEMENTS["team"]
    pool = team.team_weekly_pool_tcu_limit
    assert pool is not None, "TEAM without a pool is a flat price with unbounded cost"

    price = price_of("team")
    assert ec.monthly_ai_cost_at_ceiling_usd(pool) < ec.net_revenue_usd(price)
    assert ec.gross_margin_at_ceiling(price, pool) >= ec.MIN_GROSS_MARGIN_AT_CEILING

    # And no single member can drain it alone — otherwise one person on a
    # team silently becomes the whole subscription.
    assert team.weekly_tcu_limit < pool, "a member's own ceiling reaches the entire pool"
    assert team.weekly_tcu_limit <= pool * 0.5


def test_E03_free_plan_exposure_is_a_bounded_acquisition_cost():
    """FREE earns nothing, so any cost is spend. It is allowed to be spend —
    it is not allowed to be open-ended, and it should stay small enough to
    read as a line item rather than a risk."""
    free = PLAN_ENTITLEMENTS["free"]
    assert free.weekly_tcu_limit is not None
    monthly = ec.monthly_ai_cost_at_ceiling_usd(free.weekly_tcu_limit)
    assert monthly <= 3.00, (
        f"a free account can cost ${monthly:.2f}/month at its ceiling; that is "
        f"no longer an acquisition cost"
    )
    # FREE must also stay off the expensive bands, or the ceiling is reached
    # in a handful of requests and the plan demonstrates nothing.
    band = get_plan_limits("free").max_quality_class
    assert QUALITY_ORDER.index(band) <= QUALITY_ORDER.index("low")


# ── E04: every window binds ──────────────────────────────────────────────


@pytest.mark.parametrize("plan", ("free",) + PAID_PLANS)
def test_E04_every_window_is_a_real_constraint(plan):
    """A window nobody can reach is decoration. Daily has to bind inside a
    week, a session inside a day, and a single request must not be able to
    take a meaningful slice of a session."""
    e = PLAN_ENTITLEMENTS[plan]

    assert e.daily_tcu_limit * 7 > e.weekly_tcu_limit, (
        f"{plan}: seven full days ({e.daily_tcu_limit * 7:.0f}) do not exceed the "
        f"weekly ceiling ({e.weekly_tcu_limit:.0f}) — the weekly window can never fire"
    )

    if e.session_tcu_limit is not None:
        # A 5-hour window renews up to 4.8 times in a day.
        assert e.session_tcu_limit * 4.8 > e.daily_tcu_limit, (
            f"{plan}: a day of full sessions does not reach the daily ceiling — "
            f"the daily window can never fire"
        )
        assert e.session_tcu_limit < e.daily_tcu_limit, (
            f"{plan}: one session can spend a whole day's allowance"
        )
        assert e.max_tcu_per_request <= e.session_tcu_limit * 0.4, (
            f"{plan}: a single request can take "
            f"{e.max_tcu_per_request / e.session_tcu_limit:.0%} of a session"
        )

    assert e.max_tcu_per_request < e.daily_tcu_limit


def test_E04b_paid_plans_all_have_a_session_window():
    """The 5-hour window is the thing that makes a limit survivable — it
    renews on its own instead of locking someone out until midnight. A paid
    plan without one fails hard at the daily boundary."""
    for plan in PAID_PLANS:
        assert PLAN_ENTITLEMENTS[plan].session_tcu_limit is not None, (
            f"{plan} has no session window; its users hit a daily wall instead"
        )


# ── E05: the ladder only goes up ─────────────────────────────────────────


@pytest.mark.parametrize(
    "cheaper,dearer",
    [("free", "start"), ("start", "pro"), ("pro", "max")],
)
@pytest.mark.parametrize(
    "field", ["daily_tcu_limit", "weekly_tcu_limit", "max_tcu_per_request"]
)
def test_E05_ladder_is_monotonic(cheaper, dearer, field):
    lo = getattr(PLAN_ENTITLEMENTS[cheaper], field)
    hi = getattr(PLAN_ENTITLEMENTS[dearer], field)
    assert hi > lo, f"{dearer} gives less {field} than {cheaper}"


def test_E05b_quality_band_never_narrows_going_up():
    ladder = ["free", "start", "pro", "max"]
    tops = [QUALITY_ORDER.index(get_plan_limits(p).max_quality_class) for p in ladder]
    assert tops == sorted(tops), f"quality bands are not monotonic: {dict(zip(ladder, tops))}"


def test_E05c_team_is_never_worse_than_max_per_member():
    """Somebody on a team must not get less than they would get alone."""
    team, mx = PLAN_ENTITLEMENTS["team"], PLAN_ENTITLEMENTS["max"]
    for field in ("daily_tcu_limit", "weekly_tcu_limit", "max_tcu_per_request", "session_tcu_limit"):
        assert getattr(team, field) >= getattr(mx, field), f"TEAM members get less {field} than MAX"


# ── E06: the market rule ─────────────────────────────────────────────────

# Published monthly USD rates of the products TRINDATRON is positioned
# against, recorded here so the comparison is visible rather than implied.
# A new brand does not enter above the incumbents; if one of these changes,
# this list is what gets updated, deliberately.
COMPETITOR_CEILING_USD = {
    "start": 10.00,   # Abacus ChatLLM Basic
    "pro": 20.00,     # Claude Pro / Abacus ChatLLM Pro
    "max": 100.00,    # Claude Max 5x
    "team": 200.00,   # Claude Max 20x
}


@pytest.mark.parametrize("plan,ceiling", COMPETITOR_CEILING_USD.items())
def test_E06_never_priced_above_the_comparable_product(plan, ceiling):
    price = price_of(plan)
    assert price <= ceiling, (
        f"{plan} at ${price:.2f} is above the ${ceiling:.2f} comparable — "
        f"the one commercial rule that does not bend"
    )


def test_E06b_prices_are_ordered():
    prices = [price_of(p) for p in PAID_PLANS]
    assert prices == sorted(prices)


# ── E07: the ladder separates real profiles ──────────────────────────────
#
# Weekly mixes of the workloads the product actually runs. These are
# estimates of behaviour, not measurements, and they are deliberately on the
# heavy side — a profile that understates use would make the ceilings look
# safer than they are, which is the failure mode worth avoiding.

STEADY = {"chat turn": 150, "long chat turn": 25, "agent task": 3, "browser task": 1}
INTENSE = {"chat turn": 400, "long chat turn": 80, "agent task": 20, "browser task": 8}

# How a week's tasks distribute across quality bands once the router has
# picked the cheapest model that clears each task's floor. Also an
# assumption — the router's actual choice depends on the request — and also
# deliberately pessimistic about how much lands on the expensive bands.
ROUTING_MIX = {
    "chat turn": {"low": 0.60, "standard": 0.35, "premium": 0.05},
    "long chat turn": {"low": 0.35, "standard": 0.55, "premium": 0.10},
    "agent task": {"low": 0.10, "standard": 0.70, "premium": 0.20},
    "browser task": {"low": 0.10, "standard": 0.70, "premium": 0.20},
}

WORKLOADS = {
    w.name: w for w in (ec.CHAT_TURN, ec.LONG_CHAT_TURN, ec.AGENT_TASK, ec.BROWSER_TASK)
}


def routed_weekly_tcu(mix: dict[str, int], plan: str) -> float:
    """What a week of that profile costs on that plan, after routing.

    Demand for a band above the plan's cap lands on the cap instead, which
    is exactly what the router does.
    """
    cap = get_plan_limits(plan).max_quality_class
    cap_i = QUALITY_ORDER.index(cap)
    total = 0.0
    for name, count in mix.items():
        for band, share in ROUTING_MIX[name].items():
            effective = band if QUALITY_ORDER.index(band) <= cap_i else cap
            total += ec.workload_tcu(WORKLOADS[name], effective) * count * share
    return total


@pytest.mark.parametrize("plan", ("start", "pro", "max"))
def test_E07_steady_use_fits_every_paid_plan(plan):
    """Nobody who is simply using the product should meet a wall. If START
    cannot hold steady use, the cheapest paid plan is a trap."""
    used = routed_weekly_tcu(STEADY, plan)
    ceiling = PLAN_ENTITLEMENTS[plan].weekly_tcu_limit
    assert used <= ceiling, (
        f"{plan}: a steady week routes to {used:.0f} TCU against a {ceiling:.0f} "
        f"ceiling — ordinary use does not fit the plan it is sold for"
    )


def test_E07b_intense_use_fits_max():
    """MAX is the top of the ladder, so the heaviest honest profile has to
    fit inside it. If it does not, there is no plan to sell that user."""
    used = routed_weekly_tcu(INTENSE, "max")
    ceiling = PLAN_ENTITLEMENTS["max"].weekly_tcu_limit
    assert used <= ceiling, (
        f"MAX: an intense week routes to {used:.0f} TCU against a {ceiling:.0f} "
        f"ceiling — the top plan cannot hold the users it is for"
    )


def test_E07c_intense_use_does_not_fit_pro():
    """The other half, and the one that keeps the ladder honest: if PRO
    could absorb intense use too, either PRO is underpriced or MAX is
    unsellable. The boundary between them has to be a fact about work."""
    assert routed_weekly_tcu(INTENSE, "pro") > PLAN_ENTITLEMENTS["pro"].weekly_tcu_limit


# ── E08: the size of the quality lever ───────────────────────────────────


def test_E08_quality_band_dominates_cost():
    """Why PRO is capped at "standard".

    The band a plan may reach matters more than the ceiling it is given: the
    identical week of work costs several times more when premium models are
    on the table. This test records that multiplier so that widening a
    plan's band is never mistaken for a small change.
    """
    at_standard = sum(
        ec.workload_tcu(WORKLOADS[name], "standard") * n for name, n in INTENSE.items()
    )
    at_premium = sum(
        ec.workload_tcu(WORKLOADS[name], "premium") * n for name, n in INTENSE.items()
    )
    assert at_premium > at_standard * 3, (
        "the premium band is no longer dramatically dearer than standard — the "
        "reasoning behind PRO's cap and MAX's price needs revisiting"
    )
    # And PRO must still be standing behind that cap.
    assert get_plan_limits("pro").max_quality_class == "standard"
    assert get_plan_limits("max").max_quality_class == "premium"


def test_E08b_a_single_request_cannot_exceed_its_plans_request_cap():
    """The per-request cap is the last line: whatever a plan's band allows,
    one call must not be able to cost more than the plan says. Checked
    against the dearest single workload the product runs, at the plan's own
    cap."""
    for plan in ("free",) + PAID_PLANS:
        cap = get_plan_limits(plan).max_quality_class
        worst = max(ec.workload_tcu(w, cap) for w in (ec.CHAT_TURN, ec.LONG_CHAT_TURN))
        assert worst <= PLAN_ENTITLEMENTS[plan].max_tcu_per_request, (
            f"{plan}: one long chat turn costs {worst:.2f} TCU but the plan caps a "
            f"request at {PLAN_ENTITLEMENTS[plan].max_tcu_per_request:.2f} — normal "
            f"requests would be refused"
        )
