"""
Tests for user plan wiring:
  current_user.plan → sessions.py → stream_agent/stream_chat → RoutingRequest.user_plan → ModelRouter

Security invariant: user_plan must come ONLY from the authenticated user (current_user.plan).
The client cannot influence the plan through body, query params, or headers.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.models.user import User
from trinity_router import RoutingRequest, RoutingResult, RouterStreamChunk


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _register_and_set_plan(client, db, email: str, plan: str) -> dict:
    """Register a user, then set their plan directly in the DB (server-side only)."""
    resp = client.post("/auth/register", json={
        "email": email,
        "password": "securepass123",
        "full_name": "Plan Test User",
    })
    assert resp.status_code == 201
    data = resp.json()
    # Set plan server-side (simulating an admin or billing system action)
    user = db.query(User).filter(User.id == data["user_id"]).first()
    user.plan = plan
    db.commit()
    db.refresh(user)
    return {"token": data["access_token"], "user_id": data["user_id"], "plan": user.plan}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _capture_chat_stream(texts: list[str]):
    """Returns (captured, mock_fn). captured will hold RoutingRequest objects after call."""
    captured: list[RoutingRequest] = []

    async def _stream(request: RoutingRequest):
        captured.append(request)
        for t in texts:
            yield RouterStreamChunk(text=t)
        yield RouterStreamChunk(
            is_final=True, provider="anthropic", model="claude-test",
            input_tokens=5, output_tokens=2, cached_tokens=0,
            cost_usd=0.0, tcu=0.0,
        )

    return captured, _stream


def _capture_agent_complete(content: str):
    """Returns (captured, AsyncMock). captured will hold RoutingRequest objects after call."""
    captured: list[RoutingRequest] = []

    async def _complete(request: RoutingRequest) -> RoutingResult:
        captured.append(request)
        return RoutingResult(
            content=content,
            tool_calls=None,
            provider="anthropic",
            model="claude-test",
            input_tokens=5,
            output_tokens=2,
            cached_tokens=0,
            cost_usd=0.0,
            tcu=0.0,
            failover_used=False,
            failover_reason=None,
        )

    return captured, _complete


# ─── Plan "start" → RoutingRequest receives "start" ──────────────────────────

def test_chat_plan_start_reaches_routing_request(client, db):
    user = _register_and_set_plan(client, db, "plan_start_chat@test.com", "start")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]

    captured, mock_stream = _capture_chat_stream(["ok"])

    with patch("src.services.chat_service._router.stream", mock_stream):
        client.post(f"/sessions/{session_id}/chat", json={"content": "hi"}, headers=_auth(user["token"]))

    assert len(captured) == 1
    assert captured[0].user_plan == "start"


def test_agent_plan_start_reaches_routing_request(client, db):
    user = _register_and_set_plan(client, db, "plan_start_agent@test.com", "start")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]

    captured, mock_complete = _capture_agent_complete("done")

    with patch("src.services.agent_service._router.complete", mock_complete):
        client.post(f"/sessions/{session_id}/agent", json={"content": "hi"}, headers=_auth(user["token"]))

    assert len(captured) == 1
    assert captured[0].user_plan == "start"


# ─── Plan "pro" → RoutingRequest receives "pro" ───────────────────────────────

def test_chat_plan_pro_reaches_routing_request(client, db):
    user = _register_and_set_plan(client, db, "plan_pro_chat@test.com", "pro")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]

    captured, mock_stream = _capture_chat_stream(["ok"])

    with patch("src.services.chat_service._router.stream", mock_stream):
        client.post(f"/sessions/{session_id}/chat", json={"content": "hi"}, headers=_auth(user["token"]))

    assert len(captured) == 1
    assert captured[0].user_plan == "pro"


def test_agent_plan_pro_reaches_routing_request(client, db):
    user = _register_and_set_plan(client, db, "plan_pro_agent@test.com", "pro")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]

    captured, mock_complete = _capture_agent_complete("done")

    with patch("src.services.agent_service._router.complete", mock_complete):
        client.post(f"/sessions/{session_id}/agent", json={"content": "hi"}, headers=_auth(user["token"]))

    assert len(captured) == 1
    assert captured[0].user_plan == "pro"


# ─── Plan "max" → RoutingRequest receives "max" ───────────────────────────────

def test_chat_plan_max_reaches_routing_request(client, db):
    user = _register_and_set_plan(client, db, "plan_max_chat@test.com", "max")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]

    captured, mock_stream = _capture_chat_stream(["ok"])

    with patch("src.services.chat_service._router.stream", mock_stream):
        client.post(f"/sessions/{session_id}/chat", json={"content": "hi"}, headers=_auth(user["token"]))

    assert len(captured) == 1
    assert captured[0].user_plan == "max"


def test_agent_plan_max_reaches_routing_request(client, db):
    user = _register_and_set_plan(client, db, "plan_max_agent@test.com", "max")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]

    captured, mock_complete = _capture_agent_complete("done")

    with patch("src.services.agent_service._router.complete", mock_complete):
        client.post(f"/sessions/{session_id}/agent", json={"content": "hi"}, headers=_auth(user["token"]))

    assert len(captured) == 1
    assert captured[0].user_plan == "max"


# ─── Empty plan → fallback to "start" ────────────────────────────────────────

def test_chat_empty_plan_falls_back_to_start(client, db):
    user = _register_and_set_plan(client, db, "plan_empty_chat@test.com", "")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]

    captured, mock_stream = _capture_chat_stream(["ok"])

    with patch("src.services.chat_service._router.stream", mock_stream):
        client.post(f"/sessions/{session_id}/chat", json={"content": "hi"}, headers=_auth(user["token"]))

    assert len(captured) == 1
    assert captured[0].user_plan == "start"


def test_agent_empty_plan_falls_back_to_start(client, db):
    user = _register_and_set_plan(client, db, "plan_empty_agent@test.com", "")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]

    captured, mock_complete = _capture_agent_complete("done")

    with patch("src.services.agent_service._router.complete", mock_complete):
        client.post(f"/sessions/{session_id}/agent", json={"content": "hi"}, headers=_auth(user["token"]))

    assert len(captured) == 1
    assert captured[0].user_plan == "start"


# ─── Client-controlled input cannot override plan ────────────────────────────

def test_chat_body_has_no_user_plan_field(client, db):
    """ChatRequest schema must not accept a user_plan field from the client."""
    from src.schemas.message import ChatRequest
    fields = ChatRequest.model_fields
    assert "user_plan" not in fields, "ChatRequest must not expose user_plan to clients"
    assert "plan" not in fields, "ChatRequest must not expose plan to clients"


def test_agent_body_has_no_user_plan_field(client, db):
    """AgentRequest schema must not accept a user_plan field from the client."""
    from src.schemas.agent import AgentRequest
    fields = AgentRequest.model_fields
    assert "user_plan" not in fields, "AgentRequest must not expose user_plan to clients"
    assert "plan" not in fields, "AgentRequest must not expose plan to clients"


def test_chat_extra_body_fields_are_rejected_or_ignored(client, db, auth_headers):
    """Sending an extra 'plan' field in the body must not affect routing."""
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    captured, mock_stream = _capture_chat_stream(["ok"])

    with patch("src.services.chat_service._router.stream", mock_stream):
        # Attempt to inject a plan via body — Pydantic ignores unknown fields by default
        client.post(
            f"/sessions/{session_id}/chat",
            json={"content": "hi", "plan": "max", "user_plan": "max"},
            headers=auth_headers,
        )

    # The default user has plan="start"; plan injected via body must be ignored
    assert len(captured) == 1
    assert captured[0].user_plan == "start"


def test_agent_extra_body_fields_are_rejected_or_ignored(client, db, auth_headers):
    """Sending an extra 'plan' field in the body must not affect routing."""
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    captured, mock_complete = _capture_agent_complete("done")

    with patch("src.services.agent_service._router.complete", mock_complete):
        client.post(
            f"/sessions/{session_id}/agent",
            json={"content": "hi", "plan": "max", "user_plan": "max"},
            headers=auth_headers,
        )

    assert len(captured) == 1
    assert captured[0].user_plan == "start"


# ─── Correct plan reaches selector/limits ────────────────────────────────────

def test_plan_reaches_selector_for_chat(client, db):
    """Pro user's plan reaches the selector, which returns the pro chain."""
    from trinity_router.selector import select_provider_chain, TaskType

    user = _register_and_set_plan(client, db, "plan_selector_chat@test.com", "pro")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]

    captured, mock_stream = _capture_chat_stream(["ok"])

    with patch("src.services.chat_service._router.stream", mock_stream):
        client.post(f"/sessions/{session_id}/chat", json={"content": "hi"}, headers=_auth(user["token"]))

    assert captured[0].user_plan == "pro"
    # Verify that the pro plan maps to the expected provider chain
    chain = select_provider_chain(TaskType.CHAT, "pro")
    assert "claude-sonnet" in chain
    assert "gpt-4o-mini" not in chain  # start-only provider not in pro chain


def test_plan_reaches_limits_for_agent(client, db):
    """Max user's plan reaches limits, unlocking higher token limits."""
    from trinity_router.limits import get_plan_limits

    user = _register_and_set_plan(client, db, "plan_limits_agent@test.com", "max")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]

    captured, mock_complete = _capture_agent_complete("done")

    with patch("src.services.agent_service._router.complete", mock_complete):
        client.post(f"/sessions/{session_id}/agent", json={"content": "hi"}, headers=_auth(user["token"]))

    assert captured[0].user_plan == "max"
    # Verify that max limits are more permissive than start limits
    max_limits = get_plan_limits("max")
    start_limits = get_plan_limits("start")
    assert max_limits.max_output_tokens_per_request > start_limits.max_output_tokens_per_request
    assert max_limits.max_estimated_cost_per_request_usd > start_limits.max_estimated_cost_per_request_usd
    assert max_limits.max_failover_attempts > start_limits.max_failover_attempts
