"""TRINITY CODE and WORK are sold by plan, and only plans that include them run.

PlanEntitlement declares supports_trinity_code (START and up) and
supports_trinity_work (PRO and up), but until the 2026-09-13 pre-launch audit
the workspace routes only reserved TCU: a FREE account could run CODE tasks
and sandbox commands, and START could run WORK. The gate sits after the
ownership check, so a foreign workspace is still a 404, and before the
reservation, so a plan without the feature never reserves or spends.
"""
from __future__ import annotations

from fastapi import HTTPException

from src.models.user import User


def _signup(client, email: str):
    data = client.post("/auth/register", json={
        "email": email, "password": "securepass123", "full_name": "Gate",
    }).json()
    return data["user_id"], {"Authorization": f"Bearer {data['access_token']}"}


def _set_plan(db, user_id: str, plan: str) -> None:
    db.query(User).filter(User.id == user_id).one().plan = plan
    db.commit()


def _project(client, headers) -> str:
    resp = client.post("/projects", json={"name": "Gate project"}, headers=headers)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _code_ws(client, headers, tmp_path) -> str:
    resp = client.post("/workspaces/code", json={
        "project_id": _project(client, headers), "root_path": str(tmp_path), "language_hint": "python",
    }, headers=headers)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _work_ws(client, headers) -> str:
    resp = client.post("/workspaces/work", json={"project_id": _project(client, headers)}, headers=headers)
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _spy_reservation(monkeypatch) -> list:
    """Records reservations and then refuses them, so a request that passes
    the gate stops at a 429 without running anything."""
    calls: list = []

    def refuse(*args, **kwargs):
        calls.append(kwargs.get("plan"))
        raise HTTPException(status_code=429, detail={"error": "test_stop"})

    monkeypatch.setattr("src.api.routes.workspaces.check_and_reserve", refuse)
    return calls


def test_free_cannot_run_a_code_task(client, db, tmp_path, monkeypatch):
    user_id, headers = _signup(client, "free-code@test.com")
    ws = _code_ws(client, headers, tmp_path)
    calls = _spy_reservation(monkeypatch)

    resp = client.post(f"/workspaces/code/{ws}/task", json={"task": "list the files"}, headers=headers)

    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "feature_not_entitled"
    assert calls == []


def test_free_cannot_run_sandbox_commands(client, db, tmp_path, monkeypatch):
    _, headers = _signup(client, "free-exec@test.com")
    ws = _code_ws(client, headers, tmp_path)
    calls = _spy_reservation(monkeypatch)

    resp = client.post(f"/workspaces/code/{ws}/execute", json={"command": "ls"}, headers=headers)

    assert resp.status_code == 403
    assert calls == []


def test_start_includes_code(client, db, tmp_path, monkeypatch):
    user_id, headers = _signup(client, "start-code@test.com")
    _set_plan(db, user_id, "start")
    ws = _code_ws(client, headers, tmp_path)
    calls = _spy_reservation(monkeypatch)

    resp = client.post(f"/workspaces/code/{ws}/task", json={"task": "list the files"}, headers=headers)

    assert resp.status_code == 429, "past the gate, stopped at the reservation"
    assert calls == ["start"]


def test_start_cannot_run_work(client, db, monkeypatch):
    user_id, headers = _signup(client, "start-work@test.com")
    _set_plan(db, user_id, "start")
    ws = _work_ws(client, headers)
    calls = _spy_reservation(monkeypatch)

    resp = client.post(f"/workspaces/work/{ws}/execute", json={"task": "research competitors"}, headers=headers)

    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "feature_not_entitled"
    assert calls == []


def test_pro_includes_work(client, db, monkeypatch):
    user_id, headers = _signup(client, "pro-work@test.com")
    _set_plan(db, user_id, "pro")
    ws = _work_ws(client, headers)
    calls = _spy_reservation(monkeypatch)

    resp = client.post(f"/workspaces/work/{ws}/execute", json={"task": "research competitors"}, headers=headers)

    assert resp.status_code == 429
    assert calls == ["pro"]


def test_a_foreign_workspace_is_still_a_404_for_a_plan_without_the_feature(client, db, tmp_path, monkeypatch):
    owner_id, owner_headers = _signup(client, "owner@test.com")
    _set_plan(db, owner_id, "start")
    ws = _code_ws(client, owner_headers, tmp_path)
    _, stranger_headers = _signup(client, "stranger@test.com")   # FREE

    resp = client.post(f"/workspaces/code/{ws}/task", json={"task": "x"}, headers=stranger_headers)

    assert resp.status_code == 404
