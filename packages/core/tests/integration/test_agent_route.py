"""
Integration tests for POST /sessions/{id}/agent
All _router.complete calls are mocked — no real API calls.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from trinity_router import RoutingResult


def _routing_result(content: str | None = None, tool_calls=None, cost_usd: float = 0.0) -> RoutingResult:
    return RoutingResult(
        content=content,
        tool_calls=tool_calls,
        provider="anthropic",
        model="claude-sonnet-test",
        input_tokens=10,
        output_tokens=5,
        cached_tokens=0,
        cost_usd=cost_usd,
        tcu=cost_usd * 100.0,
        failover_used=False,
        failover_reason=None,
    )


def _make_tool_call(tool_name: str, args: dict, call_id: str = "call_1"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=tool_name, arguments=json.dumps(args)),
    )


def _parse_sse(body: str) -> list[dict]:
    events = []
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            events.append(json.loads(line[5:].strip()))
    return events


# ─── Auth & session validation ────────────────────────────────────────────────

def test_agent_requires_auth(client):
    resp = client.post("/sessions/any/agent", json={"content": "hi"})
    assert resp.status_code == 401


def test_agent_session_not_found(client, auth_headers):
    resp = client.post("/sessions/nonexistent-id/agent", json={"content": "hi"}, headers=auth_headers)
    assert resp.status_code == 404


def test_agent_session_wrong_user(client, auth_headers):
    # Register a second user and create a session as them
    resp2 = client.post("/auth/register", json={
        "email": "other@test.com", "password": "password123", "full_name": "Other"
    })
    other_token = resp2.json()["access_token"]
    other_headers = {"Authorization": f"Bearer {other_token}"}
    session_id = client.post("/sessions", json={}, headers=other_headers).json()["id"]

    # First user tries to access other's session
    resp = client.post(f"/sessions/{session_id}/agent", json={"content": "hi"}, headers=auth_headers)
    assert resp.status_code == 404


# ─── Direct answer (no tool calls) ────────────────────────────────────────────

def test_agent_direct_answer(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    direct = _routing_result("The answer is 42.")

    with patch("src.services.agent_service._router.complete", new=AsyncMock(return_value=direct)):
        resp = client.post(
            f"/sessions/{session_id}/agent",
            json={"content": "What is 6 × 7?"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    events = _parse_sse(resp.text)
    done = next(e for e in events if e["type"] == "done")
    assert done["answer"] == "The answer is 42."
    assert "message_id" in done
    assert done["iterations"] == 1


# ─── Tool call round-trip ─────────────────────────────────────────────────────

def test_agent_streams_tool_call_events(client, auth_headers, tmp_path):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    tc = _make_tool_call("list_directory", {})
    with_tool = _routing_result(tool_calls=[tc])
    final = _routing_result("The workspace is empty.")

    responses = [with_tool, final]

    with patch("src.services.agent_service._router.complete", new=AsyncMock(side_effect=responses)), \
         patch("src.services.agent_service._workspace_for", return_value=tmp_path):
        resp = client.post(
            f"/sessions/{session_id}/agent",
            json={"content": "What files are in my workspace?"},
            headers=auth_headers,
        )

    events = _parse_sse(resp.text)
    types = [e["type"] for e in events]
    assert "tool_call" in types
    assert "tool_result" in types
    assert "done" in types

    # Steps are described by activity, never by the internal tool name.
    tool_call_event = next(e for e in events if e["type"] == "tool_call")
    assert tool_call_event["activity"] == "activity.tool.list_directory"
    assert "tool" not in tool_call_event and "list_directory" not in json.dumps(tool_call_event).replace("activity.tool.list_directory", "")

    tool_result_event = next(e for e in events if e["type"] == "tool_result")
    assert tool_result_event["activity"] == "activity.tool.list_directory"
    assert "tool" not in tool_result_event
    assert isinstance(tool_result_event["success"], bool)


# ─── Message persistence ──────────────────────────────────────────────────────

def test_agent_persists_messages(client, auth_headers, db):
    from src.models.message import Message

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    direct = _routing_result("42 is the answer.")

    with patch("src.services.agent_service._router.complete", new=AsyncMock(return_value=direct)):
        client.post(
            f"/sessions/{session_id}/agent",
            json={"content": "The ultimate question"},
            headers=auth_headers,
        )

    messages = db.query(Message).filter(Message.session_id == session_id).all()
    assert len(messages) == 2
    assert messages[0].role == "user"
    assert messages[0].content == "The ultimate question"
    assert messages[1].role == "assistant"
    assert messages[1].content == "42 is the answer."


# ─── Auto-titling ─────────────────────────────────────────────────────────────

def test_agent_auto_titles_session(client, auth_headers, db):
    from src.models.session import Session as SessionModel

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    direct = _routing_result("Sure!")

    with patch("src.services.agent_service._router.complete", new=AsyncMock(return_value=direct)):
        client.post(
            f"/sessions/{session_id}/agent",
            json={"content": "Search the web for Python tutorials"},
            headers=auth_headers,
        )

    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    db.refresh(session)
    assert session.title == "Search the web for Python tutorials"


# ─── Done event structure ─────────────────────────────────────────────────────

def test_agent_done_event_has_required_fields(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    direct = _routing_result("Done!")

    with patch("src.services.agent_service._router.complete", new=AsyncMock(return_value=direct)):
        resp = client.post(
            f"/sessions/{session_id}/agent",
            json={"content": "test"},
            headers=auth_headers,
        )

    events = _parse_sse(resp.text)
    done = next(e for e in events if e["type"] == "done")
    assert "session_id" in done
    assert "message_id" in done
    assert "answer" in done
    assert "iterations" in done
    assert "usage" in done
    assert "cost_usd" not in done["usage"] and "tcu" not in done["usage"]


# ─── Records usage ────────────────────────────────────────────────────────────

def test_agent_records_usage(client, auth_headers, db):
    from src.models.usage import UsageRecord

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    direct = _routing_result("Result!", cost_usd=0.0007)

    with patch("src.services.agent_service._router.complete", new=AsyncMock(return_value=direct)):
        client.post(
            f"/sessions/{session_id}/agent",
            json={"content": "track me"},
            headers=auth_headers,
        )

    records = db.query(UsageRecord).filter(UsageRecord.session_id == session_id).all()
    assert len(records) == 1
    assert float(records[0].cost_usd) == pytest.approx(0.0007, abs=1e-9)


import pytest
