"""
ExecutionJob / ExecutionArtifact — Execution Runtime, now Durable
(see execution_worker.py's module docstring for the full worker model).

ExecutionJob is the persisted lifecycle record for one unit of autonomous,
potentially long-running work. Durable Execution's whole point is that
this row (plus checkpoint below) IS the source of truth — no in-process
task holds state the row doesn't also have. An ExecutionJob is NOT tied to
any HTTP connection AND NOT tied to any one worker process: it is created
with status="queued", returns a job_id immediately, and is picked up by
WHICHEVER worker (this process or another) next claims it — surviving an
HTTP disconnect, a worker crash, or a full process restart alike. Distinct
from AgentExecution (models/agent_execution.py), which tracks one
synchronous stream_agent() HTTP-streaming run tied to the request's own
connection — that one is deliberately NOT durable (a chat/agent request
truly does end when the connection does).

Lifecycle (all 10 base states the original spec required, plus RETRYING)
----------------------------------------------------------------------------
  CREATED -> QUEUED <-> RETRYING -> STARTING* -> RUNNING -> WAITING -> COMPLETED
                                                       \\-> FAILED
                                                       \\-> CANCELLED
                                                       \\-> EXPIRED
  (COMPLETED | FAILED | CANCELLED | EXPIRED) -> CLEANUP -> DESTROYED

  QUEUED/RETRYING -> RUNNING is the CLAIM transition (execution_worker.
  _try_claim()) — atomic, sets worker_id/lease_expires_at/heartbeat_at in
  the same UPDATE. RUNNING -> QUEUED/RETRYING is a RELEASE (a recoverable
  failure, or a reclaimed expired lease) — clears worker_id/
  lease_expires_at, sets next_run_at to a future instant (durable backoff).
  *STARTING exists in the status enum for backward compatibility but the
  current worker's claim goes straight to RUNNING (claim and "start
  running" are the same atomic step now) — no code sets STARTING today.

WAITING is used only for a Sentinel re-check/retry pause (see
sentinel_service.py) — a job never waits on user input in this phase (no
approval-gate integration here; TRINITY WORK's own approval gates are a
separate, unrelated mechanism — see work_service.py, untouched by this
module).

No double accounting
---------------------
This table has NO cost/TCU column of its own. `reservation_id` is a
reference-only pointer into the EXISTING UsageRecord table (same pattern as
AgentExecution.reservation_id) — set only when the job's work actually
invokes the Model Router (via agent_service/entitlement_service's own
check_and_reserve/finalize_usage/cancel_reservation, unmodified). A job
whose work is pure tool/shell execution with no LLM call never touches
UsageRecord and this column stays NULL.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base, UTCDateTime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Canonical status values — single source of truth, referenced by
# execution_service.py rather than re-declared there.
#
# "retrying" (added for Durable Execution — see execution_worker.py):
# distinct from "queued" purely for observability (a fresh job vs. one
# that already failed at least once and is waiting for next_run_at) — the
# worker's claim query treats both identically (status IN ('queued',
# 'retrying') AND (next_run_at IS NULL OR next_run_at <= now)).
EXECUTION_STATUSES: tuple[str, ...] = (
    "created", "queued", "starting", "running", "retrying", "waiting",
    "completed", "failed", "cancelled", "expired",
    "cleanup", "destroyed",
)
# Statuses from which no further transition is possible without first
# passing through cleanup — used by execution_service.py's guard.
EXECUTION_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled", "expired", "destroyed"}
)
# Statuses a recovery sweep must consider possibly-orphaned. "running" is
# the only one that ever carries a lease (worker_id/lease_expires_at) —
# execution_worker.reclaim_expired_leases() is what actually decides
# orphaned-ness for it (lease expired or missing), not this set alone;
# "queued"/"retrying" are never orphaned by a worker crash (nobody owned
# them yet), they're just durably waiting for any worker to claim them.
EXECUTION_LIVE_STATUSES: frozenset[str] = frozenset(
    {"created", "queued", "starting", "running", "retrying", "waiting", "cleanup"}
)

EXECUTION_PRIORITIES: tuple[str, ...] = ("critical", "high", "normal", "low")


class ExecutionJob(Base):
    """One unit of autonomous execution work — the persisted half of the
    Execution Orchestrator's in-memory task (see execution_service.py).
    """

    __tablename__ = "execution_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # ── Ownership / scoping ──────────────────────────────────────────────
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("agent_definitions.id", ondelete="SET NULL"), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)

    # ── What to run ──────────────────────────────────────────────────────
    # "shell" (only kind DockerExecutionProvider implements today — see
    # that module's docstring). Reserved for future kinds (e.g. "research",
    # "browser") without a schema change — the provider decides what job_type
    # values it understands; the orchestrator never branches on it itself.
    job_type: Mapped[str] = mapped_column(String(30), nullable=False, default="shell")
    provider_name: Mapped[str] = mapped_column(String(30), nullable=False, default="docker")
    priority: Mapped[str] = mapped_column(String(10), nullable=False, default="normal")
    # {"steps": [{"command": "..."}, ...]} — see execution_service.py's
    # module docstring for the exact shape and checkpoint/resume contract.
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)

    # ── Lifecycle ────────────────────────────────────────────────────────
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="created")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # {"completed_step_indices": [...], "step_results": {...}} — updated
    # after each step so a recoverable failure resumes instead of
    # restarting the whole plan. See execution_service.py.
    checkpoint: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Free-form summary of the final outcome (last step's output, or a
    # short description) — NOT the full log (see logs below).
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    logs: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Retry ────────────────────────────────────────────────────────────
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ── Sentinel ─────────────────────────────────────────────────────────
    # {"expected_artifacts": [...], "min_output_length": int, ...} — see
    # sentinel_service.SentinelCriteria. None = no verification requested.
    sentinel_criteria: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    sentinel_status: Mapped[str | None] = mapped_column(String(10), nullable=True)  # "pass" | "fail" | None
    sentinel_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # ── Budget / usage reference (no double accounting — see module docstring) ──
    reservation_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("usage_records.id", ondelete="SET NULL"), nullable=True)

    # ── Cancellation ─────────────────────────────────────────────────────
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # ── Durable Execution: queue / lease / retry scheduling ─────────────
    # See execution_worker.py's module docstring for the full model.
    # worker_id/lease_expires_at are both set atomically together on claim
    # (execution_worker._try_claim()) and both cleared together whenever
    # the job leaves "running" (finished, or released back to the queue
    # for retry) — never one without the other.
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    # Coarse: updated once per step (and on claim), not continuously
    # during a single long-running step — see execution_worker.py's
    # module docstring for why that granularity is sufficient (the lease
    # length is derived from the job's own timeout_seconds precisely so a
    # single step's normal duration can never outlive it).
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # NULL = eligible for claim as soon as status allows it. Set to a
    # future instant when a retryable failure releases the job back to
    # the queue (durable backoff — replaces the old in-process
    # asyncio.sleep()-based retry loop).
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)

    # ── Timing ───────────────────────────────────────────────────────────
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    artifacts: Mapped[list["ExecutionArtifact"]] = relationship(
        "ExecutionArtifact", back_populates="job", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(f"status IN ({', '.join(repr(s) for s in EXECUTION_STATUSES)})", name="ck_execution_job_status"),
        CheckConstraint(f"priority IN ({', '.join(repr(p) for p in EXECUTION_PRIORITIES)})", name="ck_execution_job_priority"),
    )


class ExecutionArtifact(Base):
    """Reference to one file an ExecutionJob produced.

    Two storage generations coexist here, distinguished by `storage_backend`:

      NULL       the original behaviour — the file lives on the per-session
                 workspace filesystem agent_service.py/code_service.py
                 already use (settings.agent_workspace_root), addressed by
                 `relative_path`. Every row written before object storage
                 existed is one of these, and shell/research jobs still
                 produce them.
      "r2"/      the file lives in object storage under `storage_key`, and
      "local"    `relative_path` keeps only the filename for display. Used by
                 artifacts too large or too long-lived for a container
                 filesystem that vanishes on the next deploy — a generated
                 deck being the first of them.

    The columns are additive rather than a replacement precisely so the
    existing rows and the existing job types keep working untouched: nothing
    about job_type="shell" changed when object storage arrived.
    """

    __tablename__ = "execution_artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id: Mapped[str] = mapped_column(String(36), ForeignKey("execution_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # Relative to the job's workspace directory — never an absolute host
    # path (see execution/provider.py's path-safety helper, reused from
    # capability/sandbox_utils.py's existing _sandboxed()).
    relative_path: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    # ── Object storage (see the class docstring) ─────────────────────────
    # NULL backend = workspace filesystem, the original behaviour.
    storage_backend: Mapped[str | None] = mapped_column(String(20), nullable=True)
    storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # When the retention sweep may remove the stored object. NULL = keep
    # until the job is deleted, which is what workspace-filesystem artifacts
    # have always done.
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)

    job: Mapped["ExecutionJob"] = relationship("ExecutionJob", back_populates="artifacts")
