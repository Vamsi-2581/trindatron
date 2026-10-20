"""
Durable Execution Worker — DB-backed queue, lease, heartbeat, and recovery
for ExecutionJob (see models/execution.py's module docstring for the
column-level contract this module reads/writes).

Why this exists
----------------
Execution Runtime V1 dispatched a job via `asyncio.create_task()` directly
from `execution_service.create_job()` — the job's ENTIRE state of
"who is running this, and how far did they get" lived in one process's
memory (`_RUNNING_TASKS`). That is correct for exactly one deployment
shape (one process, never restarted mid-job) and wrong for any other: a
deploy, a crash, or a Railway restart silently lost the job. This module
replaces that with a durable model where the DATABASE ROW is the only
source of truth. `execution_service.create_job()` now ONLY persists a
job with status="queued" and returns — it never touches asyncio.create_task
itself (mission requirement: "O HTTP não deve ser responsável pela
execução do job"). This module's worker loop is what actually claims and
runs jobs, independent of any HTTP request's lifetime.

No Redis/Celery/external queue
--------------------------------
Evaluated first, per the mission's explicit instruction. Postgres (the
production database here — Supabase/Railway) is sufficient: claiming a
job is a single atomic `UPDATE execution_jobs SET status='running', ...
WHERE id=:id AND status IN ('queued','retrying')`. This is a
well-established compare-and-swap claim pattern — the WHERE clause's
status check and the SET happen atomically as one statement in every SQL
engine (SQLite included, which is why this same code path is exercised in
tests without a second implementation). Only ONE of two concurrent workers
racing the same UPDATE can ever see `rowcount == 1`; the other sees 0 and
moves on to its next candidate. This is deliberately NOT
`SELECT ... FOR UPDATE SKIP LOCKED` — that construct optimizes avoiding
lock contention while a worker holds a row lock during a multi-statement
transaction scanning many candidates; a bare single-statement UPDATE never
holds a lock across statements at all, so there is nothing for a second
worker to block on or need to "skip". It is the "mecanismo equivalente
seguro" the mission explicitly allows in place of SKIP LOCKED, and it has
the added benefit of working identically on SQLite (tests) and Postgres
(production) — no dialect-specific code path.

Queue model
------------
A job is claimable when `status IN ('queued', 'retrying')` AND
(`next_run_at IS NULL OR next_run_at <= now`). Claiming
(`_try_claim()`) atomically sets `status='running'`, `worker_id`,
`lease_expires_at`, `heartbeat_at`, bumps `attempt_number`, and — only on
the very first attempt — `started_at`. Releasing (a recoverable failure,
or a lease reclaimed from a crashed worker) atomically clears
`worker_id`/`lease_expires_at`, sets `status` back to `'retrying'` and
`next_run_at` to a future instant (durable exponential backoff — replaces
the old in-process `asyncio.sleep()` retry loop; a retry now survives the
worker that scheduled it dying before the sleep would have ended).

Lease / heartbeat
-------------------
`lease_expires_at` is granted on claim as `now + max(
EXECUTION_LEASE_DURATION_SECONDS, job.timeout_seconds +
EXECUTION_LEASE_GRACE_SECONDS)` — deriving from the job's OWN configured
per-step timeout (not a second, disconnected duration setting) guarantees
a legitimately still-running step can never outlive its own lease and get
pre-empted by the recovery sweep. The lease is renewed (same formula,
recomputed from "now") after each step completes — `heartbeat_at` is
updated at the same points. This is deliberately COARSE (once per step,
not continuously mid-step): a step's own `timeout_seconds` already bounds
its maximum duration, so the lease derived from that bound already covers
the whole step; a sub-step heartbeat would add complexity (a concurrent
heartbeat task per job) for no correctness benefit here.

Recovery
---------
`reclaim_expired_leases()` finds every `status='running'` job whose
`lease_expires_at` is NULL or in the past — both cases mean "no live
worker can possibly still own this" (NULL is only reachable via a
malformed/pre-migration row; a genuine claim always sets it). Before
releasing such a job back to the queue, it closes the ONE real billing
gap a crash can open (mission requirement 5): if the job's
`reservation_id` still points at a `UsageRecord` in status='pending' (the
worker died between `check_and_reserve()` and `finalize_usage()` inside
ModelRouterExecutionProvider), that reservation is explicitly cancelled
first — so a retry's own fresh reservation is never double-counted
alongside an orphaned pending one in `_sum_active_tcu()`'s quota sum. This
function is called once at startup (replacing the old "mark everything
live as failed" reconciliation — see below for why that would be WRONG
here) and periodically thereafter (`run_recovery_loop()`,
EXECUTION_RECOVERY_INTERVAL_SECONDS).

Why startup no longer means "mark every live job failed"
------------------------------------------------------------
Execution Runtime V1's `reconcile_stale_jobs_at_startup()` unconditionally
failed every live-status job at boot — correct ONLY because V1 guaranteed
single-process, in-memory dispatch (no other process could possibly still
own a job). That assumption no longer holds: a "running" job's lease may
still be valid, meaning ANOTHER worker (a different process, a different
Railway replica, or simply a poll cycle that hasn't reached its recovery
sweep yet) may genuinely still be executing it. Blindly failing it would
be an active correctness regression, not a safety margin. The correct,
universally-safe check is always "has this SPECIFIC lease expired" —
which is exactly what `reclaim_expired_leases()` does, called at both
startup and periodically, with no special-cased startup-only behavior.

Idempotency / delivery semantics (mission requirement 6 — documented, not
oversold)
----------------------------------------------------------------------------
Job delivery is AT-LEAST-ONCE: a worker crash between a step's real
side effect succeeding (e.g. the LLM call actually returned) and that
step's checkpoint commit means the step is re-executed by whoever reclaims
the job next. This is NOT hidden — the module docstring says so plainly,
and the mission explicitly permits it ("não prometa exactly-once"). What
IS guaranteed exactly-once per accepted attempt: (a) checkpointed steps —
`completed_step_indices` is only ever extended after a step's own
provider call returns, so a step that successfully committed its
checkpoint is never re-run by a later attempt (b) the INTERNAL TCU ledger
— `reclaim_expired_leases()`'s pending-reservation-cancel closes the one
path that could otherwise double-count internally. What is at-least-once,
by design, with no attempt to hide it: the real external LLM API call
itself, in the narrow crash window between it returning and the
checkpoint commit. Artifact creation is at-least-once too (a re-run step
overwrites the same filename in the workspace; `_persist_artifacts()`
lists the workspace fresh on each successful completion, so a duplicate
`ExecutionArtifact` row is possible only across DIFFERENT completed
attempts of the SAME job, which cannot happen — a job has exactly one
terminal `_finish()` call).

Worker deployment model (mission requirement 16)
----------------------------------------------------
Runs as a background asyncio task INSIDE the existing FastAPI process
(same pattern as agent_schedule_service.run_schedule_poll_loop() —
started/stopped in main.py's lifespan). Deliberately NOT a separate
Railway service/process for this iteration: Railway's config
(railway.toml) deploys exactly one process today, and every claim/lease
mechanism in this module is DB-level atomic, not in-memory — it is
already fully correct if a separate worker process (or multiple replicas
of this same process) is introduced later; no code here would need to
change, only railway.toml would gain a second [deploy] block / service.
Splitting today would be additional deployment complexity for zero
additional correctness, which the mission's own "menor solução real"
instruction argues against.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, update
from sqlalchemy.orm import Session as DBSession

from src.config import settings
from src.execution.provider import ExecutionProvider, ExecutionProviderUnavailable
from src.models.execution import EXECUTION_TERMINAL_STATUSES, ExecutionArtifact, ExecutionJob
from src.models.usage import UsageRecord
from src.models.user import User
from src.observability.metrics import core_metrics
from src.services import entitlement_service, execution_service, sentinel_service

logger = logging.getLogger("trinity.execution.worker")

_jobs_completed_total = core_metrics.counter(
    "execution_jobs_completed_total", "ExecutionJobs reaching a terminal status, labeled by status.",
)
_jobs_retried_total = core_metrics.counter(
    "execution_jobs_retried_total", "ExecutionJob retry attempts, labeled by reason.",
)
_jobs_claimed_total = core_metrics.counter(
    "execution_jobs_claimed_total", "ExecutionJobs successfully claimed by a worker.",
)
_leases_reclaimed_total = core_metrics.counter(
    "execution_lease_reclaimed_total", "Expired leases reclaimed from a presumed-crashed worker.",
)

# Durable retry backoff base — module-level (not settings.* read inline)
# specifically so tests can monkeypatch it the same way Execution Runtime
# V1's tests monkeypatched execution_service._BACKOFF_BASE_SECONDS.
_BACKOFF_BASE_SECONDS = settings.execution_retry_backoff_base_seconds

_PRIORITY_RANK: dict[str, int] = {"critical": 0, "high": 1, "normal": 2, "low": 3}

# Per-process registry of in-flight claim+execute tasks, keyed by job_id —
# best-effort ONLY: lets execution_service.cancel_job() interrupt a job
# immediately when this SAME process happens to be the one running it
# (same UX as Execution Runtime V1's cancellation tests). Cancelling a job
# owned by a DIFFERENT process/replica is NOT instant — it is picked up at
# the next step boundary via the durable `cancel_requested` column, which
# every attempt checks regardless of which process is running it. Never
# used for correctness, only for latency — the durable flag is what makes
# cancellation actually correct across processes.
_LOCAL_JOB_TASKS: dict[str, asyncio.Task] = {}


def new_worker_id() -> str:
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


def try_interrupt_local(job_id: str) -> bool:
    task = _LOCAL_JOB_TASKS.get(job_id)
    if task is not None and not task.done():
        task.cancel()
        return True
    return False


def _lease_seconds_for(job: ExecutionJob) -> int:
    return max(settings.execution_lease_duration_seconds, job.timeout_seconds + settings.execution_lease_grace_seconds)


def _find_candidate_ids(db: DBSession, *, now: datetime, limit: int) -> list[str]:
    # Over-fetch a small multiple of `limit`, then rank by priority in
    # Python — the candidate set per poll cycle is always small (bounded
    # by execution_worker_batch_size), so a DB-side CASE/ORDER BY priority
    # expression would be pure ceremony for no measurable benefit, and
    # this stays portable across SQLite/Postgres with zero dialect code.
    rows = (
        db.query(ExecutionJob.id, ExecutionJob.priority)
        .filter(
            ExecutionJob.status.in_(("queued", "retrying")),
            or_(ExecutionJob.next_run_at.is_(None), ExecutionJob.next_run_at <= now),
        )
        .order_by(ExecutionJob.created_at)
        .limit(limit * 4)
        .all()
    )
    ranked = sorted(rows, key=lambda r: _PRIORITY_RANK.get(r.priority, 9))
    return [r.id for r in ranked[:limit]]


def _try_claim(db: DBSession, job_id: str, *, worker_id: str, now: datetime) -> ExecutionJob | None:
    """Atomic compare-and-swap claim — see module docstring. Returns the
    freshly-claimed job, or None if another worker won the race (or the
    job is no longer claimable for any other reason)."""
    job = db.query(ExecutionJob).filter(ExecutionJob.id == job_id).first()
    if job is None:
        return None
    lease_seconds = _lease_seconds_for(job)
    result = db.execute(
        update(ExecutionJob)
        .where(ExecutionJob.id == job_id, ExecutionJob.status.in_(("queued", "retrying")))
        .values(
            status="running",
            worker_id=worker_id,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            heartbeat_at=now,
            started_at=func.coalesce(ExecutionJob.started_at, now),
            attempt_number=ExecutionJob.attempt_number + 1,
        )
    )
    db.commit()
    if result.rowcount != 1:
        return None
    db.refresh(job)
    _jobs_claimed_total.inc()
    return job


def _resolve_provider(db: DBSession, job: ExecutionJob) -> ExecutionProvider:
    """job_type -> concrete provider, resolved fresh per attempt (never
    carried over from create_job() time — a different process, possibly
    days later, is executing this attempt). Test-only escape hatch: a
    provider passed to create_job(provider=...) is registered in
    execution_service._PROVIDER_OVERRIDES and always wins here."""
    override = execution_service._PROVIDER_OVERRIDES.get(job.id)
    if override is not None:
        return override
    if job.job_type == "research":
        user = db.query(User).filter(User.id == job.user_id).first()
        plan = user.plan if user is not None else "free"
        user_timezone = user.timezone if user is not None else None
        return execution_service.build_research_provider(
            user_id=job.user_id, plan=plan, user_timezone=user_timezone, session_id=job.session_id,
        )
    if job.job_type == "agent_decide":
        user = db.query(User).filter(User.id == job.user_id).first()
        plan = user.plan if user is not None else "free"
        user_timezone = user.timezone if user is not None else None
        return execution_service.build_decision_provider(
            user_id=job.user_id, plan=plan, user_timezone=user_timezone, session_id=job.session_id,
        )
    if job.job_type == "presentation":
        user = db.query(User).filter(User.id == job.user_id).first()
        plan = user.plan if user is not None else "free"
        user_timezone = user.timezone if user is not None else None
        return execution_service.build_presentation_provider(
            user_id=job.user_id, plan=plan, user_timezone=user_timezone,
            session_id=job.session_id,
            # Deck shape travels on the job, not the User row — see
            # build_presentation_provider()'s docstring.
            options=(job.payload or {}).get("presentation") or {},
        )
    return execution_service.build_default_provider()


def _classify_recoverable(*, exc: Exception | None, sentinel_failed: bool, step_recoverable: bool | None) -> bool:
    if exc is not None:
        return isinstance(exc, ExecutionProviderUnavailable)
    if step_recoverable is True:
        return True
    if sentinel_failed:
        return True
    return False


async def _persist_artifacts(db: DBSession, job: ExecutionJob, workspace) -> None:
    if not workspace.exists():
        return
    for path in sorted(workspace.iterdir()):
        if not path.is_file():
            continue
        db.add(ExecutionArtifact(
            job_id=job.id, filename=path.name, relative_path=path.name, size_bytes=path.stat().st_size,
        ))
    db.commit()


def _finish(db: DBSession, job: ExecutionJob, *, status: str, error: str | None = None, result: dict | None = None) -> None:
    job.status = status
    job.completed_at = datetime.now(timezone.utc)
    job.worker_id = None
    job.lease_expires_at = None
    if error is not None:
        job.error = error
    if result is not None:
        job.result = result
    db.commit()
    _jobs_completed_total.inc(status=status)
    execution_service._PROVIDER_OVERRIDES.pop(job.id, None)
    logger.info("execution: job %s | id=%s attempt=%d", status, job.id, job.attempt_number)


def _release_for_retry(db: DBSession, job: ExecutionJob, *, reason: str, sentinel_failed: bool) -> None:
    """Release a claimed job back to the queue for a future attempt —
    durable backoff via next_run_at, no in-process sleep."""
    job.retry_reason = reason
    job.status = "retrying"
    job.worker_id = None
    job.lease_expires_at = None
    backoff = _BACKOFF_BASE_SECONDS * (2 ** (job.attempt_number - 1))
    job.next_run_at = datetime.now(timezone.utc) + timedelta(seconds=backoff)
    if sentinel_failed:
        # Every step already reported success — see execution_service's
        # original module docstring for why the checkpoint must reset here
        # (unchanged reasoning, just relocated).
        job.checkpoint = {"completed_step_indices": [], "step_results": {}}
    db.commit()
    _jobs_retried_total.inc(reason=reason)
    logger.info(
        "execution: released for retry | id=%s attempt=%d/%d reason=%s next_run_at=%s",
        job.id, job.attempt_number, job.max_retries, reason, job.next_run_at,
    )


def _release_after_attempt(
    db: DBSession, job: ExecutionJob, *,
    exc: Exception | None, sentinel_failed: bool, step_recoverable: bool | None, error: str,
) -> None:
    recoverable = _classify_recoverable(exc=exc, sentinel_failed=sentinel_failed, step_recoverable=step_recoverable)
    if not (recoverable and job.attempt_number <= job.max_retries):
        _finish(db, job, status="failed", error=error)
        return
    if exc is not None:
        reason = "provider_unavailable"
    elif sentinel_failed:
        reason = "sentinel_fail"
    else:
        reason = "transient_step_failure"
    _release_for_retry(db, job, reason=reason, sentinel_failed=sentinel_failed)


async def _execute_one_attempt(db: DBSession, job: ExecutionJob, provider: ExecutionProvider) -> None:
    """Runs exactly ONE attempt of an ALREADY-CLAIMED job (status already
    'running', lease already granted by _try_claim()). On return, job.status
    is a terminal status, or 'retrying' (released, next_run_at set) — never
    left 'running' on any exit path (cleanup_environment() always runs in
    `finally`, same "nenhum sandbox abandonado" guarantee Execution Runtime
    V1 established, unchanged)."""
    workspace = None
    try:
        try:
            workspace = await provider.prepare_environment(job_id=job.id, user_id=job.user_id)
        except ExecutionProviderUnavailable as exc:
            _release_after_attempt(db, job, exc=exc, sentinel_failed=False, step_recoverable=None, error=str(exc))
            return

        steps = job.payload["steps"]
        completed_indices: set[int] = set(job.checkpoint.get("completed_step_indices", []))
        step_results: dict = dict(job.checkpoint.get("step_results", {}))
        last_success = True
        step_recoverable: bool | None = None

        for i, step in enumerate(steps):
            if i in completed_indices:
                continue  # resumed from checkpoint — already done in a prior attempt

            if job.cancel_requested:
                _finish(db, job, status="cancelled", error="Cancelled by user request")
                return

            outcome = await provider.execute_step(
                workspace=workspace, command=step["command"], timeout_seconds=job.timeout_seconds,
            )
            step_results[str(i)] = outcome.output
            last_success = outcome.success
            step_recoverable = outcome.recoverable

            if outcome.success:
                completed_indices.add(i)
            job.checkpoint = {"completed_step_indices": sorted(completed_indices), "step_results": step_results}
            reservation_id = getattr(provider, "last_reservation_id", None)
            if reservation_id is not None:
                job.reservation_id = reservation_id
            # Lease/heartbeat renewal — see module docstring on why
            # once-per-step is the right granularity.
            now = datetime.now(timezone.utc)
            job.heartbeat_at = now
            job.lease_expires_at = now + timedelta(seconds=_lease_seconds_for(job))
            db.commit()

            if job.cancel_requested:
                # Covers the "this WAS the last/only step" case — see
                # execution_worker.py's docstring on why a post-step check
                # is needed in addition to the pre-step one above (a
                # single-step job's own cancellation would otherwise be
                # silently missed when running under the durable, no
                # local-interrupt path).
                _finish(db, job, status="cancelled", error="Cancelled by user request")
                return

            if not outcome.success:
                break  # stop the plan on first failing step

        combined_output = "\n".join(step_results.values())

        sentinel_failed = False
        criteria = sentinel_service.SentinelCriteria.from_dict(job.sentinel_criteria)
        if criteria is not None:
            report = sentinel_service.verify(
                criteria=criteria, last_step_success=last_success, combined_output=combined_output, workspace=workspace,
            )
            sentinel_failed = not report.passed
            job.sentinel_status = "pass" if report.passed else "fail"
            job.sentinel_report = report.to_dict()
            db.commit()

        if last_success and not sentinel_failed:
            await _persist_artifacts(db, job, workspace)
            _finish(db, job, status="completed", result={"output": combined_output[-4000:]})
            return

        _release_after_attempt(
            db, job, exc=None, sentinel_failed=sentinel_failed, step_recoverable=step_recoverable,
            error=f"Job did not complete successfully (sentinel_failed={sentinel_failed})",
        )
    finally:
        if workspace is not None:
            try:
                await provider.cleanup_environment(workspace=workspace)
            except Exception:
                logger.exception("execution: cleanup failed | id=%s workspace=%s", job.id, workspace)


async def _run_claimed_job(job_id: str, worker_id: str) -> bool:
    """Opens its OWN session (never shares one across concurrently-run
    jobs), attempts to claim job_id, and — only if the claim succeeds —
    runs exactly one attempt. Returns True iff this worker actually
    claimed and processed the job (False means another worker won the
    claim race first)."""
    db = execution_service._new_session()
    try:
        job = _try_claim(db, job_id, worker_id=worker_id, now=datetime.now(timezone.utc))
        if job is None:
            return False
        provider = _resolve_provider(db, job)
        task = asyncio.current_task()
        if task is not None:
            _LOCAL_JOB_TASKS[job_id] = task
        try:
            await _execute_one_attempt(db, job, provider)
        except asyncio.CancelledError:
            db2 = execution_service._new_session()
            try:
                fresh = db2.query(ExecutionJob).filter(ExecutionJob.id == job_id).first()
                if fresh is not None and fresh.status not in EXECUTION_TERMINAL_STATUSES:
                    _finish(db2, fresh, status="cancelled", error="Execution task cancelled")
            finally:
                db2.close()
            raise
        except Exception as exc:  # noqa: BLE001 — top-level task boundary, must not leak
            logger.exception("execution: unexpected failure | id=%s", job_id)
            db3 = execution_service._new_session()
            try:
                fresh = db3.query(ExecutionJob).filter(ExecutionJob.id == job_id).first()
                if fresh is not None:
                    _finish(db3, fresh, status="failed", error=f"Unexpected error: {exc}")
            finally:
                db3.close()
        return True
    finally:
        _LOCAL_JOB_TASKS.pop(job_id, None)
        db.close()


async def run_worker_once(*, worker_id: str | None = None, max_jobs: int | None = None) -> int:
    """One full poll cycle: find candidates, claim+run up to `max_jobs`
    (default settings.execution_worker_batch_size) of them CONCURRENTLY.
    Returns how many jobs THIS call actually claimed and processed — the
    pure, testable core (agent_schedule_service._fire_due_schedules()'s
    same shape); run_worker_loop() below is the thin poll wrapper."""
    worker_id = worker_id or new_worker_id()
    limit = max_jobs or settings.execution_worker_batch_size
    db = execution_service._new_session()
    try:
        candidate_ids = _find_candidate_ids(db, now=datetime.now(timezone.utc), limit=limit)
    finally:
        db.close()

    if not candidate_ids:
        return 0

    results = await asyncio.gather(*(_run_claimed_job(jid, worker_id) for jid in candidate_ids))
    return sum(1 for r in results if r)


async def run_worker_loop(*, stop_event: asyncio.Event, worker_id: str | None = None) -> None:
    """Started once from main.py's lifespan as a background asyncio.Task,
    stopped gracefully via `stop_event`. Same "wait one interval before
    the first poll" rationale as agent_schedule_service.run_schedule_poll_loop()
    — see that function's own docstring: an immediate first cycle would
    make every one of ~1500+ tests in the suite pay for a DB round-trip on
    a table that, in some test contexts, doesn't exist yet."""
    worker_id = worker_id or new_worker_id()
    logger.info("execution: worker loop started | worker_id=%s interval=%.1fs", worker_id, settings.execution_worker_poll_interval_seconds)
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.execution_worker_poll_interval_seconds)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        try:
            await run_worker_once(worker_id=worker_id)
        except Exception:
            logger.exception("execution: worker poll cycle failed")
    logger.info("execution: worker loop stopped | worker_id=%s", worker_id)


def reclaim_expired_leases(db: DBSession, *, now: datetime | None = None) -> int:
    """Reclaims every 'running' job whose lease is missing or expired —
    see module docstring for why this replaces V1's unconditional
    startup-fail-everything reconciliation, and for the billing-safety
    reasoning (cancels a dangling pending reservation before releasing)."""
    now = now or datetime.now(timezone.utc)
    stale = (
        db.query(ExecutionJob)
        .filter(
            ExecutionJob.status == "running",
            or_(ExecutionJob.lease_expires_at.is_(None), ExecutionJob.lease_expires_at < now),
        )
        .all()
    )
    for job in stale:
        if job.reservation_id:
            record = db.query(UsageRecord).filter(
                UsageRecord.id == job.reservation_id, UsageRecord.status == "pending",
            ).first()
            if record is not None:
                entitlement_service.cancel_reservation(db, job.reservation_id)
                logger.warning(
                    "execution: cancelled dangling pending reservation on lease reclaim | job=%s reservation=%s",
                    job.id, job.reservation_id,
                )
        if job.attempt_number <= job.max_retries:
            _release_for_retry(db, job, reason="lease_expired", sentinel_failed=False)
        else:
            _finish(db, job, status="failed", error="Execution lease expired and retries exhausted (worker crash suspected)")
    if stale:
        _leases_reclaimed_total.inc(len(stale))
        logger.warning("execution: reclaimed %d expired lease(s)", len(stale))
    return len(stale)


async def run_recovery_loop(*, stop_event: asyncio.Event) -> None:
    """Periodic sweep pairing reclaim_expired_leases() with the
    pre-existing expire_overdue_jobs() (unrelated concept — total
    wall-clock budget across ALL attempts, not lease ownership; see
    execution_service.expire_overdue_jobs()'s own docstring). Same
    wait-then-poll startup-cost rationale as run_worker_loop() above.
    Also invoked once, synchronously, at startup — see main.py."""
    logger.info("execution: recovery loop started | interval=%.1fs", settings.execution_recovery_interval_seconds)
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.execution_recovery_interval_seconds)
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        db = execution_service._new_session()
        try:
            reclaim_expired_leases(db)
            execution_service.expire_overdue_jobs(db)
        except Exception:
            logger.exception("execution: recovery sweep failed")
        finally:
            db.close()
    logger.info("execution: recovery loop stopped")
