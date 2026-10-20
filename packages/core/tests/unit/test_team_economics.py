"""TEAM by seat: one price catalog, and no supported team costs more than it earns."""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.billing import team_pricing
from src.billing import unit_economics as ue
from src.billing.pricing import PLAN_PRICES, purchasable_plans
from src.entitlements import get_plan_entitlement


def test_prices_come_from_one_catalog():
    assert team_pricing.SEAT_PRICES_USD == {"standard": Decimal("24.90"), "premium": Decimal("99.90")}
    assert ue.PROPOSED_SEAT_USD == {"standard": 24.90, "premium": 99.90}
    assert ue.SEAT_ENTITLEMENT == team_pricing.SEAT_PLAN == {"standard": "pro", "premium": "max"}
    assert (ue.TEAM_MIN_SEATS, ue.TEAM_MAX_SEATS) == (2, 150)


def test_the_flat_team_is_not_a_product_anymore():
    assert "team" not in PLAN_PRICES
    assert "team" not in purchasable_plans()


def test_seats_carry_exactly_pro_and_max_entitlements():
    standard, premium = get_plan_entitlement("pro"), get_plan_entitlement("max")
    assert (standard.weekly_tcu_limit, standard.max_tcu_per_request) == (235.0, 11.0)
    assert (premium.weekly_tcu_limit, premium.max_tcu_per_request) == (950.0, 60.0)
    assert standard.team_weekly_pool_tcu_limit is None and premium.team_weekly_pool_tcu_limit is None


@pytest.mark.parametrize("standard,premium,total", [(2, 0, "49.80"), (0, 2, "199.80"), (3, 1, "174.60"), (149, 1, "3810.00")])
def test_the_monthly_price_is_computed_from_the_seats(standard, premium, total):
    assert team_pricing.team_monthly_price_usd(standard, premium) == Decimal(total)


@pytest.mark.parametrize("standard,premium", [(1, 0), (0, 1), (151, 0), (100, 51), (-1, 3)])
def test_seat_counts_outside_2_to_150_are_refused(standard, premium):
    with pytest.raises(team_pricing.InvalidSeatCount):
        team_pricing.team_monthly_price_usd(standard, premium)


# ── BRL added 2026-09-24 (go-live checkout-currency fix) ────────────────────


def test_brl_seat_prices_match_the_business_provided_figures():
    assert team_pricing.SEAT_PRICES["standard"] == {"USD": Decimal("24.90"), "BRL": Decimal("124.50")}
    assert team_pricing.SEAT_PRICES["premium"] == {"USD": Decimal("99.90"), "BRL": Decimal("499.50")}
    # SEAT_PRICES_USD stays the exact backward-compatible view every
    # pre-existing caller (unit_economics.py, team_service.py) reads.
    assert team_pricing.SEAT_PRICES_USD == {"standard": Decimal("24.90"), "premium": Decimal("99.90")}


def test_seat_currency_candidates_puts_usd_first_then_alphabetical():
    assert team_pricing.seat_currency_candidates() == ["USD", "BRL"]


@pytest.mark.parametrize(
    "standard,premium,currency,total",
    [(2, 0, "BRL", "249.00"), (0, 2, "BRL", "999.00"), (3, 1, "BRL", "873.00"), (2, 0, "USD", "49.80")],
)
def test_team_monthly_price_in_brl_matches_the_business_provided_figures(standard, premium, currency, total):
    assert team_pricing.team_monthly_price(standard, premium, currency) == Decimal(total)


def test_team_monthly_price_returns_none_for_an_unpriced_currency():
    assert team_pricing.team_monthly_price(2, 0, "EUR") is None


def test_team_monthly_price_defaults_to_usd_matching_team_monthly_price_usd():
    assert team_pricing.team_monthly_price(3, 1) == team_pricing.team_monthly_price_usd(3, 1)


@pytest.mark.parametrize("seats", [2, 10, 50, 150])
@pytest.mark.parametrize("premium_share", [0.0, 0.2, 0.5, 1.0])
def test_no_team_costs_more_than_it_earns_even_at_full_use(seats, premium_share):
    month = ue.seat_team_month(seats, premium_share, 1.0)
    cost = month.payment_fee + month.ai + month.infra
    assert cost < month.revenue, f"{seats} seats, {premium_share:.0%} premium: cost {cost:.2f} >= revenue {month.revenue:.2f}"
    assert month.margin is not None and month.margin >= ue.MIN_CONTRIBUTION_AT_CEILING


@pytest.mark.parametrize("seats", [2, 10, 50, 150])
def test_every_seat_type_clears_its_price_floor(seats):
    for seat in ("standard", "premium"):
        assert ue.PROPOSED_SEAT_USD[seat] > ue.seat_price_floor_usd(seat)
