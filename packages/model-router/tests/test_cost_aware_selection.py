"""Etapa 5E — tests for providers.select_cheapest_capable().

Covers test list items U (cost-aware candidate selection), V (cheapest
capable candidate wins), and W (incapable cheap candidate is rejected).

This is a pure function, not wired into the production chain-selection
path yet (see providers.py's own docstring on select_cheapest_capable) —
these tests validate the LOGIC in isolation.
"""
from __future__ import annotations

import pytest

from trinity_router.providers import PROVIDER_CATALOG, ProviderModel, select_cheapest_capable


def _model(**overrides) -> ProviderModel:
    defaults = dict(
        provider="test", model_id="test-model", context_window=100_000,
        cost_per_1m_input_usd=1.0, cost_per_1m_output_usd=1.0,
        supports_tools=True, supports_streaming=True, quality_class="standard",
    )
    defaults.update(overrides)
    return ProviderModel(**defaults)


# ── V: cheapest capable candidate wins ──────────────────────────────────────

def test_cheapest_capable_candidate_wins_among_equally_capable_options():
    expensive = _model(model_id="expensive", cost_per_1m_input_usd=10.0, cost_per_1m_output_usd=10.0)
    cheap_capable = _model(model_id="cheap-capable", cost_per_1m_input_usd=0.5, cost_per_1m_output_usd=0.5)
    result = select_cheapest_capable([expensive, cheap_capable])
    assert result is cheap_capable


def test_does_not_always_pick_the_absolute_cheapest_when_it_lacks_capability():
    """W's mirror image, stated positively: given a genuinely cheaper but
    INCAPABLE option and a slightly pricier CAPABLE one, the capable one
    must win — 'cheapest capable', not 'cheapest'."""
    ultra_cheap_no_tools = _model(model_id="ultra-cheap", cost_per_1m_input_usd=0.01,
                                    cost_per_1m_output_usd=0.01, supports_tools=False)
    capable = _model(model_id="capable", cost_per_1m_input_usd=1.0, cost_per_1m_output_usd=1.0, supports_tools=True)
    result = select_cheapest_capable([ultra_cheap_no_tools, capable], requires_tools=True)
    assert result is capable


# ── W: incapable cheap candidate is rejected outright ───────────────────────

def test_incapable_candidate_excluded_by_context_window():
    too_small = _model(model_id="small-ctx", context_window=4_000, cost_per_1m_input_usd=0.01, cost_per_1m_output_usd=0.01)
    big_enough = _model(model_id="big-ctx", context_window=200_000, cost_per_1m_input_usd=2.0, cost_per_1m_output_usd=2.0)
    result = select_cheapest_capable([too_small, big_enough], min_context_window=100_000)
    assert result is big_enough


def test_incapable_candidate_excluded_by_missing_tool_support():
    no_tools = _model(model_id="no-tools", supports_tools=False, cost_per_1m_input_usd=0.01, cost_per_1m_output_usd=0.01)
    with_tools = _model(model_id="with-tools", supports_tools=True, cost_per_1m_input_usd=5.0, cost_per_1m_output_usd=5.0)
    result = select_cheapest_capable([no_tools, with_tools], requires_tools=True)
    assert result is with_tools


def test_incapable_candidate_excluded_by_missing_streaming_support():
    no_stream = _model(model_id="no-stream", supports_streaming=False, cost_per_1m_input_usd=0.01, cost_per_1m_output_usd=0.01)
    with_stream = _model(model_id="with-stream", supports_streaming=True, cost_per_1m_input_usd=5.0, cost_per_1m_output_usd=5.0)
    result = select_cheapest_capable([no_stream, with_stream], requires_streaming=True)
    assert result is with_stream


def test_incapable_candidate_excluded_by_quality_class_floor():
    ultra_low = _model(model_id="ultra-low", quality_class="ultra_low", cost_per_1m_input_usd=0.01, cost_per_1m_output_usd=0.01)
    standard = _model(model_id="standard", quality_class="standard", cost_per_1m_input_usd=2.0, cost_per_1m_output_usd=2.0)
    result = select_cheapest_capable([ultra_low, standard], min_quality_class="standard")
    assert result is standard


def test_returns_none_when_no_candidate_qualifies():
    only_incapable = _model(supports_tools=False)
    assert select_cheapest_capable([only_incapable], requires_tools=True) is None


def test_empty_candidate_list_returns_none():
    assert select_cheapest_capable([]) is None


def test_unknown_quality_class_raises():
    with pytest.raises(ValueError):
        select_cheapest_capable([_model()], min_quality_class="ultra_premium_deluxe")


# ── U: cost-aware selection accounts for both input and output pricing ─────

def test_blended_cost_considers_both_input_and_output_pricing():
    """A model with cheap input but very expensive output must lose to one
    with balanced pricing, for an output-heavy estimate — proving the
    comparison isn't input-only."""
    cheap_input_expensive_output = _model(
        model_id="cheap-in-expensive-out", cost_per_1m_input_usd=0.01, cost_per_1m_output_usd=50.0,
    )
    balanced = _model(model_id="balanced", cost_per_1m_input_usd=1.0, cost_per_1m_output_usd=1.0)
    result = select_cheapest_capable(
        [cheap_input_expensive_output, balanced],
        input_tokens_estimate=100, max_output_tokens_estimate=10_000,
    )
    assert result is balanced


def test_does_not_mutate_input_candidates_list():
    candidates = [_model(model_id="a"), _model(model_id="b", cost_per_1m_input_usd=0.5, cost_per_1m_output_usd=0.5)]
    original = list(candidates)
    select_cheapest_capable(candidates)
    assert candidates == original


# ── Sanity check against the real production catalog ────────────────────────

def test_works_against_the_real_provider_catalog():
    """Not a mock-only exercise — proves the function behaves sensibly on
    the actual PROVIDER_CATALOG data, e.g. that the ultra-cheap tier
    genuinely wins when no other requirement excludes it."""
    all_models = list(PROVIDER_CATALOG.values())
    cheapest = select_cheapest_capable(all_models, requires_tools=True, requires_streaming=True)
    assert cheapest is not None
    assert cheapest.quality_class == "ultra_low"

    premium_only = select_cheapest_capable(all_models, min_quality_class="premium")
    assert premium_only is not None
    assert premium_only.quality_class == "premium"
