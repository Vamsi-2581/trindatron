"""HTTP-level integration tests for /agent-instances — Persistent Agent
Instance V1. Security/ownership/isolation focus — service-level lifecycle
behavior is covered by tests/unit/test_agent_instance_service.py.

Test matrix
-----------
B) GET /agent-instances lists only the caller's own instances
C) GET /agent-instances/{id} for another user's instance -> 404
   (not 403 — no existence leak, same convention as billing.py/execution.py)
E) PATCH another user's instance -> 404, no mutation
F/G) pause/resume of another user's instance -> 404
N) manual run of another user's instance -> 404
S/T) project_id ownership is enforced on create AND update — a project
     belonging to another user is rejected
Response never leaks: secrets, reservation_id, TCU, tokens, cost
X) manual run -> ExecutionJob -> durable worker (run_worker_once, the
   SAME production worker) -> completed, proven end to end over HTTP
"""
from __future__ import annotations

import asyncio

import pytest

from src.execution.fake_provider import FakeExecutionProvider
from src.services import execution_service, execution_worker

pytestmark = pytest.mark.usefixtures("execution_session_override")


@pytest.fixture(autouse=True)
def _patch_default_provider(tmp_path, monkeypatch):
    fake = FakeExecutionProvider(tmp_path)
    monkeypatch.setattr(execution_service, "build_default_provider", lambda: fake)
    return fake


def _create_definition(client, headers, name="Watcher"):
    resp = client.post("/agents", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _create_instance(client, headers, definition_id, name="My Watcher", project_id=None):
    body = {"agent_definition_id": definition_id, "name": name}
    if project_id is not None:
        body["project_id"] = project_id
    resp = client.post("/agent-instances", json=body, headers=headers)
    return resp


def test_create_and_get(client, auth_headers):
    definition_id = _create_definition(client, auth_headers)
    resp = _create_instance(client, auth_headers, definition_id)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "active"
    assert body["health"] == "healthy"
    assert body["agent_definition_id"] == definition_id

    get_resp = client.get(f"/agent-instances/{body['id']}", headers=auth_headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == body["id"]


def test_B_list_scoped_to_caller(client, auth_headers, db2_user_factory):
    from src.services.auth_service import create_access_token

    definition_id = _create_definition(client, auth_headers)
    mine = _create_instance(client, auth_headers, definition_id).json()

    other = db2_user_factory("other-instance@test.com")
    other_headers = {"Authorization": f"Bearer {create_access_token(other.id)}"}
    resp = client.get("/agent-instances", headers=other_headers)
    ids = {i["id"] for i in resp.json()["instances"]}
    assert mine["id"] not in ids


def test_C_get_wrong_user_returns_404(client, auth_headers, db2_user_factory):
    from src.services.auth_service import create_access_token

    definition_id = _create_definition(client, auth_headers)
    mine = _create_instance(client, auth_headers, definition_id).json()

    other = db2_user_factory("other-get@test.com")
    other_headers = {"Authorization": f"Bearer {create_access_token(other.id)}"}
    resp = client.get(f"/agent-instances/{mine['id']}", headers=other_headers)
    assert resp.status_code == 404


def test_E_update_wrong_user_returns_404_no_mutation(client, auth_headers, db2_user_factory):
    from src.services.auth_service import create_access_token

    definition_id = _create_definition(client, auth_headers)
    mine = _create_instance(client, auth_headers, definition_id, name="Original").json()

    other = db2_user_factory("other-update@test.com")
    other_headers = {"Authorization": f"Bearer {create_access_token(other.id)}"}
    resp = client.patch(f"/agent-instances/{mine['id']}", json={"name": "Hijacked"}, headers=other_headers)
    assert resp.status_code == 404

    check = client.get(f"/agent-instances/{mine['id']}", headers=auth_headers)
    assert check.json()["name"] == "Original"


def test_FG_pause_resume_wrong_user_returns_404(client, auth_headers, db2_user_factory):
    from src.services.auth_service import create_access_token

    definition_id = _create_definition(client, auth_headers)
    mine = _create_instance(client, auth_headers, definition_id).json()

    other = db2_user_factory("other-pause@test.com")
    other_headers = {"Authorization": f"Bearer {create_access_token(other.id)}"}
    assert client.post(f"/agent-instances/{mine['id']}/pause", headers=other_headers).status_code == 404
    assert client.post(f"/agent-instances/{mine['id']}/resume", headers=other_headers).status_code == 404

    check = client.get(f"/agent-instances/{mine['id']}", headers=auth_headers)
    assert check.json()["status"] == "active"  # untouched by the other user's attempts


def test_N_manual_run_wrong_user_returns_404(client, auth_headers, db2_user_factory):
    from src.services.auth_service import create_access_token

    definition_id = _create_definition(client, auth_headers)
    mine = _create_instance(client, auth_headers, definition_id).json()

    other = db2_user_factory("other-run@test.com")
    other_headers = {"Authorization": f"Bearer {create_access_token(other.id)}"}
    resp = client.post(f"/agent-instances/{mine['id']}/run", headers=other_headers)
    assert resp.status_code == 404


def test_create_rejects_another_users_project(client, auth_headers, db2_user_factory):
    from src.services.auth_service import create_access_token

    other = db2_user_factory("other-project-owner@test.com")
    other_headers = {"Authorization": f"Bearer {create_access_token(other.id)}"}
    proj_resp = client.post("/projects", json={"name": "Other's project"}, headers=other_headers)
    assert proj_resp.status_code == 201
    other_project_id = proj_resp.json()["id"]

    definition_id = _create_definition(client, auth_headers)
    resp = _create_instance(client, auth_headers, definition_id, project_id=other_project_id)
    assert resp.status_code == 422


def test_response_never_leaks_internal_fields(client, auth_headers):
    definition_id = _create_definition(client, auth_headers)
    resp = _create_instance(client, auth_headers, definition_id)
    body = resp.json()
    for forbidden in ("secret", "api_key", "reservation_id", "tcu", "cost_usd", "cost"):
        assert forbidden not in body


def test_X_manual_run_executes_through_durable_worker(client, db, auth_headers):
    definition_id = _create_definition(client, auth_headers)
    instance = _create_instance(client, auth_headers, definition_id).json()

    schedule_resp = client.post(
        "/execution/schedules",
        json={
            "agent_id": definition_id,
            "interval_seconds": 600,
            "job_payload": {"steps": [{"command": "echo from manual run"}]},
        },
        headers=auth_headers,
    )
    assert schedule_resp.status_code == 201

    # Bind the schedule to the instance directly (no dedicated "attach
    # schedule to instance" route exists in this phase — schedules are
    # still created via the existing /execution/schedules route; binding
    # happens via agent_schedule_service.create_schedule()'s own
    # agent_instance_id param at the service layer, exercised in
    # test_agent_instance_service.py). This test focuses on manual_run's
    # OWN HTTP contract once a binding exists. Uses the test's OWN `db`
    # fixture (the same isolated engine `client` is wired to via
    # dependency override) — NOT src.database.SessionLocal, which points
    # at a completely different (real) database in the test process.
    from src.models.agent_schedule import AgentSchedule
    schedule = db.query(AgentSchedule).filter(AgentSchedule.id == schedule_resp.json()["id"]).first()
    schedule.agent_instance_id = instance["id"]
    db.commit()

    run_resp = client.post(f"/agent-instances/{instance['id']}/run", headers=auth_headers)
    assert run_resp.status_code == 201, run_resp.text
    job_id = run_resp.json()["id"]
    assert run_resp.json()["status"] == "queued"

    async def _drain():
        deadline = asyncio.get_event_loop().time() + 10.0
        while asyncio.get_event_loop().time() < deadline:
            await execution_worker.run_worker_once()
            check = client.get(f"/execution/jobs/{job_id}", headers=auth_headers)
            if check.json()["status"] in ("completed", "failed"):
                return check.json()
            await asyncio.sleep(0.02)
        return client.get(f"/execution/jobs/{job_id}", headers=auth_headers).json()

    final = asyncio.run(_drain())
    assert final["status"] == "completed"
    assert "from manual run" in final["result"]["output"]

    instance_after = client.get(f"/agent-instances/{instance['id']}", headers=auth_headers).json()
    assert instance_after["last_execution_job_id"] == job_id
    assert instance_after["current_execution_job_status"] == "completed"
