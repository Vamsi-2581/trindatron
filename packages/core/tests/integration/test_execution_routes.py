"""HTTP-level integration tests for /execution routes and
GET /users/me/capacity — Durable Execution.

Test matrix
-----------
R01) POST /execution/jobs — 201, job dispatched (queued, then claimed and
     run by the durable worker), status pollable afterwards
R02) POST /execution/jobs — auth required (401 without token)
R03) GET /execution/jobs/{id} — 404 for another user's job (no existence leak)
R04) POST /execution/jobs/{id}/cancel — 404 for another user's job
R05) GET /execution/jobs — lists only the caller's own jobs
R06) POST /execution/jobs with invalid payload -> 422
R07) POST /execution/jobs refused at 0% capacity for a non-critical job -> 429
R08) GET /users/me/capacity — response contains NO TCU field anywhere,
     only percentages/counts/status (mission section 11's hard requirement)
R09) GET /users/me/capacity — active_agents reflects a live ExecutionJob
     (observed WHILE a worker is genuinely still claimed/running it)
R10) Job created via HTTP keeps running after this "request" completes —
     proven by polling status again after the initial 201 response,
     without this test ever touching execution_worker's internals
     directly — the durable worker's own background poll loop
     (execution_worker.run_worker_loop(), started by the `client`
     fixture's app lifespan) is what actually claims and runs it, exactly
     like a real deploy: no test-side dispatch call of any kind.
R11) POST /execution/jobs never leaks worker_id/lease_expires_at/
     reservation_id/next_run_at/heartbeat_at — the new Durable Execution
     columns are exactly as internal as TCU (mission requirement 12)
"""
from __future__ import annotations

import asyncio
import time as _time

import pytest

from src.api.deps import get_current_user
from src.execution.fake_provider import FakeExecutionProvider
from src.execution.provider import StepOutcome
from src.main import app as _app
from src.models.execution import EXECUTION_TERMINAL_STATUSES, ExecutionJob
from src.services import execution_service, execution_worker

pytestmark = pytest.mark.usefixtures("execution_session_override")


def _order_body(**overrides) -> dict:
    base = {"job_type": "shell", "payload": {"steps": [{"command": "echo hi"}]}, "priority": "normal"}
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _patch_default_provider(tmp_path, monkeypatch):
    """Every route in this file goes through create_job() WITHOUT an
    explicit provider argument (real HTTP shape — the route never injects
    a fake), so build_default_provider() — called by
    execution_worker._resolve_provider() when it claims a job — must
    return a fake here instead of DockerExecutionProvider (no real Docker
    call in this test file)."""
    fake = FakeExecutionProvider(tmp_path)
    monkeypatch.setattr(execution_service, "build_default_provider", lambda: fake)
    return fake


def _run_worker_to_completion(job_id: str, *, timeout: float = 10.0) -> str | None:
    """Sync helper (this file's tests are plain `def`, matching real HTTP
    client shape — no async test function ever awaits the durable
    worker's own internals). Drives execution_worker.run_worker_once() in
    a fresh event loop until job_id reaches a terminal status. Opens its
    OWN db session per check (execution_service._new_session(), already
    pointed at the test engine by execution_session_override) rather than
    sharing one across the sync/async boundary."""
    async def _drain() -> str | None:
        deadline = _time.monotonic() + timeout
        last_status: str | None = None
        while True:
            try:
                await execution_worker.run_worker_once()
            except asyncio.CancelledError:
                pass
            db = execution_service._new_session()
            try:
                job = db.query(ExecutionJob).filter(ExecutionJob.id == job_id).first()
                last_status = job.status if job else None
                if job is not None and job.status in EXECUTION_TERMINAL_STATUSES:
                    return last_status
            finally:
                db.close()
            if _time.monotonic() >= deadline:
                return last_status
            await asyncio.sleep(0.02)

    return asyncio.run(_drain())


def test_R01_create_job_dispatches_and_completes(client, auth_headers):
    resp = client.post("/execution/jobs", json=_order_body(), headers=auth_headers)
    assert resp.status_code == 201, resp.text
    job = resp.json()
    assert job["status"] in ("queued", "starting", "running", "retrying", "completed")

    status = _run_worker_to_completion(job["id"])
    assert status == "completed"

    status_resp = client.get(f"/execution/jobs/{job['id']}", headers=auth_headers)
    assert status_resp.status_code == 200
    assert status_resp.json()["status"] == "completed"


def test_R02_create_job_requires_auth(client):
    resp = client.post("/execution/jobs", json=_order_body())
    assert resp.status_code == 401


def test_R03_get_job_wrong_user_returns_404(client, auth_headers, db2_user_factory):
    from src.services.auth_service import create_access_token

    create_resp = client.post("/execution/jobs", json=_order_body(), headers=auth_headers)
    job_id = create_resp.json()["id"]
    _run_worker_to_completion(job_id)

    other = db2_user_factory("other-exec@test.com")
    other_token = create_access_token(other.id)
    other_headers = {"Authorization": f"Bearer {other_token}"}

    resp = client.get(f"/execution/jobs/{job_id}", headers=other_headers)
    assert resp.status_code == 404


def test_R04_cancel_wrong_user_returns_404(client, auth_headers, db2_user_factory):
    from src.services.auth_service import create_access_token

    create_resp = client.post("/execution/jobs", json=_order_body(), headers=auth_headers)
    job_id = create_resp.json()["id"]

    other = db2_user_factory("other-cancel@test.com")
    other_token = create_access_token(other.id)
    other_headers = {"Authorization": f"Bearer {other_token}"}

    resp = client.post(f"/execution/jobs/{job_id}/cancel", headers=other_headers)
    assert resp.status_code == 404

    _run_worker_to_completion(job_id)


def test_R05_list_jobs_scoped_to_caller(client, auth_headers, db2_user_factory):
    from src.services.auth_service import create_access_token

    mine = client.post("/execution/jobs", json=_order_body(), headers=auth_headers)
    _run_worker_to_completion(mine.json()["id"])

    other = db2_user_factory("other-list@test.com")
    other_token = create_access_token(other.id)
    other_headers = {"Authorization": f"Bearer {other_token}"}
    other_job = client.post("/execution/jobs", json=_order_body(), headers=other_headers)
    _run_worker_to_completion(other_job.json()["id"])

    resp = client.get("/execution/jobs", headers=auth_headers)
    ids = {j["id"] for j in resp.json()["jobs"]}
    assert mine.json()["id"] in ids
    assert other_job.json()["id"] not in ids


def test_R06_invalid_payload_returns_422(client, auth_headers):
    resp = client.post("/execution/jobs", json=_order_body(payload={"not_steps": []}), headers=auth_headers)
    assert resp.status_code == 422


def test_R07_capacity_refused_returns_429(client, auth_headers, monkeypatch):
    from src.services import adaptive_budget_service

    monkeypatch.setattr(
        adaptive_budget_service, "compute_capacity",
        lambda *a, **k: adaptive_budget_service.CapacitySnapshot(
            tier=adaptive_budget_service.TIER_PAUSE_EXECUTION, capacity_remaining_pct=0.0,
            session_pct_used=100.0, session_resets_at=None, session_active=True,
            weekly_pct_used=100.0, weekly_resets_at=None,
        ),
    )
    resp = client.post("/execution/jobs", json=_order_body(priority="normal"), headers=auth_headers)
    assert resp.status_code == 429
    assert resp.json()["detail"]["error"] == "capacity_refused"


def test_R08_capacity_response_never_contains_tcu(client, auth_headers):
    resp = client.get("/users/me/capacity", headers=auth_headers)
    assert resp.status_code == 200
    body_text = resp.text.lower()
    assert "tcu" not in body_text
    data = resp.json()
    assert set(data.keys()) == {"tier", "capacity_remaining_pct", "session", "weekly", "active_agents", "optimizing"}


def test_R09_capacity_reflects_active_agent_instances_not_raw_jobs(client, db, registered_user, auth_headers, _patch_default_provider):
    """active_agents semantics corrected by Persistent Agent Instance V1:
    counts AgentInstance.status=="active" rows, NOT currently-running
    ExecutionJobs. A raw running job with no owning instance must NOT
    move the count; an active instance (even one with no job in flight
    right now) must."""
    from src.models.agent_definition import AgentDefinition
    from src.models.agent_instance import AgentInstance

    fake = _patch_default_provider
    async def _never_finishes(**_kwargs):
        await asyncio.sleep(2.0)
        return StepOutcome(success=True, output="ok", duration_ms=1)
    fake.execute_step = _never_finishes  # type: ignore[method-assign]

    create_resp = client.post("/execution/jobs", json=_order_body(), headers=auth_headers)
    job_id = create_resp.json()["id"]

    # A raw job with NO owning AgentInstance — must not count.
    baseline = client.get("/users/me/capacity", headers=auth_headers)
    assert baseline.json()["active_agents"] == 0

    # An idle (no job in flight) but ACTIVE instance — must count.
    definition = AgentDefinition(user_id=registered_user["user_id"], name="Watcher")
    db.add(definition)
    db.commit()
    db.refresh(definition)
    instance = AgentInstance(user_id=registered_user["user_id"], agent_definition_id=definition.id, name="My Watcher", status="active")
    db.add(instance)
    db.commit()

    idle_resp = client.get("/users/me/capacity", headers=auth_headers)
    assert idle_resp.json()["active_agents"] == 1

    # A paused instance must not count.
    instance.status = "paused"
    db.commit()
    paused_resp = client.get("/users/me/capacity", headers=auth_headers)
    assert paused_resp.json()["active_agents"] == 0
    instance.status = "active"
    db.commit()

    # One event loop for the whole claim-observe-cancel-drain sequence —
    # client.get()/post() are plain synchronous calls, safe to make from
    # inside this async function (they never touch OUR loop, only
    # TestClient's own internal one); the point is keeping worker_task
    # itself alive across the whole sequence on a SINGLE loop, since an
    # asyncio.Task cannot be awaited from a different loop than the one
    # that created it.
    async def _run_and_observe():
        worker_task = asyncio.create_task(execution_worker.run_worker_once())
        await asyncio.sleep(0.05)

        # The still-running raw job (no instance) plus the one active
        # instance -> still just 1 (the job alone never counted).
        capacity_resp = client.get("/users/me/capacity", headers=auth_headers)
        assert capacity_resp.json()["active_agents"] == 1

        client.post(f"/execution/jobs/{job_id}/cancel", headers=auth_headers)
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

    asyncio.run(_run_and_observe())


def test_R10_job_survives_after_initial_request_returns(client_with_production_loops, db):
    """No test-side dispatch call anywhere in this test — the durable
    worker's own background poll loop (started by the `client` fixture's
    app lifespan, same as production) is what claims and runs this job,
    simulating a client that disconnected right after the 201 and
    reconnected later to poll. execution_worker_poll_interval_seconds is
    shrunk below so the background loop's first cycle lands well inside
    this test's own polling window.

    CP8: uses client_with_production_loops (conftest.py) instead of the
    plain `client` fixture — every other test gets a minimal, loop-free
    lifespan (see conftest.py's docstring above `client`); this is one of
    the two tests (with R12 in test_execution_research_job.py) that
    deliberately needs the REAL timer-driven execution worker. Registers
    its own user instead of using `auth_headers`/`registered_user`, which
    both depend on the plain `client` fixture and would otherwise boot a
    second, overlapping TestClient/lifespan for this same test."""
    client = client_with_production_loops
    from src.config import settings as _settings
    reg = client.post("/auth/register", json={
        "email": "r10-e2e@test.com", "password": "securepass123", "full_name": "R10 E2E",
    })
    assert reg.status_code == 201
    reg_data = reg.json()
    from src.models.user import User
    user = db.query(User).filter(User.id == reg_data["user_id"]).one()
    user.plan = "start"
    db.commit()
    auth_headers = {"Authorization": f"Bearer {reg_data['access_token']}"}
    create_resp = client.post("/execution/jobs", json=_order_body(), headers=auth_headers)
    job_id = create_resp.json()["id"]

    resp = client.get(f"/execution/jobs/{job_id}", headers=auth_headers)
    assert resp.json()["status"] in ("queued", "starting", "running", "retrying", "completed")

    deadline = _time.monotonic() + max(5.0, _settings.execution_worker_poll_interval_seconds * 3)
    final = None
    while _time.monotonic() < deadline:
        final = client.get(f"/execution/jobs/{job_id}", headers=auth_headers)
        if final.json()["status"] == "completed":
            break
        _time.sleep(0.1)
    assert final is not None and final.json()["status"] == "completed"


def test_R11_response_never_leaks_worker_internals(client, auth_headers):
    create_resp = client.post("/execution/jobs", json=_order_body(), headers=auth_headers)
    job_id = create_resp.json()["id"]
    _run_worker_to_completion(job_id)

    resp = client.get(f"/execution/jobs/{job_id}", headers=auth_headers)
    body = resp.json()
    for internal_field in ("worker_id", "lease_expires_at", "heartbeat_at", "next_run_at", "reservation_id", "tcu", "cost_usd"):
        assert internal_field not in body, f"{internal_field!r} leaked in ExecutionJobResponse"
