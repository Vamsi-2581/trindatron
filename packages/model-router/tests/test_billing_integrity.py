"""TCU has to keep matching what the providers actually bill.

Every plan ceiling in TRINDATRON is expressed in TCU, and a ceiling only bounds
spend if every billed token reaches TCU. Two paths used to leak (found in the
2026-09-13 economic audit):

1. Quality recovery. When the quality gate rejected an answer, the router
   moved to the next candidate and billed only the answer it returned. The
   provider had still charged for the rejected one, so a request that tripped
   the gate on every candidate cost up to (max_failover_attempts + 1) times
   what it charged.
2. Long-context pricing. xAI bills every token of a request at 2x once the
   prompt reaches 200k tokens; the catalog priced it at the base rate.

These tests pin both, on the non-streaming and the streaming path.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trinity_router import ModelRouter, RoutingRequest, TaskType
from trinity_router.providers import PROVIDER_CATALOG
from trinity_router.router import _priced_cost_usd
from trinity_router.tcu import calculate_actual_cost_usd, calculate_tcu

BY_MODEL_ID = {cfg.model_id: cfg for cfg in PROVIDER_CATALOG.values()}
REFUSAL = "I'm sorry, but I can't help with that."


def _response(content: str, input_tokens: int = 1_000, output_tokens: int = 200):
    msg = MagicMock(); msg.content = content; msg.tool_calls = None
    usage = MagicMock()
    usage.prompt_tokens = input_tokens
    usage.completion_tokens = output_tokens
    usage.prompt_tokens_details = None
    choice = MagicMock(); choice.message = msg
    resp = MagicMock(); resp.choices = [choice]; resp.usage = usage
    return resp


async def _no_sleep(_seconds: float) -> None:
    return None


def _router() -> ModelRouter:
    return ModelRouter(sleep_fn=_no_sleep)


def _base_cost(model_id: str, input_tokens: int, output_tokens: int) -> float:
    cfg = BY_MODEL_ID[model_id]
    return calculate_actual_cost_usd(
        cfg.cost_per_1m_input_usd, cfg.cost_per_1m_output_usd, input_tokens, output_tokens,
    )


# ── 1. A rejected answer is still paid for ───────────────────────────────


async def test_a_quality_rejected_answer_is_billed_into_the_request(monkeypatch):
    models: list[str] = []

    async def fake_completion(*_args, **kwargs):
        models.append(kwargs["model"])
        if len(models) == 1:
            return _response(REFUSAL, 1_000, 200)
        return _response("Sure, here is a birthday message for your mom!", 1_000, 200)

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", fake_completion)
    result = await _router().complete(RoutingRequest(
        messages=[{"role": "user", "content": "Can you write a birthday message for my mom?"}],
        task_type=TaskType.CHAT, user_plan="start", session_id="s1",
    ))

    assert len(models) == 2
    expected = _base_cost(models[0], 1_000, 200) + _base_cost(models[1], 1_000, 200)
    assert result.cost_usd == pytest.approx(expected)
    assert result.tcu == pytest.approx(calculate_tcu(expected))


async def test_returning_the_last_rejected_answer_bills_every_rejection_once(monkeypatch):
    """When no candidate passes, the router returns the best rejected answer.
    Its own cost must not be counted twice, and the others must not vanish."""
    models: list[str] = []

    async def always_refuse(*_args, **kwargs):
        models.append(kwargs["model"])
        return _response(REFUSAL, 800, 100)

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", always_refuse)
    result = await _router().complete(RoutingRequest(
        messages=[{"role": "user", "content": "Can you write a birthday message for my mom?"}],
        task_type=TaskType.CHAT, user_plan="pro", session_id="s1",
    ))

    assert len(models) >= 2
    expected = sum(_base_cost(m, 800, 100) for m in models)
    assert result.cost_usd == pytest.approx(expected)


async def test_a_passing_first_answer_costs_exactly_one_call(monkeypatch):
    async def fake_completion(*_args, **kwargs):
        return _response("Here you go.", 500, 50)

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", fake_completion)
    result = await _router().complete(RoutingRequest(
        messages=[{"role": "user", "content": "Say hello."}],
        task_type=TaskType.CHAT, user_plan="start", session_id="s1",
    ))
    assert result.cost_usd == pytest.approx(_base_cost(result.model, 500, 50))


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


async def test_a_quality_rejected_stream_is_billed_into_the_final_chunk(monkeypatch):
    models: list[str] = []

    async def fake_stream(*_args, **kwargs):
        models.append(kwargs["model"])
        if len(models) == 1:
            return _Stream([_text_chunk(REFUSAL), _usage_chunk(300, 40)])
        return _Stream([_text_chunk("Parabéns, mãe! Te amo muito."), _usage_chunk(300, 40)])

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", fake_stream)
    final = None
    async for chunk in _router().stream(RoutingRequest(
        messages=[{"role": "user", "content": "Escreva uma frase curta de parabéns para minha mãe."}],
        task_type=TaskType.CHAT, user_plan="start", session_id="s1",
    )):
        if chunk.is_final:
            final = chunk

    assert final is not None and len(models) == 2
    expected = _base_cost(models[0], 300, 40) + _base_cost(models[1], 300, 40)
    assert final.cost_usd == pytest.approx(expected)
    assert final.tcu == pytest.approx(calculate_tcu(expected))


# ── 2. Long-context and cached pricing ───────────────────────────────────


def test_xai_bills_every_token_at_twice_the_rate_from_200k_prompt_tokens():
    cfg = PROVIDER_CATALOG["xai-grok"]
    below = _priced_cost_usd(cfg, 199_999, 1_000, 0)
    at = _priced_cost_usd(cfg, 200_000, 1_000, 0)
    assert below == pytest.approx((199_999 * 1.25 + 1_000 * 2.50) / 1e6)
    assert at == pytest.approx(2 * (200_000 * 1.25 + 1_000 * 2.50) / 1e6)


def test_the_long_context_tier_also_doubles_cached_input():
    cfg = PROVIDER_CATALOG["xai-grok"]
    cost = _priced_cost_usd(cfg, 250_000, 0, 200_000)
    assert cost == pytest.approx(2 * (50_000 * 1.25 + 200_000 * 0.20) / 1e6)


@pytest.mark.parametrize("key", [k for k, m in PROVIDER_CATALOG.items() if m.long_context_threshold_tokens is None])
def test_models_without_a_tier_price_linearly_at_any_length(key):
    cfg = PROVIDER_CATALOG[key]
    small = _priced_cost_usd(cfg, 1_000, 100, 0)
    large = _priced_cost_usd(cfg, 100_000, 10_000, 0)
    assert large == pytest.approx(small * 100, rel=1e-6)


def test_a_tier_is_never_a_discount():
    for key, cfg in PROVIDER_CATALOG.items():
        assert cfg.long_context_price_multiplier >= 1.0, key
