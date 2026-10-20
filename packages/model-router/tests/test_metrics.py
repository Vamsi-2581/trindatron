"""Etapa 5G (Observability) — deterministic tests for the in-process
metrics registry and its instrumentation of ModelRouter/CircuitBreaker.

These tests never assert on log text (that's audit.py's job, already
covered by test_router_resilience.py/test_router_streaming_resilience.py)
— they assert on the aggregatable counters/histograms a monitoring system
would actually poll.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from trinity_router import ModelRouter, ProviderUnavailableError, RoutingRequest, TaskType
from trinity_router.circuit_breaker import CircuitBreakerRegistry
from trinity_router.metrics import Counter, Histogram, MetricsRegistry, router_metrics


# ═════════════════════════════════════════════════════════════════════════
# Primitives
# ═════════════════════════════════════════════════════════════════════════

def test_counter_accumulates_per_label_set():
    c = Counter("test_counter")
    c.inc(provider="anthropic", outcome="success")
    c.inc(provider="anthropic", outcome="success")
    c.inc(provider="anthropic", outcome="failure")
    c.inc(provider="openai", outcome="success")

    snap = c.snapshot()
    assert snap[(("outcome", "success"), ("provider", "anthropic"))] == 2
    assert snap[(("outcome", "failure"), ("provider", "anthropic"))] == 1
    assert snap[(("outcome", "success"), ("provider", "openai"))] == 1


def test_counter_reset_clears_all_labels():
    c = Counter("test_counter")
    c.inc(a="1")
    c.inc(b="2")
    c.reset()
    assert c.snapshot() == {}


def test_histogram_tracks_count_sum_min_max_avg():
    h = Histogram("test_hist")
    h.observe(10.0, provider="x")
    h.observe(30.0, provider="x")
    h.observe(20.0, provider="x")

    snap = h.snapshot()
    stats = snap[(("provider", "x"),)]
    assert stats["count"] == 3
    assert stats["sum"] == pytest.approx(60.0)
    assert stats["min"] == pytest.approx(10.0)
    assert stats["max"] == pytest.approx(30.0)
    assert stats["avg"] == pytest.approx(20.0)


def test_registry_get_or_create_returns_same_instance():
    reg = MetricsRegistry()
    c1 = reg.counter("foo")
    c2 = reg.counter("foo")
    assert c1 is c2


def test_registry_snapshot_is_json_safe():
    reg = MetricsRegistry()
    reg.counter("requests_total").inc(status="200")
    reg.histogram("latency_ms").observe(42.0, path="/chat")

    snap = reg.snapshot()
    assert snap["counters"]["requests_total"] == [{"labels": {"status": "200"}, "value": 1.0}]
    assert snap["histograms"]["latency_ms"][0]["labels"] == {"path": "/chat"}
    assert snap["histograms"]["latency_ms"][0]["count"] == 1


# ═════════════════════════════════════════════════════════════════════════
# ModelRouter instrumentation
# ═════════════════════════════════════════════════════════════════════════

def _response(input_tokens=100, output_tokens=50):
    msg = MagicMock(); msg.content = "ok"; msg.tool_calls = None
    usage = MagicMock()
    usage.prompt_tokens = input_tokens
    usage.completion_tokens = output_tokens
    usage.prompt_tokens_details = None
    choice = MagicMock(); choice.message = msg
    resp = MagicMock(); resp.choices = [choice]; resp.usage = usage
    return resp


async def test_successful_complete_records_attempt_and_cost_and_tcu(monkeypatch):
    monkeypatch.setattr("trinity_router.router.litellm.acompletion", AsyncMock(return_value=_response()))
    router = ModelRouter()

    await router.complete(RoutingRequest(
        messages=[{"role": "user", "content": "hi"}], task_type=TaskType.CHAT,
        user_plan="start", session_id="s1",
    ))

    attempts = router_metrics.counter("router_provider_attempts_total").snapshot()
    assert any(dict(k).get("outcome") == "success" for k in attempts)

    cost_snap = router_metrics.histogram("router_cost_usd").snapshot()
    assert len(cost_snap) == 1
    assert next(iter(cost_snap.values()))["count"] == 1

    tcu_snap = router_metrics.histogram("router_tcu").snapshot()
    assert len(tcu_snap) == 1


async def test_failed_then_recovered_complete_records_retry_and_failover(monkeypatch):
    calls = {"n": 0}

    async def _flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("boom")
        return _response()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", AsyncMock(side_effect=_flaky))
    router = ModelRouter(backoff_base_s=0.001, backoff_cap_s=0.001)

    result = await router.complete(RoutingRequest(
        messages=[{"role": "user", "content": "hi"}], task_type=TaskType.CHAT,
        user_plan="start", session_id="s1",
    ))
    assert result.failover_used is True

    retries = router_metrics.counter("router_retries_total").snapshot()
    assert sum(retries.values()) >= 1

    failovers = router_metrics.counter("router_failover_total").snapshot()
    assert sum(failovers.values()) == 1

    attempts = router_metrics.counter("router_provider_attempts_total").snapshot()
    outcomes = [dict(k).get("outcome") for k in attempts]
    assert "failure" in outcomes
    assert "success" in outcomes


async def test_all_providers_failing_records_chain_exhausted(monkeypatch):
    monkeypatch.setattr(
        "trinity_router.router.litellm.acompletion",
        AsyncMock(side_effect=ConnectionError("down")),
    )
    router = ModelRouter(backoff_base_s=0.001, backoff_cap_s=0.001)

    with pytest.raises(ProviderUnavailableError):
        await router.complete(RoutingRequest(
            messages=[{"role": "user", "content": "hi"}], task_type=TaskType.CHAT,
            user_plan="start", session_id="s1",
        ))

    exhausted = router_metrics.counter("router_chain_exhausted_total").snapshot()
    assert sum(exhausted.values()) == 1


async def test_open_circuit_breaker_records_skipped_outcome(monkeypatch):
    monkeypatch.setattr(
        "trinity_router.router.litellm.acompletion",
        AsyncMock(side_effect=ConnectionError("down")),
    )
    router = ModelRouter(circuit_breaker_failure_threshold=1, backoff_base_s=0.001, backoff_cap_s=0.001)

    request = RoutingRequest(
        messages=[{"role": "user", "content": "hi"}], task_type=TaskType.CHAT,
        user_plan="start", session_id="s1",
    )
    with pytest.raises(ProviderUnavailableError):
        await router.complete(request)

    router_metrics.reset()  # isolate the second call's own metrics

    with pytest.raises(ProviderUnavailableError):
        await router.complete(request)

    attempts = router_metrics.counter("router_provider_attempts_total").snapshot()
    outcomes = [dict(k).get("outcome") for k in attempts]
    assert "skipped_circuit_open" in outcomes


def test_circuit_breaker_open_transition_is_recorded():
    registry = CircuitBreakerRegistry(failure_threshold=1, cooldown_s=30.0)
    registry.record_failure("test-key")

    transitions = router_metrics.counter("router_circuit_breaker_transitions_total").snapshot()
    assert transitions[(("key", "test-key"), ("to_state", "open"))] == 1


def test_circuit_breaker_closed_transition_is_recorded():
    registry = CircuitBreakerRegistry(failure_threshold=1, cooldown_s=30.0)
    registry.record_failure("test-key")  # -> open
    registry.record_success("test-key")  # -> closed (was not closed)

    transitions = router_metrics.counter("router_circuit_breaker_transitions_total").snapshot()
    assert transitions[(("key", "test-key"), ("to_state", "closed"))] == 1
