"""
Integration tests — GET /projects/{project_id}/agents.

The endpoint is implemented in agent_definitions.py (already registered in main.py).
These tests cover the cases required by the task spec:
  1. requires_auth           — 401 without token
  2. project_not_found       — 404 for unknown project_id
  3. authorized_empty_list   — 200 empty list when no agents assigned
  4. authorized_with_agents  — 200 list with the assigned agents
  5. response_shape          — each agent has expected fields, no secrets
  6. cross_user_forbidden    — user B cannot read user A's project agents
"""
from __future__ import annotations

import uuid

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(client) -> dict:
    email = f"pal_{uuid.uuid4().hex[:8]}@test.com"
    r = client.post("/auth/register", json={"email": email, "password": "Pass1234!", "full_name": "T"})
    assert r.status_code == 201, r.text
    return r.json()


def _create_project(client, token: str) -> str:
    r = client.post("/projects", json={"name": "Agent list test"}, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _create_agent(client, token: str, name: str = "TestAgent") -> str:
    r = client.post("/agents", json={"name": name, "model_preference": "balanced"}, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _assign_agent(client, token: str, project_id: str, agent_id: str) -> None:
    r = client.post(f"/projects/{project_id}/agents/{agent_id}", headers=_auth(token))
    assert r.status_code == 201, r.text


# ── tests ──────────────────────────────────────────────────────────────────────

def test_project_agents_requires_auth(client):
    fake_id = str(uuid.uuid4())
    r = client.get(f"/projects/{fake_id}/agents")
    assert r.status_code == 401


def test_project_agents_project_not_found(client):
    user = _register(client)
    fake_id = str(uuid.uuid4())
    r = client.get(f"/projects/{fake_id}/agents", headers=_auth(user["access_token"]))
    assert r.status_code == 404


def test_project_agents_empty_list_when_no_agents(client):
    user = _register(client)
    project_id = _create_project(client, user["access_token"])
    r = client.get(f"/projects/{project_id}/agents", headers=_auth(user["access_token"]))
    assert r.status_code == 200, r.text
    assert r.json() == [], f"Expected empty list, got: {r.json()}"


def test_project_agents_returns_assigned_agents(client):
    user = _register(client)
    project_id = _create_project(client, user["access_token"])
    agent_id = _create_agent(client, user["access_token"], "MyAgent")
    _assign_agent(client, user["access_token"], project_id, agent_id)

    r = client.get(f"/projects/{project_id}/agents", headers=_auth(user["access_token"]))
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["id"] == agent_id


def test_project_agents_response_shape_and_no_secrets(client):
    user = _register(client)
    project_id = _create_project(client, user["access_token"])
    agent_id = _create_agent(client, user["access_token"], "ShapeAgent")
    _assign_agent(client, user["access_token"], project_id, agent_id)

    r = client.get(f"/projects/{project_id}/agents", headers=_auth(user["access_token"]))
    assert r.status_code == 200
    agent = r.json()[0]

    # Required fields
    for field in ("id", "name", "model_preference", "is_archived"):
        assert field in agent, f"Field '{field}' missing from response"

    # No credential fields
    raw = r.text.lower()
    for forbidden in ("api_key", "token", "credential", "secret", "password"):
        assert forbidden not in raw, f"Sensitive field '{forbidden}' found in response"


def test_project_agents_cross_user_forbidden(client):
    user_a = _register(client)
    project_id = _create_project(client, user_a["access_token"])
    agent_id = _create_agent(client, user_a["access_token"])
    _assign_agent(client, user_a["access_token"], project_id, agent_id)

    user_b = _register(client)
    r = client.get(f"/projects/{project_id}/agents", headers=_auth(user_b["access_token"]))
    assert r.status_code == 404, f"Expected 404 for cross-user access, got {r.status_code}"
