"""
TCU (TRINITY Compute Units) — server-side accounting only.

Rules:
- TCU is ALWAYS calculated server-side from real execution data.
- The client NEVER sends, sets, or influences TCU values.
- 1 TCU = $0.01 USD (1 cent) at the base exchange rate.
- The exchange rate is server-controlled and may include a platform margin.
"""
from __future__ import annotations

# 1 TCU = $0.01 USD — this rate is set server-side and never disclosed to clients
# as an adjustable parameter. Any margin goes here.
TCU_EXCHANGE_RATE: float = 100.0


def calculate_tcu(cost_usd: float) -> float:
    """Convert actual USD cost to TCU. Always called with server-derived cost_usd."""
    if cost_usd < 0:
        raise ValueError("cost_usd cannot be negative")
    return round(cost_usd * TCU_EXCHANGE_RATE, 4)


def estimate_cost_usd(
    cost_per_1m_input_usd: float,
    cost_per_1m_output_usd: float,
    estimated_input_tokens: int = 2_000,
    max_output_tokens: int = 4_096,
) -> float:
    """Conservative pre-flight cost estimate used for limit enforcement.

    Assumes the full max_output_tokens will be consumed (worst case).
    Used only for pre-request budget checks — never reported to clients as actual cost.
    """
    if estimated_input_tokens < 0 or max_output_tokens < 0:
        raise ValueError("token counts cannot be negative")
    input_cost = (estimated_input_tokens / 1_000_000) * cost_per_1m_input_usd
    output_cost = (max_output_tokens / 1_000_000) * cost_per_1m_output_usd
    return round(input_cost + output_cost, 8)


def calculate_actual_cost_usd(
    cost_per_1m_input_usd: float,
    cost_per_1m_output_usd: float,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
    cost_per_1m_cached_input_usd: float | None = None,
) -> float:
    """Calculate exact cost from real token counts returned by the provider.

    This is the authoritative cost — always server-derived, never client-supplied.

    Cached input (prompt caching)
    -----------------------------
    Providers report `prompt_tokens` as the FULL prompt and, separately, how
    much of it was served from their prompt cache. The cached portion is
    billed at a reduced rate — half on OpenAI, a tenth on Mistral — so
    charging the whole prompt at the standard rate over-bills every cache
    hit. Measured 2026-09-12 against the live APIs with the real System
    Policy as the prefix: gpt-4o-mini 1.86x, xai-grok 5.41x, and
    mistral-small — the primary for capability="agent" — 7.02x.

    That matters beyond the invoice: TCU is derived from this number and TCU
    is what a plan's quota is spent in, so an over-stated cost consumes a
    user's window faster than their real usage justifies.

    Both new parameters are optional and default to the previous behaviour
    exactly. When a provider reports no cached tokens, or the catalog has no
    verified cached price for the model, the arithmetic is unchanged — which
    is the honest default, since a cached price that has not been confirmed
    against the provider must never be assumed.
    """
    if input_tokens < 0 or output_tokens < 0 or cached_input_tokens < 0:
        raise ValueError("token counts cannot be negative")
    if cached_input_tokens > input_tokens:
        # Defensive: a provider reporting more cached than total prompt tokens
        # is incoherent. Clamping keeps billing conservative (never negative
        # uncached tokens) rather than trusting the larger number.
        cached_input_tokens = input_tokens

    if cached_input_tokens and cost_per_1m_cached_input_usd is not None:
        uncached = input_tokens - cached_input_tokens
        input_cost = (
            (uncached / 1_000_000) * cost_per_1m_input_usd
            + (cached_input_tokens / 1_000_000) * cost_per_1m_cached_input_usd
        )
    else:
        input_cost = (input_tokens / 1_000_000) * cost_per_1m_input_usd

    output_cost = (output_tokens / 1_000_000) * cost_per_1m_output_usd
    return round(input_cost + output_cost, 8)
