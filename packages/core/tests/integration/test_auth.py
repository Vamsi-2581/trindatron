import threading
from types import SimpleNamespace

import pytest

from src.api import deps
from src.services.auth_service import create_access_token


def test_register_success(client):
    resp = client.post("/auth/register", json={
        "email": "new@test.com",
        "password": "password123",
        "full_name": "New User",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert "access_token" in data
    assert data["email"] == "new@test.com"
    assert data["token_type"] == "bearer"


def test_register_duplicate_email(client):
    payload = {"email": "dup@test.com", "password": "password123"}
    client.post("/auth/register", json=payload)
    resp = client.post("/auth/register", json=payload)
    assert resp.status_code == 409


def test_register_weak_password(client):
    resp = client.post("/auth/register", json={"email": "weak@test.com", "password": "short"})
    assert resp.status_code == 422


def test_login_success(client):
    client.post("/auth/register", json={"email": "login@test.com", "password": "password123"})
    resp = client.post("/auth/login", json={"email": "login@test.com", "password": "password123"})
    assert resp.status_code == 200
    assert "access_token" in resp.json()


def test_login_wrong_password(client):
    client.post("/auth/register", json={"email": "wrong@test.com", "password": "password123"})
    resp = client.post("/auth/login", json={"email": "wrong@test.com", "password": "wrongpass"})
    assert resp.status_code == 401


def test_login_unknown_email(client):
    resp = client.post("/auth/login", json={"email": "ghost@test.com", "password": "password123"})
    assert resp.status_code == 401


def test_me_authenticated(client, registered_user, auth_headers):
    resp = client.get("/auth/me", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == registered_user["email"]
    assert data["plan"] == "start"
    assert data["is_active"] is True


def test_me_unauthenticated(client):
    resp = client.get("/auth/me")
    assert resp.status_code == 401


def test_me_invalid_token(client):
    resp = client.get("/auth/me", headers={"Authorization": "Bearer invalid.token.here"})
    assert resp.status_code == 401


# ── Teste 1 — get_current_user's DB query runs off the event loop ──────────

@pytest.mark.asyncio
async def test_get_current_user_runs_db_query_via_to_thread(client, registered_user, db, monkeypatch):
    """Connection-retention follow-up: get_current_user() was the ONLY
    un-threaded DB call in the whole /chat path (see the session's own
    analysis) — under pool contention, that blocks the entire event loop,
    not just one executor thread. Verifies BEHAVIOR (which OS thread the
    query actually ran on), not just that asyncio.to_thread() appears in
    the source text."""
    main_thread_ident = threading.get_ident()
    observed = {}

    orig = deps._get_active_user_by_id

    def spy(db_arg, user_id):
        observed["thread_ident"] = threading.get_ident()
        return orig(db_arg, user_id)

    monkeypatch.setattr(deps, "_get_active_user_by_id", spy)

    token = create_access_token(registered_user["user_id"])
    credentials = SimpleNamespace(credentials=token)

    user = await deps.get_current_user(credentials=credentials, db=db)

    assert user.id == registered_user["user_id"]
    assert "thread_ident" in observed, "the DB lookup never ran — test setup is broken"
    assert observed["thread_ident"] != main_thread_ident, (
        "get_current_user's DB query ran on the event loop's own thread "
        "instead of being dispatched via asyncio.to_thread()"
    )


@pytest.mark.asyncio
async def test_get_current_user_still_rejects_unknown_or_inactive_user(db):
    """Same behavior as before the change: no such user -> 401, exact same
    detail message — the to_thread() move must not alter this outcome."""
    from fastapi import HTTPException

    token = create_access_token("nonexistent-user-id")
    credentials = SimpleNamespace(credentials=token)

    with pytest.raises(HTTPException) as exc_info:
        await deps.get_current_user(credentials=credentials, db=db)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "User not found or account inactive"
