"""Etapa 5D — proves AgentExecutionToken release timing across every exit
path of stream_agent(), calling the generator directly (no HTTP layer) so
timing/cancellation/failure can be controlled precisely. Mirrors the
direct-generator-call pattern from test_request_control_streaming.py
(Etapa 5B) — the mechanism (thread the token into stream_agent()'s own
`finally`) is identical, this file just targets agent_scheduler_token
specifically and adds the two Agent-Scheduler-specific scenarios
(execution timeout via with_execution_timeout(), and tool-call failure).

Covers items 11-17 of the user's Etapa 5D test list:
  11. release after success
  12. release after exception
  13. release after asyncio.CancelledError
  14. release after GeneratorExit (aclose())
  15. release after timeout (via with_execution_timeout)
  16. release after provider failure
  17. release after tool failure
"""
from __future__ import annotations

import asyncio

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


async def _drain(agen):
    async for _ in agen:
        pass


# ── 11. Released after success ───────────────────────────────────────────────

async def test_slot_released_after_stream_agent_completes_successfully(db, agent_session, monkeypatch):
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)
    token = scheduler.try_acquire("user-1")

    async def _complete(request):
        return _FakeRoutingResult()

    monkeypatch.setattr(agent_service._router, "complete", _complete)

    await _drain(agent_service.stream_agent(
        db, agent_session, "hello", capability=Capability.CHAT, agent_scheduler_token=token,
    ))

    assert scheduler.global_active_count == 0


# ── 16. Released after provider failure ─────────────────────────────────────

async def test_slot_released_after_provider_failure(db, agent_session, monkeypatch):
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)
    token = scheduler.try_acquire("user-1")

    async def _failing_complete(request):
        raise ProviderUnavailableError("provider down")

    monkeypatch.setattr(agent_service._router, "complete", _failing_complete)

    events = []
    async for event in agent_service.stream_agent(
        db, agent_session, "hello", capability=Capability.CHAT, agent_scheduler_token=token,
    ):
        events.append(event)

    assert any("error" in e for e in events)
    assert scheduler.global_active_count == 0


# ── 17. Released after tool failure ─────────────────────────────────────────

async def test_slot_released_after_tool_call_failure(db, agent_session, monkeypatch):
    """The LLM requests an unknown tool name — _run_tool() returns a
    failure ToolResult (not an exception), the loop feeds that back as an
    "ERROR: ..." tool message and continues to a second iteration where it
    answers directly. Proves the scheduler slot survives a tool failure
    that does NOT crash the loop."""
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)
    token = scheduler.try_acquire("user-1")

    call_count = {"n": 0}

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
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _ToolCallResult()
        return _FakeRoutingResult()

    monkeypatch.setattr(agent_service._router, "complete", _complete)

    events = []
    async for event in agent_service.stream_agent(
        db, agent_session, "use a tool", capability=Capability.CHAT, agent_scheduler_token=token,
    ):
        events.append(event)

    assert any('"success": false' in e for e in events)
    assert scheduler.global_active_count == 0


# ── 12. Released after an unhandled exception before the loop starts ───────

async def test_slot_released_after_unhandled_exception_before_loop_starts(db, agent_session, monkeypatch):
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)
    token = scheduler.try_acquire("user-1")

    def _boom(db, session_id, user_content):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(agent_service, "_persist_user_message", _boom)

    with pytest.raises(RuntimeError):
        await _drain(agent_service.stream_agent(
            db, agent_session, "hello", capability=Capability.CHAT, agent_scheduler_token=token,
        ))

    assert scheduler.global_active_count == 0


# ── 13/14. Released after CancelledError / GeneratorExit mid-stream ────────

def _make_gated_complete(entered_event: asyncio.Event, release_event: asyncio.Event):
    async def _complete(request):
        entered_event.set()
        await release_event.wait()
        return _FakeRoutingResult()
    return _complete


async def test_slot_released_after_cancelled_error_mid_execution(db, agent_session, monkeypatch):
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)
    token = scheduler.try_acquire("user-1")

    entered = asyncio.Event()
    release = asyncio.Event()
    monkeypatch.setattr(agent_service._router, "complete", _make_gated_complete(entered, release))

    agen = agent_service.stream_agent(
        db, agent_session, "hello", capability=Capability.CHAT, agent_scheduler_token=token,
    )
    task = asyncio.create_task(_drain(agen))
    await asyncio.wait_for(entered.wait(), timeout=5)
    assert scheduler.global_active_count == 1  # still held — genuinely suspended

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert scheduler.global_active_count == 0


async def test_slot_released_after_generator_aclose_mid_execution(db, agent_session, monkeypatch):
    """Advances the generator past 3 real yields (capability_status_event,
    tool_status_event, tool_call) via sequential __anext__() calls — no
    background task needed, since each call returns promptly (the mocked
    tool_calls response is not gated). The generator is then genuinely
    suspended mid-execution (not finished — it has not yet called
    _run_tool()), matching the direct-aclose() pattern already verified
    safe in Etapa 5B's equivalent chat streaming test."""
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)
    token = scheduler.try_acquire("user-1")

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

    agen = agent_service.stream_agent(
        db, agent_session, "hello", capability=Capability.CHAT, agent_scheduler_token=token,
    )
    await agen.__anext__()  # capability_status_event
    await agen.__anext__()  # tool_status_event
    await agen.__anext__()  # tool_call event — generator now suspended right after this yield,
                            # not yet having called _run_tool()

    assert scheduler.global_active_count == 1  # still held — genuinely mid-execution

    await agen.aclose()  # simulates SSE client disconnect (Starlette calls this)

    assert scheduler.global_active_count == 0


# ── 15. Released after execution timeout (with_execution_timeout) ──────────

async def test_slot_released_after_execution_timeout(db, agent_session, monkeypatch):
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)
    token = scheduler.try_acquire("user-1")

    entered = asyncio.Event()
    never_releases = asyncio.Event()  # deliberately never set — provider hangs "forever"
    monkeypatch.setattr(agent_service._router, "complete", _make_gated_complete(entered, never_releases))

    agen = agent_service.stream_agent(
        db, agent_session, "hello", capability=Capability.CHAT, agent_scheduler_token=token,
    )
    wrapped = with_execution_timeout(agen, timeout_s=0.2)

    events = []
    async for event in wrapped:
        events.append(event)

    assert any("timed out" in e for e in events)
    assert scheduler.global_active_count == 0, "timeout must still release the scheduler slot"
