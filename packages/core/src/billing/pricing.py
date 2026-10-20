"""Commercial Checkout V1 — plan pricing catalog.

Single source of truth for what a plan COSTS (as opposed to
src/entitlements.py, which is the single source of truth for what a plan
GRANTS — usage limits/feature flags). No commercial price existed
anywhere in this codebase before this phase (verified: no
PLAN_PRICES/price_usd/monthly_price of any kind in src/ or the frontend;
plans/page.tsx displayed only TCU limits and an explicit "Preços...
disponíveis em breve" notice). These are official, business-provided
figures (not a placeholder), confirmed directly by the user for this
phase — do not alter, round, or invent other numbers without an explicit
new product decision, matching this codebase's own precedent for
provisioned commercial values (see entitlements.py's Continuity Mode
budget note for the same convention).

Only plans with a price here are purchasable through
POST /billing/checkout. "free" deliberately has no entry — it is never a
checkout target (a user is already on "free" by default; there is
nothing to buy). Every other existing plan key (see
entitlements.PLAN_ENTITLEMENTS) has an entry.
"""
from __future__ import annotations

from decimal import Decimal

# Monthly price catalog, keyed by plan then by ISO 4217 currency code.
# Every PaymentOrder.plan value must be a key here. Only USD is populated
# today — these are the same official, business-provided USD figures as
# before (do not alter/round/invent). A currency other than USD is added
# to a plan's inner dict only by an explicit new product/pricing
# decision (e.g. a real BRL figure), never derived here via FX
# conversion — see get_plan_price()'s own docstring.
# Repriced 2026-09-11 against the published rates of comparable products —
# Claude Pro $20 / Max 5x $100, Abacus ChatLLM Basic $10 / Pro $20 — on the
# rule that a new brand does not enter above the incumbents. Each number is
# then checked from the other direction in src/billing/economics.py: the
# weekly ceiling a price can honour without loss is derived from it, and
# tests fail the build if the entitlement table asks for more than the price
# can pay for.
#
# The old figures (9.99 / 29.99 / 89.99 / 249.99) were set before anyone had
# modelled what a plan costs to serve at its own ceiling. At those ceilings
# every paid plan lost money on a user who stayed entirely within the rules —
# PRO worst of all, at roughly 7x its net revenue.
#
# TEAM is sold by seat (src/billing/team_pricing.py), never as a flat plan:
# the old flat TEAM at 199.90 is deliberately absent from this catalog, so it
# cannot be listed by GET /billing/plans nor bought through /billing/checkout.
# BRL added 2026-09-24: business-provided figures for the Brazilian market —
# see the go-live checkout-currency investigation (Mercado Pago never accepts
# USD; every plan was USD-only, so no checkout could complete). Same
# provenance rule as the USD figures above: an explicit product decision,
# never derived here by FX conversion.
PLAN_PRICES: dict[str, dict[str, Decimal]] = {
    "start": {"USD": Decimal("9.90"), "BRL": Decimal("49.90")},
    "pro": {"USD": Decimal("19.90"), "BRL": Decimal("99.90")},
    "max": {"USD": Decimal("79.90"), "BRL": Decimal("399.90")},
}

CHECKOUT_CURRENCY = "USD"

# Backward-compatible view — every pre-existing caller of PLAN_PRICES_USD
# (e.g. purchasable_plans()'s sort key) keeps working unchanged.
PLAN_PRICES_USD: dict[str, Decimal] = {
    plan: prices["USD"] for plan, prices in PLAN_PRICES.items() if "USD" in prices
}


def get_plan_price(plan: str, currency: str = CHECKOUT_CURRENCY) -> Decimal | None:
    """Return the price for `plan` in `currency`, or None if `plan` is not
    purchasable (unknown plan, or "free" — never a checkout target) OR
    has no price entry for `currency` (no real-time FX conversion here —
    a currency is only ever priced by an explicit catalog entry above).

    `currency` defaults to CHECKOUT_CURRENCY (USD) so every pre-existing
    single-argument call site is unaffected."""
    return PLAN_PRICES.get(plan.lower(), {}).get(currency.upper())


def supported_currencies(plan: str) -> list[str]:
    """Currencies `plan` is actually priced in, e.g. ["USD"] today."""
    return sorted(PLAN_PRICES.get(plan.lower(), {}))


def currency_candidates(plan: str) -> list[str]:
    """Every currency `plan` is priced in, CHECKOUT_CURRENCY (USD) first.

    The priority order a currency-resolution search should try: USD first
    so behaviour is unchanged for every currency-unrestricted or USD-capable
    provider, then every other priced currency in a stable (alphabetical)
    order. See payment_router.resolve_default_currency(), the only caller.
    """
    currencies = supported_currencies(plan)
    if CHECKOUT_CURRENCY in currencies:
        return [CHECKOUT_CURRENCY] + [c for c in currencies if c != CHECKOUT_CURRENCY]
    return currencies


def purchasable_plans() -> list[str]:
    """Plans a checkout may target, in ascending USD price order — the
    order the frontend's plan-selection UI should render them in."""
    return sorted(PLAN_PRICES_USD, key=lambda p: PLAN_PRICES_USD[p])
