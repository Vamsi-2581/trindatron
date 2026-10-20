"""Etapa 5B — proves RequestControlToken release timing across every exit
path of stream_chat()/stream_agent(), calling the generators directly (no
HTTP layer) so timing/cancellation can be controlled precisely. Mirrors the
direct-generator-call pattern already used for the browser subsystem in
earlier etapas.

Covers items 8-12 of the user's test list:
  8.  slot released after the stream completes successfully
  9.  slot released after an exception during streaming
  10. slot released after asyncio.CancelledError
  11. slot released after GeneratorExit (aclose() — SSE client disconnect)
  12. slot remains held while the stream is actively running
"""
from __future__ import annotations

import asyncio

import pytest
from trinity_router import ProviderUnavailableError, RouterStreamChunk

from src.capability import Capability
from src.models.session import Session as SessionModel
from src.services import agent_service, chat_service
from src.services.request_control import ConcurrencyLimiter

pytestmark = pytest.mark.asyncio


def _make_stream_mock(texts: list[str]):
    chunks = [RouterStreamChunk(text=t) for t in texts]
    chunks.append(RouterStreamChunk(
        is_final=True, provider="anthropic", model="claude-sonnet-test",
        input_tokens=10, output_tokens=sum(len(t) for t in texts),
        cached_tokens=0, cost_usd=0.0, tcu=0.0,
    ))

    async def _stream(request):
        for chunk in chunks:
            yield chunk

    return _stream


def _make_gated_stream_mock(release_event: asyncio.Event, entered_event: asyncio.Event):
    """An async generator that yields one chunk, then blocks on release_event
    before yielding the final chunk — lets a test observe state while the
    stream is genuinely suspended mid-flight."""
    async def _stream(request):
        yield RouterStreamChunk(text="partial")
        entered_event.set()
        await release_event.wait()
        yield RouterStreamChunk(
            is_final=True, provider="anthropic", model="claude-sonnet-test",
            input_tokens=5, output_tokens=5, cached_tokens=0, cost_usd=0.0, tcu=0.0,
        )

    return _stream


@pytest.fixture
def chat_session(client, auth_headers, db) -> SessionModel:
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    return db.query(SessionModel).filter(SessionModel.id == session_id).one()


async def _drain(agen):
    async for _ in agen:
        pass


# ── 8. Released after success ───────────────────────────────────────────────

async def test_slot_released_after_stream_chat_completes_successfully(db, chat_session, monkeypatch):
    limiter = ConcurrencyLimiter(1)
    token = limiter.try_acquire()
    monkeypatch.setattr(chat_service._router, "stream", _make_stream_mock(["hello", " world"]))

    await _drain(chat_service.stream_chat(db, chat_session, "hi", request_control_token=token))

    assert limiter.active_count == 0


# ── 9. Released after an exception (internal, caught) and (unhandled) ──────

async def test_slot_released_after_provider_error_during_stream_chat(db, chat_session, monkeypatch):
    limiter = ConcurrencyLimiter(1)
    token = limiter.try_acquire()

    async def _raising_stream(request):
        raise ProviderUnavailableError("provider down")
        yield  # pragma: no cover - unreachable, makes this an async generator

    monkeypatch.setattr(chat_service._router, "stream", _raising_stream)

    events = []
    async for event in chat_service.stream_chat(db, chat_session, "hi", request_control_token=token):
        events.append(event)

    assert any("error" in e for e in events)
    assert limiter.active_count == 0


async def test_slot_released_after_unhandled_exception_before_streaming_starts(db, chat_session, monkeypatch):
    limiter = ConcurrencyLimiter(1)
    token = limiter.try_acquire()

    # *args: this double must fail for the reason the test is about (the
    # raised RuntimeError), not because its signature drifted from the real
    # function's.
    def _boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(chat_service, "_prepare_chat_context", _boom)

    with pytest.raises(RuntimeError):
        await _drain(chat_service.stream_chat(db, chat_session, "hi", request_control_token=token))

    assert limiter.active_count == 0


# ── 10. Released after asyncio.CancelledError ───────────────────────────────

async def test_slot_released_after_cancelled_error_mid_stream(db, chat_session, monkeypatch):
    limiter = ConcurrencyLimiter(1)
    token = limiter.try_acquire()

    release_event = asyncio.Event()
    entered_event = asyncio.Event()
    monkeypatch.setattr(chat_service._router, "stream", _make_gated_stream_mock(release_event, entered_event))

    agen = chat_service.stream_chat(db, chat_session, "hi", request_control_token=token)

    async def consume():
        async for _ in agen:
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered_event.wait(), timeout=5)

    assert limiter.active_count == 1  # still held — stream is genuinely suspended

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert limiter.active_count == 0


# ── 11. Released after GeneratorExit (aclose()) ─────────────────────────────

async def test_slot_released_after_generator_aclose_mid_stream(db, chat_session, monkeypatch):
    limiter = ConcurrencyLimiter(1)
    token = limiter.try_acquire()

    release_event = asyncio.Event()
    entered_event = asyncio.Event()
    monkeypatch.setattr(chat_service._router, "stream", _make_gated_stream_mock(release_event, entered_event))

    agen = chat_service.stream_chat(db, chat_session, "hi", request_control_token=token)
    first = await agen.__anext__()  # capability_status_event
    second = await agen.__anext__()  # "partial" text chunk

    # The generator is now suspended right after that yield — it has not
    # yet asked the inner mock for the next chunk (which is where
    # entered_event/release_event would come in), exactly mirroring how
    # Starlette's SSE loop holds a generator between two of its own items.
    assert limiter.active_count == 1  # still mid-stream — not released yet

    await agen.aclose()  # simulates SSE client disconnect (Starlette calls this)

    assert limiter.active_count == 0


# ── 12. Slot remains held while actively streaming (no gate, direct proof) ─

async def test_slot_held_throughout_active_streaming_then_released_on_done(db, chat_session, monkeypatch):
    limiter = ConcurrencyLimiter(1)
    token = limiter.try_acquire()

    release_event = asyncio.Event()
    entered_event = asyncio.Event()
    monkeypatch.setattr(chat_service._router, "stream", _make_gated_stream_mock(release_event, entered_event))

    agen = chat_service.stream_chat(db, chat_session, "hi", request_control_token=token)
    task = asyncio.create_task(_drain(agen))

    await asyncio.wait_for(entered_event.wait(), timeout=5)
    assert limiter.active_count == 1, "slot must still be held mid-stream, not released early"

    release_event.set()
    await asyncio.wait_for(task, timeout=5)
    assert limiter.active_count == 0, "slot must be released once the stream actually finishes"


# ── stream_agent: same finally-block guarantee, plus browser_session.close() ordering ─

async def test_slot_released_after_stream_agent_completes_via_unimplemented_capability_gate(db, chat_session):
    """Uses the UNIMPLEMENTED_CAPABILITIES early-return path (Capability.IMAGE)
    to exercise stream_agent's finally block without needing to mock the LLM
    tool loop — still proves the token is released on this exit path."""
    limiter = ConcurrencyLimiter(1)
    token = limiter.try_acquire()

    await _drain(agent_service.stream_agent(
        db, chat_session, "generate an image", capability=Capability.IMAGE, request_control_token=token,
    ))

    assert limiter.active_count == 0


async def test_slot_held_during_active_agent_loop_then_released(db, chat_session, monkeypatch):
    limiter = ConcurrencyLimiter(1)
    token = limiter.try_acquire()

    entered_event = asyncio.Event()
    release_event = asyncio.Event()

    class _FakeRoutingResult:
        model = "claude-sonnet-test"
        content = "final answer"
        tool_calls = None
        input_tokens = 5
        output_tokens = 5
        cached_tokens = 0
        cost_usd = 0.0
        tcu = 0.0

    async def _gated_complete(request):
        entered_event.set()
        await release_event.wait()
        return _FakeRoutingResult()

    monkeypatch.setattr(agent_service._router, "complete", _gated_complete)

    agen = agent_service.stream_agent(
        db, chat_session, "hello", capability=Capability.CHAT, request_control_token=token,
    )
    task = asyncio.create_task(_drain(agen))

    await asyncio.wait_for(entered_event.wait(), timeout=5)
    assert limiter.active_count == 1, "slot must still be held while the agent loop awaits the provider"

    release_event.set()
    await asyncio.wait_for(task, timeout=5)
    assert limiter.active_count == 0
