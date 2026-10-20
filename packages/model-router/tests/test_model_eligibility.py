"""Adaptive Intelligence Router — providers.get_eligible_models() tests.

Mission requirement 16's mandatory list: low quality rejected for complex
task, medium selected when sufficient, expensive elite not selected when
unnecessary, cheapest capable wins, context-too-large model ignored,
tools-required model without tools rejected. (disabled provider / circuit-
open / plan cost limit / failover / no-eligible-provider are covered at
the router level in test_dynamic_routing_decisions.py — those depend on
disabled_provider_keys/circuit breaker/PlanLimits, not this pure filter.)
"""
from __future__ import annotations

from trinity_router.providers import ProviderModel, get_eligible_models, rank_by_cost, select_cheapest_capable


def _model(**overrides) -> ProviderModel:
    defaults = dict(
        provider="test", model_id="test-model", context_window=100_000,
        cost_per_1m_input_usd=1.0, cost_per_1m_output_usd=1.0,
        supports_tools=True, supports_streaming=True, quality_class="standard",
    )
    defaults.update(overrides)
    return ProviderModel(**defaults)


# ── Quality floor ────────────────────────────────────────────────────────────

def test_low_quality_rejected_for_complex_task():
    low = _model(model_id="low", quality_class="low", cost_per_1m_input_usd=0.01, cost_per_1m_output_usd=0.01)
    standard = _model(model_id="standard", quality_class="standard", cost_per_1m_input_usd=2.0, cost_per_1m_output_usd=2.0)
    eligible = get_eligible_models([low, standard], min_quality_class="standard")
    assert eligible == [standard]


def test_medium_selected_when_sufficient():
    medium = _model(model_id="medium", quality_class="standard", cost_per_1m_input_usd=2.0, cost_per_1m_output_usd=2.0)
    eligible = get_eligible_models([medium], min_quality_class="standard")
    assert eligible == [medium]


def test_expensive_elite_not_selected_when_unnecessary():
    """The mission's own worked example (section 6): given A(low, cheap,
    rejected), B(medium, rejected), C(high/standard, eligible),
    D(premium, eligible) for a complexity requiring 'standard' — C must
    win the cost race, D must never be picked just because it's better."""
    a = _model(model_id="A", quality_class="ultra_low", cost_per_1m_input_usd=0.001, cost_per_1m_output_usd=0.001)
    b = _model(model_id="B", quality_class="low", cost_per_1m_input_usd=0.004, cost_per_1m_output_usd=0.004)
    c = _model(model_id="C", quality_class="standard", cost_per_1m_input_usd=0.010, cost_per_1m_output_usd=0.010)
    d = _model(model_id="D", quality_class="premium", cost_per_1m_input_usd=0.050, cost_per_1m_output_usd=0.050)
    result = select_cheapest_capable([a, b, c, d], min_quality_class="standard")
    assert result is c, f"expected C (cheapest capable), got {result.model_id if result else None}"


# ── Cheapest capable wins ────────────────────────────────────────────────────

def test_cheapest_capable_wins_among_eligible():
    expensive = _model(model_id="expensive", cost_per_1m_input_usd=10.0, cost_per_1m_output_usd=10.0)
    cheap = _model(model_id="cheap", cost_per_1m_input_usd=0.5, cost_per_1m_output_usd=0.5)
    eligible = get_eligible_models([expensive, cheap])
    ranked = rank_by_cost(eligible)
    assert ranked[0] is cheap
    assert ranked[1] is expensive


# ── Context window ───────────────────────────────────────────────────────────

def test_context_too_large_model_ignored():
    small_ctx = _model(model_id="small", context_window=4_000)
    big_ctx = _model(model_id="big", context_window=200_000)
    eligible = get_eligible_models([small_ctx, big_ctx], min_context_window=100_000)
    assert eligible == [big_ctx]


# ── Tools ─────────────────────────────────────────────────────────────────────

def test_tools_required_model_without_tools_rejected():
    no_tools = _model(model_id="no-tools", supports_tools=False)
    with_tools = _model(model_id="with-tools", supports_tools=True)
    eligible = get_eligible_models([no_tools, with_tools], requires_tools=True)
    assert eligible == [with_tools]


# ── Order preservation / composition with rank_by_cost ──────────────────────

def test_get_eligible_models_preserves_input_order():
    a = _model(model_id="a", priority=1)
    b = _model(model_id="b", priority=2)
    c = _model(model_id="c", priority=3)
    eligible = get_eligible_models([c, a, b])
    assert eligible == [c, a, b]  # unchanged order — callers who want cost order call rank_by_cost


def test_rank_by_cost_uses_priority_as_tiebreak():
    a = _model(model_id="a", priority=5, cost_per_1m_input_usd=1.0, cost_per_1m_output_usd=1.0)
    b = _model(model_id="b", priority=1, cost_per_1m_input_usd=1.0, cost_per_1m_output_usd=1.0)  # same cost, lower priority
    ranked = rank_by_cost([a, b])
    assert ranked == [b, a]


def test_empty_eligible_list_returns_empty():
    assert get_eligible_models([], requires_tools=True) == []


def test_get_eligible_models_never_mutates_input():
    candidates = [_model(model_id="a"), _model(model_id="b", supports_tools=False)]
    original = list(candidates)
    get_eligible_models(candidates, requires_tools=True)
    assert candidates == original
