"""
Tests for /users/me/usage endpoint and CORS configuration.

NOTE: /users/me/usage returns {plan, daily, weekly, monthly} (Fase 3).
Old all-time totals endpoint was removed. Tests updated accordingly.
monthly.limit is always null now — no operational monthly usage cap
(PARTE 10, 2026-08-31); weekly resets Sunday 12:00 in the user's local
IANA timezone (src/timezones.py), not a fixed UTC rule.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from trinity_router import RouterStreamChunk
from src.entitlements import get_plan_entitlement


def _stream_mock(texts: list[str], cost_usd: float = 0.0005, tcu: float = 0.05):
    chunks = [RouterStreamChunk(text=t) for t in texts]
    chunks.append(RouterStreamChunk(
        is_final=True, provider="anthropic", model="claude-test",
        input_tokens=10, output_tokens=5, cached_tokens=0,
        cost_usd=cost_usd, tcu=tcu,
    ))

    async def _stream(request):
        for chunk in chunks:
            yield chunk

    return _stream


# ─── /users/me/usage ─────────────────────────────────────────────────────────

def test_user_usage_requires_auth(client):
    resp = client.get("/users/me/usage")
    assert resp.status_code == 401


def test_user_usage_returns_zeros_when_no_requests(client, auth_headers):
    resp = client.get("/users/me/usage", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["daily"]["used"] == 0.0
    assert data["monthly"]["used"] == 0.0
    # auth_headers belongs to a START subscriber (conftest.registered_user);
    # a new signup itself is FREE.
    assert data["plan"] == "start"
    assert data["daily"]["limit"] == get_plan_entitlement("start").daily_tcu_limit
    assert data["monthly"]["limit"] is None  # PARTE 10 — no operational monthly usage cap


def test_user_usage_accumulates_across_sessions(client, auth_headers, db):
    session1_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    session2_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    with patch("src.services.chat_service._router.stream", _stream_mock(["Hi!"], cost_usd=0.001, tcu=0.1)):
        client.post(f"/sessions/{session1_id}/chat", json={"content": "Hello"}, headers=auth_headers)

    with patch("src.services.chat_service._router.stream", _stream_mock(["Bye!"], cost_usd=0.002, tcu=0.2)):
        client.post(f"/sessions/{session2_id}/chat", json={"content": "Goodbye"}, headers=auth_headers)

    resp = client.get("/users/me/usage", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["daily"]["used"] == pytest.approx(0.3, abs=1e-4)
    assert data["monthly"]["used"] == pytest.approx(0.3, abs=1e-4)
    # remaining should be reduced
    assert data["daily"]["remaining"] == pytest.approx(
        get_plan_entitlement("start").daily_tcu_limit - 0.3, abs=1e-4)


def test_user_usage_isolated_between_users(client, db):
    resp1 = client.post("/auth/register", json={
        "email": "user1_usage@test.com", "password": "securepass123", "full_name": "U1",
    })
    token1 = resp1.json()["access_token"]
    headers1 = {"Authorization": f"Bearer {token1}"}

    resp2 = client.post("/auth/register", json={
        "email": "user2_usage@test.com", "password": "securepass123", "full_name": "U2",
    })
    token2 = resp2.json()["access_token"]
    headers2 = {"Authorization": f"Bearer {token2}"}

    session_id = client.post("/sessions", json={}, headers=headers1).json()["id"]
    with patch("src.services.chat_service._router.stream", _stream_mock(["Hi!"], tcu=5.0)):
        client.post(f"/sessions/{session_id}/chat", json={"content": "x"}, headers=headers1)

    # user2 must see 0 usage
    resp = client.get("/users/me/usage", headers=headers2)
    assert resp.json()["daily"]["used"] == 0.0
    assert resp.json()["monthly"]["used"] == 0.0


def test_user_usage_response_has_expected_fields(client, auth_headers):
    resp = client.get("/users/me/usage", headers=auth_headers)
    data = resp.json()
    assert "plan" in data
    assert "daily" in data
    assert "monthly" in data
    assert "used" in data["daily"]
    assert "limit" in data["daily"]
    assert "remaining" in data["daily"]
    assert "used" in data["monthly"]
    assert "limit" in data["monthly"]
    assert "remaining" in data["monthly"]


# ─── CORS ────────────────────────────────────────────────────────────────────

def test_cors_allowed_origin_receives_headers(client):
    resp = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert "access-control-allow-origin" in resp.headers
    assert resp.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_cors_unknown_origin_receives_no_cors_headers(client):
    resp = client.get("/health", headers={"Origin": "http://evil.example.com"})
    assert "access-control-allow-origin" not in resp.headers


def test_cors_preflight_allowed_origin(client):
    resp = client.options(
        "/sessions",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert resp.status_code in (200, 204)
    assert "access-control-allow-origin" in resp.headers
