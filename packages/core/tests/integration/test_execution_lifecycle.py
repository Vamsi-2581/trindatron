"""Integration tests for execution_service + execution_worker — Durable
Execution's lifecycle/retry/cancel/cleanup/checkpoint/artifact/budget/
recovery behavior. Uses FakeExecutionProvider exclusively — no Docker, no
real subprocess, no real network call anywhere in this file.

All tests use the `client`+`db`+`run_worker_until_terminal` fixtures from
conftest.py: `client` triggers the FastAPI lifespan (needed so the app's
own startup code runs once), `run_worker_until_terminal` drives
execution_worker.run_worker_once() in a loop (against the SAME test
database `db` reads from — see execution_session_override, which
run_worker_until_terminal itself depends on) until the job reaches a
terminal status. This is deliberately NOT awaiting a single asyncio.Task
the old create_job() used to spawn — create_job() no longer spawns
anything; a completely separate worker (here, driven explicitly by the
test; in production, execution_worker.run_worker_loop()'s poll loop)
claims and runs the job, exactly like a real deploy would.

Test matrix (same coverage as Execution Runtime V1, re-verified against
the durable worker, plus new Durable Execution items)
------------------------------------------------------------------------
L01) create_job -> worker claims and runs it -> 'completed'
L02) Multi-step plan: all steps run in order, checkpoint tracks each
L03) A failing step with recoverable=True is retried (via next_run_at,
     not asyncio.sleep) and then succeeds — checkpoint resumes from the
     failed step, not the whole plan
L04) A failing step with recoverable=False is NOT retried — fails once
L05) Sentinel FAIL on a technically-successful job triggers a retry
L06) max_retries exhausted -> job ends 'failed', not retried forever
L07) cancel_job() before execution reaches a step -> job ends 'cancelled'
L08) cleanup_environment() is called even on failure (isolation: no
     workspace left behind)
L09) Two jobs for two different users never share a workspace path
L10) CapacityRefusedError raised for a low-priority job when capacity is
     exhausted — no job row persisted, worker never claims anything
L11) reclaim_expired_leases marks a job whose lease expired as
     'retrying' (recoverable) or 'failed' (retries exhausted) — the
     Durable Execution replacement for V1's unconditional
     reconcile_stale_jobs_at_startup()
L12) count_active_jobs reflects only live-status jobs for the given user
     (observed WHILE a job is genuinely still running, via a
     fire-and-forget worker task — see the test's own comment)
L13) Ownership: get_job returns None for another user's job (service level)
L14) ExecutionProviderUnavailable at prepare_environment -> retried (it is
     documented as recoverable — see execution_worker._classify_recoverable()
     and its module docstring's own retry-classification list) up to
     max_retries, then fails. NOTE: Execution Runtime V1's actual CODE for
     this one path always failed immediately without ever consulting
     _classify_recoverable() at all — contradicting its OWN module
     docstring, which explicitly lists ExecutionProviderUnavailable as
     recoverable. That was a real gap, not a deliberate design choice;
     this refactor's natural implementation closes it by routing this
     path through the same _release_after_attempt() every other
     recoverable failure uses — retried, as documented, not a behavior
     regression.
L15) Artifacts produced in the workspace are persisted as ExecutionArtifact rows
L16) Two workers racing the SAME claimable job — only one claims it
     (compare-and-swap correctness, the core of the whole durable model)
L17) A crashed worker's claimed job (lease force-expired, mid-attempt,
     BEFORE its checkpoint advanced) is recovered and the step re-runs
L18) A crashed worker's claimed job (lease force-expired, AFTER its
     checkpoint advanced past a step) is recovered and does NOT re-run
     the already-checkpointed step
L19) execution_worker_batch_size bounds how many queued jobs ONE poll
     cycle claims — the rest stay 'queued' for the next cycle (mission
     requirement 11: worker concurrency must be bounded, without
     duplicating Request Control/Agent Scheduler)
"""
from __future__ import annotations

import asyncio

import pytest

from src.execution.fake_provider import FakeExecutionProvider
from src.execution.provider import ExecutionProviderUnavailable, StepOutcome
from src.models.execution import ExecutionArtifact, ExecutionJob
from src.services import execution_service, execution_worker


@pytest.fixture
def fake_provider(tmp_path):
    return FakeExecutionProvider(tmp_path)


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    """Retry tests in this file exercise real backoff waits (next_run_at =
    now + backoff) — this shrinks the backoff base from 5s to 10ms so
    run_worker_until_terminal's poll loop picks a released job back up
    almost immediately, without spending real minutes of wall-clock time
    across the suite. Production's execution_worker._BACKOFF_BASE_SECONDS
    is untouched — monkeypatch reverts automatically after each test."""
    monkeypatch.setattr(execution_worker, "_BACKOFF_BASE_SECONDS", 0.01)


pytestmark = pytest.mark.usefixtures("execution_session_override")


async def test_L01_job_completes_via_worker(client, db, registered_user, fake_provider, run_worker_until_terminal):
    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        payload={"steps": [{"command": "echo hi"}]}, provider=fake_provider,
    )
    assert job.status == "queued"

    job = await run_worker_until_terminal(db, job.id)

    assert job.status == "completed"
    assert job.completed_at is not None
    assert "echo hi" in job.result["output"]
    assert job.worker_id is None  # released on terminal completion
    assert job.lease_expires_at is None


async def test_L02_multi_step_plan_tracks_checkpoint(client, db, registered_user, fake_provider, run_worker_until_terminal):
    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        payload={"steps": [{"command": "step1"}, {"command": "step2"}, {"command": "step3"}]},
        provider=fake_provider,
    )
    job = await run_worker_until_terminal(db, job.id)

    assert job.status == "completed"
    assert job.checkpoint["completed_step_indices"] == [0, 1, 2]
    assert len(fake_provider.calls) == 3


async def test_L03_recoverable_failure_retries_and_resumes_from_checkpoint(client, db, registered_user, fake_provider, run_worker_until_terminal):
    # Step 0 succeeds, step 1 fails recoverably on the FIRST attempt, then
    # succeeds on the retry — step 0 must NOT be re-executed.
    fake_provider.queue_outcome(StepOutcome(success=True, output="s1-ok", duration_ms=1))
    fake_provider.queue_outcome(StepOutcome(success=False, output="transient", duration_ms=1, recoverable=True))
    fake_provider.queue_outcome(StepOutcome(success=True, output="s2-ok-retry", duration_ms=1))

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        payload={"steps": [{"command": "step1"}, {"command": "step2"}]},
        provider=fake_provider, max_retries=2,
    )
    job = await run_worker_until_terminal(db, job.id, timeout=15.0)

    assert job.status == "completed"
    assert job.attempt_number == 2
    assert job.retry_reason == "transient_step_failure"
    # step 0 executed once (attempt 1), step 1 executed twice (fail then retry) = 3 calls total
    assert len(fake_provider.calls) == 3


async def test_L04_non_recoverable_failure_not_retried(client, db, registered_user, fake_provider, run_worker_until_terminal):
    fake_provider.queue_outcome(StepOutcome(success=False, output="permanent error", duration_ms=1, recoverable=False))

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        payload={"steps": [{"command": "bad-command"}]}, provider=fake_provider, max_retries=3,
    )
    job = await run_worker_until_terminal(db, job.id)

    assert job.status == "failed"
    assert job.attempt_number == 1  # never retried
    assert len(fake_provider.calls) == 1


async def test_L05_sentinel_fail_triggers_retry(client, db, registered_user, fake_provider, run_worker_until_terminal):
    # Deterministic (no real-time race): the SECOND execute_step() call
    # writes the expected artifact as a direct side effect of running,
    # rather than a background task racing the retry's own backoff.
    calls = {"n": 0}
    async def _write_file_on_second_call(*, workspace, command, timeout_seconds):
        calls["n"] += 1
        if calls["n"] >= 2:
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "out.txt").write_text("ok", encoding="utf-8")
            return StepOutcome(success=True, output="ran and wrote file", duration_ms=1)
        return StepOutcome(success=True, output="ran but no file", duration_ms=1)
    fake_provider.execute_step = _write_file_on_second_call  # type: ignore[method-assign]

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        payload={"steps": [{"command": "maybe-write-file"}]},
        sentinel_criteria={"expected_artifacts": ["out.txt"]},
        provider=fake_provider, max_retries=1,
    )

    job = await run_worker_until_terminal(db, job.id, timeout=15.0)

    assert job.attempt_number == 2
    assert job.retry_reason == "sentinel_fail"
    assert job.status == "completed"
    assert job.sentinel_status == "pass"


async def test_L06_retries_exhausted_ends_failed(client, db, registered_user, fake_provider, run_worker_until_terminal):
    for _ in range(5):
        fake_provider.queue_outcome(StepOutcome(success=False, output="always fails", duration_ms=1, recoverable=True))

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        payload={"steps": [{"command": "flaky"}]}, provider=fake_provider, max_retries=2,
    )
    job = await run_worker_until_terminal(db, job.id, timeout=30.0)

    assert job.status == "failed"
    assert job.attempt_number == 3  # initial + 2 retries, then stop
    assert "sentinel_failed=False" in job.error


async def test_L07_cancel_before_start_ends_cancelled(client, db, registered_user, fake_provider, run_worker_until_terminal):
    # A slow first step gives us a window to call cancel_job() before it
    # finishes — the worker's fire-and-forget claim task keeps running
    # concurrently with this test's own cancel_job() call.
    async def _slow_then_check(**_kwargs):
        await asyncio.sleep(0.3)
        return StepOutcome(success=True, output="too late", duration_ms=1)
    fake_provider.execute_step = _slow_then_check  # type: ignore[method-assign]

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        payload={"steps": [{"command": "slow"}, {"command": "next"}]}, provider=fake_provider,
    )
    worker_task = asyncio.create_task(execution_worker.run_worker_once())
    await asyncio.sleep(0.05)  # let the task start and enter the first step
    execution_service.cancel_job(db, job.id, registered_user["user_id"])
    try:
        await worker_task
    except asyncio.CancelledError:
        pass  # try_interrupt_local() cancelled the in-flight claim task directly

    db.expire_all()
    job = db.query(ExecutionJob).filter(ExecutionJob.id == job.id).first()
    assert job.status == "cancelled"


async def test_L08_cleanup_runs_even_on_failure(client, db, registered_user, fake_provider, run_worker_until_terminal):
    fake_provider.queue_outcome(StepOutcome(success=False, output="boom", duration_ms=1, recoverable=False))

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        payload={"steps": [{"command": "boom"}]}, provider=fake_provider,
    )
    job = await run_worker_until_terminal(db, job.id)

    workspace = fake_provider._workspace_root / registered_user["user_id"] / job.id
    assert not workspace.exists()


async def test_L09_two_users_never_share_a_workspace(client, db, registered_user, fake_provider, db2_user_factory, run_worker_until_terminal):
    user2 = db2_user_factory("user2@test.com")
    job1 = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        payload={"steps": [{"command": "a"}]}, provider=fake_provider,
    )
    job2 = execution_service.create_job(
        db, user_id=user2.id, plan="pro",
        payload={"steps": [{"command": "b"}]}, provider=fake_provider,
    )
    job1 = await run_worker_until_terminal(db, job1.id)
    job2 = await run_worker_until_terminal(db, job2.id)

    ws1 = str(fake_provider._workspace_root / registered_user["user_id"] / job1.id)
    ws2 = str(fake_provider._workspace_root / user2.id / job2.id)
    assert ws1 != ws2
    assert registered_user["user_id"] in ws1
    assert user2.id in ws2


async def test_L10_capacity_refused_no_job_persisted(client, db, registered_user, fake_provider, monkeypatch):
    from src.services import adaptive_budget_service

    monkeypatch.setattr(
        adaptive_budget_service, "compute_capacity",
        lambda *a, **k: adaptive_budget_service.CapacitySnapshot(
            tier=adaptive_budget_service.TIER_PAUSE_EXECUTION, capacity_remaining_pct=0.0,
            session_pct_used=100.0, session_resets_at=None, session_active=True,
            weekly_pct_used=100.0, weekly_resets_at=None,
        ),
    )
    before_count = db.query(ExecutionJob).count()

    with pytest.raises(execution_service.CapacityRefusedError):
        execution_service.create_job(
            db, user_id=registered_user["user_id"], plan="pro",
            payload={"steps": [{"command": "x"}]}, priority="low", provider=fake_provider,
        )

    assert db.query(ExecutionJob).count() == before_count  # nothing persisted
    assert len(fake_provider.calls) == 0  # never dispatched


def test_L11_reclaim_expired_leases_marks_stale_running_jobs(db, registered_user):
    from datetime import datetime, timedelta, timezone

    recoverable_job = ExecutionJob(
        user_id=registered_user["user_id"], job_type="shell", provider_name="docker",
        payload={"steps": [{"command": "x"}]}, status="running", checkpoint={},
        worker_id="dead-worker-1", lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=5),
        attempt_number=1, max_retries=2,
    )
    exhausted_job = ExecutionJob(
        user_id=registered_user["user_id"], job_type="shell", provider_name="docker",
        payload={"steps": [{"command": "x"}]}, status="running", checkpoint={},
        worker_id="dead-worker-2", lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=5),
        attempt_number=3, max_retries=2,
    )
    db.add_all([recoverable_job, exhausted_job])
    db.commit()

    reconciled = execution_worker.reclaim_expired_leases(db)

    db.refresh(recoverable_job)
    db.refresh(exhausted_job)
    assert reconciled == 2
    assert recoverable_job.status == "retrying"
    assert recoverable_job.retry_reason == "lease_expired"
    assert recoverable_job.worker_id is None
    assert recoverable_job.lease_expires_at is None
    assert recoverable_job.next_run_at is not None
    assert exhausted_job.status == "failed"
    assert "lease expired" in exhausted_job.error.lower()


async def test_L12_count_active_jobs_only_live_statuses(client, db, registered_user, fake_provider, run_worker_until_terminal):
    completed = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        payload={"steps": [{"command": "x"}]}, provider=fake_provider,
    )
    await run_worker_until_terminal(db, completed.id)

    slow_provider = FakeExecutionProvider(fake_provider._workspace_root)
    async def _never_finishes(**_kwargs):
        await asyncio.sleep(2.0)
        return StepOutcome(success=True, output="ok", duration_ms=1)
    slow_provider.execute_step = _never_finishes  # type: ignore[method-assign]
    running = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        payload={"steps": [{"command": "slow"}]}, provider=slow_provider,
    )
    # Fire-and-forget, same as a real poll loop's own concurrency — this
    # test observes the job WHILE it is genuinely still claimed/running,
    # not after the fact.
    worker_task = asyncio.create_task(execution_worker.run_worker_once())
    await asyncio.sleep(0.05)

    assert execution_service.count_active_jobs(db, registered_user["user_id"]) == 1

    execution_service.cancel_job(db, running.id, registered_user["user_id"])
    try:
        await worker_task
    except asyncio.CancelledError:
        pass


def test_L13_get_job_ownership_isolated(db, registered_user, db2_user_factory):
    user2 = db2_user_factory("owner-check@test.com")
    job = ExecutionJob(
        user_id=registered_user["user_id"], job_type="shell", provider_name="docker",
        payload={"steps": [{"command": "x"}]}, status="completed", checkpoint={},
    )
    db.add(job)
    db.commit()

    assert execution_service.get_job(db, job.id, user2.id) is None
    assert execution_service.get_job(db, job.id, registered_user["user_id"]) is not None


async def test_L14_provider_unavailable_is_retried_then_fails(client, db, registered_user, tmp_path, run_worker_until_terminal):
    provider = FakeExecutionProvider(tmp_path, available=False)
    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        payload={"steps": [{"command": "x"}]}, provider=provider, max_retries=2,
    )
    job = await run_worker_until_terminal(db, job.id, timeout=15.0)

    assert job.status == "failed"
    assert job.attempt_number == 3  # initial + 2 retries, each classified recoverable
    assert job.retry_reason == "provider_unavailable"
    assert "unavailable" in job.error.lower() or job.error is not None


async def test_L15_artifacts_persisted_after_completion(client, db, registered_user, fake_provider, run_worker_until_terminal):
    async def _write_artifact_then_succeed(*, workspace, command, timeout_seconds):
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "report.txt").write_text("done", encoding="utf-8")
        return StepOutcome(success=True, output="wrote report.txt", duration_ms=1)
    fake_provider.execute_step = _write_artifact_then_succeed  # type: ignore[method-assign]

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        payload={"steps": [{"command": "write"}]}, provider=fake_provider,
    )
    job = await run_worker_until_terminal(db, job.id)

    artifacts = db.query(ExecutionArtifact).filter(ExecutionArtifact.job_id == job.id).all()
    assert len(artifacts) == 1
    assert artifacts[0].filename == "report.txt"
    assert artifacts[0].size_bytes == 4


async def test_L16_two_workers_racing_same_job_only_one_claims(client, db, registered_user, fake_provider):
    """The core correctness property of the whole durable model: the
    compare-and-swap UPDATE...WHERE status='queued' claim (see
    execution_worker._try_claim()'s docstring) must let exactly ONE of two
    concurrent claim attempts against the SAME job succeed."""
    from datetime import datetime, timezone

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        payload={"steps": [{"command": "x"}]}, provider=fake_provider,
    )

    now = datetime.now(timezone.utc)
    results = await asyncio.gather(
        execution_worker._run_claimed_job(job.id, "worker-A"),
        execution_worker._run_claimed_job(job.id, "worker-B"),
    )
    assert sorted(results) == [False, True]  # exactly one claimed it
    assert len(fake_provider.calls) == 1  # the step ran exactly once

    db.expire_all()
    job = db.query(ExecutionJob).filter(ExecutionJob.id == job.id).first()
    assert job.status == "completed"
    assert job.attempt_number == 1  # never double-attempted


async def test_L17_crash_before_checkpoint_reruns_the_step(client, db, registered_user, fake_provider, run_worker_until_terminal, monkeypatch):
    """Simulates a worker dying mid-step (before that step's checkpoint
    commit) by force-expiring the lease immediately after claim, then
    letting reclaim_expired_leases() + a fresh claim pick it back up. The
    step must run again — nothing was checkpointed yet, so there is
    nothing to resume from."""
    from datetime import datetime, timedelta, timezone

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        payload={"steps": [{"command": "a"}, {"command": "b"}]}, provider=fake_provider, max_retries=2,
    )

    claimed = execution_worker._try_claim(db, job.id, worker_id="crashed-worker", now=datetime.now(timezone.utc))
    assert claimed is not None
    # Simulate the crash: force the lease into the past WITHOUT running
    # any step (checkpoint stays exactly as create_job() left it: empty).
    claimed.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    reconciled = execution_worker.reclaim_expired_leases(db)
    assert reconciled == 1

    job = await run_worker_until_terminal(db, job.id, timeout=15.0)
    assert job.status == "completed"
    # Both steps ran from a real (second) attempt — none were skipped as
    # "already checkpointed", because nothing was.
    assert len(fake_provider.calls) == 2
    assert job.checkpoint["completed_step_indices"] == [0, 1]


async def test_L18_crash_after_checkpoint_does_not_rerun_completed_step(client, db, registered_user, fake_provider, run_worker_until_terminal):
    """Same crash simulation as L17, but the job's checkpoint already
    records step 0 as completed (a real worker ran step 0, then 'died'
    before step 1) — the recovered attempt must resume at step 1, not
    re-run step 0 (mission requirement: 'não executar novamente uma etapa
    já concluída quando isso puder ser evitado'). The checkpoint state is
    constructed directly (rather than racing a real cancellation, which
    would go through _run_claimed_job's own GRACEFUL cancel handling — a
    different, already-covered code path, not a crash) so the recovery
    property itself is tested deterministically, with no timing race."""
    from datetime import datetime, timedelta, timezone

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        payload={"steps": [{"command": "a"}, {"command": "b"}]}, provider=fake_provider, max_retries=2,
    )

    claimed = execution_worker._try_claim(db, job.id, worker_id="crashed-worker", now=datetime.now(timezone.utc))
    assert claimed is not None
    claimed.checkpoint = {"completed_step_indices": [0], "step_results": {"0": "step0-ok"}}
    claimed.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    reconciled = execution_worker.reclaim_expired_leases(db)
    assert reconciled == 1

    job = await run_worker_until_terminal(db, job.id, timeout=15.0)
    assert job.status == "completed"
    # Exactly ONE real call — only step 1 (the recovered attempt); step 0
    # was never re-executed, only its checkpoint was ever consulted.
    assert len(fake_provider.calls) == 1
    assert job.checkpoint["completed_step_indices"] == [0, 1]


async def test_L19_worker_batch_size_bounds_claims_per_cycle(client, db, registered_user, fake_provider, monkeypatch):
    from src.config import settings as _settings
    monkeypatch.setattr(_settings, "execution_worker_batch_size", 2)

    jobs = [
        execution_service.create_job(
            db, user_id=registered_user["user_id"], plan="pro",
            payload={"steps": [{"command": f"job-{i}"}]}, provider=fake_provider,
        )
        for i in range(5)
    ]

    claimed_count = await execution_worker.run_worker_once()
    assert claimed_count == 2

    db.expire_all()
    statuses = [db.query(ExecutionJob).filter(ExecutionJob.id == j.id).first().status for j in jobs]
    assert statuses.count("queued") == 3
    assert statuses.count("completed") == 2
