"""TEAM by seat — the single source of truth for what a team seat costs and grants.

Decision recorded in docs/TEAM_SEATS.md (2026-09-13), implemented 2026-09-14:

  Standard seat  US$ 24.90 / month  → PRO's entitlement (235 TCU/week, 11 per request)
  Premium seat   US$ 99.90 / month  → MAX's entitlement (950 TCU/week, 60 per request, premium band)
  2 to 150 seats per team, mixed freely. No shared pool, no base fee, no overage.

BRL added 2026-09-24 (same go-live investigation and provenance rule as
src/billing/pricing.py's PLAN_PRICES — an explicit business figure, never
FX-derived): standard R$124.50, premium R$499.50.

A seat never invents a quota: its holder's effective plan IS the individual
plan named here, so every window, reservation and economics test that holds
PRO and MAX holds a seat too. unit_economics.py reads these prices from here.

The flat TEAM (US$ 199.90 with a 2,400 TCU pool) is no longer sold. Its price
is kept below only so the historical comparison in unit_economics.py and the
entitlement of accounts an operator already placed on it keep working.
"""
from __future__ import annotations

from decimal import Decimal

TEAM_SEATS_PLAN = "team_seats"  # PaymentOrder.plan value for a seat purchase

SEAT_TYPES: tuple[str, ...] = ("standard", "premium")

# Keyed by seat type then by ISO 4217 currency code — same shape as
# pricing.PLAN_PRICES, same reason (get_plan_price()'s own docstring: no
# real-time FX conversion, a currency is only ever priced by an explicit
# catalog entry).
SEAT_PRICES: dict[str, dict[str, Decimal]] = {
    "standard": {"USD": Decimal("24.90"), "BRL": Decimal("124.50")},
    "premium": {"USD": Decimal("99.90"), "BRL": Decimal("499.50")},
}
SEAT_PLAN: dict[str, str] = {"standard": "pro", "premium": "max"}

# Backward-compatible view — every pre-existing caller (unit_economics.py's
# safety net, team_service.py's USD-specific call sites) keeps working
# unchanged; this is still THE figure the economics tests hold against.
SEAT_PRICES_USD: dict[str, Decimal] = {seat: prices["USD"] for seat, prices in SEAT_PRICES.items()}

MIN_SEATS = 2
MAX_SEATS = 150

#: Not purchasable. See the module docstring.
LEGACY_FLAT_TEAM_USD = Decimal("199.90")


class InvalidSeatCount(ValueError):
    pass


def validate_seat_counts(standard: int, premium: int) -> None:
    if standard < 0 or premium < 0:
        raise InvalidSeatCount("seat counts cannot be negative")
    total = standard + premium
    if total < MIN_SEATS or total > MAX_SEATS:
        raise InvalidSeatCount(f"a team has between {MIN_SEATS} and {MAX_SEATS} seats")


def seat_currency_candidates() -> list[str]:
    """Every currency BOTH seat types are priced in, USD first — the same
    priority-order convention as pricing.currency_candidates(). A currency
    only one seat type has a price for is excluded: a mixed standard+premium
    checkout must be payable entirely in one currency."""
    common = set(SEAT_PRICES["standard"]) & set(SEAT_PRICES["premium"])
    return (["USD"] if "USD" in common else []) + sorted(common - {"USD"})


def team_monthly_price(standard: int, premium: int, currency: str = "USD") -> Decimal | None:
    """The monthly total in `currency`, or None if either seat type has no
    price entry for it (no partial pricing — see seat_currency_candidates())."""
    validate_seat_counts(standard, premium)
    currency = currency.upper()
    prices = SEAT_PRICES
    if currency not in prices["standard"] or currency not in prices["premium"]:
        return None
    return prices["standard"][currency] * standard + prices["premium"][currency] * premium


def team_monthly_price_usd(standard: int, premium: int) -> Decimal:
    """Kept exactly as before — the USD figure unit_economics.py's safety
    net and every pre-existing caller hold against. Never returns None:
    USD is always priced for both seat types."""
    return team_monthly_price(standard, premium, "USD")
