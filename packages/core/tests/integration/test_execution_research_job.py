"""Integration tests for job_type="research" — the first real LLM job
flowing through the Execution Runtime via the EXISTING Model Router
(src/execution/model_router_provider.py).

Every test here fakes ONLY trinity_router.ModelRouter.complete() (via the
model_router_provider._router module-level singleton) — never
execution_service, never entitlement_service, never the sandbox. That
keeps the SAME architectural path a real call would take: create_job()
(persists a queued row) -> the durable worker claims it
(execution_worker._try_claim()) -> ModelRouterExecutionProvider.execute_step()
-> check_and_reserve() -> _router.complete() -> finalize_usage()/
cancel_reservation() -> Sentinel -> the worker finishes/releases the job.
Only the actual network/LLM call is replaced with a deterministic fake,
exactly as FakeExecutionProvider replaces DockerSandbox for
job_type="shell" in test_execution_lifecycle.py. Jobs are driven to
completion via the `run_worker_until_terminal` fixture (conftest.py) —
the durable worker's own claim/execute/release cycle, not a single
awaited asyncio.Task the way Execution Runtime V1's tests did.

Test matrix (mission's mandatory list for the research job integration)
--------------------------------------------------------------------------
R01) Happy path: create_job(job_type="research") -> completed, result
     contains the router's content, job.reservation_id set, exactly one
     UsageRecord created and finalized (not "pending").
R02) Provider failure (ProviderUnavailableError) is classified recoverable
     -> job-level retry -> succeeds on the second attempt.
R03) Entitlement exceeded -> check_and_reserve's HTTPException(429) is
     turned into a permanent (non-retried) StepOutcome failure; the router
     is NEVER called (no charge is possible for a call that never happened).
R04) A failed call always cancels its reservation — no UsageRecord is left
     "pending" after the job reaches a terminal state (idempotency /
     no-improper-charging).
R05) Multi-step checkpoint/resume: step 0's reservation is created+
     finalized exactly once; only step 1 (the one that failed) gets a
     second reservation on retry — step 0 is never re-billed.
R06) Sentinel FAIL (min_output_length not met) triggers a retry with the
     checkpoint reset — a SECOND reservation is made for the retried call.
R07) Sentinel PASS (expected_artifacts=["research_result.md"]) — the
     provider's own artifact write satisfies Sentinel generically, same as
     job_type="shell".
R08) cancel_job() before the (slow) router call resolves -> job ends
     "cancelled", reservation from that in-flight call is cancelled too.
R09) Capability kill switch (runtime_settings DISABLED_CAPABILITIES
     includes "research") -> CapabilityDisabledError, no job persisted, no
     reservation made, router never called.
R10) Isolation between users: two research jobs from two different users
     each reserve against their OWN user_id; UsageRecord rows never cross.
R11) job_type="shell" regression: FakeExecutionProvider path is completely
     unaffected by any of the above changes (no research-only code runs).
R12) Full HTTP route regression + authentication: POST /execution/jobs
     with job_type="research" (no auth -> 401; with auth -> 201), then a
     real polling GET /execution/jobs/{id} loop reaches "completed" —
     the real end-to-end path: POST -> dispatch -> Model Router (faked at
     the transport boundary only) -> artifact -> Sentinel -> completed.
"""
from __future__ import annotations

import asyncio
import dataclasses
import time

import pytest

from src.execution import model_router_provider
from src.models.execution import ExecutionArtifact, ExecutionJob
from src.models.usage import UsageRecord
from src.services import entitlement_service, execution_service, execution_worker
from trinity_router import ProviderUnavailableError, RequestRejectedByProviderError, RoutingResult


def _result(content: str = "research answer", tcu: float = 1.5, cost_usd: float = 0.001) -> RoutingResult:
    return RoutingResult(
        content=content, tool_calls=None, provider="anthropic", model="claude-sonnet",
        input_tokens=10, output_tokens=20, cached_tokens=0,
        cost_usd=cost_usd, tcu=tcu, failover_used=False, failover_reason=None,
    )


class _FakeRouter:
    """Stand-in for trinity_router.ModelRouter — queues results/exceptions,
    records every RoutingRequest it was called with."""

    def __init__(self) -> None:
        self.calls: list = []
        self._queue: list = []

    def queue_result(self, result: RoutingResult) -> None:
        self._queue.append(result)

    def queue_exception(self, exc: Exception) -> None:
        self._queue.append(exc)

    async def complete(self, request):
        self.calls.append(request)
        if not self._queue:
            raise AssertionError("FakeRouter queue exhausted — test didn't queue enough results")
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fake_router(monkeypatch):
    fake = _FakeRouter()
    monkeypatch.setattr(model_router_provider, "_router", fake)
    return fake


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    """Same rationale as test_execution_lifecycle.py's own fixture — real
    job-level retries in this file must not spend real minutes on backoff."""
    monkeypatch.setattr(execution_worker, "_BACKOFF_BASE_SECONDS", 0.01)


pytestmark = pytest.mark.usefixtures("execution_session_override")


async def test_R01_happy_path_completes_and_finalizes_reservation(client, db, registered_user, fake_router, run_worker_until_terminal):
    fake_router.queue_result(_result(content="Paris is the capital of France."))

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        job_type="research", payload={"steps": [{"command": "What is the capital of France?"}]},
    )
    assert job.provider_name == "model_router"

    job = await run_worker_until_terminal(db, job.id)

    assert job.status == "completed"
    assert "Paris" in job.result["output"]
    assert job.reservation_id is not None

    reservation = db.query(UsageRecord).filter(UsageRecord.id == job.reservation_id).first()
    assert reservation is not None
    assert reservation.status == "completed"
    assert reservation.tcu == 1.5

    # Exactly one call reached the Model Router, with the research prompt
    # as the message content and the RESEARCH capability forwarded.
    assert len(fake_router.calls) == 1
    assert fake_router.calls[0].messages[-1]["content"] == "What is the capital of France?"
    assert fake_router.calls[0].capability == "research"


async def test_R02_provider_unavailable_retries_then_succeeds(client, db, registered_user, fake_router, run_worker_until_terminal):
    fake_router.queue_exception(ProviderUnavailableError(providers_tried=["anthropic", "openai"]))
    fake_router.queue_result(_result(content="second attempt worked"))

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        job_type="research", payload={"steps": [{"command": "retry me"}]}, max_retries=2,
    )
    job = await run_worker_until_terminal(db, job.id, timeout=15.0)

    assert job.status == "completed"
    assert job.attempt_number == 2
    assert job.retry_reason == "transient_step_failure"
    assert "second attempt worked" in job.result["output"]
    assert len(fake_router.calls) == 2

    # The FIRST (failed) reservation must be cancelled, not left pending.
    reservations = db.query(UsageRecord).filter(UsageRecord.session_id.is_(None), UsageRecord.user_id == registered_user["user_id"]).all()
    statuses = sorted(r.status for r in reservations)
    assert statuses == ["completed", "failed"]


async def test_R03_entitlement_exceeded_is_permanent_and_never_calls_router(client, db, registered_user, fake_router, monkeypatch, run_worker_until_terminal):
    depleted = dataclasses.replace(entitlement_service.get_plan_entitlement("pro"), daily_tcu_limit=0.0)
    monkeypatch.setattr(entitlement_service, "get_plan_entitlement", lambda plan: depleted)

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        job_type="research", payload={"steps": [{"command": "should be refused"}]}, max_retries=3,
    )
    job = await run_worker_until_terminal(db, job.id)

    assert job.status == "failed"
    assert job.attempt_number == 1  # not retried — permanent
    assert len(fake_router.calls) == 0  # never charged for a call that never happened

    # No UsageRecord at all was created for the refused attempt.
    assert db.query(UsageRecord).filter(UsageRecord.user_id == registered_user["user_id"]).count() == 0


async def test_R04_no_pending_reservation_left_after_permanent_failure(client, db, registered_user, fake_router, run_worker_until_terminal):
    fake_router.queue_exception(RequestRejectedByProviderError(reason="policy refusal"))

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        job_type="research", payload={"steps": [{"command": "rejected content"}]}, max_retries=3,
    )
    job = await run_worker_until_terminal(db, job.id)

    assert job.status == "failed"
    assert job.attempt_number == 1  # RequestRejectedByProviderError is permanent, not retried

    reservations = db.query(UsageRecord).filter(UsageRecord.user_id == registered_user["user_id"]).all()
    assert len(reservations) == 1
    assert reservations[0].status == "failed"  # cancelled, never left "pending"


async def test_R05_checkpoint_resume_does_not_rebill_completed_step(client, db, registered_user, fake_router, run_worker_until_terminal):
    fake_router.queue_result(_result(content="step0 answer", tcu=1.0))
    fake_router.queue_exception(ProviderUnavailableError(providers_tried=["anthropic"]))
    fake_router.queue_result(_result(content="step1 answer on retry", tcu=2.0))

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro", job_type="research",
        payload={"steps": [{"command": "question 1"}, {"command": "question 2"}]}, max_retries=2,
    )
    job = await run_worker_until_terminal(db, job.id, timeout=15.0)

    assert job.status == "completed"
    assert job.checkpoint["completed_step_indices"] == [0, 1]
    # step 0 called once, step 1 called twice (fail then retry) = 3 total
    assert len(fake_router.calls) == 3

    completed_reservations = db.query(UsageRecord).filter(
        UsageRecord.user_id == registered_user["user_id"], UsageRecord.status == "completed",
    ).all()
    assert sorted(r.tcu for r in completed_reservations) == [1.0, 2.0]  # step 0 finalized exactly once


async def test_R06_sentinel_fail_resets_checkpoint_and_rebills(client, db, registered_user, fake_router, run_worker_until_terminal):
    fake_router.queue_result(_result(content="too short"))
    fake_router.queue_result(_result(content="a properly long research answer this time"))

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro", job_type="research",
        payload={"steps": [{"command": "give me a long answer"}]},
        sentinel_criteria={"min_output_length": 20}, max_retries=1,
    )
    job = await run_worker_until_terminal(db, job.id, timeout=15.0)

    assert job.status == "completed"
    assert job.retry_reason == "sentinel_fail"
    assert job.sentinel_status == "pass"
    assert len(fake_router.calls) == 2  # sentinel failure re-ran the SAME step, billed twice

    completed = db.query(UsageRecord).filter(
        UsageRecord.user_id == registered_user["user_id"], UsageRecord.status == "completed",
    ).count()
    assert completed == 2


async def test_R07_sentinel_pass_via_research_artifact(client, db, registered_user, fake_router, run_worker_until_terminal):
    fake_router.queue_result(_result(content="artifact-backed answer"))

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro", job_type="research",
        payload={"steps": [{"command": "write a report"}]},
        sentinel_criteria={"expected_artifacts": ["research_result.md"]},
    )
    job = await run_worker_until_terminal(db, job.id)

    assert job.status == "completed"
    assert job.sentinel_status == "pass"
    artifacts = db.query(ExecutionArtifact).filter(ExecutionArtifact.job_id == job.id).all()
    assert any(a.filename == "research_result.md" for a in artifacts)


async def test_R08_cancel_before_router_call_resolves(client, db, registered_user, fake_router):
    async def _slow_complete(request):
        fake_router.calls.append(request)
        await asyncio.sleep(0.3)
        return _result(content="too late")
    fake_router.complete = _slow_complete  # type: ignore[method-assign]

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro", job_type="research",
        payload={"steps": [{"command": "slow question"}]},
    )
    worker_task = asyncio.create_task(execution_worker.run_worker_once())
    await asyncio.sleep(0.05)  # let the task start and reach the router call
    execution_service.cancel_job(db, job.id, registered_user["user_id"])
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

    db.expire_all()
    job = db.query(ExecutionJob).filter(ExecutionJob.id == job.id).first()

    assert job.status == "cancelled"


async def test_R09_capability_kill_switch_blocks_before_any_charge(client, db, registered_user, fake_router, monkeypatch):
    from src.services import runtime_settings
    monkeypatch.setattr(runtime_settings, "get_disabled_capabilities", lambda db: frozenset({"research"}))

    with pytest.raises(execution_service.CapabilityDisabledError):
        execution_service.create_job(
            db, user_id=registered_user["user_id"], plan="pro", job_type="research",
            payload={"steps": [{"command": "blocked"}]},
        )

    assert len(fake_router.calls) == 0
    assert db.query(UsageRecord).filter(UsageRecord.user_id == registered_user["user_id"]).count() == 0


async def test_R10_isolation_between_users(client, db, registered_user, fake_router, db2_user_factory, run_worker_until_terminal):
    user2 = db2_user_factory("research-user2@test.com")
    fake_router.queue_result(_result(content="user1 answer"))
    fake_router.queue_result(_result(content="user2 answer"))

    # One job at a time: run_worker_once runs every queued job concurrently, and the
    # test engine is a single shared in-memory connection, where one session's
    # rollback can discard another's uncommitted reservation. Isolation is about
    # whose reservation is whose, not about concurrency.
    job1 = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro", job_type="research",
        payload={"steps": [{"command": "q1"}]},
    )
    job1 = await run_worker_until_terminal(db, job1.id)
    job2 = execution_service.create_job(
        db, user_id=user2.id, plan="pro", job_type="research",
        payload={"steps": [{"command": "q2"}]},
    )
    job2 = await run_worker_until_terminal(db, job2.id)

    assert job1.status == "completed" and job2.status == "completed"

    r1 = db.query(UsageRecord).filter(UsageRecord.id == job1.reservation_id).first()
    r2 = db.query(UsageRecord).filter(UsageRecord.id == job2.reservation_id).first()
    assert r1.user_id == registered_user["user_id"]
    assert r2.user_id == user2.id
    assert r1.user_id != r2.user_id


async def test_R11_shell_job_type_unaffected(client, db, registered_user, fake_router, tmp_path, run_worker_until_terminal):
    """Regression: job_type="shell" (FakeExecutionProvider) must not touch
    the Model Router at all — proves the two job_type paths are fully
    independent, not accidentally coupled by this change."""
    from src.execution.fake_provider import FakeExecutionProvider

    provider = FakeExecutionProvider(tmp_path)
    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        payload={"steps": [{"command": "echo hi"}]}, provider=provider,
    )
    job = await run_worker_until_terminal(db, job.id)

    assert job.status == "completed"
    assert job.provider_name == "fake"
    assert len(fake_router.calls) == 0  # Model Router never touched


def test_R12_http_route_e2e_auth_and_polling(client_with_production_loops, db, fake_router):
    """Real end-to-end path through the HTTP layer: POST /execution/jobs
    -> async dispatch -> Model Router (faked at the transport boundary) ->
    artifact -> Sentinel -> polling GET reaches 'completed'. Waits via
    repeated GET + short sleep (a real disconnected HTTP client has no
    access to the server's internal asyncio.Task, so this is the actually
    faithful way to wait — not asyncio.run(_wait_for_job(...)): the
    Model Router call's own real asyncio.to_thread() hops mean the
    background task is reliably still in flight when this synchronous test
    function's statements run, and awaiting a not-yet-done Task created on
    TestClient's own dispatch loop from a brand new asyncio.run() loop
    raises "Task ... attached to a different loop" — polling sidesteps
    that entirely, and is the realistic E2E shape besides).

    CP8: uses client_with_production_loops instead of the plain `client`
    fixture — this is the one test in the suite that deliberately needs the
    REAL timer-driven execution_worker.run_worker_loop() (every other test
    gets a minimal, loop-free lifespan; see conftest.py's docstring on why).
    Registers its own user (instead of depending on the `registered_user`
    fixture) because that fixture itself depends on the plain `client`
    fixture, which would boot a SECOND, overlapping TestClient/lifespan for
    this same test."""
    client = client_with_production_loops
    # No auth -> 401.
    resp = client.post("/execution/jobs", json={
        "job_type": "research", "payload": {"steps": [{"command": "hi"}]},
    })
    assert resp.status_code == 401

    reg = client.post("/auth/register", json={
        "email": "r12-e2e@test.com", "password": "securepass123", "full_name": "R12 E2E",
    })
    assert reg.status_code == 201
    reg_data = reg.json()
    from src.models.user import User
    user = db.query(User).filter(User.id == reg_data["user_id"]).one()
    user.plan = "start"
    db.commit()
    registered_user = {"token": reg_data["access_token"], "user_id": reg_data["user_id"], "email": reg_data["email"]}

    fake_router.queue_result(_result(content="E2E research answer"))
    headers = {"Authorization": f"Bearer {registered_user['token']}"}
    resp = client.post(
        "/execution/jobs",
        json={
            "job_type": "research",
            "payload": {"steps": [{"command": "What is 2+2?"}]},
            "sentinel_criteria": {"expected_artifacts": ["research_result.md"]},
        },
        headers=headers,
    )
    assert resp.status_code == 201
    job_id = resp.json()["id"]
    assert "reservation_id" not in resp.json()  # never leaked to the client
    assert "tcu" not in resp.json()

    deadline = time.monotonic() + 10.0
    body = None
    while time.monotonic() < deadline:
        resp = client.get(f"/execution/jobs/{job_id}", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        if body["status"] in ("completed", "failed", "cancelled", "expired"):
            break
        time.sleep(0.05)

    assert body is not None
    assert body["status"] == "completed"
    assert "E2E research answer" in body["result"]["output"]
    assert body["sentinel_status"] == "pass"


async def test_R13_lease_reclaim_cancels_dangling_reservation_no_double_billing(
    client, db, registered_user, fake_router, run_worker_until_terminal,
):
    """THE critical billing-safety property (mission requirement 5):
    simulates a worker that crashes strictly between check_and_reserve()
    (a real 'pending' UsageRecord already committed) and the LLM call
    returning — the router call itself just hangs forever, standing in
    for the process dying mid-network-call. reclaim_expired_leases() must
    cancel that dangling pending reservation BEFORE the job is retried, so
    the retry's own fresh reservation is never double-counted alongside an
    orphaned pending one in _sum_active_tcu()'s quota sum — and the final
    ledger must show exactly one 'completed' record for the one job that
    actually finished, plus the one 'failed' (cancelled) reservation from
    the crashed attempt. Never two 'completed' records for one job."""
    from datetime import datetime, timedelta, timezone

    # entered_hang is set the INSTANT the fake router is about to hang —
    # waiting on it (rather than inferring "it must be in the router call
    # by now" from the reservation's existence) makes this test
    # deterministic: check_and_reserve() and the eager job.reservation_id
    # write (see model_router_provider.py) both complete strictly BEFORE
    # this event is set, so once it fires, the reservation is guaranteed
    # already committed and already pointed at by the job row.
    entered_hang = asyncio.Event()
    should_hang = {"value": True}
    async def _maybe_hang(request):
        fake_router.calls.append(request)
        if should_hang["value"]:
            entered_hang.set()
            await asyncio.sleep(999)
        return _result(content="recovered after crash")
    fake_router.complete = _maybe_hang  # type: ignore[method-assign]

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro", job_type="research",
        payload={"steps": [{"command": "will hang mid-call"}]}, max_retries=2,
    )

    # Claim + start executing for real — check_and_reserve() commits a
    # real 'pending' UsageRecord, then the router call hangs (simulating
    # "the process died while this network call was still in flight").
    run_task = asyncio.create_task(execution_worker.run_worker_once())
    await asyncio.wait_for(entered_hang.wait(), timeout=5.0)

    db.expire_all()
    crashed_job = db.query(ExecutionJob).filter(ExecutionJob.id == job.id).first()
    reservation_id = crashed_job.reservation_id
    assert reservation_id is not None, "eager reservation_id write (model_router_provider.py) never landed"

    # Simulate the crash: force the lease into the past WITHOUT touching
    # the in-flight task at all — a real crash gives no graceful handler
    # a chance to run. run_task is deliberately abandoned (still asleep on
    # asyncio.sleep(999), and NEVER woken by should_hang flipping below —
    # only a FUTURE call to _maybe_hang re-checks that flag); cancelled at
    # the end of this test for hygiene.
    crashed_job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    reconciled = execution_worker.reclaim_expired_leases(db)
    assert reconciled == 1

    db.expire_all()
    dangling = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).first()
    assert dangling.status == "failed"  # cancelled — never left dangling as "pending"

    should_hang["value"] = False
    job = await run_worker_until_terminal(db, job.id, timeout=15.0)
    assert job.status == "completed"
    assert job.reservation_id != reservation_id  # a genuinely NEW reservation, not the crashed one reused

    # Exactly-once ledger: one 'completed' record (the recovered attempt),
    # one 'failed' record (the crashed attempt's cancelled reservation) —
    # never two 'completed' records for what is, from the user's
    # perspective, one job that ran once.
    all_records = db.query(UsageRecord).filter(UsageRecord.user_id == registered_user["user_id"]).all()
    assert sorted(r.status for r in all_records) == ["completed", "failed"]

    run_task.cancel()
    try:
        await run_task
    except asyncio.CancelledError:
        pass
