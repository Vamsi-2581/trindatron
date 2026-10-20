"""A request never costs more than the budget it was authorised for.

Two ways a request exceeded its ceiling until the 2026-09-13 pre-launch audit:

1. Preflight priced every prompt at 2,000 input tokens, so a prompt carrying
   a long document passed a cost check its real input could not.
2. Every failover attempt was checked against the WHOLE ceiling while
   quality-rejected answers were billed, so a request that tripped the gate
   on every candidate could cost (max_failover_attempts + 1) ceilings.

Core holds each call to its reservation through max_cost_usd_override; these
tests prove the router actually keeps the call inside that number.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trinity_router import ModelRouter, RoutingRequest, TaskType
from trinity_router.limits import get_plan_limits
from trinity_router.providers import PROVIDER_CATALOG
from trinity_router.router import ProviderUnavailableError, _estimate_input_tokens
from trinity_router.tcu import estimate_cost_usd

REFUSAL = "I'm sorry, but I can't help with that."
PREMIUM_PROMPT = (
    "Explain step by step why this design fails, compare the trade-offs, prove the invariant, "
    "then refactor it. First analyze the traceback, then rewrite the function and class below. "
) * 30 + "\n```python\ndef f(x):\n    return x\n```\n"


async def _no_sleep(_seconds: float) -> None:
    return None


def _router() -> ModelRouter:
    return ModelRouter(sleep_fn=_no_sleep)


def _response(content: str, input_tokens: int, output_tokens: int):
    msg = MagicMock(); msg.content = content; msg.tool_calls = None
    usage = MagicMock()
    usage.prompt_tokens = input_tokens
    usage.completion_tokens = output_tokens
    usage.prompt_tokens_details = None
    choice = MagicMock(); choice.message = msg
    resp = MagicMock(); resp.choices = [choice]; resp.usage = usage
    return resp


class _Stream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _text_chunk(text: str):
    c = MagicMock()
    c.choices = [MagicMock(delta=MagicMock(content=text))]
    c.usage = None
    return c


def _usage_chunk(input_tokens: int, output_tokens: int):
    c = MagicMock()
    c.choices = [MagicMock(delta=MagicMock(content=None))]
    usage = MagicMock()
    usage.prompt_tokens = input_tokens
    usage.completion_tokens = output_tokens
    usage.prompt_tokens_details = None
    c.usage = usage
    return c


# ── 1. The prompt's real size is priced ──────────────────────────────────


def test_a_short_prompt_is_estimated_exactly_as_before():
    assert _estimate_input_tokens([{"role": "user", "content": "hi"}], None) == 2_000


def test_a_long_prompt_is_estimated_from_its_text():
    assert _estimate_input_tokens([{"role": "user", "content": "x" * 30_000}], None) == 10_000


def test_tools_count_towards_the_estimate():
    tools = [{"type": "function", "function": {"name": "t", "description": "d" * 9_000}}]
    assert _estimate_input_tokens([{"role": "user", "content": "x" * 6_000}], tools) > 2_000


def test_preflight_refuses_a_call_its_real_input_cannot_afford():
    router = _router()
    limits = get_plan_limits("max")
    opus = PROVIDER_CATALOG["claude-opus"]
    assert router._preflight(limits, opus, None, "claude-opus", input_tokens_estimate=2_000)[1] is False
    assert router._preflight(limits, opus, None, "claude-opus", input_tokens_estimate=200_000)[1] is True


async def test_a_premium_request_too_large_for_its_ceiling_is_refused_not_overbilled(monkeypatch):
    calls: list[str] = []

    async def never(*_a, **kwargs):
        calls.append(kwargs["model"])
        return _response("ok", 10, 10)

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", never)
    huge = PREMIUM_PROMPT + ("context " * 150_000)   # ~400k tokens of input
    with pytest.raises(ProviderUnavailableError):
        await _router().complete(RoutingRequest(
            messages=[{"role": "user", "content": huge}],
            task_type=TaskType.CHAT, user_plan="max", session_id="s", capability="chat",
        ))
    assert calls == []


# ── 2. Rejected answers spend the request's budget ───────────────────────


async def test_rejections_stop_once_the_budget_cannot_pay_for_another_attempt(monkeypatch):
    models: list[str] = []

    async def always_refuse(*_a, **kwargs):
        models.append(kwargs["model"])
        return _response(REFUSAL, 10_000, 5_000)

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", always_refuse)
    budget = 0.008
    result = await _router().complete(RoutingRequest(
        messages=[{"role": "user", "content": "Can you write a birthday message for my mom?"}],
        task_type=TaskType.CHAT, user_plan="pro", session_id="s", capability="chat",
        max_cost_usd_override=budget,
    ))

    assert len(models) == 1, "a second attempt could not be paid for, so it must not be made"
    assert result.cost_usd <= budget


async def test_a_streamed_request_stops_the_same_way(monkeypatch):
    models: list[str] = []

    async def always_refuse(*_a, **kwargs):
        models.append(kwargs["model"])
        return _Stream([_text_chunk(REFUSAL), _usage_chunk(10_000, 5_000)])

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", always_refuse)
    budget = 0.008
    final = None
    async for chunk in _router().stream(RoutingRequest(
        messages=[{"role": "user", "content": "Can you write a birthday message for my mom?"}],
        task_type=TaskType.CHAT, user_plan="pro", session_id="s", capability="chat",
        max_cost_usd_override=budget,
    )):
        if chunk.is_final:
            final = chunk

    assert final is not None and len(models) == 1
    assert final.cost_usd <= budget


async def test_rejections_still_recover_while_the_budget_allows(monkeypatch):
    models: list[str] = []

    async def refuse_then_answer(*_a, **kwargs):
        models.append(kwargs["model"])
        if len(models) == 1:
            return _response(REFUSAL, 500, 50)
        return _response("Happy birthday, mom! Thank you for everything.", 500, 50)

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", refuse_then_answer)
    result = await _router().complete(RoutingRequest(
        messages=[{"role": "user", "content": "Can you write a birthday message for my mom?"}],
        task_type=TaskType.CHAT, user_plan="pro", session_id="s", capability="chat",
    ))
    assert len(models) == 2
    assert "birthday" in result.content.lower()


# ── 3. Each plan's cap buys the premium tier it sells ────────────────────


@pytest.mark.parametrize("plan", ["max", "team"])
def test_the_plan_cap_affords_opus_for_an_ordinary_premium_request(plan):
    limits = get_plan_limits(plan)
    opus = PROVIDER_CATALOG["claude-opus"]
    estimate = estimate_cost_usd(
        opus.cost_per_1m_input_usd, opus.cost_per_1m_output_usd,
        estimated_input_tokens=_estimate_input_tokens([{"role": "user", "content": PREMIUM_PROMPT}], None),
        max_output_tokens=limits.max_output_tokens_per_request,
    )
    assert estimate <= limits.max_estimated_cost_per_request_usd
