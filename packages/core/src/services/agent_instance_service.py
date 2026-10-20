"""AgentInstance service — Persistent Agent Instance V1.

AgentInstance does NOT execute anything itself. Every function here
either manages the instance's own identity/lifecycle row, or — for
manual_run() — delegates to the EXACT SAME execution_service.create_job()
every other caller (the HTTP execution route, AgentSchedule's own ACT
step) already uses. There is no second execution path, no direct
provider/ModelRouter call, no parallel queue:

    User -> AgentInstance -> AgentSchedule -> ExecutionJob -> Worker -> Result

Ownership
----------
Every read/write here is scoped by (id, user_id) — same 404-not-403
convention already established throughout this codebase (billing.py,
execution.py) so a wrong-user request can't even distinguish "doesn't
exist" from "exists but isn't yours".

No billing surface
--------------------
create/list/get/update/pause/resume are all pure metadata operations —
none of them call check_and_reserve/finalize_usage/cancel_reservation or
touch UsageRecord. manual_run() creates an ExecutionJob via
execution_service.create_job() exactly like a scheduled ACT would — that
job's own billing (if job_type="research") follows the EXISTING,
unmodified entitlement path; AgentInstance itself never becomes a party
to it.

Health reconciliation lives in agent_schedule_service.py, not here
------------------------------------------------------------------------
reconcile_health_from_job() below is a small, pure(-ish) function this
module owns (it only knows how to interpret an ExecutionJob's terminal
status against an AgentInstance's failure_count/status) — but it's
CALLED by agent_schedule_service._fire_due_schedules(), lazily, right
before that schedule's next ACT (see that module's own docstring for
exactly when and why). No new poller, no new worker: the existing poll
loop is the only thing that ever calls this.

Metrics
-------
core_metrics only has Counter/Histogram primitives (see
src/observability/metrics.py — no Gauge exists anywhere in this
codebase, and adding one is out of scope for this phase). The mission's
requested "agent_instances_created/active/paused/failed" are therefore
modeled as transition counters, not live gauges: `_created_total` fires
once per create_agent_instance() call; `_status_transitions_total`
(labeled `to_status`) fires on every status change reaching
active/paused/error (create -> active, resume -> active, pause ->
paused, reconcile_health_from_job crossing the failure threshold ->
error) — summing a label's count over a window answers "how many
instances became X", the counter-only equivalent of a gauge snapshot.
`_manual_runs_total` fires once per successful manual_run().
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session as DBSession

from src.config import settings
from src.models.agent_definition import AgentDefinition
from src.models.agent_instance import AGENT_INSTANCE_STATUSES, AgentInstance
from src.models.agent_schedule import AgentSchedule
from src.models.execution import EXECUTION_TERMINAL_STATUSES, ExecutionJob
from src.observability.metrics import core_metrics
from src.services import execution_service

_instances_created_total = core_metrics.counter(
    "agent_instances_created_total", "AgentInstance rows created.",
)
_instances_status_transitions_total = core_metrics.counter(
    "agent_instances_status_transitions_total", "AgentInstance status transitions, labeled by to_status (active|paused|error).",
)
_manual_runs_total = core_metrics.counter(
    "agent_instance_manual_runs_total", "Manual runs (POST /agent-instances/{id}/run) that produced an ExecutionJob.",
)


class AgentInstanceNotFound(Exception):
    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id
        super().__init__(f"AgentInstance {instance_id!r} not found")


class NoScheduleForInstance(Exception):
    """manual_run() found no AgentSchedule to source an action from."""

    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id
        super().__init__(f"AgentInstance {instance_id!r} has no schedule to run")


class InstanceNotRunnable(Exception):
    """manual_run() refused: the instance is paused/disabled/error."""

    def __init__(self, instance_id: str, status: str) -> None:
        self.instance_id = instance_id
        self.status = status
        super().__init__(f"AgentInstance {instance_id!r} is {status!r}, not runnable")


class InstanceJobAlreadyActive(Exception):
    """manual_run() refused: a non-terminal ExecutionJob already exists
    for this instance's schedule — same at-most-one-active-job-per-schedule
    invariant agent_schedule_service.py's own WAKE cycle enforces, reused
    here rather than a second concurrency rule."""

    def __init__(self, instance_id: str, job_id: str) -> None:
        self.instance_id = instance_id
        self.job_id = job_id
        super().__init__(f"AgentInstance {instance_id!r} already has an active job ({job_id!r})")


def _owned(db: DBSession, instance_id: str, user_id: str) -> AgentInstance | None:
    return (
        db.query(AgentInstance)
        .filter(AgentInstance.id == instance_id, AgentInstance.user_id == user_id)
        .first()
    )


def create_agent_instance(
    db: DBSession, *, user_id: str, agent_definition_id: str, name: str, project_id: str | None = None,
) -> AgentInstance:
    definition = (
        db.query(AgentDefinition)
        .filter(AgentDefinition.id == agent_definition_id, AgentDefinition.user_id == user_id)
        .first()
    )
    if definition is None:
        raise ValueError(f"AgentDefinition {agent_definition_id!r} not found for this user")

    if project_id is not None:
        from src.models.project import Project
        project = db.query(Project).filter(Project.id == project_id, Project.user_id == user_id).first()
        if project is None:
            raise ValueError(f"Project {project_id!r} not found for this user")

    instance = AgentInstance(
        user_id=user_id, agent_definition_id=agent_definition_id, project_id=project_id,
        name=name, status="active",
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    _instances_created_total.inc()
    _instances_status_transitions_total.inc(to_status="active")
    return instance


def get_agent_instance(db: DBSession, instance_id: str, user_id: str) -> AgentInstance | None:
    return _owned(db, instance_id, user_id)


def list_agent_instances(db: DBSession, user_id: str) -> list[AgentInstance]:
    return (
        db.query(AgentInstance)
        .filter(AgentInstance.user_id == user_id)
        .order_by(AgentInstance.created_at.desc())
        .all()
    )


def count_active_instances(db: DBSession, user_id: str) -> int:
    """The CORRECT semantics for "active_agents" in GET /users/me/capacity
    — AgentInstance.status == "active", NOT a count of currently-running
    ExecutionJobs (an active instance idling between wakes still counts;
    a raw ExecutionJob with no owning instance never did and still
    doesn't)."""
    return db.query(AgentInstance).filter(AgentInstance.user_id == user_id, AgentInstance.status == "active").count()


def update_agent_instance(
    db: DBSession, instance_id: str, user_id: str, *, name: str | None = None, project_id: str | None = ...,
) -> AgentInstance | None:
    """Only `name`/`project_id` are settable here — NOT `status` (goes
    through pause_agent_instance()/resume_agent_instance() so the state
    machine's own invariants stay enforced in one place), NOT `user_id`/
    ownership, NOT anything execution- or billing-related (none of those
    are even columns on this model — see agent_instance.py's docstring).
    `project_id=...` (Ellipsis sentinel) means "leave unchanged"; pass
    `project_id=None` explicitly to clear it."""
    instance = _owned(db, instance_id, user_id)
    if instance is None:
        return None
    if name is not None:
        instance.name = name
    if project_id is not ...:
        if project_id is not None:
            from src.models.project import Project
            project = db.query(Project).filter(Project.id == project_id, Project.user_id == user_id).first()
            if project is None:
                raise ValueError(f"Project {project_id!r} not found for this user")
        instance.project_id = project_id
    db.commit()
    db.refresh(instance)
    return instance


def pause_agent_instance(db: DBSession, instance_id: str, user_id: str) -> AgentInstance | None:
    """Idempotent — pausing an already-paused/disabled instance is a
    no-op success, not an error (matches deactivate_schedule()'s own
    idempotent-safe contract). Does NOT cancel an in-flight ExecutionJob
    — the mission is explicit that pause must not invent a new
    cancellation mechanism; execution_service.cancel_job() already exists
    for that, as its own separate, explicit action."""
    instance = _owned(db, instance_id, user_id)
    if instance is None:
        return None
    if instance.status == "active":
        instance.status = "paused"
        db.commit()
        db.refresh(instance)
        _instances_status_transitions_total.inc(to_status="paused")
    return instance


def resume_agent_instance(db: DBSession, instance_id: str, user_id: str) -> AgentInstance | None:
    """Only resumes from "paused" — an "error" instance needs an operator/
    user to have addressed whatever caused it (still just flips status;
    no other side effect), a "disabled" instance stays disabled (no
    dedicated disable endpoint exists in this phase — see
    agent_instance.py's docstring). Never touches any AgentSchedule row:
    each schedule's own next_run_at kept advancing on its absolute clock
    the whole time (see agent_schedule_service.py), so resuming just
    means the NEXT already-scheduled wake is allowed to proceed —
    deliberately not "fire immediately", which would risk exactly the
    "duplicar execução" the mission said to avoid."""
    instance = _owned(db, instance_id, user_id)
    if instance is None:
        return None
    if instance.status == "paused":
        instance.status = "active"
        db.commit()
        db.refresh(instance)
        _instances_status_transitions_total.inc(to_status="active")
    return instance


def _schedule_for_manual_run(db: DBSession, instance: AgentInstance) -> AgentSchedule | None:
    return (
        db.query(AgentSchedule)
        .filter(AgentSchedule.agent_instance_id == instance.id, AgentSchedule.is_active.is_(True))
        .order_by(AgentSchedule.created_at.desc())
        .first()
    )


def _instance_has_active_job(db: DBSession, instance: AgentInstance) -> bool:
    if instance.last_execution_job_id is None:
        return False
    job = db.query(ExecutionJob).filter(ExecutionJob.id == instance.last_execution_job_id).first()
    return job is not None and job.status not in EXECUTION_TERMINAL_STATUSES


def manual_run(db: DBSession, *, instance_id: str, user_id: str) -> ExecutionJob:
    """POST /agent-instances/{id}/run — does NOT execute anything itself.
    Resolves the instance's most-recently-created active AgentSchedule
    for its action config (via agent_schedule_service._resolve_act_spec(),
    the SAME function a real WAKE cycle uses — no second
    payload-resolution path) and calls execution_service.create_job()
    exactly once. Skips CHECK/DECIDE deliberately (a manual run IS the
    user's own explicit decision to act now — gating it behind "did the
    external content also change" would defeat the point of a manual
    trigger) but still enforces the SAME capacity gate and the SAME
    at-most-one-active-job-per-schedule invariant as a real WAKE, so a
    manual run can never bypass either."""
    from src.services.agent_schedule_service import _resolve_act_spec  # local import: avoids a module-load cycle (agent_schedule_service imports execution_service; this keeps agent_instance_service -> agent_schedule_service one-directional and lazy)
    from src.models.user import User

    instance = _owned(db, instance_id, user_id)
    if instance is None:
        raise AgentInstanceNotFound(instance_id)
    if instance.status != "active":
        raise InstanceNotRunnable(instance_id, instance.status)

    schedule = _schedule_for_manual_run(db, instance)
    if schedule is None:
        raise NoScheduleForInstance(instance_id)

    if _instance_has_active_job(db, instance):
        raise InstanceJobAlreadyActive(instance_id, instance.last_execution_job_id)

    user = db.query(User).filter(User.id == user_id).first()
    agent = db.query(AgentDefinition).filter(AgentDefinition.id == schedule.agent_id).first()
    effective_priority = schedule.priority if schedule.priority is not None else agent.priority
    act_job_type, act_payload = _resolve_act_spec(schedule)

    # execution_service.CapacityRefusedError intentionally propagates
    # uncached — the route maps it to 429, identical to a real WAKE's own
    # capacity refusal; no special-casing for "manual".
    job = execution_service.create_job(
        db, user_id=user_id, plan=user.plan, user_timezone=user.timezone,
        agent_id=schedule.agent_id, job_type=act_job_type, payload=act_payload,
        priority=effective_priority, timeout_seconds=agent.execution_timeout_seconds,
    )

    instance.last_execution_job_id = job.id
    instance.last_run_at = datetime.now(timezone.utc)
    db.commit()
    _manual_runs_total.inc()
    return job


def reconcile_health_from_job(instance: AgentInstance, job: ExecutionJob | None) -> None:
    """Interprets ONE terminal ExecutionJob's outcome for the owning
    AgentInstance's health. Never called for a non-terminal job (a
    "retrying" job is not a failure — see module docstring). A
    "completed" job resets failure_count to 0 (recovery). A "failed"/
    "cancelled"/"expired" job increments failure_count and records
    last_error; once failure_count reaches
    settings.agent_instance_failure_threshold, status becomes "error" —
    but ONLY if the instance is currently "active" (never overrides an
    already-"paused"/"disabled" instance's own status). Caller
    (agent_schedule_service._fire_due_schedules()) is responsible for
    calling db.commit() — this function only mutates the ORM object."""
    if job is None or job.status not in EXECUTION_TERMINAL_STATUSES:
        return
    if job.status == "completed":
        instance.failure_count = 0
        instance.last_error = None
        return
    instance.failure_count += 1
    instance.last_error = (job.error or job.status)[:500]
    if instance.status == "active" and instance.failure_count >= settings.agent_instance_failure_threshold:
        instance.status = "error"
        _instances_status_transitions_total.inc(to_status="error")


def derive_health(instance: AgentInstance) -> str:
    """Read-only, derived-only — see agent_instance.py's docstring on why
    no second health worker exists. "healthy": no recorded failures.
    "degraded": at least one failure but below the auto-error threshold.
    "failed": status == "error" (already crossed the threshold)."""
    if instance.status == "error":
        return "failed"
    if instance.failure_count > 0:
        return "degraded"
    return "healthy"
