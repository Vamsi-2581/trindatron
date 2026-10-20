"""
Integration tests for capability plumbing through the /message endpoint.

Verifies that:
1. classify() is called exactly ONCE per /message request (no double-classification).
2. The capability value reaches RoutingRequest.capability in the model router.
3. Provider chains are identical to pre-plumbing behavior (no regression).
4. The /chat and /agent legacy endpoints still work (capability=None fallback).
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trinity_router import RouterStreamChunk, RoutingRequest


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


def _make_agent_result(content="done"):
    r = MagicMock()
    r.content = content
    r.tool_calls = None
    r.provider = "anthropic"
    r.model = "claude-sonnet-test"
    r.input_tokens = 5
    r.output_tokens = 5
    r.cached_tokens = 0
    r.cost_usd = 0.0
    r.tcu = 0.0
    r.failover_used = False
    r.failover_reason = None
    return r


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


def _new_session(client, auth_headers, title="Propagation test"):
    return client.post(
        "/sessions", json={"title": title}, headers=auth_headers
    ).json()["id"]


def _message(client, auth_headers, session_id, content):
    return client.post(
        f"/sessions/{session_id}/message",
        json={"content": content},
        headers=auth_headers,
    )


# ── 1. classify() is called exactly once per /message request ─────────────────

def test_classify_called_exactly_once_for_chat_capability(client, auth_headers):
    """CHAT message: classify() runs in sessions.py and NOT again in chat_service."""
    session_id = _new_session(client, auth_headers)
    call_count = {"n": 0}

    import src.capability.classifier as _classifier_mod
    original_classify = _classifier_mod.classify

    def _counting_classify(content):
        call_count["n"] += 1
        return original_classify(content)

    with patch("src.api.routes.sessions.classify", side_effect=_counting_classify), \
         patch("src.services.chat_service.classify", side_effect=_counting_classify), \
         patch("src.services.chat_service._router.stream", _make_chat_stream(["hi"])):
        resp = _message(client, auth_headers, session_id, "Hello, how are you?")

    assert resp.status_code == 200
    assert call_count["n"] == 1, (
        f"classify() was called {call_count['n']} times — expected exactly 1"
    )


def test_classify_called_exactly_once_for_agent_capability(client, auth_headers):
    """CODE message: classify() runs in sessions.py and NOT again in agent_service."""
    session_id = _new_session(client, auth_headers)
    call_count = {"n": 0}

    import src.capability.classifier as _classifier_mod
    original_classify = _classifier_mod.classify

    def _counting_classify(content):
        call_count["n"] += 1
        return original_classify(content)

    with patch("src.api.routes.sessions.classify", side_effect=_counting_classify), \
         patch("src.services.agent_service.classify", side_effect=_counting_classify), \
         patch("src.services.agent_service._router.complete",
               AsyncMock(return_value=_make_agent_result())):
        resp = _message(
            client, auth_headers, session_id,
            "Write a function that reverses a list"
        )

    assert resp.status_code == 200
    assert call_count["n"] == 1, (
        f"classify() was called {call_count['n']} times — expected exactly 1"
    )


# ── 2. capability.value reaches RoutingRequest ────────────────────────────────

def test_capability_value_in_routing_request_for_chat(client, auth_headers):
    """For a CHAT message, RoutingRequest.capability == 'chat'."""
    session_id = _new_session(client, auth_headers)
    captured: list[RoutingRequest] = []

    original_stream = __import__(
        "src.services.chat_service", fromlist=["_router"]
    )._router.stream

    async def _spy_stream(request: RoutingRequest):
        captured.append(request)
        for chunk in [RouterStreamChunk(text="ok"), RouterStreamChunk(
            is_final=True, provider="anthropic", model="test",
            input_tokens=1, output_tokens=1, cached_tokens=0,
            cost_usd=0.0, tcu=0.0,
        )]:
            yield chunk

    with patch("src.services.chat_service._router.stream", _spy_stream):
        _message(client, auth_headers, session_id, "Hello there")

    assert len(captured) == 1
    assert captured[0].capability == "chat"


def test_capability_value_in_routing_request_for_agent(client, auth_headers):
    """For a CODE message that also expresses real execution intent,
    RoutingRequest.capability == 'code' and it dispatches through Agent.

    Execution Mode etapa (2026-09-07): a pure text-generation CODE message
    ("write a function that reverses a list") no longer dispatches to
    Agent at all — infer_execution_mode() correctly keeps it on CHAT,
    since it needs no tools (see test_execution_mode.py). This test's own
    purpose is "does capability propagate correctly for an AGENT-dispatched
    request", so the message now also carries genuine execution intent
    ("save the fix to disk, and run the tests") to actually exercise that
    path — the previous text no longer does, on purpose."""
    session_id = _new_session(client, auth_headers)
    captured: list[RoutingRequest] = []

    async def _spy_complete(request: RoutingRequest):
        captured.append(request)
        return _make_agent_result()

    with patch("src.services.agent_service._router.complete", _spy_complete):
        _message(
            client, auth_headers, session_id,
            "Debug this code, save the fix to disk, and run the tests."
        )

    assert len(captured) == 1
    assert captured[0].capability == "code"


def test_research_capability_value_in_routing_request(client, auth_headers):
    """RESEARCH message with real web-search intent: RoutingRequest.capability == 'research'.

    Execution Mode etapa (2026-09-07): a generic research question
    ("search for information about X") no longer implies Agent/tools —
    only a message that actually asks for a real web search does (see
    test_execution_mode.py's RESEARCH examples). Updated accordingly."""
    session_id = _new_session(client, auth_headers)
    captured: list[RoutingRequest] = []

    async def _spy_complete(request: RoutingRequest):
        captured.append(request)
        return _make_agent_result()

    with patch("src.services.agent_service._router.complete", _spy_complete):
        _message(
            client, auth_headers, session_id,
            "Search the web for information about Python async generators and summarize the sources."
        )

    assert len(captured) == 1
    assert captured[0].capability == "research"


# ── 3. Provider chains are identical — no regression ─────────────────────────

def test_chat_message_still_returns_done_event(client, auth_headers):
    """Plumbing change does not affect SSE output for CHAT requests."""
    session_id = _new_session(client, auth_headers)

    with patch("src.services.chat_service._router.stream", _make_chat_stream(["Hello!"])):
        resp = _message(client, auth_headers, session_id, "Hello")

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    done_events = [e for e in events if e["type"] == "done"]
    assert len(done_events) == 1
    assert "usage" in done_events[0]


def test_agent_message_still_returns_done_event(client, auth_headers):
    """Plumbing change does not affect SSE output for AGENT requests."""
    session_id = _new_session(client, auth_headers)

    with patch("src.services.agent_service._router.complete",
               AsyncMock(return_value=_make_agent_result("The answer"))):
        resp = _message(
            client, auth_headers, session_id,
            "Write a function that reverses a list and run the tests"
        )

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    done_events = [e for e in events if e["type"] == "done"]
    assert len(done_events) == 1


# ── 4. Legacy /chat and /agent endpoints: capability=None fallback works ───────

def test_legacy_chat_endpoint_still_works(client, auth_headers):
    """/chat endpoint calls stream_chat without capability — fallback classify() fires."""
    session_id = _new_session(client, auth_headers)
    classify_calls = {"n": 0}

    import src.capability.classifier as _classifier_mod
    original = _classifier_mod.classify

    def _counting(content):
        classify_calls["n"] += 1
        return original(content)

    with patch("src.services.chat_service.classify", side_effect=_counting), \
         patch("src.services.chat_service._router.stream", _make_chat_stream(["ok"])):
        resp = client.post(
            f"/sessions/{session_id}/chat",
            json={"content": "Hello"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    # Legacy /chat does NOT pass capability → chat_service must classify internally
    assert classify_calls["n"] == 1


def test_legacy_agent_endpoint_still_works(client, auth_headers):
    """/agent endpoint calls stream_agent without capability — fallback classify() fires."""
    session_id = _new_session(client, auth_headers)
    classify_calls = {"n": 0}

    import src.capability.classifier as _classifier_mod
    original = _classifier_mod.classify

    def _counting(content):
        classify_calls["n"] += 1
        return original(content)

    with patch("src.services.agent_service.classify", side_effect=_counting), \
         patch("src.services.agent_service._router.complete",
               AsyncMock(return_value=_make_agent_result())):
        resp = client.post(
            f"/sessions/{session_id}/agent",
            json={"content": "Research topic"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    assert classify_calls["n"] == 1
