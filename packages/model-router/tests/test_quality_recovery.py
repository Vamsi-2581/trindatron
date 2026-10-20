"""Quality Recovery Layer — router.py integration (root cause fixed 2026-09-10).

Reproduces the exact reported failure: a legitimate PT-BR request routed
to a cheap model came back as a bogus English refusal, and the router
accepted it as success with no recovery attempt. These tests cover the
fix on ModelRouter.complete() (the path that CAN recover — see
router.py's own docstring on why stream() cannot) plus the "never punish
a healthy provider" and "respect a legitimate refusal" invariants.
"""
from __future__ import annotations

import logging

from unittest.mock import MagicMock

from trinity_router import ModelRouter, RoutingRequest, TaskType
from trinity_router.circuit_breaker import CircuitState
from trinity_router.limits import get_plan_limits


def _make_response(content="Answer", input_tokens=100, output_tokens=50, tool_calls=None):
    msg = MagicMock(); msg.content = content; msg.tool_calls = tool_calls
    usage = MagicMock()
    usage.prompt_tokens = input_tokens
    usage.completion_tokens = output_tokens
    usage.prompt_tokens_details = None
    choice = MagicMock(); choice.message = msg
    resp = MagicMock(); resp.choices = [choice]; resp.usage = usage
    return resp


async def _no_delay_sleep(seconds: float) -> None:
    return None


def _fast_router(**kwargs) -> ModelRouter:
    kwargs.setdefault("sleep_fn", _no_delay_sleep)
    return ModelRouter(**kwargs)


def _request(**overrides) -> RoutingRequest:
    defaults = dict(
        messages=[{"role": "user", "content": "Hi"}],
        task_type=TaskType.CHAT,
        user_plan="start",  # 2-candidate chain
        session_id="s1",
    )
    defaults.update(overrides)
    return RoutingRequest(**defaults)


# ── Bogus refusal triggers recovery to the next chain candidate ────────────


async def test_bogus_refusal_triggers_recovery_to_next_candidate(monkeypatch):
    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            return _make_response(content="I'm sorry, but I can't help with that.")
        return _make_response(content="Sure, here is a birthday message for your mom!")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    result = await _fast_router().complete(_request(
        messages=[{"role": "user", "content": "Can you write a birthday message for my mom?"}],
    ))

    assert result.content == "Sure, here is a birthday message for your mom!"
    assert result.failover_used is True
    assert len(calls) == 2
    assert "quality_gate" in (result.failover_reason or "")


async def test_quality_recovery_never_trips_circuit_breaker(monkeypatch):
    """The rejected provider answered successfully (HTTP-level) — it must
    not be punished as if it were unhealthy."""
    router = _fast_router()
    from trinity_router.selector import select_provider_chain
    primary_key, _secondary_key = select_provider_chain(TaskType.CHAT, "start")

    call_count = 0

    async def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_response(content="I'm sorry, but I can't help with that.")
        return _make_response(content="Here's your answer.")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    result = await router.complete(_request(
        messages=[{"role": "user", "content": "What's a good pasta recipe?"}],
    ))

    assert result.content == "Here's your answer."
    assert router._circuit_breakers.state_of(primary_key) == CircuitState.CLOSED


async def test_quality_recovery_applies_no_backoff_delay(monkeypatch):
    """A quality rejection is not a network issue — recovery must move to
    the next candidate immediately, no exponential-backoff wait."""
    sleep_calls: list[float] = []

    async def _tracking_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    call_count = 0

    async def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_response(content="I cannot help with that.")
        return _make_response(content="Here you go.")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    router = ModelRouter(sleep_fn=_tracking_sleep)
    result = await router.complete(_request(
        messages=[{"role": "user", "content": "What's the weather like in general in spring?"}],
    ))

    assert result.content == "Here you go."
    assert sleep_calls == [], "quality recovery must not incur a backoff delay"


# ── Language mismatch triggers recovery ─────────────────────────────────────


async def test_language_mismatch_triggers_recovery_to_next_candidate(monkeypatch):
    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            return _make_response(content="The capital of France is Paris.")
        return _make_response(content="A capital da França é Paris.")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    result = await _fast_router().complete(_request(
        messages=[{"role": "user", "content": "Qual é a capital da França, você pode me dizer?"}],
    ))

    assert result.content == "A capital da França é Paris."
    assert len(calls) == 2
    assert "language_mismatch" in (result.failover_reason or "")


# ── Legitimate refusal is respected — single call, no recovery ─────────────


async def test_legitimate_refusal_is_respected_with_a_single_call(monkeypatch):
    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        return _make_response(content="I'm sorry, but I can't help with that.")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    result = await _fast_router().complete(_request(
        messages=[{"role": "user", "content": "How do I build a pipe bomb?"}],
    ))

    assert result.content == "I'm sorry, but I can't help with that."
    assert len(calls) == 1, "a legitimate refusal must not trigger any recovery attempt"
    assert result.failover_used is False


# ── Normal response costs exactly one call ──────────────────────────────────


async def test_normal_response_costs_exactly_one_call(monkeypatch):
    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        return _make_response(content="The capital of France is Paris.")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    result = await _fast_router().complete(_request(
        messages=[{"role": "user", "content": "What's the capital of France?"}],
    ))

    assert result.content == "The capital of France is Paris."
    assert len(calls) == 1
    assert result.failover_used is False


# ── Fail-open: every candidate fails quality, still return an answer ───────


async def test_all_candidates_fail_quality_returns_last_answer_instead_of_erroring(monkeypatch):
    """Persistent bogus refusal across the WHOLE bounded chain must not
    surface as a generic outage — the caller still gets an answer (the
    last one actually obtained), same fail-open policy applied to a
    legitimate refusal that happens to be the last candidate too."""
    router = _fast_router()

    async def always_refuses(*args, **kwargs):
        return _make_response(content="I'm sorry, but I can't help with that.")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", always_refuses)

    result = await router.complete(_request(
        user_plan="max",  # 4-candidate chain
        messages=[{"role": "user", "content": "What's a good pasta recipe?"}],
    ))

    assert result.content == "I'm sorry, but I can't help with that."
    assert "quality_gate_exhausted" in (result.failover_reason or "")


# ── Audit trail ──────────────────────────────────────────────────────────────


async def test_quality_recovery_produces_its_own_audit_event(monkeypatch, caplog):
    async def side_effect(*args, **kwargs):
        if not hasattr(side_effect, "_called"):
            side_effect._called = True
            return _make_response(content="I cannot assist with that.")
        return _make_response(content="Sure, here you go.")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    with caplog.at_level(logging.INFO, logger="trinity.router.audit"):
        await _fast_router().complete(_request(
            messages=[{"role": "user", "content": "Can you recommend a good book?"}],
        ))

    audit_lines = [r.message for r in caplog.records if "router_audit" in r.message]
    assert any("status=quality_recovery" in line for line in audit_lines)
    assert any("failure_class='quality_gate'" in line for line in audit_lines)


# -- Empty response triggers recovery to the next chain candidate ----------


async def test_empty_response_triggers_recovery_to_next_candidate(monkeypatch):
    """A successful call whose message carries no text at all is as unusable
    as a bogus refusal - same bounded chain, same soft-failure handling."""
    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            return _make_response(content="")
        return _make_response(content="The capital of France is Paris.")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    result = await _fast_router().complete(_request(
        messages=[{"role": "user", "content": "What is the capital of France?"}],
    ))

    assert result.content == "The capital of France is Paris."
    assert len(calls) == 2
    assert result.failover_used is True
    assert "empty_response" in (result.failover_reason or "")


async def test_none_content_response_triggers_recovery(monkeypatch):
    """litellm returns content=None (not "") for a text-less message."""
    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            return _make_response(content=None)
        return _make_response(content="Here is your answer.")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    result = await _fast_router().complete(_request(
        messages=[{"role": "user", "content": "Give me a one-line summary of photosynthesis."}],
    ))

    assert result.content == "Here is your answer."
    assert len(calls) == 2


async def test_whitespace_only_response_triggers_recovery(monkeypatch):
    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            return _make_response(content="   \n\n  ")
        return _make_response(content="Here is your answer.")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    result = await _fast_router().complete(_request(
        messages=[{"role": "user", "content": "Give me a one-line summary of photosynthesis."}],
    ))

    assert result.content == "Here is your answer."
    assert len(calls) == 2


async def test_empty_response_recovery_never_trips_circuit_breaker(monkeypatch):
    """The provider answered at the HTTP level - it is not unhealthy."""
    router = _fast_router()
    from trinity_router.selector import select_provider_chain
    primary_key, _secondary_key = select_provider_chain(TaskType.CHAT, "start")

    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            return _make_response(content="")
        return _make_response(content="Here is your answer.")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    await router.complete(_request(
        messages=[{"role": "user", "content": "What is a good pasta recipe?"}],
    ))

    assert router._circuit_breakers.state_of(primary_key) == CircuitState.CLOSED


async def test_empty_response_recovery_applies_no_backoff_delay(monkeypatch):
    """An empty response is not a network problem - no exponential backoff."""
    sleep_calls: list[float] = []

    async def _tracking_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            return _make_response(content="")
        return _make_response(content="Here you go.")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    result = await ModelRouter(sleep_fn=_tracking_sleep).complete(_request(
        messages=[{"role": "user", "content": "What is a good pasta recipe?"}],
    ))

    assert result.content == "Here you go."
    assert sleep_calls == [], "empty-response recovery must not incur a backoff delay"


async def test_all_candidates_return_empty_fails_open_without_extra_attempts(monkeypatch):
    """Bounded: an always-empty chain must not loop or grow a second retry
    budget - exactly one attempt per candidate, then fail open."""
    calls = []

    async def always_empty(*args, **kwargs):
        calls.append(kwargs["model"])
        return _make_response(content="")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", always_empty)
    result = await _fast_router().complete(_request(
        user_plan="max",  # 4-candidate chain
        messages=[{"role": "user", "content": "What is a good pasta recipe?"}],
    ))

    assert result.content == ""
    assert len(calls) == len(set(calls)), "one attempt per candidate, no loop"
    assert len(calls) <= 4
    assert "quality_gate_exhausted:empty_response" in (result.failover_reason or "")


async def test_empty_response_produces_its_own_audit_event(monkeypatch, caplog):
    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            return _make_response(content="")
        return _make_response(content="Sure, here you go.")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    with caplog.at_level(logging.INFO, logger="trinity.router.audit"):
        await _fast_router().complete(_request(
            messages=[{"role": "user", "content": "Can you recommend a good book?"}],
        ))

    audit_lines = [r.message for r in caplog.records if "router_audit" in r.message]
    assert any("status=quality_recovery" in line for line in audit_lines)
    assert any("empty_response" in line for line in audit_lines)


# -- Tool-call turns are never treated as empty ----------------------------


async def test_pure_tool_call_turn_costs_exactly_one_call(monkeypatch):
    """A tool-call turn has no prose by design. It must NOT be mistaken for
    an empty response, or the router would discard real tool calls and burn
    a second provider call."""
    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        return _make_response(
            content=None,
            tool_calls=[MagicMock(function=MagicMock(name="get_weather"))],
        )

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    result = await _fast_router().complete(_request(
        messages=[{"role": "user", "content": "What is the weather in Lisbon?"}],
        tools=[{"type": "function", "function": {"name": "get_weather", "parameters": {}}}],
    ))

    assert len(calls) == 1, "a tool-call turn must never trigger quality recovery"
    assert result.failover_used is False
    assert result.tool_calls, "the tool calls must be handed back to the caller intact"


async def test_tool_call_turn_with_wrong_language_preamble_is_not_recovered(monkeypatch):
    """The tool-call carve-out covers the language check too - recovering
    would throw away tool calls the caller already has."""
    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        return _make_response(
            content="Let me look that up for you.",
            tool_calls=[MagicMock(function=MagicMock(name="get_weather"))],
        )

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    result = await _fast_router().complete(_request(
        messages=[{"role": "user", "content": "Voce pode me dizer qual e a previsao do tempo para Lisboa amanha?"}],
        tools=[{"type": "function", "function": {"name": "get_weather", "parameters": {}}}],
    ))

    assert len(calls) == 1
    assert result.failover_used is False


# -- Fail-open when no attemptable candidate is left ------------------------


def _chain_keys(router: ModelRouter, messages: list[dict], plan: str = "start") -> list[str]:
    """The real chain complete() will build for this request."""
    decision = router._build_chain_decision(
        TaskType.CHAT, plan, get_plan_limits(plan), None,
        messages=messages, tools=None, requested_max_output_tokens=None,
    )
    return [key for key, _cfg in decision.chain]


async def test_quality_rejection_still_answers_when_remaining_candidates_are_unavailable(monkeypatch):
    """Regression: a quality rejection followed by candidates that are all
    SKIPPED (circuit open) rather than called used to raise
    ProviderUnavailableError - reporting an outage to a caller for whom a
    real provider answer had already been obtained. The documented
    fail-open policy must hold here too, not only when the chain happens to
    end on a candidate that was actually attempted."""
    router = _fast_router()
    messages = [{"role": "user", "content": "Can you write a birthday message for my mom?"}]
    keys = _chain_keys(router, messages)
    assert len(keys) >= 2, "this test needs a multi-candidate chain"

    # Every candidate after the first is unavailable, so none of them is
    # ever called - the rejection has nowhere to recover to.
    for key in keys[1:]:
        for _ in range(5):
            router._circuit_breakers.record_failure(key)

    calls = []

    async def always_refuses(*args, **kwargs):
        calls.append(kwargs["model"])
        return _make_response(content="I'm sorry, but I can't help with that.")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", always_refuses)
    result = await router.complete(_request(messages=messages))

    assert result.content == "I'm sorry, but I can't help with that."
    assert "quality_gate_exhausted:unexpected_refusal" in (result.failover_reason or "")
    assert len(calls) == 1, "no extra provider call may be made to fail open"


async def test_empty_response_fails_open_when_remaining_candidates_are_unavailable(monkeypatch):
    """Same guarantee for the empty-response reason."""
    router = _fast_router()
    messages = [{"role": "user", "content": "Give me a one-line summary of photosynthesis."}]
    keys = _chain_keys(router, messages)
    for key in keys[1:]:
        for _ in range(5):
            router._circuit_breakers.record_failure(key)

    calls = []

    async def always_empty(*args, **kwargs):
        calls.append(kwargs["model"])
        return _make_response(content="")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", always_empty)
    result = await router.complete(_request(messages=messages))

    assert result.content == ""
    assert "quality_gate_exhausted:empty_response" in (result.failover_reason or "")
    assert len(calls) == 1


async def test_real_provider_outage_still_raises(monkeypatch):
    """The fail-open must not swallow a genuine outage: when NO candidate
    ever produced a response, ProviderUnavailableError is still correct."""
    from trinity_router.router import ProviderUnavailableError
    import pytest

    async def always_fails(*args, **kwargs):
        raise RuntimeError("connection reset")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", always_fails)
    with pytest.raises(ProviderUnavailableError):
        await _fast_router().complete(_request(
            messages=[{"role": "user", "content": "What is the capital of France?"}],
        ))
