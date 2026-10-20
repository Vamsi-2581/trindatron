"""
Integration tests for TRINITY MEMORY — /users/me/memories CRUD.

Invariants:
  - All endpoints require auth (401 without token)
  - POST creates a memory scoped to the authenticated user
  - GET lists only the caller's memories (user isolation)
  - DELETE removes the memory; 404 on unknown or other-user's memory
  - /context endpoint returns formatted memory block for prompt injection
  - Empty content and oversized content are rejected (422)
"""
from __future__ import annotations

import pytest


# ─── Auth guards ──────────────────────────────────────────────────────────────

def test_list_memories_requires_auth(client):
    resp = client.get("/users/me/memories")
    assert resp.status_code == 401


def test_create_memory_requires_auth(client):
    resp = client.post("/users/me/memories", json={"content": "hello"})
    assert resp.status_code == 401


def test_delete_memory_requires_auth(client):
    resp = client.delete("/users/me/memories/fake-id")
    assert resp.status_code == 401


def test_context_requires_auth(client):
    resp = client.get("/users/me/memories/context")
    assert resp.status_code == 401


# ─── Empty state ──────────────────────────────────────────────────────────────

def test_list_memories_empty_initially(client, auth_headers):
    resp = client.get("/users/me/memories", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []


def test_context_empty_string_when_no_memories(client, auth_headers):
    resp = client.get("/users/me/memories/context", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["context"] == ""


# ─── CRUD ─────────────────────────────────────────────────────────────────────

def test_create_memory_returns_201(client, auth_headers):
    resp = client.post("/users/me/memories", json={"content": "I prefer concise answers"}, headers=auth_headers)
    assert resp.status_code == 201
    data = resp.json()
    assert data["content"] == "I prefer concise answers"
    assert "id" in data
    assert "created_at" in data
    assert data["source_session_id"] is None


def test_list_memories_returns_created_entry(client, auth_headers):
    client.post("/users/me/memories", json={"content": "My name is Diego"}, headers=auth_headers)
    resp = client.get("/users/me/memories", headers=auth_headers)
    assert resp.status_code == 200
    contents = [m["content"] for m in resp.json()]
    assert "My name is Diego" in contents


def test_delete_memory_returns_204(client, auth_headers):
    created = client.post("/users/me/memories", json={"content": "to delete"}, headers=auth_headers).json()
    resp = client.delete(f"/users/me/memories/{created['id']}", headers=auth_headers)
    assert resp.status_code == 204


def test_delete_memory_removes_it_from_list(client, auth_headers):
    created = client.post("/users/me/memories", json={"content": "ephemeral fact"}, headers=auth_headers).json()
    client.delete(f"/users/me/memories/{created['id']}", headers=auth_headers)
    resp = client.get("/users/me/memories", headers=auth_headers)
    ids = [m["id"] for m in resp.json()]
    assert created["id"] not in ids


def test_delete_nonexistent_memory_returns_404(client, auth_headers):
    resp = client.delete("/users/me/memories/does-not-exist", headers=auth_headers)
    assert resp.status_code == 404


# ─── User isolation ───────────────────────────────────────────────────────────

def _register_second_user(client) -> dict:
    resp = client.post("/auth/register", json={
        "email": "memory_other@test.com",
        "password": "securepass123",
        "full_name": "Other User",
    })
    assert resp.status_code == 201
    data = resp.json()
    return {"Authorization": f"Bearer {data['access_token']}"}


def test_user_cannot_see_another_users_memories(client, auth_headers):
    client.post("/users/me/memories", json={"content": "private fact"}, headers=auth_headers)
    other_headers = _register_second_user(client)
    resp = client.get("/users/me/memories", headers=other_headers)
    assert resp.status_code == 200
    assert resp.json() == []


def test_user_cannot_delete_another_users_memory(client, auth_headers):
    created = client.post("/users/me/memories", json={"content": "owned by first"}, headers=auth_headers).json()
    other_headers = _register_second_user(client)
    resp = client.delete(f"/users/me/memories/{created['id']}", headers=other_headers)
    assert resp.status_code == 404


# ─── Validation ───────────────────────────────────────────────────────────────

def test_create_memory_empty_content_rejected(client, auth_headers):
    resp = client.post("/users/me/memories", json={"content": "   "}, headers=auth_headers)
    assert resp.status_code == 422


def test_create_memory_oversized_content_rejected(client, auth_headers):
    resp = client.post("/users/me/memories", json={"content": "x" * 2001}, headers=auth_headers)
    assert resp.status_code == 422


# ─── Context injection ────────────────────────────────────────────────────────

def test_context_contains_all_memories(client, auth_headers):
    client.post("/users/me/memories", json={"content": "Fact A"}, headers=auth_headers)
    client.post("/users/me/memories", json={"content": "Fact B"}, headers=auth_headers)
    resp = client.get("/users/me/memories/context", headers=auth_headers)
    ctx = resp.json()["context"]
    assert "Fact A" in ctx
    assert "Fact B" in ctx


def test_context_format_has_header(client, auth_headers):
    client.post("/users/me/memories", json={"content": "something"}, headers=auth_headers)
    resp = client.get("/users/me/memories/context", headers=auth_headers)
    ctx = resp.json()["context"]
    assert ctx.startswith("#")


# ─── source_session_id ────────────────────────────────────────────────────────

def test_create_memory_with_source_session(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    resp = client.post(
        "/users/me/memories",
        json={"content": "session-linked fact", "source_session_id": session_id},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["source_session_id"] == session_id
