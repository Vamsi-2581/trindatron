"""Etapa 5F — tests for RoutingRequest.forced_provider_key (the minimal
escape hatch Continuity Mode uses to pin execution to one pre-selected
economic model, bypassing select_provider_chain() without touching it).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trinity_router import ModelRouter, ProviderUnavailableError, RoutingRequest, TaskType
from trinity_router.providers import PROVIDER_CATALOG


def _make_response(content="Answer"):
    msg = MagicMock(); msg.content = content; msg.tool_calls = None
    usage = MagicMock()
    usage.prompt_tokens = 100
    usage.completion_tokens = 50
    usage.prompt_tokens_details = None
    choice = MagicMock(); choice.message = msg
    resp = MagicMock(); resp.choices = [choice]; resp.usage = usage
    return resp


async def _no_delay_sleep(seconds: float) -> None:
    return None


def _request(**overrides) -> RoutingRequest:
    defaults = dict(
        messages=[{"role": "user", "content": "Hi"}],
        task_type=TaskType.CHAT, user_plan="start", session_id="s1",
    )
    defaults.update(overrides)
    return RoutingRequest(**defaults)


def test_default_forced_provider_key_is_none():
    req = _request()
    assert req.forced_provider_key is None


async def test_normal_chain_selection_unaffected_when_not_forced():
    """forced_provider_key=None (the default) must produce the EXACT same
    chain as before this etapa — select_provider_chain() still decides."""
    from trinity_router.limits import get_plan_limits
    from trinity_router.selector import select_provider_chain

    router = ModelRouter(sleep_fn=_no_delay_sleep)
    plan_limits = get_plan_limits("start")
    expected = select_provider_chain(TaskType.CHAT, "start")[: plan_limits.max_failover_attempts + 1]

    chain = router._build_chain(TaskType.CHAT, "start", plan_limits)
    assert [k for k, _ in chain] == expected


async def test_forced_provider_key_pins_to_a_single_candidate(monkeypatch):
    calls = []

    async def track(*args, **kwargs):
        calls.append(kwargs["model"])
        return _make_response()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", track)
    router = ModelRouter(sleep_fn=_no_delay_sleep)

    result = await router.complete(_request(forced_provider_key="gemini-25-flash-lite"))
    assert result.content == "Answer"
    assert calls == [PROVIDER_CATALOG["gemini-25-flash-lite"].model_id]
    assert result.provider == "google"


async def test_forced_provider_key_never_fails_over_to_a_different_candidate(monkeypatch):
    """A failure on the ONE forced candidate must raise
    ProviderUnavailableError — never silently try a different (possibly
    premium) model instead."""
    async def always_fail(*args, **kwargs):
        raise Exception("down")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", always_fail)
    router = ModelRouter(sleep_fn=_no_delay_sleep)

    with pytest.raises(ProviderUnavailableError) as exc_info:
        await router.complete(_request(forced_provider_key="gemini-25-flash-lite"))
    assert exc_info.value.providers_tried == ["gemini-25-flash-lite"]


async def test_forced_provider_key_unknown_to_catalog_raises_provider_unavailable(monkeypatch):
    router = ModelRouter(sleep_fn=_no_delay_sleep)
    with pytest.raises(ProviderUnavailableError):
        await router.complete(_request(forced_provider_key="not-a-real-catalog-key"))


async def test_forced_provider_key_still_honors_circuit_breaker(monkeypatch):
    """The single forced candidate is not exempt from Etapa 5E's circuit
    breaker — if it's already OPEN, the call is rejected without even
    attempting litellm."""
    router = ModelRouter(sleep_fn=_no_delay_sleep, circuit_breaker_failure_threshold=1)
    router._circuit_breakers.record_failure("gemini-25-flash-lite")  # opens immediately (threshold=1)

    calls = []

    async def track(*args, **kwargs):
        calls.append(kwargs["model"])
        return _make_response()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", track)
    with pytest.raises(ProviderUnavailableError):
        await router.complete(_request(forced_provider_key="gemini-25-flash-lite"))
    assert calls == [], "litellm must never be called for a forced key whose breaker is OPEN"


async def test_forced_provider_key_used_in_stream(monkeypatch):
    class _FakeStream:
        def __init__(self, chunks):
            self._chunks = chunks
            self.usage = None

        def __aiter__(self):
            return self._aiter()

        async def _aiter(self):
            for c in self._chunks:
                yield c

    def _text_chunk(text):
        c = MagicMock()
        c.choices = [MagicMock(delta=MagicMock(content=text))]
        c.usage = None
        return c

    calls = []

    async def track(*args, **kwargs):
        calls.append(kwargs["model"])
        return _FakeStream([_text_chunk("hi")])

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", track)
    router = ModelRouter(sleep_fn=_no_delay_sleep)

    results = [c async for c in router.stream(_request(forced_provider_key="groq-llama4-scout"))]
    assert calls == [PROVIDER_CATALOG["groq-llama4-scout"].model_id]
    final = next(c for c in results if c.is_final)
    assert final.provider == "groq"


# ── tools/tool_choice: None (not litellm.NOT_GIVEN) when absent ────────────
# Regression guard for the 2026-09-07 Mistral incident: litellm.acompletion(
# tools=litellm.NOT_GIVEN) raises "'NotGiven' object is not iterable" —
# and tool_choice=litellm.NOT_GIVEN raises "Invalid tool choice" — for
# Mistral specifically, even though every other provider already in this
# catalog (Anthropic/OpenAI/Gemini/Groq/xAI) accepted both sentinels
# identically. router.py's complete() now passes None instead, which is
# the universally-accepted "omit this" value confirmed live against all
# six providers.


async def test_complete_sends_none_not_not_given_for_absent_tools(monkeypatch):
    """When the caller supplies no tools/tool_choice at all, complete()
    must call litellm.acompletion with tools=None and tool_choice=None —
    never litellm.NOT_GIVEN, which breaks Mistral specifically."""
    import litellm

    captured = {}

    async def track(*args, **kwargs):
        captured.update(kwargs)
        return _make_response()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", track)
    router = ModelRouter(sleep_fn=_no_delay_sleep)

    await router.complete(_request(forced_provider_key="mistral-small"))

    assert captured["tools"] is None
    assert captured["tool_choice"] is None
    assert captured["tools"] is not litellm.NOT_GIVEN
    assert captured["tool_choice"] is not litellm.NOT_GIVEN


async def test_complete_passes_through_real_tools_unchanged(monkeypatch):
    """When the caller DOES supply tools/tool_choice, complete() must send
    those exact values unchanged — the None fix must not swallow or alter
    real tool definitions."""
    captured = {}

    async def track(*args, **kwargs):
        captured.update(kwargs)
        msg = MagicMock()
        msg.content = None
        msg.tool_calls = [MagicMock(function=MagicMock(name="get_weather"))]
        usage = MagicMock(prompt_tokens=10, completion_tokens=5, prompt_tokens_details=None)
        choice = MagicMock(message=msg)
        resp = MagicMock(choices=[choice], usage=usage)
        return resp

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", track)
    router = ModelRouter(sleep_fn=_no_delay_sleep)

    real_tools = [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}]
    await router.complete(_request(
        forced_provider_key="mistral-small", tools=real_tools, tool_choice="auto",
    ))

    assert captured["tools"] == real_tools
    assert captured["tool_choice"] == "auto"


async def test_complete_none_fix_does_not_break_already_working_providers(monkeypatch):
    """The None-instead-of-NOT_GIVEN fix is provider-agnostic — anthropic/
    openai/google/groq/xai, which already tolerated NOT_GIVEN just fine,
    must keep working identically now that None is sent instead."""
    async def track(*args, **kwargs):
        return _make_response()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", track)
    router = ModelRouter(sleep_fn=_no_delay_sleep)

    for key in ["claude-sonnet", "gpt-4o-mini", "gemini-25-flash-lite", "groq-llama4-scout", "xai-grok"]:
        result = await router.complete(_request(forced_provider_key=key))
        assert result.provider == PROVIDER_CATALOG[key].provider
