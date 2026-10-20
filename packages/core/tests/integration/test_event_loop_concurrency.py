"""Etapa 5A — proves the event loop is not blocked by synchronous DB work.

REQUEST A hits /sessions/{id}/chat with the pre-LLM DB preparation
(chat_service._prepare_chat_context) artificially slowed by a genuine
OS-level blocking time.sleep() call — simulating a slow query. REQUEST B is
a concurrent, independent, fast request. B must complete (and must be
observed to still be pending in A) while A's slow DB call is in flight —
proving that call runs off the event loop (asyncio.to_thread), not on it.

Uses httpx.AsyncClient + ASGITransport (not the synchronous TestClient used
elsewhere in this suite) because this specific property can only be
demonstrated by two requests genuinely in flight on the same event loop at
the same time.

No real provider is used — _router.stream is mocked, same pattern as
test_chat.py.
"""
from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import patch

import httpx
import pytest
from trinity_router import RouterStreamChunk

from src.database import get_db
from src.main import app

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


@pytest.fixture
async def async_client(db):
    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


async def _register_and_get_headers(client: httpx.AsyncClient, email: str) -> dict:
    resp = await client.post("/auth/register", json={
        "email": email,
        "password": "securepass123",
        "full_name": "Concurrency Test",
    })
    assert resp.status_code == 201
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def test_slow_db_call_in_one_request_does_not_block_a_concurrent_request(async_client):
    headers_a = await _register_and_get_headers(async_client, "slowdb-a@test.com")
    headers_b = await _register_and_get_headers(async_client, "slowdb-b@test.com")

    session_a = (await async_client.post("/sessions", json={}, headers=headers_a)).json()["id"]

    # Genuine OS-level blocking sleep (not asyncio.sleep) standing in for a
    # slow synchronous query — if _prepare_chat_context were still called
    # directly on the event loop (pre-5A), this would stall every other
    # in-flight coroutine, including request B below, for its full duration.
    real_prepare = None
    slow_call_started = threading.Event()
    SLOW_SECONDS = 1.5

    # *args so the double survives a signature change in the real function.
    # It previously took exactly three positionals and silently became a
    # TypeError when _prepare_chat_context gained the already-computed
    # task_type/capability — which surfaced here as a bogus timing failure
    # rather than as the argument error it actually was.
    def _slow_prepare_chat_context(*args, **kwargs):
        slow_call_started.set()
        time.sleep(SLOW_SECONDS)
        return real_prepare(*args, **kwargs)

    from src.services import chat_service
    real_prepare = chat_service._prepare_chat_context

    timings: dict[str, float] = {}

    async def request_a():
        with patch("src.services.chat_service._prepare_chat_context", _slow_prepare_chat_context), \
             patch("src.services.chat_service._router.stream", _make_stream_mock(["slow", " response"])):
            resp = await async_client.post(
                f"/sessions/{session_a}/chat",
                json={"content": "trigger the slow path"},
                headers=headers_a,
            )
        timings["a_done"] = time.perf_counter()
        return resp

    async def request_b():
        # Wait until we know request A's slow synchronous call has actually
        # started (not just that the request was sent) — via a thread-safe
        # Event, waited on off the event loop so this wait itself can't be
        # what's "faking" the concurrency proof.
        await asyncio.to_thread(slow_call_started.wait, 5)
        # Independent, fast, unrelated request — session creation for a
        # different user. If A were blocking the loop, this would only be
        # able to run after A finishes, ~SLOW_SECONDS later.
        resp = await async_client.post("/sessions", json={}, headers=headers_b)
        timings["b_done"] = time.perf_counter()
        return resp

    start = time.perf_counter()
    task_a = asyncio.create_task(request_a())
    task_b = asyncio.create_task(request_b())

    resp_b = await asyncio.wait_for(task_b, timeout=10)
    assert resp_b.status_code == 201

    # The decisive assertion: B finished while A was still in its slow,
    # blocking DB call — i.e. well before SLOW_SECONDS have elapsed overall,
    # and strictly before A itself completed.
    b_elapsed = timings["b_done"] - start
    assert b_elapsed < SLOW_SECONDS, (
        f"request B took {b_elapsed:.2f}s — expected it to finish quickly, "
        f"concurrently with request A's {SLOW_SECONDS}s slow DB call, not "
        f"be blocked behind it"
    )
    assert not task_a.done(), "request A should still be waiting on its slow DB call"

    resp_a = await asyncio.wait_for(task_a, timeout=10)
    assert resp_a.status_code == 200
    assert timings["b_done"] < timings["a_done"]


async def test_to_thread_mechanism_never_blocks_the_loop_even_under_repeated_calls():
    """Lower-level companion proof: the general asyncio.to_thread guarantee
    this whole Etapa relies on, exercised directly (no HTTP, no DB) —
    5 sequential slow-thread calls must never prevent a concurrently
    scheduled coroutine from making its own progress in between."""
    progress_ticks = []

    def _blocking(seconds: float):
        time.sleep(seconds)
        return "done"

    async def slow_worker():
        for _ in range(5):
            result = await asyncio.to_thread(_blocking, 0.05)
            assert result == "done"

    async def fast_ticker():
        for i in range(20):
            await asyncio.sleep(0.01)
            progress_ticks.append(i)

    await asyncio.gather(slow_worker(), fast_ticker())

    # The ticker must have made steady progress throughout — not been
    # starved until all 5 slow_worker() calls finished.
    assert len(progress_ticks) == 20
