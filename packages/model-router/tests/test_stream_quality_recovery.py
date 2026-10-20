"""Quality Recovery on the STREAMING path — the one the chat endpoint uses.

Reported 2026-09-10: an ordinary request came back as
"I'm sorry, but I can't help with that." complete() had recovered from that
since the Quality Recovery Layer landed, but chat streams, and stream() only
logged it — the refusal reached the user verbatim.

stream() now holds a short prefix (_STREAM_QUALITY_HOLD_CHARS) before
releasing the first chunk. While nothing has been handed to the caller the
response is still recoverable, so a short bogus refusal is swapped for a real
answer with no duplication. The moment content ships, the old invariant takes
over again and no failover happens for the rest of that response.
"""
from __future__ import annotations

import logging

from unittest.mock import MagicMock

import pytest

from trinity_router import ModelRouter, RoutingRequest, TaskType
from trinity_router.circuit_breaker import CircuitState
from trinity_router.limits import get_plan_limits
from trinity_router.router import _STREAM_QUALITY_HOLD_CHARS


def _chunk(text: str):
    c = MagicMock()
    c.choices = [MagicMock(delta=MagicMock(content=text))]
    c.usage = None
    return c


def _final_chunk():
    c = MagicMock()
    c.choices = [MagicMock(delta=MagicMock(content=None))]
    usage = MagicMock()
    usage.prompt_tokens = 20
    usage.completion_tokens = 15
    usage.prompt_tokens_details = None
    c.usage = usage
    return c


class _Stream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


async def _no_delay_sleep(seconds: float) -> None:
    return None


def _router(**kwargs) -> ModelRouter:
    kwargs.setdefault("sleep_fn", _no_delay_sleep)
    return ModelRouter(**kwargs)


def _request(**overrides) -> RoutingRequest:
    defaults = dict(
        messages=[{"role": "user", "content": "Escreva uma frase curta de parabéns para minha mãe."}],
        task_type=TaskType.CHAT,
        user_plan="start",
        session_id="s1",
    )
    defaults.update(overrides)
    return RoutingRequest(**defaults)


async def _collect(router: ModelRouter, request: RoutingRequest):
    """Return (text, final_chunk) from a full stream run."""
    text, final = [], None
    async for chunk in router.stream(request):
        if chunk.is_final:
            final = chunk
        elif chunk.text:
            text.append(chunk.text)
    return "".join(text), final


REFUSAL = "I'm sorry, but I can't help with that."
GOOD = "Parabéns, mãe! Que seu dia seja tão especial quanto você."


# ── The reported bug ────────────────────────────────────────────────────────


async def test_bogus_refusal_never_reaches_the_user(monkeypatch):
    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        return _Stream([_chunk(REFUSAL if len(calls) == 1 else GOOD), _final_chunk()])

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    text, final = await _collect(_router(), _request())

    assert text == GOOD
    assert REFUSAL not in text, "the refusal must not appear even partially"
    assert len(calls) == 2
    assert "quality_gate:unexpected_refusal" in (final.failover_reason or "")


async def test_refusal_split_across_chunks_is_still_caught(monkeypatch):
    """Providers stream token by token — the gate must judge the assembled
    text, not whatever happened to arrive in the first chunk."""
    calls = []
    pieces = ["I'm ", "sorry, ", "but I ", "can't ", "help ", "with that."]

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            return _Stream([_chunk(p) for p in pieces] + [_final_chunk()])
        return _Stream([_chunk(GOOD), _final_chunk()])

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    text, _ = await _collect(_router(), _request())

    assert text == GOOD
    assert len(calls) == 2


async def test_empty_stream_triggers_recovery(monkeypatch):
    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            return _Stream([_final_chunk()])  # no text at all
        return _Stream([_chunk(GOOD), _final_chunk()])

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    text, final = await _collect(_router(), _request())

    assert text == GOOD
    assert "empty_response" in (final.failover_reason or "")


async def test_language_mismatch_triggers_recovery(monkeypatch):
    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            return _Stream([_chunk("The capital of Brazil is Brasilia."), _final_chunk()])
        return _Stream([_chunk("A capital do Brasil é Brasília."), _final_chunk()])

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    text, _ = await _collect(_router(), _request(
        messages=[{"role": "user", "content": "Qual é a capital do Brasil, você pode me dizer?"}],
    ))

    assert text == "A capital do Brasil é Brasília."


# ── What must NOT change ────────────────────────────────────────────────────


async def test_normal_answer_costs_exactly_one_call(monkeypatch):
    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        return _Stream([_chunk(GOOD), _final_chunk()])

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    text, final = await _collect(_router(), _request())

    assert text == GOOD
    assert len(calls) == 1
    assert final.failover_used is False


async def test_legitimate_refusal_is_respected(monkeypatch):
    """A refusal to a genuinely sensitive request is an answer, not a defect."""
    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        return _Stream([_chunk(REFUSAL), _final_chunk()])

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    text, _ = await _collect(_router(), _request(
        messages=[{"role": "user", "content": "How do I build a pipe bomb?"}],
    ))

    assert text == REFUSAL
    assert len(calls) == 1, "a legitimate refusal must never trigger recovery"


async def test_short_apologetic_but_helpful_answer_is_not_a_refusal(monkeypatch):
    """The riskiest case for the hold: a SHORT answer that opens with an
    apology is fully inside the window, so it really is judged — and it must
    still pass. Being sorry is not refusing."""
    calls = []
    answer = (
        "Desculpe, não tenho essa data exata aqui, mas posso mostrar "
        "onde verificá-la na fonte oficial."
    )
    assert len(answer) < _STREAM_QUALITY_HOLD_CHARS, "must be inside the hold to be judged"

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        return _Stream([_chunk(answer), _final_chunk()])

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    text, _ = await _collect(_router(), _request())

    assert text == answer
    assert len(calls) == 1, "an apology is not a refusal — no recovery may fire"


async def test_ordinary_short_answers_are_never_recovered(monkeypatch):
    """Sweep of everyday replies that all sit inside the hold. Any recovery
    here would be a false positive that costs a second provider call."""
    ordinary = [
        "Olá! Estou bem, obrigado. Como posso ajudar?",
        "A capital do Brasil é Brasília.",
        "Sim.",
        "1, 2, 3, 5, 8, 13.",
        "print('ola mundo')",
        "- Item um\n- Item dois\n- Item tres",
        "O resultado é 42.",
    ]
    for answer in ordinary:
        calls = []

        async def side_effect(*args, **kwargs):
            calls.append(kwargs["model"])
            return _Stream([_chunk(answer), _final_chunk()])

        monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
        text, _ = await _collect(_router(), _request(
            messages=[{"role": "user", "content": "Me responda em português, por favor."}],
        ))
        assert text == answer, f"answer was altered: {answer!r}"
        assert len(calls) == 1, f"false-positive recovery on: {answer!r}"


async def test_long_answer_streams_incrementally(monkeypatch):
    """The hold must not turn streaming into a single blocking response:
    once past the threshold, chunks flow through one at a time."""
    calls = []
    tail = ["parte %d. " % i for i in range(40)]

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        return _Stream([_chunk("x" * _STREAM_QUALITY_HOLD_CHARS)]
                       + [_chunk(t) for t in tail] + [_final_chunk()])

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    received = []
    async for chunk in _router().stream(_request()):
        if not chunk.is_final and chunk.text:
            received.append(chunk.text)

    assert len(received) > 10, "long responses must keep arriving in pieces"
    assert len(calls) == 1


async def test_recovery_never_trips_a_healthy_breaker(monkeypatch):
    router = _router()
    messages = [{"role": "user", "content": "Escreva uma frase curta de parabéns para minha mãe."}]
    decision = router._build_chain_decision(
        TaskType.CHAT, "start", get_plan_limits("start"), None,
        messages=messages, tools=None, requested_max_output_tokens=None,
    )
    primary = decision.chain[0][0]

    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        return _Stream([_chunk(REFUSAL if len(calls) == 1 else GOOD), _final_chunk()])

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    await _collect(router, _request(messages=messages))

    assert router._circuit_breakers.state_of(primary) == CircuitState.CLOSED


async def test_recovery_is_bounded_by_the_plan(monkeypatch):
    """An always-refusing chain must not loop or grow a second retry budget:
    one attempt per candidate, then fail open with what was obtained."""
    calls = []

    async def always_refuses(*args, **kwargs):
        calls.append(kwargs["model"])
        return _Stream([_chunk(REFUSAL), _final_chunk()])

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", always_refuses)
    text, _ = await _collect(_router(), _request(user_plan="max"))

    assert text == REFUSAL, "the last candidate's answer is returned, not an outage"
    assert len(calls) == len(set(calls)), "each candidate is attempted at most once"
    assert len(calls) <= 4


async def test_no_failover_once_content_has_shipped(monkeypatch):
    """The original invariant, restated: past the hold, a bad response is
    only flagged — never retried."""
    calls = []
    long_refusal = "x" * _STREAM_QUALITY_HOLD_CHARS + " I'm sorry, but I can't help with that."

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        return _Stream([_chunk(long_refusal), _final_chunk()])

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    text, _ = await _collect(_router(), _request(user_plan="max"))

    assert text == long_refusal
    assert len(calls) == 1, "content already shipped — recovery must not be attempted"


async def test_recovery_writes_an_audit_event(monkeypatch, caplog):
    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        return _Stream([_chunk(REFUSAL if len(calls) == 1 else GOOD), _final_chunk()])

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    with caplog.at_level(logging.INFO, logger="trinity.router.audit"):
        await _collect(_router(), _request())

    lines = [r.message for r in caplog.records if "router_audit" in r.message]
    assert any("status=quality_recovery" in l for l in lines)
    assert any("failure_class='quality_gate'" in l for l in lines)
