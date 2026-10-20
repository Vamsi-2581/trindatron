"""
AgentSchedule — persistent-agent trigger (Persistent Agent Runtime V1,
Tier 1 — WAKE/CHECK/DECIDE/ACT/SLEEP; originally Execution Runtime V1,
mission section 13 "PERSISTENT AGENTS", which only built WAKE/ACT).

Deliberately minimal: an interval-based trigger ("run this agent's job
every N seconds"), not a cron-expression engine. Checked by a single
in-process polling loop started in main.py's lifespan
(agent_schedule_service.run_schedule_poll_loop) — same "in-memory,
single-process" philosophy already established and documented by
agent_scheduler.py/request_control.py, not a new pattern invented here.

CHECK / DECIDE (Tier 1 — non-LLM only)
------------------------------------------
`last_check_signature`/`last_decision`/`last_decision_at` (added with
Tier 1) make the CHECK -> DECIDE part of the cycle observable and
persisted, not just the WAKE/ACT/SLEEP part V1 had. See
services/agent_check_source.py (CHECK) and
services/adaptive_budget_service.should_skip_llm() (DECIDE) — both
exclusively non-LLM by construction in this phase.
`last_decision` is one of `AGENT_SCHEDULE_DECISIONS` below — enforced by
a CHECK constraint (see __table_args__), not just convention.

DECIDE (Tier 2 — LLM DECIDE, additive on top of Tier 1)
------------------------------------------------------------
A schedule opts into an LLM-driven DECIDE by using the
`{"check": {...}, "decide": {...}}` job_payload shape (as opposed to
Tier 1's `{"check": {...}, "action": {...}}`, or the legacy
`{"steps": [...]}` shape — see agent_schedule_service.py's own module
docstring for the full three-shape dispatch). `pending_decision_job_id`
tracks the outstanding DECIDE ExecutionJob (a real, durable, billed
ExecutionJob — see src/execution/agent_decision_provider.py) for such a
schedule: non-NULL means "a decision is in flight or awaiting
consumption", NULL means "no decision outstanding". It is cleared via an
atomic compare-and-swap UPDATE (see agent_schedule_service._consume_pending_decision())
— crash-safe consumption, not an in-memory or best-effort flag; a
schedule's `act` decision can therefore never produce more than one ACT
ExecutionJob even across a process restart, two overlapping poll cycles,
or two workers. See docs/PERSISTENT_AGENTS.md's "Tier 2" section for the
full WAKE -> CHECK -> DECIDE ExecutionJob -> ACT ExecutionJob -> Sentinel
-> SLEEP cycle.

Does NOT duplicate agent_scheduler.py (AgentScheduler/InMemoryAgentScheduler):
that module is concurrency ADMISSION control for synchronous /agent HTTP
executions — it has no concept of time-based triggering and is untouched
by this file. AgentSchedule is a TRIGGER SOURCE that calls
execution_service.create_job() exactly like the HTTP route
(api/routes/execution.py) does — manual (HTTP) and scheduled (this) are
two callers of the same orchestrator, not two orchestrators.

Event/webhook/dependency-based triggering (also named in section 13) are
NOT implemented here — see docs/EXECUTION_RUNTIME.md's "Known gaps" for
why (webhook triggering is a small, real extension of this same table;
dependency-chaining is a materially bigger feature, marked a pending item,
not faked).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base, UTCDateTime

# Controlled vocabulary for `last_decision` — enforced by a DB CHECK
# constraint below, not just convention (same pattern
# models/execution.py's EXECUTION_STATUSES/ck_execution_job_status already
# established).
AGENT_SCHEDULE_DECISIONS: tuple[str, ...] = (
    "acted", "skipped", "capacity_refused",
    # Tier 2 — LLM DECIDE additions. "acted"/"capacity_refused" above are
    # REUSED for a Tier-2 ACT (same meaning, same value — no duplicate
    # vocabulary for "a job was created" vs "capacity refused it").
    "decide_pending",  # a DECIDE ExecutionJob was created, awaiting a terminal result
    "decide_failed",   # the DECIDE job reached a non-"completed" terminal status
    "decide_invalid",  # the DECIDE job completed but its output failed validation
    "decide_skip",     # the DECIDE job completed and the model chose "skip"
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AgentSchedule(Base):
    """WAKE -> CHECK -> DECIDE -> ACT -> SLEEP, driven by interval_seconds.

    One row = one recurring trigger for one agent. next_run_at is the only
    field the poll loop reads to decide whether to fire; it is advanced to
    `now + interval_seconds` immediately after a job is created for this
    schedule (not after the job COMPLETES — a schedule fires on a fixed
    cadence regardless of how long the previous run took, matching the
    "absolute clock, never extended" philosophy session_window_service.py
    already established for the entitlement session window).
    """

    __tablename__ = "agent_schedules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_definitions.id", ondelete="CASCADE"), nullable=False, index=True)
    # Persistent Agent Instance V1 — which AgentInstance this schedule
    # belongs to. Deliberately ADDITIVE, not a repurposing of `agent_id`
    # above (which keeps pointing at AgentDefinition, completely
    # unchanged, exactly as every existing caller/test already expects):
    # renaming/redirecting agent_id would have touched every read site in
    # agent_schedule_service.py for zero benefit. Nullable — a schedule
    # created through the raw create_schedule()/direct-construction path
    # (every schedule that predates this phase, and any future caller
    # that genuinely has no instance concept) is fully legitimate with
    # this NULL; see agent_schedule_service.py's module docstring for
    # exactly what changes (instance-status gating, health reconciliation)
    # when it's set vs. not.
    agent_instance_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("agent_instances.id", ondelete="CASCADE"), nullable=True, index=True,
    )

    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    # Payload template forwarded to execution_service.create_job() verbatim
    # each time this schedule fires — same shape as ExecutionJob.payload.
    job_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    # NULL = "no override, inherit AgentDefinition.priority at ACT time"
    # (Persistent Agent Runtime V1) — distinct from an explicit value,
    # which always wins over the agent's own default. See
    # agent_schedule_service._fire_due_schedules()'s effective_priority
    # resolution. Previously NOT NULL with a "low" default, which made
    # "no override" inexpressible — that was the gap being closed here,
    # not a reinterpretation of an existing override's meaning: a caller
    # who explicitly sent "low" before still gets "low" after.
    priority: Mapped[str | None] = mapped_column(String(10), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    next_run_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    last_run_job_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("execution_jobs.id", ondelete="SET NULL"), nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    # ── CHECK / DECIDE (Tier 1) ─────────────────────────────────────────
    last_check_signature: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_decision_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    # ── DECIDE (Tier 2 — LLM DECIDE) ─────────────────────────────────────
    pending_decision_job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("execution_jobs.id", ondelete="SET NULL"), nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "last_decision IS NULL OR last_decision IN (" + ", ".join(repr(d) for d in AGENT_SCHEDULE_DECISIONS) + ")",
            name="ck_agent_schedule_last_decision",
        ),
    )
