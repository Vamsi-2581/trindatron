"""Agent Execution Recovery — Etapa 5I (Request Idempotency & Execution
Recovery).

Persisted, unconditional lifecycle tracking for every stream_agent() run
— unlike idempotency_service.py (opt-in, only when the client sends an
Idempotency-Key header), an AgentExecution row is created for EVERY /agent
execution, so a crashed or restarted process leaves a diagnosable trail
instead of a silently abandoned pending UsageRecord.

Explicitly NOT automatic resume: this module's job is limited to (1)
knowing an execution existed, (2) knowing what its last known state was,
(3) making sure it never stays ambiguously "admitted"/"running" forever,
(4) making sure its TCU/Continuity accounting is correctly closed out when
found stale. Nothing here re-invokes stream_agent() or continues a
previous execution's work — see AgentExecution's own docstring for the
five states this module transitions between.

Does NOT introduce a second financial ledger — see
_reconcile_one_stale_execution()'s use of the ALREADY-EXISTING
cancel_reservation()/true_up_continuity() primitives, never new logic.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session as DBSession

from src.models.agent_execution import AgentExecution
from src.observability.metrics import core_metrics
from src.services.continuity_entitlement_service import true_up_continuity
from src.services.entitlement_service import cancel_reservation

logger = logging.getLogger("trinity.agent_execution")

_transitions_total = core_metrics.counter(
    "agent_execution_transitions_total", "AgentExecution state transitions, labeled by to_status.",
)
_reconciled_total = core_metrics.counter(
    "agent_execution_reconciled_total", "Stale AgentExecution rows reconciled, labeled by trigger (startup|on_demand).",
)


def create_admitted_execution(
    db: DBSession, *, user_id: str, session_id: str,
    reservation_id: str | None, continuity_execution_id: str | None,
    idempotency_key_id: str | None,
) -> str:
    """Called from sessions.py right where StreamingResponse(stream_agent(...))
    is about to be constructed — Request Control, kill switches, Agent
    Scheduler, and TCU reservation/Continuity have ALL already succeeded
    by this point. Returns the new AgentExecution's id, threaded into
    stream_agent() exactly like reservation_id/continuity_execution_id
    already are."""
    execution = AgentExecution(
        user_id=user_id, session_id=session_id,
        reservation_id=reservation_id, continuity_execution_id=continuity_execution_id,
        idempotency_key_id=idempotency_key_id,
        status="admitted",
    )
    db.add(execution)
    db.commit()
    db.refresh(execution)
    _transitions_total.inc(to_status="admitted")
    return execution.id


def mark_running(db: DBSession, execution_id: str | None) -> None:
    """Called as the first statement inside stream_agent()'s own try
    block — distinguishes "scheduled to run" (admitted) from "actually
    started" (running)."""
    if execution_id is None:
        return
    _update_status(db, execution_id, "running")


def mark_completed(db: DBSession, execution_id: str | None, *, message_id: str, iterations_done: int) -> None:
    """Mirrors the existing `_cancel_on_exit = False` success marker in
    stream_agent() — called at the exact same point."""
    if execution_id is None:
        return
    row = db.query(AgentExecution).filter(AgentExecution.id == execution_id).first()
    if row is None:
        logger.warning("agent_execution: mark_completed on missing execution | id=%s", execution_id)
        return
    row.status = "completed"
    row.message_id = message_id
    row.iterations_done = iterations_done
    db.commit()
    _transitions_total.inc(to_status="completed")


def mark_failed(db: DBSession, execution_id: str | None, *, note: str | None = None) -> None:
    """A handled exception path (BudgetExceededError/ProviderUnavailableError/
    generic Exception) — the client already received a clean SSE "error"
    event before this is called."""
    if execution_id is None:
        return
    _update_status(db, execution_id, "failed", note=note)


def mark_interrupted(db: DBSession, execution_id: str | None, *, note: str | None = None) -> None:
    """GeneratorExit / asyncio.CancelledError — client disconnect, or
    with_execution_timeout() cutting the execution off. Distinct from
    "failed": the client never received a clean error event for this
    path (the stream was simply cut)."""
    if execution_id is None:
        return
    _update_status(db, execution_id, "interrupted", note=note)


def _update_status(db: DBSession, execution_id: str, status: str, *, note: str | None = None) -> None:
    row = db.query(AgentExecution).filter(AgentExecution.id == execution_id).first()
    if row is None:
        logger.warning("agent_execution: %s on missing execution | id=%s", status, execution_id)
        return
    row.status = status
    if note is not None:
        row.note = note
    db.commit()
    _transitions_total.inc(to_status=status)


def _reconcile_one_stale_execution(db: DBSession, row: AgentExecution) -> None:
    """Financial safety net for one stale execution: cancel any pending
    TCU reservation (full refund, existing semantics) and true-up any
    Continuity debit to a full refund (actual_tcu=0.0 — the same "no
    partial credit for an attempt that never reached a confirmed success"
    rule already governing every other interruption path in
    chat_service.py/agent_service.py, not a new rule invented here)."""
    status_was = row.status
    age_s = (datetime.now(timezone.utc) - row.updated_at).total_seconds()
    if row.reservation_id is not None:
        cancel_reservation(db, row.reservation_id)
    if row.continuity_execution_id is not None:
        true_up_continuity(
            db, user_id=row.user_id, estimated_tcu=0.0, actual_tcu=0.0,
            execution_id=row.continuity_execution_id,
        )
    row.status = "interrupted"
    row.note = "reconciled: stale admitted/running row, no terminal state ever recorded"
    db.commit()
    logger.warning(
        "agent_execution: reconciled stale execution | id=%s status_was=%s age_s=%.0f",
        row.id, status_was, age_s,
    )


def reconcile_stale_executions_at_startup(db: DBSession) -> int:
    """Unconditional startup sweep — called once from main.py's
    lifespan(), alongside _run_startup_cleanup() and
    reconcile_stale_idempotency_keys_at_startup(). ANY row still
    "admitted"/"running" at boot is, by construction, from a previous
    process instance. Returns the number of rows reconciled."""
    stale = (
        db.query(AgentExecution)
        .filter(AgentExecution.status.in_(("admitted", "running")))
        .all()
    )
    for row in stale:
        _reconcile_one_stale_execution(db, row)
    if stale:
        _reconciled_total.inc(len(stale), trigger="startup")
    return len(stale)
