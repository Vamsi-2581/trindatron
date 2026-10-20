"""Etapa 5E — streaming-specific resilience tests.

Covers test list items M (streaming failure before first useful content —
failover IS allowed) and N (streaming failure after useful content — NO
failover, no duplicated/restarted content, error propagates as-is).
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from trinity_router import ModelRouter, RoutingRequest, TaskType


async def _no_delay_sleep(seconds: float) -> None:
    return None


def _request(**overrides) -> RoutingRequest:
    defaults = dict(
        messages=[{"role": "user", "content": "Hi"}],
        task_type=TaskType.CHAT,
        user_plan="start",
        session_id="s1",
    )
    defaults.update(overrides)
    return RoutingRequest(**defaults)


class _FakeStream:
    """Mimics litellm's CustomStreamWrapper: async-iterable, raises
    mid-iteration if `raise_after` chunks have already been yielded."""
    def __init__(self, chunks, raise_after: int | None = None, usage=None):
        self._chunks = chunks
        self._raise_after = raise_after
        self.usage = usage

    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        # `raise_after=N` means: yield exactly N chunks, then raise — for
        # N == 0 that means raising before any chunk is yielded at all.
        for i, chunk in enumerate(self._chunks):
            if self._raise_after is not None and i == self._raise_after:
                raise ConnectionError("stream dropped mid-flight")
            yield chunk
        if self._raise_after is not None and self._raise_after == len(self._chunks):
            raise ConnectionError("stream dropped mid-flight")


def _text_chunk(text: str):
    c = MagicMock()
    c.choices = [MagicMock(delta=MagicMock(content=text))]
    c.usage = None
    return c


# ── M: failure before first useful content — failover allowed ──────────────

async def test_stream_failover_when_initial_call_fails(monkeypatch):
    """The failure happens before litellm.acompletion(stream=True) even
    returns a stream object — this is the pre-first-content window, and
    failover to the next candidate is expected (identical mechanism to
    complete())."""
    call_count = 0

    async def fail_first_then_succeed(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise Exception("connection refused")
        return _FakeStream([_text_chunk("Hello "), _text_chunk("world")])

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", fail_first_then_succeed)
    router = ModelRouter(sleep_fn=_no_delay_sleep)

    results = [c async for c in router.stream(_request())]
    text = "".join(c.text for c in results if not c.is_final)
    assert text == "Hello world"
    final = next(c for c in results if c.is_final)
    assert final.failover_used is True
    assert call_count == 2


# ── N: failure after useful content — no failover, no duplication ──────────

async def test_stream_failure_after_first_chunk_propagates_without_retry(monkeypatch):
    """Once real content has started streaming, a mid-stream failure must
    NOT trigger a second litellm.acompletion call (no restart on another
    provider) and must NOT be swallowed — it propagates to the caller
    exactly as raised."""
    call_count = 0

    async def only_call(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _FakeStream(
            [_text_chunk("Hello "), _text_chunk("wor")],  # 2 real chunks...
            raise_after=2,  # ...then the 3rd __anext__() raises
        )

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", only_call)
    router = ModelRouter(sleep_fn=_no_delay_sleep)

    received_text = []
    with pytest.raises(ConnectionError):
        async for chunk in router.stream(_request()):
            if not chunk.is_final:
                received_text.append(chunk.text)

    # Exactly the content that was genuinely produced before the failure —
    # nothing lost, nothing duplicated, nothing from a "restarted" second
    # attempt. It arrives as ONE chunk rather than two because the response
    # was still inside stream()'s quality hold when the error hit (see
    # _STREAM_QUALITY_HOLD_CHARS): the held text is flushed on the way out.
    # Chunk boundaries were never part of this contract; the content is.
    assert "".join(received_text) == "Hello wor"
    assert call_count == 1, "a mid-stream failure must never trigger a second provider attempt"


async def test_stream_failure_after_content_does_not_duplicate_on_retry_attempt(monkeypatch):
    """Companion proof, framed the other way: even though the chain has a
    SECOND eligible candidate available, it must never be reached once
    streaming has begun — litellm.acompletion is called exactly once, for
    the first (and only) candidate, regardless of chain length."""
    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        return _FakeStream([_text_chunk("partial")], raise_after=1)

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    router = ModelRouter(sleep_fn=_no_delay_sleep)

    with pytest.raises(ConnectionError):
        async for _ in router.stream(_request(user_plan="max")):  # 4-candidate chain
            pass

    assert len(calls) == 1, f"expected exactly 1 provider attempt, got {len(calls)}: {calls}"


async def test_stream_no_content_yielded_before_failure_still_no_retry(monkeypatch):
    """Edge case: the stream object is returned successfully (so
    litellm.acompletion itself didn't raise — we're past the failover
    window) but raises on its very FIRST __anext__() before any text chunk
    was produced. Per this router's design (failover is only attempted
    around the initial litellm.acompletion() call, never around consuming
    the returned stream), this must still propagate without retry."""
    calls = []

    async def side_effect(*args, **kwargs):
        calls.append(kwargs["model"])
        return _FakeStream([_text_chunk("never seen")], raise_after=0)

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", side_effect)
    router = ModelRouter(sleep_fn=_no_delay_sleep)

    received = []
    with pytest.raises(ConnectionError):
        async for chunk in router.stream(_request(user_plan="max")):
            if not chunk.is_final:
                received.append(chunk.text)

    assert received == []
    assert len(calls) == 1
