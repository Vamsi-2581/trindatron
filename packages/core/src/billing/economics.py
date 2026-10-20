"""The business model, as executable code.

Plan prices and usage ceilings are two halves of one decision, and until now
they were set independently: the ceilings assumed compute was nearly free
and the prices assumed the ceilings would never be reached. Modelled
properly, every paid plan could be pushed into a loss by a user staying
entirely within the rules.

This module is where that stops being guesswork. It holds the cost inputs,
derives what a plan can afford to spend, and is imported by both the
entitlement table (which must not exceed what it says) and the tests that
fail the build if someone raises a ceiling without re-running the maths.

Nothing here is invented:
  1 TCU = $0.01                trinity_router.tcu.TCU_EXCHANGE_RATE
  plan prices                  src.billing.pricing.PLAN_PRICES
  per-model token costs        trinity_router.providers.PROVIDER_CATALOG
  payment fees                 the published Stripe card rate below

Everything else is arithmetic over those four.
"""
from __future__ import annotations

from dataclasses import dataclass

from trinity_router.tcu import TCU_EXCHANGE_RATE

# ── Cost inputs ──────────────────────────────────────────────────────────

USD_PER_TCU: float = 1.0 / TCU_EXCHANGE_RATE   # 1 TCU = $0.01
WEEKS_PER_MONTH: float = 52 / 12               # 4.333…

# Stripe's standard card rate for online payments. The only non-AI cost
# large enough to matter per user: at the START price it eats 6% of the
# ticket, almost all of it the fixed $0.30.
STRIPE_PERCENT_FEE: float = 0.029
STRIPE_FIXED_FEE_USD: float = 0.30

# Share of NET revenue a plan may spend on providers when a user consumes
# every last unit the plan allows. The remainder covers infrastructure and
# is the gross margin at the worst case the product permits.
#
# 0.55 is chosen so the floor holds where it is hardest: the worst case is
# not an average user, it is a user who went looking for the ceiling. A
# plan that only breaks even there has no room for the fixed costs that do
# not scale with a subscription (hosting, storage, the Postgres instance).
MAX_AI_COST_SHARE_OF_NET: float = 0.55

# What a plan must still return at the ceiling, after providers.
MIN_GROSS_MARGIN_AT_CEILING: float = 1.0 - MAX_AI_COST_SHARE_OF_NET


# ── Revenue ──────────────────────────────────────────────────────────────


def payment_fee_usd(price_usd: float) -> float:
    """What the processor takes from one monthly charge."""
    return price_usd * STRIPE_PERCENT_FEE + STRIPE_FIXED_FEE_USD


def net_revenue_usd(price_usd: float) -> float:
    """What actually arrives, per user per month."""
    return price_usd - payment_fee_usd(price_usd)


# ── The ceiling a price can afford ───────────────────────────────────────


def affordable_weekly_tcu(price_usd: float, *, share: float = MAX_AI_COST_SHARE_OF_NET) -> float:
    """The largest weekly ceiling a monthly price can honour without loss.

    The WEEKLY window is the one that caps monthly spend: however many
    5-hour or daily windows a user opens, they cannot exceed the weekly
    allowance, so monthly worst case = weekly x weeks-per-month. That makes
    this the number every other window has to fit underneath.
    """
    monthly_ai_budget = net_revenue_usd(price_usd) * share
    return (monthly_ai_budget / WEEKS_PER_MONTH) / USD_PER_TCU


def monthly_ai_cost_at_ceiling_usd(weekly_tcu: float) -> float:
    return weekly_tcu * USD_PER_TCU * WEEKS_PER_MONTH


def gross_margin_at_ceiling(price_usd: float, weekly_tcu: float) -> float:
    """Margin on a user who consumed every unit the plan allows.

    Negative means the product loses money on a customer who did nothing
    wrong — the single outcome this module exists to make impossible.
    """
    net = net_revenue_usd(price_usd)
    return (net - monthly_ai_cost_at_ceiling_usd(weekly_tcu)) / net


# ── What a ceiling buys in real work ─────────────────────────────────────
#
# A ceiling in TCU means nothing until it is expressed in tasks. These
# shapes are the ones the product actually runs; token counts are
# deliberate, conservative estimates of each, and the cost of running one
# is computed from the real catalogue rather than assumed.


@dataclass(frozen=True)
class Workload:
    """One unit of user-visible work."""
    name: str
    calls: int            # provider calls it takes (an agent loop makes many)
    input_tokens: int     # per call, including the context carried along
    output_tokens: int    # per call

    def cost_usd(self, cost_per_1m_in: float, cost_per_1m_out: float) -> float:
        per_call = (
            self.input_tokens * cost_per_1m_in + self.output_tokens * cost_per_1m_out
        ) / 1_000_000
        return per_call * self.calls

    def tcu(self, cost_per_1m_in: float, cost_per_1m_out: float) -> float:
        return self.cost_usd(cost_per_1m_in, cost_per_1m_out) / USD_PER_TCU


# A short answer, little context.
CHAT_TURN = Workload("chat turn", calls=1, input_tokens=2_000, output_tokens=500)
# A long answer in a conversation that has been going a while.
LONG_CHAT_TURN = Workload("long chat turn", calls=1, input_tokens=12_000, output_tokens=1_500)
# A ReAct loop: several calls, each carrying the growing transcript plus
# tool results. The expensive shape, and the one users reach for.
AGENT_TASK = Workload("agent task", calls=8, input_tokens=10_000, output_tokens=800)
# Browser work carries page text back into context on every step.
BROWSER_TASK = Workload("browser task", calls=10, input_tokens=14_000, output_tokens=600)


def workload_tcu(workload: Workload, tier: str) -> float:
    """Cost of one workload at a given quality tier, from the live catalogue.

    Imported lazily so this module stays importable without the router
    package resolved (the migration environment, for instance).
    """
    from trinity_router.providers import PROVIDER_CATALOG

    candidates = [c for c in PROVIDER_CATALOG.values() if c.quality_class == tier]
    if not candidates:
        raise ValueError(f"no model in the catalogue at tier {tier!r}")
    # The router picks the cheapest capable candidate, so that is what a
    # tier costs in practice — not the dearest one at that tier.
    cheapest = min(
        candidates,
        key=lambda c: c.cost_per_1m_input_usd + c.cost_per_1m_output_usd,
    )
    return workload.tcu(cheapest.cost_per_1m_input_usd, cheapest.cost_per_1m_output_usd)
