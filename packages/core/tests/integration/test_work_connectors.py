"""
Integration tests — GET /workspaces/work/{id}/connectors (Seção 11 da Surface de API).

Tests:
  1. requires_auth           — 401 without token
  2. workspace_not_found     — 404 for unknown workspace_id
  3. authorized_returns_200  — workspace owner gets 200
  4. response_shape          — response has workspace_id + connectors list
  5. no_secrets_in_response  — no api_key / token / credential fields
  6. connectors_is_empty_list — empty list until connectors are configured
"""
from __future__ import annotations

import uuid

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(client) -> dict:
    email = f"conn_{uuid.uuid4().hex[:8]}@test.com"
    r = client.post("/auth/register", json={"email": email, "password": "Pass1234!", "full_name": "T"})
    assert r.status_code == 201, r.text
    return r.json()


def _create_project(client, token: str) -> str:
    r = client.post("/projects", json={"name": "Connector test project"}, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _create_work_ws(client, token: str, project_id: str) -> str:
    r = client.post(
        "/workspaces/work",
        json={"project_id": project_id, "requires_approval": True, "approval_policy": "smart"},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ── tests ──────────────────────────────────────────────────────────────────────

def test_connectors_requires_auth(client):
    fake_id = str(uuid.uuid4())
    r = client.get(f"/workspaces/work/{fake_id}/connectors")
    assert r.status_code == 401


def test_connectors_workspace_not_found(client):
    user = _register(client)
    fake_id = str(uuid.uuid4())
    r = client.get(f"/workspaces/work/{fake_id}/connectors", headers=_auth(user["access_token"]))
    assert r.status_code == 404


def test_connectors_authorized_returns_200(client):
    user = _register(client)
    project_id = _create_project(client, user["access_token"])
    ws_id = _create_work_ws(client, user["access_token"], project_id)

    r = client.get(f"/workspaces/work/{ws_id}/connectors", headers=_auth(user["access_token"]))
    assert r.status_code == 200, r.text


def test_connectors_response_shape(client):
    user = _register(client)
    project_id = _create_project(client, user["access_token"])
    ws_id = _create_work_ws(client, user["access_token"], project_id)

    r = client.get(f"/workspaces/work/{ws_id}/connectors", headers=_auth(user["access_token"]))
    assert r.status_code == 200
    data = r.json()
    assert "workspace_id" in data, f"workspace_id missing: {data}"
    assert "connectors" in data, f"connectors missing: {data}"
    assert data["workspace_id"] == ws_id


def test_connectors_no_secrets_in_response(client):
    user = _register(client)
    project_id = _create_project(client, user["access_token"])
    ws_id = _create_work_ws(client, user["access_token"], project_id)

    r = client.get(f"/workspaces/work/{ws_id}/connectors", headers=_auth(user["access_token"]))
    assert r.status_code == 200
    raw = r.text.lower()
    for forbidden in ("api_key", "token", "credential", "secret", "password"):
        assert forbidden not in raw, f"Sensitive field '{forbidden}' found in response: {raw}"


def test_connectors_is_empty_list(client):
    user = _register(client)
    project_id = _create_project(client, user["access_token"])
    ws_id = _create_work_ws(client, user["access_token"], project_id)

    r = client.get(f"/workspaces/work/{ws_id}/connectors", headers=_auth(user["access_token"]))
    assert r.status_code == 200
    assert r.json()["connectors"] == [], "Expected empty connectors list before any are configured"


def test_connectors_cross_user_forbidden(client):
    """User B cannot access User A's workspace connectors."""
    user_a = _register(client)
    project_id = _create_project(client, user_a["access_token"])
    ws_id = _create_work_ws(client, user_a["access_token"], project_id)

    user_b = _register(client)
    r = client.get(f"/workspaces/work/{ws_id}/connectors", headers=_auth(user_b["access_token"]))
    assert r.status_code == 404, f"Expected 404 for cross-user access, got {r.status_code}"
