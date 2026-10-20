"""AgentSchedule service — the trigger source for persistent agents
(Persistent Agent Runtime V1, Tier 1). See models/agent_schedule.py's
module docstring for why this is NOT a duplicate of agent_scheduler.py.

WAKE -> CHECK -> DECIDE -> ACT -> SLEEP (Tier 1 — CHECK/DECIDE non-LLM)
------------------------------------------------------------------------
_fire_due_schedules() is the whole cycle, one schedule at a time:
  WAKE    — schedule is due (next_run_at <= now); next_run_at is advanced
            to `now + interval_seconds` immediately, before anything
            else, preserving the existing "absolute clock, never
            extended by how long this cycle takes" rule unchanged.
  CHECK   — src.services.agent_check_source.CheckSource.check() — pure,
            non-LLM, never reserves TCU (see that module's docstring for
            why it structurally cannot). Result persisted onto
            AgentSchedule.last_check_signature every cycle, unconditionally.
  DECIDE  — adaptive_budget_service.should_skip_llm(has_state_changed=...)
            — also pure, non-LLM. No change detected -> last_decision=
            "skipped", no ExecutionJob, SLEEP.
  ACT     — only reached when DECIDE says to proceed AND the concurrency
            invariant below allows it. Exactly one execution_service.
            create_job() call — same orchestrator entry point the HTTP
            route uses, unchanged. Tier 2 (an LLM-driven DECIDE) is
            explicitly out of scope here.
  SLEEP   — last_decision/last_decision_at persisted regardless of which
            branch was taken; next_run_at was already advanced at WAKE.

Concurrency invariant: at most one ACTIVE ExecutionJob per schedule
----------------------------------------------------------------------
Before creating an ACT job, _schedule_has_active_job() checks whether
`schedule.last_run_job_id` (already existed — this reuses it, no new
column or query pattern) still points at a non-terminal job. If so, ACT
is skipped this cycle (last_decision="skipped", next_run_at unaffected,
no new UsageRecord is possible since no job is created). This is a plain
per-schedule check via the existing FK, not a distributed lock — see
docs/EXECUTION_RUNTIME.md's Persistent Agent Runtime section for why
that's sufficient at this phase's scale.

AgentDefinition defaults (mission gap this phase also closes)
-------------------------------------------------------------------
AgentDefinition.priority/execution_timeout_seconds existed since
Execution Runtime V1's migration 0018 but were never read by this file.
ACT now resolves: priority = schedule.priority if not None else
agent.priority (schedule.priority is nullable specifically to make "no
override" expressible — see models/agent_schedule.py); timeout_seconds =
agent.execution_timeout_seconds always (AgentSchedule has no
timeout-override field of its own, so there is nothing to conditionally
prefer over the agent's own setting).

job_payload shapes — three, chosen by which keys are present
----------------------------------------------------------------------
  Legacy (every schedule from before Tier 1, and any schedule that still
  just wants "run this shell plan on an interval, react to my own
  payload changing"): `job_payload` IS the ExecutionJob payload directly
  — `{"steps": [...]}`. job_type is "shell". CHECK is
  PayloadSignatureCheckSource. Completely unchanged.

  Tier 1 — CHECK + static ACT (the first real use case — HTTP
  monitoring): `job_payload = {"check": {"type": "http", "url": ..., ...}, "action": {"job_type": "research"|"shell", "payload": {"steps": [...]}}}`.
  `check` picks the CheckSource (agent_check_source.build_check_source_for_schedule());
  `action.job_type`/`action.payload` are passed to execution_service.create_job()
  UNCHANGED in shape — no translation layer, so create_job()'s own
  `_validate_payload()` is the ONLY payload validation that ever runs.

  Tier 2 — CHECK + LLM DECIDE (see "Tier 2" section below):
  `job_payload = {"check": {...}, "decide": {}}` — `decide`'s mere
  presence (value currently unused, reserved for future per-schedule
  overrides) opts this schedule into an LLM-driven DECIDE instead of
  Tier 1's static `action`. Mutually exclusive with `action` — see
  create_schedule()'s validation.

Tier 2 — LLM DECIDE (Persistent Agent Runtime — LLM Decision Runtime)
--------------------------------------------------------------------------
WAKE -> CHECK -> [create a DECIDE ExecutionJob] -> SLEEP, then, on a LATER
wake once that job is terminal: [consume it] -> ACT ExecutionJob -> SLEEP.
The scheduler NEVER blocks waiting for the LLM — see
_process_llm_decide_schedule()/_consume_pending_decision() below.

DECIDE is a REAL, durable, billed ExecutionJob (job_type="agent_decide",
src/execution/agent_decision_provider.py) — claimed/leased/retried/
recovered by the EXISTING Durable Execution Worker, no new worker, no
new billing path (see that provider's own docstring for the exact
check_and_reserve/finalize_usage/cancel_reservation sequence, reused
verbatim). Its structured output is validated by
agent_decision.parse_decision() — malformed output is a DECIDE job
FAILURE, never an implicit ACT.

Crash-safe, exactly-at-most-once consumption
------------------------------------------------
`AgentSchedule.pending_decision_job_id` tracks the outstanding DECIDE
job. Consuming a terminal DECIDE job (deciding whether to create an ACT)
happens through an ATOMIC compare-and-swap UPDATE
(`pending_decision_job_id = NULL WHERE pending_decision_job_id = <that
job's id>`) — the SAME claim TECHNIQUE execution_worker.py's own
`_try_claim()` uses (reused, not reinvented) applied one layer up, at the
schedule. Only the caller that wins the CAS proceeds to create the ACT
job; every other concurrent/duplicate/restarted attempt sees rowcount=0
and does nothing. This guarantees "no máximo um ACT" (at most one ACT per
decision) even across a process restart, two overlapping poll cycles, or
two workers — see _consume_pending_decision()'s own docstring for the
full crash-window analysis. The one accepted failure mode is FAIL-CLOSED,
never fail-open: an exceptionally rare crash between the CAS commit and a
successful create_job() call can silently drop an "act" decision (zero
ACTs), but can NEVER produce two.

Allowed ACT actions from a DECIDE — deliberately narrow
------------------------------------------------------------
Only `job_type="research"` (see agent_decision.ALLOWED_DECIDE_ACT_JOB_TYPES)
— the LLM can never choose "shell" or any other job_type. `user_id`/
`agent_id`/`session_id`/`project_id` for the ACT job are ALWAYS taken from
the schedule/instance (server-controlled), NEVER from the LLM's output —
structurally, the model has no way to change ownership or reach a
workspace it doesn't already have.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session as DBSession

from src.database import SessionLocal
from src.models.agent_definition import AgentDefinition
from src.models.agent_instance import AgentInstance
from src.models.agent_schedule import AgentSchedule
from src.models.execution import EXECUTION_TERMINAL_STATUSES, ExecutionJob
from src.models.user import User
from src.observability.metrics import core_metrics
from src.services import agent_decision, agent_instance_service, execution_service
from src.services.adaptive_budget_service import should_skip_llm
from src.services.agent_check_source import CheckSource, build_check_source_for_schedule

logger = logging.getLogger("trinity.agent_schedule")

_schedules_fired_total = core_metrics.counter(
    "agent_schedule_fired_total", "AgentSchedule firings that created an ExecutionJob.",
)
_schedules_skipped_total = core_metrics.counter(
    "agent_schedule_skipped_total", "AgentSchedule firings skipped, labeled by reason.",
)
# CHECK outcomes, distinct from DECIDE ("skipped"/"acted"/"capacity_refused"
# above) — lets observability tell "nothing changed" apart from "the check
# itself couldn't complete" (timeout/connection/invalid URL), which
# _schedules_skipped_total's reason="no_change_detected" alone conflates.
_check_results_total = core_metrics.counter(
    "agent_schedule_check_result_total", "CHECK outcomes, labeled by result (changed|unchanged|error).",
)

# Tier 2 — LLM DECIDE. Deliberately separate from the Tier-1 counters
# above (which stay byte-for-byte reusable by a Tier-1 schedule) so
# "how many DECIDE ExecutionJobs did we create/act-on/skip/fail" is
# readable without conflating it with the non-LLM CHECK/DECIDE counts.
_agent_decisions_created_total = core_metrics.counter(
    "agent_decisions_created_total", "DECIDE ExecutionJobs created (Tier 2).",
)
_agent_decisions_act_total = core_metrics.counter(
    "agent_decisions_act_total", "Consumed DECIDE jobs that produced an ACT ExecutionJob.",
)
_agent_decisions_skip_total = core_metrics.counter(
    "agent_decisions_skip_total", "Consumed DECIDE jobs where the model chose to skip.",
)
_agent_decisions_failed_total = core_metrics.counter(
    "agent_decisions_failed_total", "DECIDE jobs that could not produce an ACT, labeled by reason.",
)
_agent_decision_latency_ms = core_metrics.histogram(
    "agent_decision_latency_ms", "Milliseconds between a DECIDE job's creation and its consumption.",
)

# How often the poll loop wakes up to check for due schedules. Coarser than
# any individual schedule's own interval (minimum 60s, see
# CreateAgentScheduleRequest) is fine — a schedule fires up to POLL_INTERVAL_SECONDS
# late, never early, never twice for the same due window (next_run_at is
# advanced past `now` before the job is created).
POLL_INTERVAL_SECONDS = 30


def create_schedule(
    db: DBSession, *, user_id: str, agent_id: str, interval_seconds: int,
    job_payload: dict, priority: str | None = None, agent_instance_id: str | None = None,
) -> AgentSchedule:
    """priority=None (the new default — was "low") means "no override,
    inherit AgentDefinition.priority at ACT time" — see module docstring.

    agent_instance_id=None (the default) is fully legitimate — a schedule
    with no owning AgentInstance behaves exactly as it always has (no
    instance-status gate, no health reconciliation — see
    _fire_due_schedules()). When given, it's validated to belong to the
    SAME user and to reference the SAME agent_definition_id as `agent_id`
    — a schedule can never be bound to someone else's instance, or to an
    instance of a DIFFERENT AgentDefinition than the one it fires."""
    agent = db.query(AgentDefinition).filter(
        AgentDefinition.id == agent_id, AgentDefinition.user_id == user_id,
    ).first()
    if agent is None:
        raise ValueError(f"AgentDefinition {agent_id!r} not found for this user")

    if agent_instance_id is not None:
        instance = db.query(AgentInstance).filter(
            AgentInstance.id == agent_instance_id, AgentInstance.user_id == user_id,
        ).first()
        if instance is None:
            raise ValueError(f"AgentInstance {agent_instance_id!r} not found for this user")
        if instance.agent_definition_id != agent_id:
            raise ValueError("agent_instance_id does not belong to the given agent_id's AgentDefinition")

    if isinstance(job_payload, dict) and "check" in job_payload and "action" in job_payload and "decide" in job_payload:
        # Tier 1's static "action" and Tier 2's LLM "decide" are mutually
        # exclusive DECIDE strategies for the SAME schedule — see module
        # docstring's three-shape dispatch. A schedule with both would be
        # ambiguous about which DECIDE governs it; rejected outright rather
        # than silently picking one.
        raise ValueError('job_payload cannot contain both "action" and "decide" — pick exactly one DECIDE strategy')

    schedule = AgentSchedule(
        user_id=user_id,
        agent_id=agent_id,
        agent_instance_id=agent_instance_id,
        interval_seconds=interval_seconds,
        job_payload=job_payload,
        priority=priority,
        next_run_at=datetime.now(timezone.utc) + timedelta(seconds=interval_seconds),
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


def list_schedules(db: DBSession, user_id: str) -> list[AgentSchedule]:
    return db.query(AgentSchedule).filter(AgentSchedule.user_id == user_id).order_by(AgentSchedule.created_at.desc()).all()


def deactivate_schedule(db: DBSession, schedule_id: str, user_id: str) -> AgentSchedule | None:
    schedule = db.query(AgentSchedule).filter(
        AgentSchedule.id == schedule_id, AgentSchedule.user_id == user_id,
    ).first()
    if schedule is None:
        return None
    schedule.is_active = False
    db.commit()
    db.refresh(schedule)
    return schedule


def _schedule_has_active_job(db: DBSession, schedule: AgentSchedule) -> bool:
    """At-most-one-active-job-per-schedule invariant. Reuses the existing
    last_run_job_id pointer — no new column, no new query pattern, no
    distributed lock (see module docstring)."""
    if schedule.last_run_job_id is None:
        return False
    last_job = db.query(ExecutionJob).filter(ExecutionJob.id == schedule.last_run_job_id).first()
    return last_job is not None and last_job.status not in EXECUTION_TERMINAL_STATUSES


def _resolve_act_spec(schedule: AgentSchedule) -> tuple[str, dict]:
    """(job_type, payload) for execution_service.create_job() — see
    module docstring's "job_payload shapes" section. Returns whatever it
    finds with NO validation of its own; a malformed action.payload
    surfaces via create_job()'s own _validate_payload() -> ValueError,
    the same "invalid_payload" skip path this file already had."""
    raw = schedule.job_payload if isinstance(schedule.job_payload, dict) else {}
    if "check" in raw:
        action = raw.get("action") or {}
        return action.get("job_type", "shell"), action.get("payload") or {}
    if _is_task_schedule(schedule):
        task = raw.get("task") or {}
        return task.get("job_type", "research"), task.get("payload") or {}
    return "shell", raw  # legacy shape — the whole payload IS the ExecutionJob payload


def _is_task_schedule(schedule: AgentSchedule) -> bool:
    """A scheduled task (src/services/scheduled_task_service.py): runs on its clock, with no CHECK/DECIDE."""
    raw = schedule.job_payload if isinstance(schedule.job_payload, dict) else {}
    return isinstance(raw.get("task"), dict) and "check" not in raw


def _check_result_metric_label(check_result) -> str:
    if check_result.reason in ("timeout", "connection_error") or (check_result.reason or "").startswith("invalid_url"):
        return "error"
    return "changed" if check_result.changed else "unchanged"


def _is_llm_decide_schedule(schedule: AgentSchedule) -> bool:
    """Tier 2 opt-in — see module docstring's job_payload shapes. A
    schedule with `pending_decision_job_id` already set is ALSO treated
    as Tier 2 for this cycle even if job_payload were somehow edited
    since (defensive — job_payload isn't mutable via any current route,
    but this keeps consumption correct regardless)."""
    raw = schedule.job_payload if isinstance(schedule.job_payload, dict) else {}
    return schedule.pending_decision_job_id is not None or ("check" in raw and "decide" in raw)


def _effective_priority(schedule: AgentSchedule, agent: AgentDefinition) -> str:
    return schedule.priority if schedule.priority is not None else agent.priority


async def _process_llm_decide_schedule(
    db: DBSession, schedule: AgentSchedule, agent: AgentDefinition, instance: AgentInstance | None,
    user: User, now: datetime, *, check_source: CheckSource | None,
) -> bool:
    """One WAKE cycle for a Tier-2 schedule. Returns True iff an ACT
    ExecutionJob was created THIS cycle (for the `fired` counter) — never
    True for merely creating a DECIDE job, which is not itself an action.

    Never blocks on the LLM: creating a DECIDE job only ever persists a
    queued row (execution_service.create_job(), unchanged); the durable
    worker executes it independently, on its own poll loop."""
    if schedule.pending_decision_job_id is not None:
        return await _consume_pending_decision(db, schedule, agent, instance, user, now)

    if _schedule_has_active_job(db, schedule):
        # An ACT from a PREVIOUS decision is still running — no new DECIDE
        # while it is, mirroring Tier 1's own at-most-one-active-job rule
        # one layer up (see module docstring's CONCURRENCY GATE).
        schedule.last_decision = "skipped"
        schedule.last_decision_at = now
        db.commit()
        _schedules_skipped_total.inc(reason="job_already_active")
        return False

    active_check_source = check_source or build_check_source_for_schedule(schedule)
    check_result = await active_check_source.check(schedule)
    previous_signature = schedule.last_check_signature
    schedule.last_check_signature = check_result.signature
    _check_results_total.inc(result=_check_result_metric_label(check_result))

    if should_skip_llm(has_state_changed=check_result.changed):
        # Item 10: never call the LLM if nothing changed — the SAME cheap,
        # non-LLM pre-filter Tier 1 uses, reused unmodified.
        schedule.last_decision = "skipped"
        schedule.last_decision_at = now
        db.commit()
        _schedules_skipped_total.inc(reason="no_change_detected")
        return False

    decide_payload = agent_decision.build_decide_payload(
        agent_name=agent.name, agent_objective=agent.description, agent_instructions=agent.system_instructions,
        instance_id=instance.id if instance else None, instance_status=instance.status if instance else None,
        instance_failure_count=instance.failure_count if instance else 0,
        check_changed=check_result.changed, check_reason=check_result.reason,
        previous_signature=previous_signature, new_signature=check_result.signature,
        observed_excerpt=check_result.observed_excerpt,
    )

    try:
        decide_job = execution_service.create_job(
            db, user_id=schedule.user_id, plan=user.plan, user_timezone=user.timezone,
            agent_id=schedule.agent_id, job_type="agent_decide", payload=decide_payload,
            priority=_effective_priority(schedule, agent), timeout_seconds=agent.execution_timeout_seconds,
        )
    except execution_service.CapacityRefusedError:
        # Item 7: capacity impeding DECIDE itself — record and move on,
        # never retry-loop creating DECIDE jobs.
        schedule.last_decision = "capacity_refused"
        schedule.last_decision_at = now
        db.commit()
        _schedules_skipped_total.inc(reason="capacity_refused")
        _agent_decisions_failed_total.inc(reason="capacity_refused_decide")
        return False

    schedule.pending_decision_job_id = decide_job.id
    schedule.last_decision = "decide_pending"
    schedule.last_decision_at = now
    db.commit()
    _agent_decisions_created_total.inc()
    logger.info("agent_schedule: DECIDE created | schedule=%s job=%s", schedule.id, decide_job.id)
    return False


async def _consume_pending_decision(
    db: DBSession, schedule: AgentSchedule, agent: AgentDefinition, instance: AgentInstance | None,
    user: User, now: datetime,
) -> bool:
    """Crash-safe, at-most-once consumption of a terminal DECIDE job —
    see module docstring's "Crash-safe, exactly-at-most-once consumption"
    section for the full analysis. Returns True iff this call created the
    ACT ExecutionJob."""
    decide_job = db.query(ExecutionJob).filter(ExecutionJob.id == schedule.pending_decision_job_id).first()
    if decide_job is None or decide_job.status not in EXECUTION_TERMINAL_STATUSES:
        # Still running/retrying (or — defensively — vanished): do
        # nothing this cycle. The EXISTING durable worker's own lease/
        # retry/reclaim machinery is what may still be recovering it (item
        # 9's "DECIDE worker morre antes de concluir" scenario) — no new
        # recovery code needed here.
        return False

    decide_job_id = decide_job.id

    # Atomic claim — the SAME compare-and-swap TECHNIQUE
    # execution_worker._try_claim() uses, applied here so no more than one
    # caller (this cycle, a concurrent cycle, a future second worker) can
    # ever consume the SAME decision. See module docstring.
    result = db.execute(
        update(AgentSchedule)
        .where(AgentSchedule.id == schedule.id, AgentSchedule.pending_decision_job_id == decide_job_id)
        .values(pending_decision_job_id=None)
    )
    db.commit()
    if result.rowcount != 1:
        # Someone else already consumed this exact decision — item 8
        # scenarios C/D/E. Nothing to do.
        return False
    schedule.pending_decision_job_id = None  # keep the in-memory object in sync with the CAS write above

    # ExecutionJob.created_at is plain DateTime(timezone=True) (not the
    # UTCDateTime type — see database.py's own docstring on why that
    # matters): naive on SQLite, aware on Postgres. Normalize defensively
    # before subtracting from `now` (always aware) to avoid
    # "can't subtract offset-naive and offset-aware datetimes".
    created_at = decide_job.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    _agent_decision_latency_ms.observe(max(0.0, (now - created_at).total_seconds() * 1000))

    if decide_job.status != "completed":
        schedule.last_decision = "decide_failed"
        schedule.last_decision_at = now
        db.commit()
        _agent_decisions_failed_total.inc(reason=decide_job.status)
        logger.info("agent_schedule: DECIDE not completed | schedule=%s job=%s status=%s", schedule.id, decide_job_id, decide_job.status)
        return False

    raw_output = (decide_job.result or {}).get("output") if isinstance(decide_job.result, dict) else None
    decision = agent_decision.parse_decision(raw_output)
    if decision is None:
        # Defense in depth — agent_decision_provider.py already validates
        # before marking the step successful; re-validated here so this
        # function never trusts a stored result blindly (mission item 2).
        schedule.last_decision = "decide_invalid"
        schedule.last_decision_at = now
        db.commit()
        _agent_decisions_failed_total.inc(reason="invalid_json")
        logger.warning("agent_schedule: DECIDE output invalid | schedule=%s job=%s", schedule.id, decide_job_id)
        return False

    if decision.decision == "skip":
        schedule.last_decision = "decide_skip"
        schedule.last_decision_at = now
        db.commit()
        _agent_decisions_skip_total.inc()
        return False

    # decision.decision == "act"
    if _schedule_has_active_job(db, schedule):
        # Extremely rare race (e.g. a manual run slipped in while this
        # DECIDE was outstanding) — never double up on an ACT.
        schedule.last_decision = "skipped"
        schedule.last_decision_at = now
        db.commit()
        _schedules_skipped_total.inc(reason="job_already_active")
        return False

    # ACT sentinel_criteria: fixed, server-controlled — never LLM-supplied
    # (see agent_decision.py's decision contract, which has no
    # sentinel_criteria field at all). A minimal non-empty-output check is
    # enough to genuinely exercise Sentinel for job_type="research" without
    # letting the model configure its own verification.
    sentinel_criteria = {"min_output_length": 1} if decision.action_job_type == "research" else None

    try:
        act_job = execution_service.create_job(
            db, user_id=schedule.user_id, plan=user.plan, user_timezone=user.timezone,
            agent_id=schedule.agent_id, job_type=decision.action_job_type, payload=decision.action_payload,
            priority=_effective_priority(schedule, agent), timeout_seconds=agent.execution_timeout_seconds,
            sentinel_criteria=sentinel_criteria,
        )
    except execution_service.CapacityRefusedError:
        schedule.last_decision = "capacity_refused"
        schedule.last_decision_at = now
        db.commit()
        _agent_decisions_failed_total.inc(reason="capacity_refused_act")
        return False
    except ValueError:
        # decision.action_payload failed create_job()'s own
        # _validate_payload() despite passing parse_decision()'s shape
        # check (e.g. an empty command string edge case) — never crash the
        # poll cycle for one bad decision.
        logger.exception("agent_schedule: DECIDE action payload invalid | schedule=%s job=%s", schedule.id, decide_job_id)
        schedule.last_decision = "decide_invalid"
        schedule.last_decision_at = now
        db.commit()
        _agent_decisions_failed_total.inc(reason="invalid_action_payload")
        return False

    if instance is not None:
        previous_job = (
            db.query(ExecutionJob).filter(ExecutionJob.id == instance.last_execution_job_id).first()
            if instance.last_execution_job_id else None
        )
        agent_instance_service.reconcile_health_from_job(instance, previous_job)
        instance.last_execution_job_id = act_job.id
        instance.last_run_at = now

    schedule.last_run_job_id = act_job.id
    schedule.last_run_at = now
    schedule.last_decision = "acted"
    schedule.last_decision_at = now
    db.commit()
    _agent_decisions_act_total.inc()
    _schedules_fired_total.inc()
    logger.info("agent_schedule: DECIDE consumed -> ACT | schedule=%s decide_job=%s act_job=%s", schedule.id, decide_job_id, act_job.id)
    return True


async def _fire_due_schedules(db: DBSession, *, now: datetime, check_source: CheckSource | None = None) -> int:
    """WAKE -> CHECK -> DECIDE -> ACT -> SLEEP: one poll cycle — see module
    docstring for the full breakdown. Returns the number of schedules
    that actually created a job. `check_source`, when given, forces THAT
    ONE CheckSource for every schedule this cycle (test-only override);
    the normal/production path resolves a source PER SCHEDULE via
    agent_check_source.build_check_source_for_schedule() — needed because
    an HTTP-checked schedule and a legacy schedule can be due in the SAME
    cycle. `async def` (not sync, unlike Tier 1) because CHECK may now do
    real network I/O — see agent_check_source.py's module docstring for
    why blocking the event loop here would be a real defect, not a
    style choice."""
    due = (
        db.query(AgentSchedule)
        .filter(AgentSchedule.is_active.is_(True), AgentSchedule.next_run_at <= now)
        .all()
    )
    fired = 0
    for schedule in due:
        # Advance next_run_at from `now`, not from the old next_run_at —
        # same "absolute clock, no drift accumulation" rule
        # session_window_service.py already established: a schedule that
        # was due 10 minutes late (process was down) does not then fire
        # every interval_seconds "to catch up". Advanced BEFORE
        # CHECK/DECIDE/ACT so the fixed cadence never stretches out
        # regardless of what this cycle decides or how long it takes.
        schedule.next_run_at = now + timedelta(seconds=schedule.interval_seconds)
        db.commit()

        user = db.query(User).filter(User.id == schedule.user_id).first()
        if user is None or not user.is_active:
            _schedules_skipped_total.inc(reason="user_inactive")
            continue

        # A persistent agent is agent mode running on a timer. A plan without
        # agent mode (FREE, or an account since downgraded) never fires one —
        # checked here, at every wake, because this is where spend begins and
        # because a plan can change long after the schedule was created.
        from src.entitlements import get_plan_entitlement

        if not get_plan_entitlement(user.plan).supports_agent_mode:
            schedule.last_decision = "skipped"
            schedule.last_decision_at = now
            db.commit()
            _schedules_skipped_total.inc(reason="plan_not_entitled")
            continue

        agent = db.query(AgentDefinition).filter(AgentDefinition.id == schedule.agent_id).first()
        if agent is None:
            # FK is ondelete="CASCADE" — a schedule cannot outlive its
            # agent in normal operation; defensive only.
            logger.error("agent_schedule: agent_id=%s not found for schedule=%s", schedule.agent_id, schedule.id)
            _schedules_skipped_total.inc(reason="agent_not_found")
            continue

        # Persistent Agent Instance V1 — a schedule with NO owning
        # instance (agent_instance_id is None: every schedule that
        # predates this phase, or one created without an instance on
        # purpose) is entirely unaffected — no gate, no health
        # reconciliation, byte-for-byte the same behavior as before this
        # phase. A schedule that DOES have one skips its ENTIRE cycle
        # (CHECK included — no point paying for e.g. a real HTTP request
        # for an agent the user explicitly paused) whenever the instance
        # isn't "active".
        instance: AgentInstance | None = None
        if schedule.agent_instance_id is not None:
            instance = db.query(AgentInstance).filter(AgentInstance.id == schedule.agent_instance_id).first()
            if instance is not None and instance.status != "active":
                schedule.last_decision = "skipped"
                schedule.last_decision_at = now
                db.commit()
                _schedules_skipped_total.inc(reason=f"instance_{instance.status}")
                continue

        # Tier 2 — LLM DECIDE. Entirely separate code path from Tier 1's
        # CHECK/DECIDE/ACT below (which stays byte-for-byte unmodified) —
        # see _process_llm_decide_schedule()/_consume_pending_decision()
        # and the module docstring's "Tier 2" section.
        if _is_llm_decide_schedule(schedule):
            if await _process_llm_decide_schedule(db, schedule, agent, instance, user, now, check_source=check_source):
                fired += 1
            continue

        # A scheduled task runs because its time came: there is no source to CHECK and nothing to DECIDE.
        # Everything after (concurrency invariant, capacity refusal, ACT, bookkeeping) is shared.
        if not _is_task_schedule(schedule):
            # CHECK — non-LLM, no TCU (agent_check_source.py).
            active_check_source = check_source or build_check_source_for_schedule(schedule)
            check_result = await active_check_source.check(schedule)
            schedule.last_check_signature = check_result.signature
            _check_results_total.inc(result=_check_result_metric_label(check_result))

            # DECIDE — non-LLM (adaptive_budget_service.should_skip_llm).
            if should_skip_llm(has_state_changed=check_result.changed):
                schedule.last_decision = "skipped"
                schedule.last_decision_at = now
                db.commit()
                _schedules_skipped_total.inc(reason="no_change_detected")
                logger.info(
                    "agent_schedule: skipped (no change) | schedule=%s reason=%s",
                    schedule.id, check_result.reason,
                )
                continue

        # Concurrency invariant — checked right before ACT, not before
        # CHECK/DECIDE, so the schedule's own observability (signature/
        # decision) still updates every cycle regardless.
        if _schedule_has_active_job(db, schedule):
            schedule.last_decision = "skipped"
            schedule.last_decision_at = now
            db.commit()
            _schedules_skipped_total.inc(reason="job_already_active")
            logger.info(
                "agent_schedule: skipped (job already active) | schedule=%s last_run_job_id=%s",
                schedule.id, schedule.last_run_job_id,
            )
            continue

        # AgentDefinition defaults — see module docstring.
        effective_priority = schedule.priority if schedule.priority is not None else agent.priority
        act_job_type, act_payload = _resolve_act_spec(schedule)

        try:
            job = execution_service.create_job(
                db, user_id=schedule.user_id, plan=user.plan, user_timezone=user.timezone,
                agent_id=schedule.agent_id, job_type=act_job_type,
                payload=act_payload, priority=effective_priority,
                timeout_seconds=agent.execution_timeout_seconds,
            )
        except execution_service.CapacityRefusedError:
            # Exactly mission section 10's intent: a low-priority persistent
            # agent is the FIRST thing adaptive consumption defers — this is
            # not an error, it's the policy working as designed.
            schedule.last_decision = "capacity_refused"
            schedule.last_decision_at = now
            db.commit()
            _schedules_skipped_total.inc(reason="capacity_refused")
            continue
        except ValueError:
            logger.exception("agent_schedule: invalid payload for schedule=%s", schedule.id)
            _schedules_skipped_total.inc(reason="invalid_payload")
            continue

        # Health reconciliation (Persistent Agent Instance V1) — read the
        # OUTGOING job's terminal outcome (if any) exactly once, right
        # before it's superseded by the new one. See
        # agent_instance_service.reconcile_health_from_job()'s own
        # docstring for the exact rule (only "completed"/"failed"/
        # "cancelled"/"expired" count; "retrying" never does). This is
        # the ONLY place that ever mutates AgentInstance health — no
        # second poller, no worker-side hook.
        if instance is not None:
            previous_job = (
                db.query(ExecutionJob).filter(ExecutionJob.id == instance.last_execution_job_id).first()
                if instance.last_execution_job_id else None
            )
            agent_instance_service.reconcile_health_from_job(instance, previous_job)
            instance.last_execution_job_id = job.id
            instance.last_run_at = now

        schedule.last_run_job_id = job.id
        schedule.last_run_at = now
        schedule.last_decision = "acted"
        schedule.last_decision_at = now
        db.commit()
        fired += 1
        _schedules_fired_total.inc()
        logger.info("agent_schedule: fired | schedule=%s job=%s", schedule.id, job.id)

    return fired


async def run_schedule_poll_loop(*, stop_event: asyncio.Event) -> None:
    """Started once from main.py's lifespan as a background asyncio.Task,
    stopped via `stop_event` on shutdown. In-process/single-worker, same
    documented limitation as every other scheduler-shaped component in
    this codebase (see models/agent_schedule.py's module docstring).

    Waits ONE interval before the first poll cycle (rather than firing
    immediately on startup) — deliberately, not an oversight. main.py's
    lifespan runs on EVERY app startup, including once per test in the
    whole suite (`client` fixture uses `with TestClient(app) as c:`, which
    triggers this same lifespan). An immediate first cycle means every one
    of those ~1500 tests pays for a real DB round-trip (against a database
    that, in the test environment, doesn't even have this feature's tables
    yet) plus exception-logging on every single test teardown — a real,
    measured multi-minute slowdown across the full suite for a poll loop
    almost none of those tests ever exercise. Waiting first means a test
    whose lifespan starts and stops within one interval (effectively all
    of them) never touches the DB via this loop at all. Production
    behavior is unaffected in any way that matters: persistent agents are
    fired on a scale of minutes, so a delay of one interval before the
    very first check after a fresh deploy is immaterial.
    """
    logger.info("agent_schedule: poll loop started | interval=%ds", POLL_INTERVAL_SECONDS)
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass  # normal — just means it's time for the next poll
        if stop_event.is_set():
            break

        db = SessionLocal()
        try:
            await _fire_due_schedules(db, now=datetime.now(timezone.utc))
        except Exception:
            logger.exception("agent_schedule: poll cycle failed")
        finally:
            db.close()
    logger.info("agent_schedule: poll loop stopped")
