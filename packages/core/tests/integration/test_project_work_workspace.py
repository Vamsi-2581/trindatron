"""
Integration tests — GET /projects/{project_id}/workspace/work.

Tests:
  1. requires_auth                     — 401 without token
  2. project_not_found                 — 404 for unknown project_id
  3. no_work_workspace                 — 404 when project exists but has no WORK workspace
  4. returns_work_workspace            — 200 with correct workspace when one exists
  5. response_shape                    — response contains id, project_id, requires_approval, approval_policy
  6. cross_user_forbidden              — another user cannot access someone else's project workspace
"""
from __future__ import annotations

import uuid

import pytest


# ── helpers ───────────────────────────────────────────────────────────────────

def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register(client) -> dict:
    email = f"pww_{uuid.uuid4().hex[:8]}@test.com"
    r = client.post("/auth/register", json={"email": email, "password": "Pass1234!", "full_name": "T"})
    assert r.status_code == 201, r.text
    return r.json()


def _create_project(client, token: str, name: str = "Test Project") -> str:
    r = client.post("/projects", json={"name": name}, headers=_auth(token))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _create_work_ws(client, token: str, project_id: str) -> dict:
    r = client.post(
        "/workspaces/work",
        json={"project_id": project_id, "requires_approval": True, "approval_policy": "smart"},
        headers=_auth(token),
    )
    assert r.status_code == 201, r.text
    return r.json()


# ── tests ──────────────────────────────────────────────────────────────────────

def test_work_workspace_by_project_requires_auth(client):
    fake_id = str(uuid.uuid4())
    r = client.get(f"/projects/{fake_id}/workspace/work")
    assert r.status_code == 401


def test_work_workspace_project_not_found(client):
    user = _register(client)
    fake_id = str(uuid.uuid4())
    r = client.get(f"/projects/{fake_id}/workspace/work", headers=_auth(user["access_token"]))
    assert r.status_code == 404


def test_work_workspace_not_created_returns_404(client):
    user = _register(client)
    project_id = _create_project(client, user["access_token"])
    # No WORK workspace created for this project
    r = client.get(f"/projects/{project_id}/workspace/work", headers=_auth(user["access_token"]))
    assert r.status_code == 404
    assert "WORK" in r.json()["detail"]


def test_work_workspace_returns_correct_workspace(client):
    user = _register(client)
    project_id = _create_project(client, user["access_token"])
    ws = _create_work_ws(client, user["access_token"], project_id)

    r = client.get(f"/projects/{project_id}/workspace/work", headers=_auth(user["access_token"]))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["id"] == ws["id"]
    assert data["project_id"] == project_id


def test_work_workspace_response_shape(client):
    user = _register(client)
    project_id = _create_project(client, user["access_token"])
    _create_work_ws(client, user["access_token"], project_id)

    r = client.get(f"/projects/{project_id}/workspace/work", headers=_auth(user["access_token"]))
    assert r.status_code == 200
    data = r.json()
    assert "id" in data
    assert "project_id" in data
    assert "requires_approval" in data
    assert "approval_policy" in data


def test_work_workspace_cross_user_forbidden(client):
    user_a = _register(client)
    project_id = _create_project(client, user_a["access_token"])
    _create_work_ws(client, user_a["access_token"], project_id)

    user_b = _register(client)
    r = client.get(f"/projects/{project_id}/workspace/work", headers=_auth(user_b["access_token"]))
    assert r.status_code == 404, f"Expected 404 for cross-user access, got {r.status_code}"
