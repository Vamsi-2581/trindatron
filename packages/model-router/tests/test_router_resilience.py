"""Etapa 5E — ModelRouter-level resilience tests.

Covers test list items G (provider failover), H (model failover — same
mechanism, different chain entries), I (max attempts), J (backoff wired
into the router's actual retry loop, not just retry.py in isolation), K
(circuit breaker wired into the router), O (cancellation during
retry/backoff), P (provider attempt audit), and X (provider unavailable
but an alternative provider succeeds).

All tests inject a fast, no-op-delay `sleep_fn` into ModelRouter() so the
suite stays fast while still exercising the real backoff call sites (as
opposed to mocking backoff_delay away entirely, which would prove nothing
about whether the router actually invokes it).
"""
from __future__ import annotations

import asyncio

import pytest
from unittest.mock import MagicMock

from trinity_router import (
    ModelRouter,
    ProviderUnavailableError,
    RequestRejectedByProviderError,
    RoutingRequest,
    TaskType,
)
from trinity_router.circuit_breaker import CircuitState


def _make_response(content="Answer", input_tokens=100, output_tokens=50):
    msg = MagicMock(); msg.content = content; msg.tool_calls = None
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
        user_plan="start",  # 2-provider chain: groq-llama4-scout, gemini-25-flash-lite
        session_id="s1",
    )
    defaults.update(overrides)
    return RoutingRequest(**defaults)


# ── G/H/X: provider/model failover ──────────────────────────────────────────

async def test_provider_failover_on_retryable_error(monkeypatch):
    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            raise Exception("connection reset")
        return _make_response(content="from second provider")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    result = await _fast_router().complete(_request())
    assert result.content == "from second provider"
    assert result.failover_used is True
    assert len(calls) == 2
    assert calls[0] != calls[1]


async def test_alternative_provider_succeeds_when_primary_unavailable(monkeypatch):
    """X: primary provider entirely unavailable (always raises), alternative succeeds."""
    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == calls[0]:
            raise Exception("service unavailable")
        return _make_response(content="alternative provider answer")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    result = await _fast_router().complete(_request(user_plan="pro"))  # 3-provider chain
    assert result.content == "alternative provider answer"


# ── I: max attempts bounded ──────────────────────────────────────────────────

async def test_max_attempts_bounded_by_plan_chain_length(monkeypatch):
    call_count = 0

    async def always_fail(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise Exception("down")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", always_fail)
    from trinity_router.limits import get_plan_limits
    max_expected = get_plan_limits("max").max_failover_attempts + 1

    with pytest.raises(ProviderUnavailableError):
        await _fast_router().complete(_request(user_plan="max"))
    assert call_count <= max_expected
    assert call_count > 0


# ── J: backoff wired into the router's real retry loop ─────────────────────

async def test_backoff_delay_invoked_between_failover_attempts(monkeypatch):
    sleep_calls: list[float] = []

    async def _tracking_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    call_count = 0

    async def fail_then_succeed(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # A transient provider failure. (A 429 from a model on a free allowance — START's primary runs on
            # Groq's free tier — cools that source down instead: see test_free_pool.py.)
            raise Exception("service temporarily overloaded")
        return _make_response()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", fail_then_succeed)
    router = ModelRouter(sleep_fn=_tracking_sleep, backoff_base_s=0.01, backoff_cap_s=1.0)
    await router.complete(_request())

    assert len(sleep_calls) == 1, "exactly one backoff must occur between the two attempts"
    assert sleep_calls[0] > 0


async def test_no_backoff_before_the_first_attempt(monkeypatch):
    sleep_calls: list[float] = []

    async def _tracking_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    async def ok(*a, **k):
        return _make_response()
    monkeypatch.setattr("trinity_router.router.litellm.acompletion", ok)
    router = ModelRouter(sleep_fn=_tracking_sleep)

    await router.complete(_request())
    assert sleep_calls == [], "no backoff should occur before the very first attempt"


async def test_no_backoff_for_non_retryable_provider_error(monkeypatch):
    """Auth/config failures move to the next candidate immediately — no
    reason to wait, since the next provider is an independent system."""
    sleep_calls: list[float] = []

    async def _tracking_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    call_count = 0

    class AuthenticationError(Exception):
        pass

    async def auth_fail_then_succeed(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise AuthenticationError("invalid api key")
        return _make_response()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", auth_fail_then_succeed)
    router = ModelRouter(sleep_fn=_tracking_sleep)
    await router.complete(_request())
    assert sleep_calls == [], "non-retryable-provider failures must not incur a backoff delay"


# ── E/F wired end to end: non-retryable request aborts the whole chain ─────

async def test_invalid_request_aborts_chain_without_trying_other_candidates(monkeypatch):
    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        class InvalidRequestError(Exception):
            pass
        raise InvalidRequestError("malformed messages")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    with pytest.raises(RequestRejectedByProviderError):
        await _fast_router().complete(_request(user_plan="max"))  # 4-candidate chain
    assert len(calls) == 1, "a non-retryable request failure must abort immediately, not try other candidates"


async def test_content_policy_refusal_aborts_chain(monkeypatch):
    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        class ContentPolicyViolationError(Exception):
            pass
        raise ContentPolicyViolationError("refused")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    with pytest.raises(RequestRejectedByProviderError) as exc_info:
        await _fast_router().complete(_request(user_plan="max"))
    assert len(calls) == 1
    assert "sk-" not in str(exc_info.value).lower()


# ── K: circuit breaker wired into the router ────────────────────────────────

async def test_circuit_breaker_skips_a_known_bad_provider_on_next_call(monkeypatch):
    router = ModelRouter(sleep_fn=_no_delay_sleep, circuit_breaker_failure_threshold=1, circuit_breaker_cooldown_s=30.0)
    from trinity_router.providers import PROVIDER_CATALOG
    from trinity_router.selector import select_provider_chain
    chain_keys = select_provider_chain(TaskType.CHAT, "start")
    primary_key, secondary_key = chain_keys[0], chain_keys[1]
    primary_model_id = PROVIDER_CATALOG[primary_key].model_id

    calls: list[str] = []

    async def primary_always_fails_secondary_always_succeeds(*args, **kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == primary_model_id:
            raise Exception("down")
        return _make_response(content="from secondary")

    monkeypatch.setattr(
        "trinity_router.router.litellm.acompletion", primary_always_fails_secondary_always_succeeds,
    )

    # First logical call: primary fails (its breaker opens, threshold=1),
    # secondary succeeds — the call itself succeeds via normal failover.
    result = await router.complete(_request())
    assert result.content == "from secondary"
    assert router._circuit_breakers.state_of(primary_key) == CircuitState.OPEN
    assert router._circuit_breakers.state_of(secondary_key) == CircuitState.CLOSED

    # Second logical call — the primary's breaker is now OPEN, so it must
    # be skipped WITHOUT even attempting litellm.acompletion again for it.
    calls.clear()
    result2 = await router.complete(_request())
    assert result2.content == "from secondary"
    assert primary_model_id not in calls, (
        "the OPEN-circuit primary provider must not have been called at all on the second logical call"
    )


async def test_successful_call_keeps_circuit_closed(monkeypatch):
    router = ModelRouter(sleep_fn=_no_delay_sleep)
    from trinity_router.selector import select_provider_chain
    primary_key = select_provider_chain(TaskType.CHAT, "start")[0]

    async def ok(*args, **kwargs):
        return _make_response()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", ok)
    await router.complete(_request())
    assert router._circuit_breakers.state_of(primary_key) == CircuitState.CLOSED


# ── O: cancellation during retry/backoff ────────────────────────────────────

async def test_cancellation_during_backoff_propagates_cleanly(monkeypatch):
    """A real asyncio.sleep-based backoff, cancelled mid-wait, must
    propagate CancelledError out of complete() — never be swallowed, never
    silently continue to the next candidate."""
    call_count = 0

    async def always_fail(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise Exception("connection reset")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", always_fail)
    # Real asyncio.sleep this time (default sleep_fn) so there's a genuine
    # cancellation point to interrupt.
    router = ModelRouter(backoff_base_s=5.0, backoff_cap_s=30.0)

    task = asyncio.create_task(router.complete(_request(user_plan="max")))
    await asyncio.sleep(0.05)  # let the first attempt fail and enter backoff
    assert not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Only the first candidate was ever attempted before cancellation hit
    # during the backoff wait for the second.
    assert call_count == 1


async def test_cancellation_during_normal_execution_still_works(monkeypatch):
    """Baseline sanity: cancelling mid-provider-call (not mid-backoff)
    still propagates correctly — 5E must not have regressed this."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def hang(*args, **kwargs):
        started.set()
        await release.wait()
        return _make_response()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", hang)
    router = ModelRouter()
    task = asyncio.create_task(router.complete(_request()))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ── P: provider attempt audit ───────────────────────────────────────────────

async def test_each_attempt_produces_its_own_audit_event(monkeypatch, caplog):
    import logging
    call_count = 0

    async def fail_then_succeed(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("rate limited")
        return _make_response()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", fail_then_succeed)
    with caplog.at_level(logging.INFO, logger="trinity.router.audit"):
        await _fast_router().complete(_request())

    audit_lines = [r.message for r in caplog.records if "router_audit" in r.message]
    assert len(audit_lines) == 2, f"expected 2 audit events (1 failed attempt + 1 success), got {len(audit_lines)}"
    assert "attempt=1" in audit_lines[0]
    assert "status=attempt_failed" in audit_lines[0]
    assert "retryable=True" in audit_lines[0]
    assert "attempt=2" in audit_lines[1]
    assert "status=failover" in audit_lines[1]

    # Both events share the same logical_request_id — same logical call.
    import re
    ids = [re.search(r"logical_request_id=(\S+)", line).group(1) for line in audit_lines]
    assert ids[0] == ids[1]
    assert ids[0] != ""


async def test_attempt_audit_event_never_contains_raw_exception_text(monkeypatch, caplog):
    import logging
    secret = "sk-ant-super-secret-12345"

    async def leak_then_succeed(*args, **kwargs):
        if not hasattr(leak_then_succeed, "_called"):
            leak_then_succeed._called = True
            raise Exception(f"AuthenticationError: key={secret}")
        return _make_response()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", leak_then_succeed)
    with caplog.at_level(logging.INFO, logger="trinity.router.audit"):
        await _fast_router().complete(_request())

    full_log = "\n".join(r.message for r in caplog.records)
    assert secret not in full_log
