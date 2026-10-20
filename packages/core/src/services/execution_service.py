"""
Execution Orchestrator — job lifecycle CRUD + admission control.

TRINDATRON CORE -> create_job() -> ExecutionJob row (status="queued")
                          |
                    ExecutionPolicy   <-  everything below this line moved
                    (admission: Usage/    to execution_worker.py — see that
                     Budget via           module's docstring for the full
                     adaptive_budget_     durable queue/lease/heartbeat/
                     service.py)          recovery model.

This module now does exactly what its name says and no more: validate a
job request, run admission control (capacity tier, capability kill
switch), persist the row, and answer read/list/cancel queries. It does
NOT execute anything and does NOT decide retries — see
execution_worker.py, which claims and runs jobs from a completely
separate poll loop, independent of any HTTP request's lifetime
(mission requirement: "O HTTP não deve ser responsável pela execução do
job"). This split is itself the fix for Execution Runtime V1's core
limitation: create_job() used to call `asyncio.create_task()` directly,
meaning the job's execution lived only in this ONE process's memory and
did not survive a restart. Now create_job() only ever writes one row.

Checkpoint / resume
---------------------
payload = {"steps": [{"command": "..."}, {"command": "..."}, ...]}
checkpoint = {"completed_step_indices": [0, 1], "step_results": {"0": "...", "1": "..."}}
On a retry triggered by a STEP failure, already-completed step indices are
skipped — a 5-step plan that fails on step 4 resumes at step 4, not step 1.
On a retry triggered by a SENTINEL failure, the checkpoint is reset to
empty instead — see execution_worker._release_for_retry()'s docstring.

Sentinel integration
----------------------
Runs sentinel_service.verify() after the last step completes IF
ExecutionJob.sentinel_criteria was supplied at creation — a job created
without criteria skips verification entirely.

Budget / adaptive consumption
--------------------------------
create_job() calls adaptive_budget_service.compute_capacity() +
priority_admitted() BEFORE persisting anything — a job whose priority isn't
admitted at the current capacity tier is refused with
CapacityRefusedError, never silently queued to run later. No TCU
reservation is made by this module itself (see models/execution.py's
module docstring) — reservation_id stays NULL unless the job's own work
calls into the Model Router through the ordinary entitlement
check_and_reserve/finalize_usage/cancel_reservation path (job_type="shell"
never does this; job_type="research" does, once per step, entirely inside
ModelRouterExecutionProvider — see execution/model_router_provider.py).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session as DBSession

from src.capability.classifier import Capability
from src.config import settings
from src.database import SessionLocal as _real_SessionLocal
from src.execution.agent_decision_provider import AgentDecisionExecutionProvider
from src.execution.docker_provider import DockerExecutionProvider
from src.execution.model_router_provider import ModelRouterExecutionProvider
from src.execution.presentation_provider import PresentationExecutionProvider
from src.execution.provider import ExecutionProvider
from src.models.execution import (
    EXECUTION_LIVE_STATUSES,
    EXECUTION_PRIORITIES,
    ExecutionJob,
)
from src.observability.metrics import core_metrics
from src.services import adaptive_budget_service
from src.services.runtime_settings import is_capability_disabled

logger = logging.getLogger("trinity.execution")

_jobs_created_total = core_metrics.counter(
    "execution_jobs_created_total", "ExecutionJobs created, labeled by priority.",
)

# Overridable session factory — execution_worker.py's poll loop and every
# ExecutionProvider that needs its own DB session (ModelRouterExecutionProvider)
# run in a background asyncio.Task that outlives any single HTTP request, so
# neither can receive a session via FastAPI's Depends(get_db). Production
# always uses the real SessionLocal; tests monkeypatch this ONE name
# (execution_service._session_factory) via the execution_session_override
# fixture so every background write lands in the same isolated test
# database the test's own `db` fixture reads from.
_session_factory = _real_SessionLocal


def _new_session() -> DBSession:
    return _session_factory()


# Test-only escape hatch: create_job(provider=...) registers the override
# here, keyed by job_id; execution_worker._resolve_provider() checks it
# before building a real provider from job_type. Never populated in
# production (no route ever passes `provider=`). Popped by
# execution_worker._finish() once the job reaches a terminal status.
_PROVIDER_OVERRIDES: dict[str, ExecutionProvider] = {}


class CapacityRefusedError(Exception):
    """Raised by create_job() when the current adaptive-capacity tier does
    not admit this job's priority. `tier` and `capacity_remaining_pct` are
    for logging/response — never construct a message containing raw TCU."""

    def __init__(self, tier: str, capacity_remaining_pct: float, priority: str) -> None:
        self.tier = tier
        self.capacity_remaining_pct = capacity_remaining_pct
        self.priority = priority
        super().__init__(
            f"Execution refused: priority={priority!r} not admitted at capacity tier {tier!r}"
        )


class JobNotFound(Exception):
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"ExecutionJob {job_id!r} not found")


class CapabilityDisabledError(Exception):
    """Raised by create_job() for job_type="research" when an operator has
    disabled Capability.RESEARCH via the Etapa 5H kill switch
    (runtime_settings.py). Mirrors sessions.py's own
    _reject_if_capability_disabled() guard for /chat and /agent — without
    this check, the same kill switch could be silently bypassed by routing
    a research request through the Execution Runtime instead of /chat."""

    def __init__(self, capability: str) -> None:
        self.capability = capability
        super().__init__(f"Capability {capability!r} is temporarily disabled")


# job_type values the orchestrator will admit. Payload SHAPE validation
# below is intentionally identical for every job_type: {"steps": [{"command":
# "..."}, ...]} — for "shell" each step's command is a shell command string;
# for "research" it's a research prompt string; for "agent_decide" (Tier 2
# — LLM DECIDE, see agent_decision.py) it's the DECIDE user-message text
# (see execution/model_router_provider.py's module docstring for why
# reusing the same key, rather than adding a job_type-specific payload
# shape, keeps execution_worker's step loop itself free of any job_type
# branch). "agent_decide" is created ONLY by agent_schedule_service.py's
# own Tier-2 WAKE cycle — no HTTP route ever accepts it directly (see
# schemas/execution.py's CreateExecutionJobRequest), so a user can never
# request an LLM DECIDE job for themselves outside the persistent-agent
# cycle it exists for.
_JOB_TYPES: tuple[str, ...] = ("shell", "research", "agent_decide", "presentation")


def build_default_provider() -> ExecutionProvider:
    """The provider for job_type="shell" — DockerExecutionProvider, the only
    real provider for pure tool/shell execution in this codebase (see
    docker_provider.py's docstring for why DesktopSandbox isn't a fit). A
    provider that is merely unavailable right now (Docker not running) is a
    runtime condition execution_worker surfaces as
    ExecutionProviderUnavailable — never silently swapped for a fake."""
    return DockerExecutionProvider(settings.agent_workspace_root)


def build_research_provider(
    *, user_id: str, plan: str, user_timezone: str | None, session_id: str | None,
) -> ExecutionProvider:
    """The provider for job_type="research" — routes each step's prompt
    through the EXISTING Model Router (see model_router_provider.py's
    module docstring). Called fresh by execution_worker._resolve_provider()
    for EVERY attempt (never cached across attempts/processes) because
    plan/user_timezone can legitimately change between when a job was
    created and when a — possibly much later, possibly different-process —
    worker actually claims it; re-resolving from the current User row each
    time is more correct than trusting a create-time snapshot."""
    return ModelRouterExecutionProvider(
        settings.agent_workspace_root,
        user_id=user_id, plan=plan, user_timezone=user_timezone, session_id=session_id,
        session_factory=_new_session,
    )


def build_presentation_provider(
    *, user_id: str, plan: str, user_timezone: str | None, session_id: str | None,
    options: dict | None = None,
) -> ExecutionProvider:
    """The provider for job_type="presentation". Same re-resolve-fresh-per-
    attempt rationale as build_research_provider() above, with one addition:
    `options` (slide count, language, export format) comes from the job's own
    payload rather than the User row, because it describes what was asked for
    and must not drift between attempts the way plan legitimately can."""
    return PresentationExecutionProvider(
        settings.agent_workspace_root,
        user_id=user_id, plan=plan, user_timezone=user_timezone, session_id=session_id,
        session_factory=_new_session, options=options,
    )


def build_decision_provider(
    *, user_id: str, plan: str, user_timezone: str | None, session_id: str | None,
) -> ExecutionProvider:
    """The provider for job_type="agent_decide" (Tier 2 — LLM DECIDE, see
    agent_decision.py). Same re-resolve-fresh-per-attempt rationale as
    build_research_provider() above."""
    return AgentDecisionExecutionProvider(
        settings.agent_workspace_root,
        user_id=user_id, plan=plan, user_timezone=user_timezone, session_id=session_id,
        session_factory=_new_session,
    )


def _validate_payload(job_type: str, payload: dict) -> list[dict]:
    if job_type not in _JOB_TYPES:
        raise ValueError(f"Unsupported job_type: {job_type!r}; must be one of {_JOB_TYPES}")
    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("payload.steps must be a non-empty list")
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or not step.get("command"):
            raise ValueError(f"payload.steps[{i}] must be an object with a non-empty 'command'")
    return steps


def create_job(
    db: DBSession,
    *,
    user_id: str,
    plan: str,
    user_timezone: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
    project_id: str | None = None,
    job_type: str = "shell",
    payload: dict,
    priority: str = "normal",
    timeout_seconds: int = 300,
    max_retries: int = 2,
    sentinel_criteria: dict | None = None,
    provider: ExecutionProvider | None = None,
) -> ExecutionJob:
    """Validate, admit, and PERSIST one ExecutionJob with status="queued".
    Returns immediately once the row is committed — the caller (an HTTP
    route, or AgentSchedule's own firing) can return job_id to the client
    right away. Execution is entirely the durable worker's job (see
    execution_worker.py) — this function never spawns a task, never
    touches asyncio.

    `provider`, when given, is a TEST-ONLY override (registered in
    _PROVIDER_OVERRIDES) — no production caller ever passes it; the
    worker always resolves a real provider from job_type otherwise.
    """
    if priority not in EXECUTION_PRIORITIES:
        raise ValueError(f"Invalid priority {priority!r}; must be one of {EXECUTION_PRIORITIES}")

    _validate_payload(job_type, payload)  # fail fast, before any DB write

    if job_type in ("research", "agent_decide") and is_capability_disabled(db, Capability.RESEARCH.value):
        # Same Etapa 5H kill switch /chat and /agent already respect (see
        # sessions.py's _reject_if_capability_disabled) — checked here too
        # so disabling Capability.RESEARCH can't be bypassed by routing the
        # same request through the Execution Runtime instead. "agent_decide"
        # reuses Capability.RESEARCH's kill switch too (same TaskType.CHAT
        # shape, same underlying capability being disabled — see
        # agent_decision_provider.py's own docstring) rather than inventing
        # a second, separately-toggled switch for what is operationally the
        # same lever an operator would reach for.
        logger.warning("execution: capability disabled | user=%s job_type=%s", user_id, job_type)
        raise CapabilityDisabledError("research")

    capacity = adaptive_budget_service.compute_capacity(
        db, user_id=user_id, plan=plan, user_timezone=user_timezone,
    )
    if not adaptive_budget_service.priority_admitted(capacity.tier, priority):
        logger.warning(
            "execution: capacity refused | user=%s priority=%s tier=%s capacity_remaining_pct=%.1f",
            user_id, priority, capacity.tier, capacity.capacity_remaining_pct,
        )
        raise CapacityRefusedError(capacity.tier, capacity.capacity_remaining_pct, priority)

    if provider is not None:
        provider_name = provider.provider_name
    elif job_type == "research":
        provider_name = "model_router"
    elif job_type == "agent_decide":
        provider_name = "agent_decision"
    elif job_type == "presentation":
        provider_name = "presentation"
    else:
        provider_name = "docker"

    job = ExecutionJob(
        user_id=user_id,
        agent_id=agent_id,
        session_id=session_id,
        project_id=project_id,
        job_type=job_type,
        provider_name=provider_name,
        priority=priority,
        payload=payload,
        status="queued",
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        sentinel_criteria=sentinel_criteria,
        checkpoint={"completed_step_indices": [], "step_results": {}},
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    if provider is not None:
        _PROVIDER_OVERRIDES[job.id] = provider

    _jobs_created_total.inc(priority=priority)
    logger.info("execution: job created | id=%s user=%s priority=%s tier=%s", job.id, user_id, priority, capacity.tier)

    return job


def get_job(db: DBSession, job_id: str, user_id: str) -> ExecutionJob | None:
    return db.query(ExecutionJob).filter(ExecutionJob.id == job_id, ExecutionJob.user_id == user_id).first()


def list_jobs(db: DBSession, user_id: str, *, status: str | None = None, limit: int = 50) -> list[ExecutionJob]:
    q = db.query(ExecutionJob).filter(ExecutionJob.user_id == user_id)
    if status is not None:
        q = q.filter(ExecutionJob.status == status)
    return q.order_by(ExecutionJob.created_at.desc()).limit(limit).all()


def count_active_jobs(db: DBSession, user_id: str) -> int:
    """"Agentes ativos" for the user-facing capacity view — counts jobs in
    a live (non-terminal) status. See models/execution.py's
    EXECUTION_LIVE_STATUSES."""
    return (
        db.query(ExecutionJob)
        .filter(ExecutionJob.user_id == user_id, ExecutionJob.status.in_(EXECUTION_LIVE_STATUSES))
        .count()
    )


def cancel_job(db: DBSession, job_id: str, user_id: str) -> ExecutionJob:
    """Request cancellation. Sets cancel_requested (durable — checked by
    whichever worker/process is running this job at its next step
    boundary, regardless of who that is) AND, best-effort, interrupts the
    live asyncio.Task immediately if THIS process happens to be the one
    running it (see execution_worker.try_interrupt_local()). Lazy import
    to avoid a module-load-time circular dependency: execution_worker.py
    imports FROM this module (build_default_provider, _new_session,
    _PROVIDER_OVERRIDES, ...); this function only needs execution_worker
    at call time, long after both modules have finished importing."""
    job = get_job(db, job_id, user_id)
    if job is None:
        raise JobNotFound(job_id)
    if job.status in ("completed", "failed", "cancelled", "expired", "destroyed"):
        return job  # idempotent — already terminal, nothing to cancel
    job.cancel_requested = True
    db.commit()
    db.refresh(job)

    from src.services import execution_worker
    execution_worker.try_interrupt_local(job_id)
    return job


def expire_overdue_jobs(db: DBSession, *, now: datetime | None = None) -> int:
    """Sweep for jobs whose TOTAL wall-clock budget across all attempts has
    elapsed — a different concept from a single worker's lease (see
    execution_worker.reclaim_expired_leases()): this bounds how long a job
    may keep being retried in total, lease ownership aside. Intended to be
    called periodically (see execution_worker.run_recovery_loop())."""
    now = now or datetime.now(timezone.utc)
    overdue = (
        db.query(ExecutionJob)
        .filter(
            ExecutionJob.status.in_(EXECUTION_LIVE_STATUSES),
            ExecutionJob.started_at.isnot(None),
        )
        .all()
    )
    count = 0
    for job in overdue:
        deadline = job.started_at + timedelta(seconds=job.timeout_seconds * (job.max_retries + 2))
        if now > deadline:
            job.status = "expired"
            job.error = "Execution exceeded its maximum allotted wall-clock time"
            job.completed_at = now
            job.worker_id = None
            job.lease_expires_at = None
            count += 1
    if count:
        db.commit()
    return count
