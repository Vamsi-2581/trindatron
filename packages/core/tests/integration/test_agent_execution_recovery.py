"""Etapa 5I — Agent Execution Recovery.

Proves AgentExecution's persisted lifecycle (admitted -> running ->
completed | failed | interrupted) is set correctly across every exit path
of stream_agent(), and that reconciliation of a stale row never leaves a
UsageRecord reservation permanently pending (REGRA FINANCEIRA CRÍTICA).

Calls stream_agent() directly (no HTTP layer) — same direct-generator-call
pattern as test_agent_scheduler_streaming.py (Etapa 5D), which already
proves AgentExecutionToken release timing across these exact same exit
paths; this file targets AgentExecution's persisted status instead.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from trinity_router import ProviderUnavailableError

from src.capability import Capability
from src.models.agent_execution import AgentExecution
from src.models.session import Session as SessionModel
from src.models.usage import UsageRecord
from src.services import agent_service
from src.services.agent_execution_service import (
    create_admitted_execution,
    reconcile_stale_executions_at_startup,
)
from src.services.entitlement_service import check_and_reserve

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


@pytest.fixture
def user_id(registered_user) -> str:
    return registered_user["user_id"]


@pytest.fixture
def reservation_id(db, user_id, agent_session) -> str:
    return check_and_reserve(db, user_id=user_id, session_id=agent_session.id, plan="start")


async def _drain(agen):
    async for _ in agen:
        pass


# ── Happy path — admitted -> running -> completed ───────────────────────────

async def test_agent_execution_completes_after_successful_run(db, user_id, agent_session, reservation_id, monkeypatch):
    execution_id = create_admitted_execution(
        db, user_id=user_id, session_id=agent_session.id,
        reservation_id=reservation_id, continuity_execution_id=None, idempotency_key_id=None,
    )
    row = db.query(AgentExecution).filter(AgentExecution.id == execution_id).first()
    assert row.status == "admitted"

    async def _complete(request):
        return _FakeRoutingResult()

    monkeypatch.setattr(agent_service._router, "complete", _complete)

    await _drain(agent_service.stream_agent(
        db, agent_session, "hello", capability=Capability.CHAT,
        reservation_id=reservation_id, agent_execution_id=execution_id,
    ))

    db.refresh(row)
    assert row.status == "completed"
    assert row.message_id is not None
    assert row.iterations_done == 1


# ── Provider failure — failed, reservation refunded (scenario 8/9) ─────────

async def test_agent_execution_marked_failed_and_reservation_refunded_on_provider_failure(
    db, user_id, agent_session, reservation_id, monkeypatch,
):
    execution_id = create_admitted_execution(
        db, user_id=user_id, session_id=agent_session.id,
        reservation_id=reservation_id, continuity_execution_id=None, idempotency_key_id=None,
    )

    async def _failing_complete(request):
        raise ProviderUnavailableError("provider down")

    monkeypatch.setattr(agent_service._router, "complete", _failing_complete)

    async for _ in agent_service.stream_agent(
        db, agent_session, "hello", capability=Capability.CHAT,
        reservation_id=reservation_id, agent_execution_id=execution_id,
    ):
        pass

    row = db.query(AgentExecution).filter(AgentExecution.id == execution_id).first()
    assert row.status == "failed"

    reservation = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).first()
    assert reservation.status == "failed"
    assert float(reservation.tcu) == 0.0  # REGRA FINANCEIRA CRÍTICA — full refund, never left pending


# ── GeneratorExit (client disconnect) — interrupted (scenario 8/9) ─────────

async def test_agent_execution_marked_interrupted_on_generator_aclose(
    db, user_id, agent_session, reservation_id, monkeypatch,
):
    execution_id = create_admitted_execution(
        db, user_id=user_id, session_id=agent_session.id,
        reservation_id=reservation_id, continuity_execution_id=None, idempotency_key_id=None,
    )

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
        db, agent_session, "hello", capability=Capability.CHAT,
        reservation_id=reservation_id, agent_execution_id=execution_id,
    )
    await agen.__anext__()  # capability_status_event
    await agen.__anext__()  # tool_status_event
    await agen.__anext__()  # tool_call event — suspended before _run_tool()

    row = db.query(AgentExecution).filter(AgentExecution.id == execution_id).first()
    assert row.status == "running"  # still mid-execution

    await agen.aclose()  # simulates SSE client disconnect

    db.refresh(row)
    assert row.status == "interrupted"
    reservation = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).first()
    assert reservation.status == "failed"
    assert float(reservation.tcu) == 0.0


# ── asyncio.CancelledError mid-execution — interrupted ──────────────────────

def _make_gated_complete(entered_event: asyncio.Event, release_event: asyncio.Event):
    async def _complete(request):
        entered_event.set()
        await release_event.wait()
        return _FakeRoutingResult()
    return _complete


async def test_agent_execution_marked_interrupted_on_cancelled_error(
    db, user_id, agent_session, reservation_id, monkeypatch,
):
    execution_id = create_admitted_execution(
        db, user_id=user_id, session_id=agent_session.id,
        reservation_id=reservation_id, continuity_execution_id=None, idempotency_key_id=None,
    )

    entered = asyncio.Event()
    release = asyncio.Event()
    monkeypatch.setattr(agent_service._router, "complete", _make_gated_complete(entered, release))

    agen = agent_service.stream_agent(
        db, agent_session, "hello", capability=Capability.CHAT,
        reservation_id=reservation_id, agent_execution_id=execution_id,
    )
    task = asyncio.create_task(_drain(agen))
    await asyncio.wait_for(entered.wait(), timeout=5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = db.query(AgentExecution).filter(AgentExecution.id == execution_id).first()
    assert row.status == "interrupted"
    reservation = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).first()
    assert reservation.status == "failed"


# ── Startup reconciliation — never leaves a stale row ambiguous forever ────

async def test_reconcile_stale_executions_at_startup_finalizes_orphan_rows(db, user_id, agent_session, reservation_id):
    execution_id = create_admitted_execution(
        db, user_id=user_id, session_id=agent_session.id,
        reservation_id=reservation_id, continuity_execution_id=None, idempotency_key_id=None,
    )
    row = db.query(AgentExecution).filter(AgentExecution.id == execution_id).first()
    row.status = "running"
    row.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()

    count = reconcile_stale_executions_at_startup(db)

    assert count == 1
    db.refresh(row)
    assert row.status == "interrupted"
    reservation = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).first()
    assert reservation.status == "failed"
    assert float(reservation.tcu) == 0.0


async def test_reconcile_stale_executions_at_startup_is_a_noop_when_none_stale(db, user_id, agent_session, reservation_id):
    execution_id = create_admitted_execution(
        db, user_id=user_id, session_id=agent_session.id,
        reservation_id=reservation_id, continuity_execution_id=None, idempotency_key_id=None,
    )
    row = db.query(AgentExecution).filter(AgentExecution.id == execution_id).first()
    row.status = "completed"
    db.commit()

    count = reconcile_stale_executions_at_startup(db)

    assert count == 0
    db.refresh(row)
    assert row.status == "completed"  # untouched


async def test_reconcile_stale_execution_with_continuity_link_true_ups_to_zero(db, user_id, agent_session, monkeypatch):
    """No new financial ledger: a stale row linked to a Continuity
    execution must be closed out via the EXISTING true_up_continuity()
    primitive, with a full refund (actual_tcu=0.0) — never a new
    refund/charge rule invented in this etapa."""
    execution_id = create_admitted_execution(
        db, user_id=user_id, session_id=agent_session.id,
        reservation_id=None, continuity_execution_id="continuity-exec-1", idempotency_key_id=None,
    )
    row = db.query(AgentExecution).filter(AgentExecution.id == execution_id).first()
    row.status = "admitted"
    row.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()

    calls = []

    def _fake_true_up(db_arg, *, user_id, estimated_tcu, actual_tcu, execution_id):
        calls.append((user_id, estimated_tcu, actual_tcu, execution_id))

    monkeypatch.setattr("src.services.agent_execution_service.true_up_continuity", _fake_true_up)

    count = reconcile_stale_executions_at_startup(db)

    assert count == 1
    assert calls == [(user_id, 0.0, 0.0, "continuity-exec-1")]
    db.refresh(row)
    assert row.status == "interrupted"
