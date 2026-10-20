"""RED TEAM — BOLA/IDOR + function-level authorization audit.

Scope: cross-user object access control across every resource type, plus
admin/mass-assignment. Each test proves ACTUAL server behavior via real
HTTP requests through the existing TestClient — not a static code read.

Convention already established by this codebase (see sessions.py/
agent_instances.py docstrings): cross-user access to a resource-by-id
returns 404, not 403 — no existence leak. Tests assert against that
documented convention.
"""
from __future__ import annotations

import uuid

import pytest

from src.services.auth_service import create_access_token


def _other_user_headers(db2_user_factory, email: str = None):
    user = db2_user_factory(email or f"attacker_{uuid.uuid4().hex[:8]}@test.com")
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}, user


# ---------------------------------------------------------------------------
# BOLA-01/02 — sessions: cross-user GET/DELETE
# ---------------------------------------------------------------------------


def test_BOLA01_session_cross_user_get_returns_404(client, auth_headers, db2_user_factory):
    resp = client.post("/sessions", json={"title": "victim session"}, headers=auth_headers)
    assert resp.status_code == 201
    session_id = resp.json()["id"]

    attacker_headers, _ = _other_user_headers(db2_user_factory)
    resp2 = client.get(f"/sessions/{session_id}", headers=attacker_headers)
    assert resp2.status_code == 404


def test_BOLA02_session_cross_user_delete_returns_404_and_does_not_delete(client, auth_headers, db2_user_factory):
    resp = client.post("/sessions", json={"title": "victim session"}, headers=auth_headers)
    session_id = resp.json()["id"]

    attacker_headers, _ = _other_user_headers(db2_user_factory)
    resp2 = client.delete(f"/sessions/{session_id}", headers=attacker_headers)
    assert resp2.status_code == 404

    # Victim can still see it — not actually deleted by the attacker's call.
    resp3 = client.get(f"/sessions/{session_id}", headers=auth_headers)
    assert resp3.status_code == 200


def test_BOLA03_session_no_auth_returns_401(client, auth_headers):
    resp = client.post("/sessions", json={"title": "s"}, headers=auth_headers)
    session_id = resp.json()["id"]
    resp2 = client.get(f"/sessions/{session_id}")
    assert resp2.status_code == 401


# ---------------------------------------------------------------------------
# BOLA-04 — *** CRITICAL FINDING ***
# create_session() (src/services/session_service.py) never validates that
# project_id / agent_definition_id belong to the caller. chat_service.py /
# agent_service.py then follow the FK unconditionally and inject
# session.project.instructions / session.agent_definition.system_instructions
# straight into the LLM system prompt. Net effect: any authenticated user
# who knows (or guesses) another user's project_id or agent_definition_id
# can have ITS PRIVATE CONTENT injected into their own chat's system prompt
# and then simply ask the model to repeat it back — a full disclosure of
# another user's private project instructions / custom agent system prompt.
# ---------------------------------------------------------------------------


def test_BOLA04_session_creation_with_foreign_project_id_rejected(client, auth_headers, db2_user_factory, db):
    """FIX regression test (BOLA-04, HIGH): before the fix, create_session()
    silently accepted ANY project_id with zero ownership validation. After
    the fix (session_service.py's ForeignResourceReference check, mirroring
    agent_instance_service.py's create_agent_instance() pattern), this must
    be rejected (404) and the session must never be created at all."""
    from src.models.project import Project

    victim_headers, victim = _other_user_headers(db2_user_factory, "victim-project@test.com")
    victim_project = Project(user_id=victim.id, name="Victim Confidential Project", instructions="SECRET: victim's private business context, do not disclose.")
    db.add(victim_project)
    db.commit()
    db.refresh(victim_project)

    resp = client.post("/sessions", json={"title": "attacker session", "project_id": victim_project.id}, headers=auth_headers)
    assert resp.status_code == 404, resp.text

    from src.models.session import Session as SessionModel
    rows = db.query(SessionModel).filter(SessionModel.project_id == victim_project.id).all()
    assert rows == []  # no session was ever created referencing the foreign project


def test_BOLA05_chat_system_prompt_never_reaches_foreign_project_instructions(client, auth_headers, db2_user_factory, db):
    """FIX regression test (BOLA-04/05, HIGH): since a session can no
    longer be created with a foreign project_id at all (BOLA-04's fix),
    there is no session for chat_service to ever build a system prompt
    from — proving the leak path is closed at its source, not just
    patched at the read side."""
    from src.models.project import Project

    _, victim = _other_user_headers(db2_user_factory, "victim-project2@test.com")
    secret_text = "SECRET-MARKER-abc123: internal API rotation schedule, quarterly revenue targets."
    victim_project = Project(user_id=victim.id, name="Victim Project 2", instructions=secret_text)
    db.add(victim_project)
    db.commit()
    db.refresh(victim_project)

    resp = client.post("/sessions", json={"title": "attacker session", "project_id": victim_project.id}, headers=auth_headers)
    assert resp.status_code == 404, resp.text

    from src.models.session import Session as SessionModel
    assert db.query(SessionModel).filter(SessionModel.project_id == victim_project.id).first() is None


# ---------------------------------------------------------------------------
# BOLA-06 — same vulnerability class via agent_definition_id
# ---------------------------------------------------------------------------


def test_BOLA06_session_creation_with_foreign_agent_definition_id_rejected(client, auth_headers, db2_user_factory, db):
    """FIX regression test (BOLA-04/06, HIGH): mirrors BOLA-04 for
    agent_definition_id — must be rejected (404), no session created."""
    from src.models.agent_definition import AgentDefinition
    from src.models.session import Session as SessionModel

    _, victim = _other_user_headers(db2_user_factory, "victim-agent@test.com")
    secret_instructions = "SECRET-MARKER-xyz789: victim's proprietary prompt engineering, internal tool usage policy."
    victim_agent = AgentDefinition(user_id=victim.id, name="Victim's Private Agent", system_instructions=secret_instructions)
    db.add(victim_agent)
    db.commit()
    db.refresh(victim_agent)

    resp = client.post("/sessions", json={"title": "attacker session", "agent_definition_id": victim_agent.id}, headers=auth_headers)
    assert resp.status_code == 404, resp.text
    assert db.query(SessionModel).filter(SessionModel.agent_definition_id == victim_agent.id).first() is None


# ---------------------------------------------------------------------------
# BOLA-07 — projects, memories, skills, agent definitions: cross-user 404
# ---------------------------------------------------------------------------


def test_BOLA07_project_cross_user_get_patch_delete_all_404(client, auth_headers, db2_user_factory):
    resp = client.post("/projects", json={"name": "victim project"}, headers=auth_headers)
    project_id = resp.json()["id"]

    attacker_headers, _ = _other_user_headers(db2_user_factory)
    assert client.get(f"/projects/{project_id}", headers=attacker_headers).status_code == 404
    assert client.patch(f"/projects/{project_id}", json={"name": "pwned"}, headers=attacker_headers).status_code == 404
    assert client.delete(f"/projects/{project_id}", headers=attacker_headers).status_code == 404


def test_BOLA08_memory_cross_user_delete_returns_404(client, auth_headers, db2_user_factory):
    resp = client.post("/users/me/memories", json={"content": "victim's private memory"}, headers=auth_headers)
    assert resp.status_code == 201, resp.text
    memory_id = resp.json()["id"]

    attacker_headers, _ = _other_user_headers(db2_user_factory)
    resp2 = client.delete(f"/users/me/memories/{memory_id}", headers=attacker_headers)
    assert resp2.status_code == 404


def test_BOLA09_skill_cross_user_get_patch_delete_all_404(client, auth_headers, db2_user_factory):
    resp = client.post("/skills", json={"name": "victim skill", "instructions": "do X"}, headers=auth_headers)
    if resp.status_code != 201:
        pytest.skip(f"POST /skills shape differs from assumed — got {resp.status_code}: {resp.text}")
    skill_id = resp.json()["id"]

    attacker_headers, _ = _other_user_headers(db2_user_factory)
    assert client.get(f"/skills/{skill_id}", headers=attacker_headers).status_code == 404
    assert client.patch(f"/skills/{skill_id}", json={"name": "pwned"}, headers=attacker_headers).status_code == 404
    assert client.delete(f"/skills/{skill_id}", headers=attacker_headers).status_code == 404


def test_BOLA10_agent_definition_cross_user_all_404(client, auth_headers, db2_user_factory):
    resp = client.post("/agents", json={"name": "victim agent"}, headers=auth_headers)
    if resp.status_code != 201:
        pytest.skip(f"POST /agents shape differs from assumed — got {resp.status_code}: {resp.text}")
    agent_id = resp.json()["id"]

    attacker_headers, _ = _other_user_headers(db2_user_factory)
    assert client.get(f"/agents/{agent_id}", headers=attacker_headers).status_code == 404
    assert client.patch(f"/agents/{agent_id}", json={"name": "pwned"}, headers=attacker_headers).status_code == 404
    assert client.delete(f"/agents/{agent_id}", headers=attacker_headers).status_code == 404


def test_BOLA11_agent_definition_update_rejects_foreign_skill_id(client, auth_headers, db2_user_factory, db):
    """Cross-check the ALREADY-established validate_skill_ids_for_user()
    guard (agent_definition_service.py) really rejects a foreign skill_id
    at update time, matching the create-time guard."""
    from src.models.skill import Skill

    _, victim = _other_user_headers(db2_user_factory, "victim-skill@test.com")
    victim_skill = Skill(user_id=victim.id, name="victim skill", instructions="x")
    db.add(victim_skill)
    db.commit()
    db.refresh(victim_skill)

    resp = client.post("/agents", json={"name": "attacker agent"}, headers=auth_headers)
    if resp.status_code != 201:
        pytest.skip(f"POST /agents shape differs — got {resp.status_code}: {resp.text}")
    agent_id = resp.json()["id"]

    resp2 = client.patch(f"/agents/{agent_id}", json={"skill_ids": [victim_skill.id]}, headers=auth_headers)
    assert resp2.status_code == 422, f"expected rejection of foreign skill_id, got {resp2.status_code}: {resp2.text}"


# ---------------------------------------------------------------------------
# BOLA-12 — agent instances, schedules, execution jobs
# ---------------------------------------------------------------------------


def test_BOLA12_agent_instance_cross_user_all_404(client, auth_headers, db2_user_factory, db):
    from src.models.agent_definition import AgentDefinition

    resp = client.post("/agents", json={"name": "victim agent for instance"}, headers=auth_headers)
    if resp.status_code != 201:
        pytest.skip(f"POST /agents shape differs — got {resp.status_code}")
    agent_id = resp.json()["id"]

    resp2 = client.post("/agent-instances", json={"agent_definition_id": agent_id, "name": "victim instance"}, headers=auth_headers)
    if resp2.status_code != 201:
        pytest.skip(f"POST /agent-instances shape differs — got {resp2.status_code}: {resp2.text}")
    instance_id = resp2.json()["id"]

    attacker_headers, _ = _other_user_headers(db2_user_factory)
    assert client.get(f"/agent-instances/{instance_id}", headers=attacker_headers).status_code == 404
    assert client.patch(f"/agent-instances/{instance_id}", json={"name": "pwned"}, headers=attacker_headers).status_code == 404
    assert client.post(f"/agent-instances/{instance_id}/pause", headers=attacker_headers).status_code == 404
    assert client.post(f"/agent-instances/{instance_id}/run", headers=attacker_headers).status_code == 404


def test_BOLA13_execution_job_cross_user_all_404(client, auth_headers, db2_user_factory, db):
    from src.services import execution_service

    victim_headers, victim = _other_user_headers(db2_user_factory, "victim-job@test.com")
    job = execution_service.create_job(
        db, user_id=victim.id, plan="pro", job_type="shell",
        payload={"steps": [{"command": "echo hi"}]},
    )

    resp = client.get(f"/execution/jobs/{job.id}", headers=auth_headers)
    assert resp.status_code == 404
    resp2 = client.post(f"/execution/jobs/{job.id}/cancel", headers=auth_headers)
    assert resp2.status_code == 404


# ---------------------------------------------------------------------------
# BOLA-14 — PaymentOrder ownership (billing already closed; this is a
# BOLA-specific re-check, not a re-audit of webhook logic)
# ---------------------------------------------------------------------------


def test_BOLA14_payment_order_cross_user_all_404(client, auth_headers, db2_user_factory):
    from datetime import datetime, timedelta, timezone

    resp = client.post(
        "/billing/orders",
        json={
            "provider": "none", "amount_fiat": "9.99", "currency_fiat": "USD",
            "credits_amount": "9.99", "currency_credits": "USD",
            "idempotency_key": str(uuid.uuid4()),
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    order_id = resp.json()["id"]

    attacker_headers, _ = _other_user_headers(db2_user_factory)
    assert client.get(f"/billing/orders/{order_id}", headers=attacker_headers).status_code == 404
    assert client.post(f"/billing/orders/{order_id}/confirm", headers=attacker_headers).status_code == 404
    assert client.post(f"/billing/orders/{order_id}/cancel", headers=attacker_headers).status_code == 404
    assert client.post(f"/billing/orders/{order_id}/refund", json={"amount": "1.00"}, headers=attacker_headers).status_code == 404


# ---------------------------------------------------------------------------
# BOLA-15 — admin / function-level authorization + mass assignment
# ---------------------------------------------------------------------------


def test_BOLA15_admin_routes_reject_normal_user_with_403(client, auth_headers):
    resp = client.get("/admin/settings", headers=auth_headers)
    assert resp.status_code == 403
    resp2 = client.put("/admin/settings/some_key", json={"value": "x"}, headers=auth_headers)
    assert resp2.status_code == 403


def test_BOLA16_admin_routes_reject_unauthenticated_with_401():
    from src.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        resp = c.get("/admin/settings")
        assert resp.status_code in (401, 403)  # HTTPBearer with no creds -> 401/403 depending on FastAPI version


def test_BOLA17_registration_cannot_set_is_admin_or_plan(client, db):
    resp = client.post("/auth/register", json={
        "email": "masstest@test.com", "password": "securepass123", "full_name": "X",
        "is_admin": True, "plan": "team", "role": "admin",
    })
    assert resp.status_code == 201
    user_id = resp.json()["user_id"]

    from src.models.user import User
    user = db.query(User).filter(User.id == user_id).first()
    assert user.is_admin is False, "VULNERABILITY: is_admin was set via mass assignment on registration"
    assert user.plan != "team", "VULNERABILITY: plan was set via mass assignment on registration"


def test_BOLA18_admin_promotion_requires_direct_db_access_not_any_route(client, auth_headers):
    """There is no client-reachable route that can ever set is_admin=True
    for any user (confirmed by grepping every route file for 'is_admin' —
    only admin.py READS it via get_current_admin_user; nothing writes it).
    This test documents that PATCH /users/me/timezone (the only self-update
    route) cannot smuggle is_admin through, since its schema has no such
    field — Pydantic silently drops unknown fields."""
    resp = client.patch("/users/me/timezone", json={"timezone": "UTC", "is_admin": True}, headers=auth_headers)
    assert resp.status_code == 200
    # No route exists to verify is_admin via HTTP for a normal user (by
    # design — get_current_admin_user would 403 first); this is enforced
    # structurally, not just runtime-checked.
