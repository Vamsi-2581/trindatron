"""Etapa 5H — global Agent kill switch (agent_enabled).

Covers: enabled (default) -> normal execution unaffected; disabled -> 503,
before TCU reservation/Agent Scheduler/provider; disabled rejection
consumes no TCU (no CreditTransaction/UsageRecord), calls no provider,
occupies no scheduler slot; an execution ALREADY IN PROGRESS when the
switch flips is NOT interrupted (direct-generator proof, same style as
tests/integration/test_request_control_streaming.py and
test_agent_scheduler_streaming.py — the switch is only checked in the
route-handler GATE, never inside stream_agent() itself, so this is true
by construction, and this test proves it rather than just asserting it).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.models.usage import UsageRecord
from src.services import agent_service
from src.services.runtime_settings import AGENT_ENABLED, set_setting


def _disable_agent(db, admin_user_id: str = "test-admin") -> None:
    set_setting(db, AGENT_ENABLED, False, admin_user_id=admin_user_id)


# ═════════════════════════════════════════════════════════════════════════
# HTTP-level: enabled (default) vs. disabled
# ═════════════════════════════════════════════════════════════════════════

def test_agent_enabled_default_executes_normally(client, auth_headers):
    from trinity_router import RoutingResult

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    direct = RoutingResult(
        content="42", tool_calls=None, provider="anthropic", model="claude-sonnet-test",
        input_tokens=5, output_tokens=5, cached_tokens=0, cost_usd=0.0, tcu=0.0,
        failover_used=False, failover_reason=None,
    )
    with patch("src.services.agent_service._router.complete", new=AsyncMock(return_value=direct)):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "hi"}, headers=auth_headers)
    assert resp.status_code == 200


def test_agent_disabled_rejects_with_503(client, db, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    _disable_agent(db)

    with patch("src.services.agent_service._router.complete") as complete_spy:
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "hi"}, headers=auth_headers)

    assert resp.status_code == 503
    complete_spy.assert_not_called()


def test_agent_disabled_rejection_consumes_no_tcu(client, db, auth_headers):
    from src.models.usage import UsageRecord

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    _disable_agent(db)

    before = db.query(UsageRecord).count()
    with patch("src.services.agent_service._router.complete"):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "hi"}, headers=auth_headers)
    after = db.query(UsageRecord).count()

    assert resp.status_code == 503
    assert after == before  # no pending reservation was ever created


def test_agent_disabled_does_not_occupy_scheduler(client, db, auth_headers):
    from src.services.agent_scheduler import agent_scheduler

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    _disable_agent(db)

    with patch("src.services.agent_scheduler.agent_scheduler.try_acquire") as acquire_spy:
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "hi"}, headers=auth_headers)

    assert resp.status_code == 503
    acquire_spy.assert_not_called()
    assert agent_scheduler.global_active_count == 0


def test_agent_disabled_also_rejects_message_agent_branch(client, db, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    _disable_agent(db)

    with patch("src.services.agent_service._router.complete") as complete_spy:
        resp = client.post(
            f"/sessions/{session_id}/message",
            json={"content": "search the web for python tutorials"},  # classifies to an AGENT task_type capability
            headers=auth_headers,
        )

    assert resp.status_code == 503
    complete_spy.assert_not_called()


def test_agent_disabled_does_not_affect_chat_path(client, db, auth_headers):
    """agent_enabled=False must not touch /chat at all — /chat never
    calls stream_agent()."""
    from trinity_router import RouterStreamChunk

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    _disable_agent(db)

    chunks = [RouterStreamChunk(text="hi")]
    chunks.append(RouterStreamChunk(is_final=True, provider="anthropic", model="claude-sonnet-test",
                                     input_tokens=5, output_tokens=5, cached_tokens=0, cost_usd=0.0, tcu=0.0))

    async def _stream(request):
        for c in chunks:
            yield c

    with patch("src.services.chat_service._router.stream", _stream):
        resp = client.post(f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=auth_headers)
    assert resp.status_code == 200


# ═════════════════════════════════════════════════════════════════════════
# In-flight execution is NOT interrupted by a switch flip mid-stream
# (direct-generator proof, mirrors test_request_control_streaming.py)
# ═════════════════════════════════════════════════════════════════════════

pytestmark_asyncio = pytest.mark.asyncio


@pytest.fixture
def agent_session(client, auth_headers, db):
    from src.models.session import Session as SessionModel

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    return db.query(SessionModel).filter(SessionModel.id == session_id).one()


@pytest.mark.asyncio
async def test_in_flight_agent_execution_survives_a_mid_stream_disable(db, agent_session):
    """stream_agent() itself never re-checks agent_enabled — the switch is
    a route-handler GATE, checked once before stream_agent() is ever
    called. This test proves that structurally: starts stream_agent()
    directly (bypassing the HTTP gate entirely, exactly as if it had
    already passed it), flips the switch mid-execution, and confirms the
    execution completes normally regardless."""
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

    with patch("src.services.agent_service._router.complete", new=_gated_complete):
        agen = agent_service.stream_agent(db, agent_session, "hello")
        task = asyncio.create_task(_drain(agen))

        await asyncio.wait_for(entered_event.wait(), timeout=5)
        # Flip the switch WHILE the execution is genuinely suspended mid-call.
        set_setting(db, AGENT_ENABLED, False, admin_user_id="test-admin")

        release_event.set()
        await asyncio.wait_for(task, timeout=5)  # completes normally, not interrupted

    from src.models.message import Message
    messages = db.query(Message).filter(Message.session_id == agent_session.id).all()
    assert any(m.role == "assistant" and m.content == "final answer" for m in messages)


async def _drain(agen):
    async for _ in agen:
        pass
