"""Etapa 5D correction — deterministic proof that with_execution_timeout()
(and, transitively, stream_agent()'s own execution) never leaves an
asyncio Task pending after any exit path: success, exception,
CancelledError, GeneratorExit/aclose, timeout, provider failure, tool
failure, browser failure.

Method: capture asyncio.all_tasks() (excluding this test's own currently-
running task) immediately before and immediately after each scenario runs
to completion. No sleep()-based polling — every scenario below fully
awaits the operation under test (including its own internal cancel+await
cleanup, which with_execution_timeout() now performs synchronously before
yielding control back) before the "after" snapshot is taken, so the
before/after sets are directly, deterministically comparable with no
timing race. Any task set difference means something was left running or
pending — the assertion fails immediately with the offending task objects
for diagnosis.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from trinity_router import ProviderUnavailableError

from src.capability import Capability
from src.models.session import Session as SessionModel
from src.services import agent_service
from src.services.agent_scheduler import InMemoryAgentScheduler, with_execution_timeout

pytestmark = pytest.mark.asyncio


class _FakeRoutingResult:
    model = "claude-sonnet-test"
    content = "final answer"
    tool_calls = None
    input_tokens = 5
    output_tokens = 5
    cached_tokens = 0
    cost_usd = 0.0
    tcu = 0.0


@pytest.fixture
def agent_session(client, auth_headers, db) -> SessionModel:
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    return db.query(SessionModel).filter(SessionModel.id == session_id).one()


def _pending_tasks() -> set:
    current = asyncio.current_task()
    return {t for t in asyncio.all_tasks() if t is not current and not t.done()}


async def _assert_no_new_pending_tasks(coro_factory):
    """Runs `coro_factory()` (an async callable that fully drives one
    scenario to completion) and asserts no Task it created is left pending
    afterward."""
    before = _pending_tasks()
    await coro_factory()
    after = _pending_tasks()
    leaked = after - before
    assert not leaked, f"leaked pending task(s) after scenario: {leaked}"


# ── 1. Success ───────────────────────────────────────────────────────────────

async def test_no_task_leak_after_success(db, agent_session, monkeypatch):
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)

    async def _complete(request):
        return _FakeRoutingResult()

    monkeypatch.setattr(agent_service._router, "complete", _complete)

    async def run():
        token = scheduler.try_acquire("user-1")
        agen = agent_service.stream_agent(
            db, agent_session, "hello", capability=Capability.CHAT, agent_scheduler_token=token,
        )
        async for _ in with_execution_timeout(agen, timeout_s=5.0):
            pass

    await _assert_no_new_pending_tasks(run)
    assert scheduler.global_active_count == 0


# ── 2. Exception (provider failure) ─────────────────────────────────────────

async def test_no_task_leak_after_provider_failure(db, agent_session, monkeypatch):
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)

    async def _failing_complete(request):
        raise ProviderUnavailableError("provider down")

    monkeypatch.setattr(agent_service._router, "complete", _failing_complete)

    async def run():
        token = scheduler.try_acquire("user-1")
        agen = agent_service.stream_agent(
            db, agent_session, "hello", capability=Capability.CHAT, agent_scheduler_token=token,
        )
        async for _ in with_execution_timeout(agen, timeout_s=5.0):
            pass

    await _assert_no_new_pending_tasks(run)
    assert scheduler.global_active_count == 0


# ── 3. Tool failure ──────────────────────────────────────────────────────────

async def test_no_task_leak_after_tool_failure(db, agent_session, monkeypatch):
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)

    class _ToolCallResult:
        model = "claude-sonnet-test"
        content = None
        input_tokens = 5
        output_tokens = 5
        cached_tokens = 0
        cost_usd = 0.0
        tcu = 0.0

        class _Call:
            id = "call_1"

            class function:
                name = "nonexistent_tool"
                arguments = "{}"

        tool_calls = [_Call()]

    call_count = {"n": 0}

    async def _complete(request):
        call_count["n"] += 1
        return _ToolCallResult() if call_count["n"] == 1 else _FakeRoutingResult()

    monkeypatch.setattr(agent_service._router, "complete", _complete)

    async def run():
        token = scheduler.try_acquire("user-1")
        agen = agent_service.stream_agent(
            db, agent_session, "use a tool", capability=Capability.CHAT, agent_scheduler_token=token,
        )
        async for _ in with_execution_timeout(agen, timeout_s=5.0):
            pass

    await _assert_no_new_pending_tasks(run)
    assert scheduler.global_active_count == 0


# ── 4. Unhandled exception before the loop even starts ─────────────────────

async def test_no_task_leak_after_unhandled_exception_before_loop_starts(db, agent_session, monkeypatch):
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)

    def _boom(db, session_id, user_content):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(agent_service, "_persist_user_message", _boom)

    async def run():
        token = scheduler.try_acquire("user-1")
        agen = agent_service.stream_agent(
            db, agent_session, "hello", capability=Capability.CHAT, agent_scheduler_token=token,
        )
        with pytest.raises(RuntimeError):
            async for _ in with_execution_timeout(agen, timeout_s=5.0):
                pass

    await _assert_no_new_pending_tasks(run)
    assert scheduler.global_active_count == 0


# ── 5. CancelledError mid-execution ─────────────────────────────────────────

def _make_gated_complete(entered: asyncio.Event, release: asyncio.Event):
    async def _complete(request):
        entered.set()
        await release.wait()
        return _FakeRoutingResult()
    return _complete


async def test_no_task_leak_after_cancelled_error(db, agent_session, monkeypatch):
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)
    entered = asyncio.Event()
    release = asyncio.Event()  # never set — the cancellation preempts it
    monkeypatch.setattr(agent_service._router, "complete", _make_gated_complete(entered, release))

    token = scheduler.try_acquire("user-1")
    agen = agent_service.stream_agent(
        db, agent_session, "hello", capability=Capability.CHAT, agent_scheduler_token=token,
    )
    wrapped = with_execution_timeout(agen, timeout_s=5.0)

    async def consume():
        async for _ in wrapped:
            pass

    before = _pending_tasks()
    task = asyncio.create_task(consume())
    await asyncio.wait_for(entered.wait(), timeout=5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # `task` itself (the consumer we created) is done now — it is excluded
    # from `before`/`after` naturally since it didn't exist in `before`
    # and IS done (filtered by _pending_tasks()'s `not t.done()`).
    after = _pending_tasks()
    leaked = after - before
    assert not leaked, f"leaked pending task(s) after CancelledError: {leaked}"
    assert scheduler.global_active_count == 0


# ── 6. GeneratorExit / aclose() mid-execution ───────────────────────────────

async def test_no_task_leak_after_generator_aclose(db, agent_session, monkeypatch):
    """Advances past 3 real yields (no concurrency needed — see
    test_agent_scheduler_streaming.py's identical technique), then calls
    aclose() directly on the with_execution_timeout()-wrapped generator."""
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)

    class _ToolCallResult:
        model = "claude-sonnet-test"
        content = None
        input_tokens = 5
        output_tokens = 5
        cached_tokens = 0
        cost_usd = 0.0
        tcu = 0.0

        class _Call:
            id = "call_1"

            class function:
                name = "nonexistent_tool"
                arguments = "{}"

        tool_calls = [_Call()]

    async def _complete(request):
        return _ToolCallResult()

    monkeypatch.setattr(agent_service._router, "complete", _complete)

    before = _pending_tasks()
    token = scheduler.try_acquire("user-1")
    agen = agent_service.stream_agent(
        db, agent_session, "hello", capability=Capability.CHAT, agent_scheduler_token=token,
    )
    wrapped = with_execution_timeout(agen, timeout_s=5.0)

    await wrapped.__anext__()  # capability_status_event
    await wrapped.__anext__()  # tool_status_event
    await wrapped.__anext__()  # tool_call event

    assert scheduler.global_active_count == 1
    await wrapped.aclose()

    after = _pending_tasks()
    leaked = after - before
    assert not leaked, f"leaked pending task(s) after aclose(): {leaked}"
    assert scheduler.global_active_count == 0


# ── 7. Timeout ───────────────────────────────────────────────────────────────

async def test_no_task_leak_after_timeout(db, agent_session, monkeypatch):
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)

    async def _never_completes(request):
        await asyncio.sleep(3600)

    monkeypatch.setattr(agent_service._router, "complete", _never_completes)

    async def run():
        token = scheduler.try_acquire("user-1")
        agen = agent_service.stream_agent(
            db, agent_session, "hello", capability=Capability.CHAT, agent_scheduler_token=token,
        )
        async for _ in with_execution_timeout(agen, timeout_s=0.2):
            pass

    await _assert_no_new_pending_tasks(run)
    assert scheduler.global_active_count == 0


# ── 8. Browser failure ───────────────────────────────────────────────────────

async def test_no_task_leak_after_browser_tool_failure(db, agent_session, monkeypatch):
    """browser_navigate raising inside the real (Playwright-boundary-mocked)
    BrowserSession — proves no task is leaked when the failure originates
    from the browser tool path specifically, not just the LLM/provider
    path exercised by the other scenarios above."""
    from trinity_agent.browser.session import BrowserSession
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)

    class _BrowserToolCallResult:
        model = "claude-sonnet-test"
        content = None
        input_tokens = 5
        output_tokens = 5
        cached_tokens = 0
        cost_usd = 0.0
        tcu = 0.0

        class _Call:
            id = "call_1"

            class function:
                name = "browser_navigate"
                arguments = '{"url": "https://example.com/"}'

        tool_calls = [_Call()]

    call_count = {"n": 0}

    async def _complete(request):
        call_count["n"] += 1
        return _BrowserToolCallResult() if call_count["n"] == 1 else _FakeRoutingResult()

    async def _raising_navigate(self, url):
        raise RuntimeError("simulated browser navigation failure")

    monkeypatch.setattr(agent_service._router, "complete", _complete)

    async def run():
        token = scheduler.try_acquire("user-1")
        agen = agent_service.stream_agent(
            db, agent_session, "go to example.com", capability=Capability.CHAT, agent_scheduler_token=token,
        )
        with patch.object(BrowserSession, "navigate", new=_raising_navigate):
            async for _ in with_execution_timeout(agen, timeout_s=5.0):
                pass

    await _assert_no_new_pending_tasks(run)
    assert scheduler.global_active_count == 0
