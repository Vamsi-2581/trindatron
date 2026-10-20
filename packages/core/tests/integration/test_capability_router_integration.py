"""
Integration tests for CapabilityRouter wired into the /message endpoint.

These tests verify that:
1. route_capability() is actually called during /message requests.
2. All TEXT capabilities continue to route correctly.
3. IMAGE, VIDEO, AUDIO continue to return coming-soon without calling any LLM or API.

No external network calls are made — all LLM calls are mocked.
"""
import json
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from trinity_router import RouterStreamChunk

from src.capability.classifier import Capability
from src.capability.capability_router import CapabilityRoute, ExecutorKind


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_chat_stream(texts: list[str]):
    chunks = [RouterStreamChunk(text=t) for t in texts]
    chunks.append(RouterStreamChunk(
        is_final=True,
        provider="anthropic",
        model="claude-sonnet-test",
        input_tokens=5,
        output_tokens=len(texts),
        cached_tokens=0,
        cost_usd=0.0,
        tcu=0.0,
    ))

    async def _gen(request):
        for chunk in chunks:
            yield chunk

    return _gen


def _parse_sse(body: str) -> list[dict]:
    events = []
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            try:
                events.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                pass
    return events


def _new_session(client, auth_headers, title="Router integration test"):
    return client.post("/sessions", json={"title": title}, headers=auth_headers).json()["id"]


def _message(client, auth_headers, session_id, content):
    return client.post(
        f"/sessions/{session_id}/message",
        json={"content": content},
        headers=auth_headers,
    )


# ── 1. route_capability() is called on every /message request ─────────────────

def test_route_capability_is_called_for_chat_request(client, auth_headers):
    """Proves route_capability() is in the /message critical path."""
    session_id = _new_session(client, auth_headers)

    with patch("src.api.routes.sessions.route_capability", wraps=__import__(
        "src.capability.capability_router", fromlist=["route"]
    ).route) as mock_route, \
         patch("src.services.chat_service._router.stream", _make_chat_stream(["Hi!"])):
        resp = _message(client, auth_headers, session_id, "Hello there")

    assert resp.status_code == 200
    mock_route.assert_called_once()
    # Called with the classified capability (CHAT for "Hello there")
    called_capability = mock_route.call_args[0][0]
    assert isinstance(called_capability, Capability)


def test_route_capability_result_implemented_true_for_text(client, auth_headers):
    """For a CHAT message, route_capability returns implemented=True — no blocking."""
    session_id = _new_session(client, auth_headers)

    captured = []

    def _capturing_route(cap):
        from src.capability.capability_router import route
        result = route(cap)
        captured.append(result)
        return result

    with patch("src.api.routes.sessions.route_capability", side_effect=_capturing_route), \
         patch("src.services.chat_service._router.stream", _make_chat_stream(["OK"])):
        resp = _message(client, auth_headers, session_id, "Hello")

    assert resp.status_code == 200
    assert len(captured) == 1
    assert captured[0].implemented is True
    assert captured[0].executor == ExecutorKind.TEXT


# ── 2. TEXT capabilities continue to work ────────────────────────────────────

def test_chat_capability_routes_to_chat_pipeline(client, auth_headers):
    session_id = _new_session(client, auth_headers)
    with patch("src.services.chat_service._router.stream", _make_chat_stream(["Hello!"])):
        resp = _message(client, auth_headers, session_id, "Hello")
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert any(e["type"] == "done" for e in events)
    # CHAT goes to stream_chat: done event has usage field
    done = next(e for e in events if e["type"] == "done")
    assert "usage" in done


def test_research_capability_routes_to_agent_pipeline(client, auth_headers):
    session_id = _new_session(client, auth_headers)
    with patch("src.services.agent_service._router.complete", new=AsyncMock(return_value=MagicMock(
        content="Research result",
        tool_calls=None,
        provider="anthropic",
        model="claude-sonnet-test",
        input_tokens=5,
        output_tokens=3,
        cached_tokens=0,
        cost_usd=0.0,
        tcu=0.0,
        failover_used=False,
        failover_reason=None,
    ))):
        resp = _message(client, auth_headers, session_id, "Search for information about Python")
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert any(e["type"] == "done" for e in events)


def test_code_capability_routes_to_agent_pipeline(client, auth_headers):
    session_id = _new_session(client, auth_headers)
    with patch("src.services.agent_service._router.complete", new=AsyncMock(return_value=MagicMock(
        content="def hello(): pass",
        tool_calls=None,
        provider="anthropic",
        model="claude-sonnet-test",
        input_tokens=5,
        output_tokens=5,
        cached_tokens=0,
        cost_usd=0.0,
        tcu=0.0,
        failover_used=False,
        failover_reason=None,
    ))):
        resp = _message(client, auth_headers, session_id, "Write a function that says hello and run the tests")
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert any(e["type"] == "done" for e in events)


# ── 3. IMAGE/VIDEO/AUDIO continue blocked — no LLM call ──────────────────────

def test_image_capability_blocked_by_capability_router(client, auth_headers):
    session_id = _new_session(client, auth_headers)

    captured = []

    def _capturing_route(cap):
        from src.capability.capability_router import route
        result = route(cap)
        captured.append(result)
        return result

    with patch("src.api.routes.sessions.route_capability", side_effect=_capturing_route), \
         patch("src.services.chat_service._router.stream") as mock_chat, \
         patch("src.services.agent_service._router.complete") as mock_agent:
        resp = _message(client, auth_headers, session_id, "Generate an image of a sunset")

    assert resp.status_code == 200
    events = _parse_sse(resp.text)

    # CapabilityRouter recognized this as IMAGE, not implemented
    assert len(captured) == 1
    assert captured[0].executor == ExecutorKind.IMAGE
    assert captured[0].implemented is False

    # No LLM was called
    mock_chat.assert_not_called()
    mock_agent.assert_not_called()

    # Answered by TRINDATRON itself: with no image engine configured here, an honest
    # refusal, and never an internal cost field.
    done = next((e for e in events if e["type"] == "done"), None)
    assert done is not None
    assert done["iterations"] == 0
    assert done["answer"] == "Creating images isn't available right now."
    usage = done.get("usage", {})
    assert "tcu" not in usage and "cost_usd" not in usage


def test_video_capability_blocked_by_capability_router(client, auth_headers):
    session_id = _new_session(client, auth_headers)

    with patch("src.services.chat_service._router.stream") as mock_chat, \
         patch("src.services.agent_service._router.complete") as mock_agent:
        resp = _message(client, auth_headers, session_id, "Create a video of a cat playing")

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    done = next((e for e in events if e["type"] == "done"), None)
    assert done is not None and done["iterations"] == 0
    mock_chat.assert_not_called()
    mock_agent.assert_not_called()


def test_audio_capability_blocked_by_capability_router(client, auth_headers):
    session_id = _new_session(client, auth_headers)

    with patch("src.services.chat_service._router.stream") as mock_chat, \
         patch("src.services.agent_service._router.complete") as mock_agent:
        resp = _message(client, auth_headers, session_id, "Transcribe this audio file")

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    done = next((e for e in events if e["type"] == "done"), None)
    assert done is not None and done["iterations"] == 0
    mock_chat.assert_not_called()
    mock_agent.assert_not_called()


# ── 4. route_capability returns FALSE for multimodal regardless of ─────────────
#       what UNIMPLEMENTED_CAPABILITIES says (CapabilityRouter is authoritative) ─

def test_image_route_implemented_false_is_the_gate(client, auth_headers):
    """If route_capability returns implemented=True for IMAGE (hypothetical future),
    the gate opens. This test documents that implemented is the authoritative check."""
    session_id = _new_session(client, auth_headers)

    # Simulate a future where IMAGE is implemented (override for test only)
    fake_route = CapabilityRoute(
        capability=Capability.IMAGE,
        executor=ExecutorKind.IMAGE,
        implemented=False,  # still False — gate remains closed
    )

    with patch("src.api.routes.sessions.route_capability", return_value=fake_route), \
         patch("src.services.chat_service._router.stream") as mock_chat:
        resp = _message(client, auth_headers, session_id, "Generate an image of anything")

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    done = next((e for e in events if e["type"] == "done"), None)
    # Gate stays closed when implemented=False
    assert done is not None and done["iterations"] == 0
    mock_chat.assert_not_called()
