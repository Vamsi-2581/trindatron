"""
AgentInstance — Persistent Agent Instance V1.

The missing entity the architecture actually needed. Before this:

    AgentDefinition = configuration/template ("a research-monitor agent
                       that uses these skills, this system prompt")
    AgentSchedule    = a trigger ("run agent X's action every N minutes")
    ExecutionJob     = one execution ("this one ACT, right now")

None of those three is "the persistent agent the user owns and looks
at" — AgentDefinition is a reusable template (a user can have schedules
against it, or none), AgentSchedule is a bare trigger with no identity of
its own beyond its interval, and ExecutionJob is transient by design (one
per attempt). AgentInstance is that missing entity:

    User -> AgentInstance -> AgentSchedule -> ExecutionJob -> Worker -> Result

AgentInstance does NOT execute anything itself — see
services/agent_instance_service.py's module docstring. It owns identity
and lifecycle (status: active/paused/disabled/error) for one persistent
agent the user created from a definition; actual work is still entirely
AgentSchedule's WAKE/CHECK/DECIDE/ACT cycle and execution_service/
execution_worker's durable job machinery, both completely unmodified by
this phase.

Fields deliberately NOT included (checked against "não copiar
indiscriminadamente" before adding each one)
------------------------------------------------------------------------
- No `enabled` boolean alongside `status` — status already fully encodes
  "is this live" (active) vs "intentionally stopped" (paused/disabled)
  vs "in trouble" (error). A second boolean would just be able to
  disagree with status for no reason.
- No `next_run_at` — an instance can have zero, one, or several
  AgentSchedule rows (1 -> N); a single persisted "next run" on the
  instance would either be wrong (multiple schedules) or a stale copy of
  a value AgentSchedule.next_run_at already owns. Callers that want "when
  does this instance next wake" derive it (MIN across its schedules) at
  read time — see agent_instance_service.py.
- No `config`/`runtime metadata` JSON blob — nothing in this phase needs
  per-instance configuration beyond what AgentDefinition already
  provides (name/skills/system prompt) and AgentSchedule already provides
  (interval/check/action). Adding a speculative blob "for later" is
  exactly what was asked not to do.
- No secrets, no API keys, no billing fields — see
  agent_instance_service.py's module docstring; billing continues to flow
  entirely through AgentSchedule -> ExecutionJob -> the existing
  entitlement reserve/finalize/cancel path, untouched.

Health derivation (no second retry engine, no second worker)
------------------------------------------------------------------
`failure_count`/`last_error`/`status="error"` are updated by
agent_schedule_service._fire_due_schedules() itself, lazily, at the
moment a schedule is about to fire its NEXT ACT — see that module's
"Health reconciliation" docstring section for exactly when and why (never
by execution_worker.py, never by a new poller). ExecutionJob's own
retry/recovery (leases, backoff, Sentinel) is completely separate and
unmodified: a job in status="retrying" does not, by itself, make its
owning instance unhealthy — only a job that ultimately reaches
status="failed" counts toward failure_count.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base

AGENT_INSTANCE_STATUSES: tuple[str, ...] = ("active", "paused", "disabled", "error")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AgentInstance(Base):
    __tablename__ = "agent_instances"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    agent_definition_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_definitions.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    # Reuses the EXISTING Project model — there is no generic Workspace
    # table in this codebase (CodeWorkspace/WorkWorkspace are feature-
    # specific to TRINITY CODE/WORK, not agent-related), so no
    # `workspace_id` field was added here; inventing a new generic
    # workspace concept for this one field would be exactly the kind of
    # speculative addition this phase was told not to make.
    project_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="active", index=True)

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_execution_job_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("execution_jobs.id", ondelete="SET NULL"), nullable=True,
    )
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(repr(s) for s in AGENT_INSTANCE_STATUSES) + ")",
            name="ck_agent_instance_status",
        ),
    )
