"""Etapa 5D, section 19 — dedicated real-timeout tests for the Agent
Scheduler's with_execution_timeout(). Uses an artificially slow (never-
completing) mocked provider call — never a real provider — and verifies:
  - the timeout actually fires;
  - the underlying execution is genuinely cancelled (its own cleanup runs:
    scheduler token released, browser session closed);
  - the scheduler slot returns to exactly zero;
  - a brand-new execution can be admitted immediately afterward (no
    lingering rejection, no leaked state).
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

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


async def test_timeout_fires_and_emits_an_error_event(db, agent_session, monkeypatch):
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)
    token = scheduler.try_acquire("user-1")

    async def _never_completes(request):
        await asyncio.sleep(3600)  # would only fire if the timeout failed to cancel it
        return _FakeRoutingResult()  # pragma: no cover

    monkeypatch.setattr(agent_service._router, "complete", _never_completes)

    agen = agent_service.stream_agent(
        db, agent_session, "hello", capability=Capability.CHAT, agent_scheduler_token=token,
    )
    wrapped = with_execution_timeout(agen, timeout_s=0.2)

    events = [e async for e in wrapped]
    payloads = [json.loads(e[len("data: "):]) for e in events if e.startswith("data: ") and "[DONE]" not in e]

    error_events = [p for p in payloads if p.get("type") == "error"]
    assert len(error_events) == 1
    assert "timed out" in error_events[0]["message"].lower()
    assert events[-1] == "data: [DONE]\n\n"


async def test_underlying_task_is_genuinely_cancelled_on_timeout(db, agent_session, monkeypatch):
    """Proves the cancellation isn't just cosmetic — the mocked provider
    coroutine is actually torn down (its own CancelledError observed), not
    left running detached in the background after the timeout fires."""
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)
    token = scheduler.try_acquire("user-1")

    was_cancelled = asyncio.Event()

    async def _never_completes(request):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            was_cancelled.set()
            raise

    monkeypatch.setattr(agent_service._router, "complete", _never_completes)

    agen = agent_service.stream_agent(
        db, agent_session, "hello", capability=Capability.CHAT, agent_scheduler_token=token,
    )
    wrapped = with_execution_timeout(agen, timeout_s=0.2)
    async for _ in wrapped:
        pass

    assert was_cancelled.is_set(), "the underlying provider call was never actually cancelled"


async def test_browser_session_is_closed_on_timeout(db, agent_session, monkeypatch):
    """Cleanup must reach beyond the scheduler token — stream_agent()'s
    `finally` also closes its BrowserSession, and that must still happen
    when the execution ends via a scheduler timeout, not just on normal
    completion/exception paths (already covered by
    test_agent_browser_integration.py)."""
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)
    token = scheduler.try_acquire("user-1")

    async def _never_completes(request):
        await asyncio.sleep(3600)

    closed = asyncio.Event()
    from trinity_agent.browser.session import BrowserSession
    original_close = BrowserSession.close

    async def _spy_close(self):
        closed.set()
        await original_close(self)

    monkeypatch.setattr(agent_service._router, "complete", _never_completes)
    with patch.object(BrowserSession, "close", new=_spy_close):
        agen = agent_service.stream_agent(
            db, agent_session, "hello", capability=Capability.CHAT, agent_scheduler_token=token,
        )
        wrapped = with_execution_timeout(agen, timeout_s=0.2)
        async for _ in wrapped:
            pass

    assert closed.is_set(), "BrowserSession.close() was never called after a scheduler timeout"


async def test_slot_returns_to_zero_and_admits_a_new_execution_after_timeout(db, agent_session, monkeypatch):
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)
    token = scheduler.try_acquire("user-1")

    async def _never_completes(request):
        await asyncio.sleep(3600)

    monkeypatch.setattr(agent_service._router, "complete", _never_completes)

    agen = agent_service.stream_agent(
        db, agent_session, "hello", capability=Capability.CHAT, agent_scheduler_token=token,
    )
    async for _ in with_execution_timeout(agen, timeout_s=0.2):
        pass

    assert scheduler.global_active_count == 0

    # A brand-new execution is admitted immediately — no lingering
    # rejection, no leaked global/per-user state from the timed-out one.
    new_token = scheduler.try_acquire("user-1")
    assert scheduler.global_active_count == 1
    new_token.release()
    assert scheduler.global_active_count == 0


async def test_timeout_end_to_end_through_the_agent_http_endpoint(client, auth_headers, monkeypatch):
    """Real HTTP round trip: a hung provider call, a short configured
    timeout, and confirmation the response still completes (with a
    timeout error event, not a hung connection) and the process-wide
    scheduler singleton is clean afterward."""
    from src.services.agent_scheduler import agent_scheduler as process_scheduler

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    monkeypatch.setattr("src.config.settings.agent_scheduler_execution_timeout_seconds", 0.2)

    async def _never_completes(request):
        await asyncio.sleep(3600)

    with patch("src.services.agent_service._router.complete", new=_never_completes):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "hang forever"}, headers=auth_headers)

    assert resp.status_code == 200  # SSE stream itself completes normally (200), carries an error event
    assert "timed out" in resp.text.lower()
    assert process_scheduler.global_active_count == 0
