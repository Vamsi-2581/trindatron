"""Integration tests for the unified POST /sessions/{id}/message endpoint (G-8-3)."""
import json
from unittest.mock import patch

from trinity_router import RouterStreamChunk


def _make_chat_mock(texts: list[str], cost_usd: float = 0.0):
    chunks = [RouterStreamChunk(text=t) for t in texts]
    chunks.append(RouterStreamChunk(
        is_final=True,
        provider="anthropic",
        model="claude-sonnet-test",
        input_tokens=10,
        output_tokens=sum(len(t) for t in texts),
        cached_tokens=0,
        cost_usd=cost_usd,
        tcu=cost_usd * 100.0,
    ))

    async def _stream(request):
        for chunk in chunks:
            yield chunk

    return _stream


def _parse_sse_events(body: str) -> list[dict]:
    events = []
    for line in body.strip().split("\n"):
        line = line.strip()
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            try:
                events.append(json.loads(line[5:].strip()))
            except json.JSONDecodeError:
                pass
    return events


def test_message_requires_auth(client):
    resp = client.post("/sessions/any-id/message", json={"content": "Hello"})
    assert resp.status_code == 401


def test_message_session_not_found(client, auth_headers):
    resp = client.post("/sessions/nonexistent/message", json={"content": "Hello"}, headers=auth_headers)
    assert resp.status_code == 404


def test_message_routes_chat_capability_to_chat_pipeline(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    with patch("src.services.chat_service._router.stream", _make_chat_mock(["Hello from TRINITY!"])):
        resp = client.post(
            f"/sessions/{session_id}/message",
            json={"content": "Hello"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]

    events = _parse_sse_events(resp.text)
    types = [e["type"] for e in events]
    assert "text" in types
    assert "done" in types


def test_message_status_events_include_key_field(client, auth_headers):
    """Status events from the unified endpoint must carry the semantic `key` field."""
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    with patch("src.services.chat_service._router.stream", _make_chat_mock(["OK"])):
        resp = client.post(
            f"/sessions/{session_id}/message",
            json={"content": "Hello"},
            headers=auth_headers,
        )

    events = _parse_sse_events(resp.text)
    status_events = [e for e in events if e.get("type") == "status"]

    for ev in status_events:
        assert "key" in ev, f"status event missing `key`: {ev}"
        assert "message" in ev, f"status event missing `message`: {ev}"
        # key must use dot-notation
        assert "." in ev["key"], f"key not dot-notation: {ev['key']}"


def test_message_done_event_has_expected_fields(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    with patch("src.services.chat_service._router.stream", _make_chat_mock(["Done!"], cost_usd=0.001)):
        resp = client.post(
            f"/sessions/{session_id}/message",
            json={"content": "Go"},
            headers=auth_headers,
        )

    events = _parse_sse_events(resp.text)
    done_events = [e for e in events if e.get("type") == "done"]
    assert len(done_events) == 1
    done = done_events[0]
    assert "message_id" in done
    assert "usage" in done
    assert "tcu" not in done["usage"] and "cost_usd" not in done["usage"]
