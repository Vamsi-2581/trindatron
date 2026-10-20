"""Etapa 5E — mocked stress test: 10, 50, and 100 concurrent logical
requests through a SHARED ModelRouter() instance (so circuit breaker state
genuinely persists/interacts across concurrent callers, the realistic
production scenario — Core's `_router = ModelRouter()` singleton).

No real providers. Verifies:
  - no retry storm (litellm call count is tightly bounded by
    logical_requests * chain_length, never more)
  - no asyncio task leak (asyncio.all_tasks() diffed before/after)
  - no double charge (each successful logical request's cost_usd/tcu
    reflects exactly the ONE successful attempt's token usage, and the sum
    across all N results matches the expected total exactly)
  - no orphan task, no event-loop blocking (the whole burst completes
    promptly under a wall-clock budget, using an injected zero-delay
    sleep_fn so backoff doesn't inflate wall time — a real blocking bug
    would still show up as many-second stalls, not sub-second completion)
  - circuit breaker opens for a genuinely-always-failing candidate and
    stays open across the burst
  - failover works under real concurrency
"""
from __future__ import annotations

import asyncio
import time

import pytest
from unittest.mock import MagicMock

from trinity_router import ModelRouter, ProviderUnavailableError, RoutingRequest, TaskType
from trinity_router.circuit_breaker import CircuitState
from trinity_router.providers import PROVIDER_CATALOG
from trinity_router.selector import select_provider_chain
from trinity_router.tcu import TCU_EXCHANGE_RATE


def _make_response(input_tokens=100, output_tokens=50):
    msg = MagicMock(); msg.content = "ok"; msg.tool_calls = None
    usage = MagicMock()
    usage.prompt_tokens = input_tokens
    usage.completion_tokens = output_tokens
    usage.prompt_tokens_details = None
    choice = MagicMock(); choice.message = msg
    resp = MagicMock(); resp.choices = [choice]; resp.usage = usage
    return resp


async def _no_delay_sleep(seconds: float) -> None:
    return None


def _pending_tasks() -> set:
    current = asyncio.current_task()
    return {t for t in asyncio.all_tasks() if t is not current and not t.done()}


def _request(session_id: str) -> RoutingRequest:
    return RoutingRequest(
        messages=[{"role": "user", "content": "Hi"}],
        task_type=TaskType.CHAT,
        user_plan="start",  # 2-provider chain: groq-llama4-scout, gemini-25-flash-lite
        session_id=session_id,
    )


@pytest.mark.parametrize("n", [10, 50, 100])
async def test_concurrent_burst_no_retry_storm_no_leak_no_double_charge(n, monkeypatch):
    chain_keys = select_provider_chain(TaskType.CHAT, "start")
    primary_key = chain_keys[0]
    primary_model_id = PROVIDER_CATALOG[primary_key].model_id
    # Adaptive Intelligence Router V2: the real chain is built from GLOBAL
    # candidate discovery (providers.get_candidate_models()), not from
    # select_provider_chain()'s 2-entry curated list — so the actual
    # runner-up for this trivial "Hi" / START-plan request is whichever
    # catalog model is cheapest EXCLUDING the primary (groq-llama4-scout),
    # truncated to START's own max_failover_attempts+1 = 2 total
    # candidates. gpt-4o-mini ($0.15/$0.60) is that cheapest runner-up
    # across the whole catalog — NOT select_provider_chain()'s own
    # curated secondary (gemini-25-flash-lite, $0.30/$2.50) anymore.
    secondary_key = "gpt-4o-mini"

    call_count = {"n": 0}

    async def primary_fails_secondary_succeeds(*args, **kwargs):
        call_count["n"] += 1
        if kwargs["model"] == primary_model_id:
            raise Exception("connection reset")
        return _make_response(input_tokens=100, output_tokens=50)

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", primary_fails_secondary_succeeds)
    router = ModelRouter(sleep_fn=_no_delay_sleep)

    before = _pending_tasks()
    start = time.perf_counter()
    results = await asyncio.gather(*(
        router.complete(_request(f"stress-{i}")) for i in range(n)
    ))
    elapsed = time.perf_counter() - start
    after = _pending_tasks()

    # No event-loop blocking: with a zero-delay sleep_fn, N concurrent
    # 2-attempt calls against pure in-memory mocks must complete quickly
    # regardless of N — a real blocking bug would stall this for seconds.
    assert elapsed < 5.0, f"burst of {n} took {elapsed:.2f}s — suspiciously slow, possible blocking"

    # No retry storm: this shared router's circuit breaker (default
    # threshold=3) trips on the always-failing primary after its first 3
    # failures across the whole burst — every request after that skips
    # the primary entirely (is_available() returns False) and goes
    # straight to the secondary. Total litellm calls is therefore n
    # (secondary, once per request) + 3 (primary, until its breaker
    # opens) — NOT 2n, which is itself a demonstration that the breaker
    # actively suppresses a retry storm against a known-bad candidate
    # rather than hammering it once per logical request forever.
    assert call_count["n"] == n + 3, (
        f"expected exactly {n + 3} litellm calls (n secondary + 3 primary "
        f"before its breaker opens), got {call_count['n']}"
    )

    # No orphan/leaked asyncio Task.
    leaked = after - before
    assert not leaked, f"leaked pending task(s) after the burst: {leaked}"

    # No double charge: every result reflects exactly the secondary
    # provider's ONE successful attempt (100 input / 50 output tokens),
    # never an accumulation across the failed primary attempt too.
    assert len(results) == n
    for r in results:
        assert r.input_tokens == 100
        assert r.output_tokens == 50
        assert r.failover_used is True
        assert r.cost_usd == pytest.approx(
            PROVIDER_CATALOG[secondary_key].cost_per_1m_input_usd * 100 / 1_000_000
            + PROVIDER_CATALOG[secondary_key].cost_per_1m_output_usd * 50 / 1_000_000,
            rel=1e-6,
        )
        assert r.tcu == pytest.approx(r.cost_usd * TCU_EXCHANGE_RATE, rel=1e-6)

    total_cost = sum(r.cost_usd for r in results)
    expected_total = n * results[0].cost_usd
    assert total_cost == pytest.approx(expected_total, rel=1e-6), (
        "sum of per-request costs drifted from n * per-request cost — "
        "suggests cost leaking in from a failed attempt somewhere"
    )


@pytest.mark.parametrize("n", [10, 50, 100])
async def test_concurrent_burst_circuit_breaker_and_failover_hold(n, monkeypatch):
    """A provider that ALWAYS fails must have its breaker open partway
    through the burst and stay open — later logical requests in the same
    burst must skip it (no litellm call for it) yet still succeed via the
    healthy secondary. Proves circuit breaker + failover both hold under
    genuine concurrency, not just sequential calls."""
    chain_keys = select_provider_chain(TaskType.CHAT, "start")
    primary_key, secondary_key = chain_keys[0], chain_keys[1]
    primary_model_id = PROVIDER_CATALOG[primary_key].model_id

    primary_calls = {"n": 0}

    async def primary_always_fails_secondary_always_succeeds(*args, **kwargs):
        if kwargs["model"] == primary_model_id:
            primary_calls["n"] += 1
            raise Exception("down")
        return _make_response()

    monkeypatch.setattr(
        "trinity_router.router.litellm.acompletion", primary_always_fails_secondary_always_succeeds,
    )
    router = ModelRouter(sleep_fn=_no_delay_sleep, circuit_breaker_failure_threshold=3, circuit_breaker_cooldown_s=60.0)

    before = _pending_tasks()
    results = await asyncio.gather(*(
        router.complete(_request(f"cb-stress-{i}")) for i in range(n)
    ))
    after = _pending_tasks()

    assert len(results) == n
    assert all(r.failover_used for r in results), "every logical request must have failed over to the secondary"
    assert router._circuit_breakers.state_of(primary_key) == CircuitState.OPEN
    # Once OPEN (after `circuit_breaker_failure_threshold` failures), later
    # concurrently-racing requests may still have already been mid-flight
    # against the primary when it tripped — so primary_calls can exceed
    # the threshold slightly under real concurrency, but must be nowhere
    # near N (proving the breaker genuinely suppressed most attempts).
    assert primary_calls["n"] < n, (
        f"circuit breaker did not meaningfully reduce calls to the failing "
        f"provider: {primary_calls['n']} calls out of {n} requests"
    )
    assert not (after - before), "leaked pending task(s) after the circuit-breaker burst"


async def test_concurrent_burst_all_candidates_exhausted_raises_cleanly_no_leak():
    """100 concurrent logical requests where EVERY candidate fails for
    every request — every call must cleanly raise ProviderUnavailableError,
    with no leaked task and no partial/inconsistent state."""
    async def always_fail(*args, **kwargs):
        raise Exception("all down")

    router = ModelRouter(sleep_fn=_no_delay_sleep)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("trinity_router.router.litellm.acompletion", always_fail)

        before = _pending_tasks()

        async def attempt(i):
            try:
                await router.complete(_request(f"exhaust-{i}"))
                return "unexpected_success"
            except ProviderUnavailableError:
                return "expected_failure"

        results = await asyncio.gather(*(attempt(i) for i in range(100)))
        after = _pending_tasks()

    assert results.count("expected_failure") == 100
    assert not (after - before)
