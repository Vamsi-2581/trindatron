"""No plan may lose money on a customer who stays within its rules.

The rule these tests enforce is TRINDATRON's own: a plan does not go on sale
while any usage the plan PERMITS costs more than the plan earns. The worst
case is modelled, never the average — "most people will not use it all" is
not an argument a limit can rely on.

test_plan_economics.py already holds a 45% gross margin at the ceiling at the
Stripe US card rate. This file holds the fuller statement from
src/billing/unit_economics.py: the WORST fee of any reachable processor,
infrastructure, and an allowance for provider spend that can escape TCU,
with at least 20% of net revenue left over. Raising a weekly ceiling above
what that allows fails the build here and names the number.
"""
from __future__ import annotations

import pytest

from src.billing import unit_economics as ue
from src.entitlements import PLAN_ENTITLEMENTS


# ── U01/U02: every paid plan survives its own ceiling at the worst fee ──


@pytest.mark.parametrize("plan", ue.PAID_PLANS)
def test_U01_no_paid_plan_loses_money_at_its_ceiling_on_the_worst_processor(plan):
    members = 150 if plan == "team" else 1
    month = ue.plan_month(plan, 1.0, members=members)
    assert month.contribution > 0, (
        f"{plan}: a customer at the ceiling costs {month.ai + month.infra:.2f} against "
        f"{month.net:.2f} of net revenue on {ue.worst_case_fee(month.revenue).name}"
    )
    assert month.margin >= ue.MIN_CONTRIBUTION_AT_CEILING, (
        f"{plan}: {month.margin:.1%} left at the ceiling, below the "
        f"{ue.MIN_CONTRIBUTION_AT_CEILING:.0%} floor"
    )


@pytest.mark.parametrize("plan", ue.PAID_PLANS)
def test_U02_current_ceiling_is_within_the_safe_ceiling(plan):
    members = 150 if plan == "team" else 1
    current = ue.binding_weekly_tcu(plan)
    safe = ue.safe_weekly_ceiling_tcu(plan, members=members)
    assert current <= safe, (
        f"{plan}: {current:,.0f} TCU/week exceeds the safe ceiling of {safe:,.0f} — "
        f"lower the ceiling or raise the price"
    )


def test_U02b_the_worst_processor_is_the_one_modelled():
    for plan in ue.PAID_PLANS:
        price = ue.price_usd(plan)
        worst = ue.worst_case_fee(price)
        assert all(worst.fee_usd(price) >= p.fee_usd(price) for p in ue.PROCESSORS)


# ── U03: TEAM ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("members", ue.TEAM_SEAT_COUNTS)
def test_U03_flat_team_stays_profitable_at_any_supported_headcount(members):
    """The pool bounds provider spend; infrastructure grows with members. At
    the 150-member mark (the market's own ceiling) TEAM must still clear the floor."""
    month = ue.flat_team_month(members, 1.0)
    assert month.margin >= ue.MIN_CONTRIBUTION_AT_CEILING, (members, month.margin)


def test_U03b_the_pool_is_what_bounds_flat_team():
    team = PLAN_ENTITLEMENTS["team"]
    assert team.team_weekly_pool_tcu_limit is not None
    assert ue.binding_weekly_tcu("team") == team.team_weekly_pool_tcu_limit


def test_U03f_team_headcount_is_capped_where_the_model_proves_it():
    """Infrastructure is per account, so a pool alone does not bound TEAM.
    Raising the cap past the largest headcount U03 proves must fail here."""
    cap = PLAN_ENTITLEMENTS["team"].team_max_members
    assert cap is not None
    assert cap <= max(ue.TEAM_SEAT_COUNTS)
    assert ue.flat_team_month(cap, 1.0).margin >= ue.MIN_CONTRIBUTION_AT_CEILING


@pytest.mark.parametrize("seat", ("standard", "premium"))
def test_U03c_proposed_seat_prices_clear_the_floor_and_undercut_the_market(seat):
    floor = ue.seat_price_floor_usd(seat)
    proposed = ue.PROPOSED_SEAT_USD[seat]
    market_monthly = ue.MARKET_SEATS_USD[f"claude_team_{seat}"][1]
    assert floor <= proposed <= market_monthly, (seat, floor, proposed, market_monthly)


@pytest.mark.parametrize("seats", ue.TEAM_SEAT_COUNTS)
@pytest.mark.parametrize("premium_share", ue.TEAM_PREMIUM_SHARES)
def test_U03d_every_seat_mix_is_profitable_at_full_use(seats, premium_share):
    month = ue.seat_team_month(seats, premium_share, 1.0)
    assert month.margin >= ue.MIN_CONTRIBUTION_AT_CEILING, (seats, premium_share, month.margin)


def test_U03e_seats_keep_each_members_allowance_where_a_flat_pool_dilutes_it():
    """The commercial reason to move to seats: at 20 members a flat pool gives
    each person less than START does, while a Standard seat carries PRO's
    allowance whatever the headcount."""
    flat = ue.flat_team_month(20, 1.0)
    seats = ue.seat_team_month(20, 0.0, 1.0)
    assert flat.tcu_per_member_week < PLAN_ENTITLEMENTS["start"].weekly_tcu_limit * 1.2
    assert seats.tcu_per_member_week == PLAN_ENTITLEMENTS["pro"].weekly_tcu_limit


# ── U04: FREE ─────────────────────────────────────────────────────────────


def test_U04_one_free_account_costs_under_a_dollar_at_its_ceiling():
    assert ue.ai_cost_month_usd("free", 1.0) <= 1.00


@pytest.mark.parametrize("accounts", (100, 1_000, 10_000, 100_000, 10_000_000))
def test_U04b_free_exposure_does_not_grow_with_the_number_of_accounts(accounts):
    from src.config import Settings

    budget_day = Settings.model_fields["free_plan_global_daily_budget_usd"].default
    assert ue.free_population_ai_month_usd(accounts, 1.0) <= budget_day * ue.DAYS_PER_MONTH + 1e-9


# ── U05: overage ──────────────────────────────────────────────────────────


def test_U05_credits_sold_at_cost_lose_money():
    """Why overage is not implemented: at 1 TCU = $0.01 a credit pack returns
    less than the processor takes. A pack has to be priced above cost."""
    assert ue.credit_pack_margin(10.0, 1_000) < 0
    assert ue.minimum_usd_per_tcu_for_credits(10.0) > ue.USD_PER_TCU * 1.2


@pytest.mark.parametrize("amount", [1.0, 5.0, 10.0, 49.9, 200.0])
def test_U05b_the_most_credits_a_payment_can_buy_still_clears_the_floor(amount):
    """POST /billing/orders refuses anything above this cap, so the cap itself
    must be profitable at the worst processor."""
    tcu = ue.max_credits_for_payment(amount, "USD", "TCU")
    assert tcu > 0
    assert ue.credit_pack_margin(amount, tcu) >= ue.MIN_CONTRIBUTION_AT_CEILING - 1e-9


def test_U05c_payments_that_cannot_be_valued_buy_nothing():
    assert ue.max_credits_for_payment(0.25, "USD", "TCU") == 0.0    # below the fixed fee
    assert ue.max_credits_for_payment(10.0, "BRL", "TCU") is None   # no exchange rate here
    assert ue.max_credits_for_payment(10.0, "USD", "EUR") is None
    assert ue.max_credits_for_payment(10.0, "USDT", "USD") == 10.0


# ── U06: the model itself ─────────────────────────────────────────────────


def test_U06_infrastructure_changes_tier_as_the_base_grows():
    small = ue.infrastructure_month({"free": 8, "start": 1, "pro": 1})
    large = ue.infrastructure_month({"free": 8_000, "start": 1_000, "pro": 700, "max": 250, "team": 250})
    assert small["core_replicas"] == 1 and small["redis_required"] == 0
    assert large["core_replicas"] > 1 and large["redis_required"] == 1
    assert large["total"] > small["total"]


def test_U07_a_seat_never_undercuts_its_individual_plan_per_unit():
    for seat, plan in ue.SEAT_ENTITLEMENT.items():
        seat_rate = ue.seat_usd_per_weekly_tcu(seat)
        individual = ue.usd_per_weekly_tcu(ue.price_usd(plan), ue.binding_weekly_tcu(plan))
        assert individual <= seat_rate <= individual * ue.SEAT_PREMIUM_OVER_INDIVIDUAL_MAX, (seat, seat_rate, individual)


def test_U07b_a_premium_seat_costs_in_proportion_to_the_capacity_it_carries():
    price_ratio = ue.PROPOSED_SEAT_USD["premium"] / ue.PROPOSED_SEAT_USD["standard"]
    capacity_ratio = ue.binding_weekly_tcu("max") / ue.binding_weekly_tcu("pro")
    assert abs(price_ratio / capacity_ratio - 1.0) <= 0.10


def test_U07c_every_seat_carries_its_entitlement_below_its_safe_ceiling():
    for seat, plan in ue.SEAT_ENTITLEMENT.items():
        assert ue.binding_weekly_tcu(plan) <= ue.seat_safe_weekly_ceiling_tcu(seat), seat


def test_U07d_flat_team_is_not_proportional_at_a_small_headcount():
    """Why seats: two people on flat TEAM pay more per unit they can actually
    use than they would for two MAX subscriptions."""
    two_max = ue.usd_per_weekly_tcu(2 * ue.price_usd("max"), 2 * ue.binding_weekly_tcu("max"))
    assert ue.flat_team_usd_per_usable_tcu(2) > two_max


def test_U07e_flat_team_dilutes_below_free_at_its_member_cap():
    """...and at the other end each member of a full flat TEAM gets less than FREE."""
    per_member = ue.flat_team_month(ue.TEAM_MAX_SEATS, 1.0).tcu_per_member_week
    assert per_member < PLAN_ENTITLEMENTS["free"].weekly_tcu_limit


def test_U06b_the_report_renders():
    text = ue.report()
    assert "Plan ceilings" in text and "TEAM seat floor" in text
