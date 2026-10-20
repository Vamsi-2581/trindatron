"""
Integration tests for Fase I — Multimodal Abstraction.

Verifies that IMAGE, VIDEO, and AUDIO requests that cannot run here end with an
honest done event, without calling the LLM and without spending anything.
"""
import json
from unittest.mock import AsyncMock, patch

import pytest


def _parse_sse(body: str) -> list[dict]:
    events = []
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            events.append(json.loads(line[5:].strip()))
    return events


def _send(client, auth_headers, content: str):
    session = client.post(
        "/sessions",
        json={"title": "Multimodal test"},
        headers=auth_headers,
    ).json()
    resp = client.post(
        f"/sessions/{session['id']}/message",
        json={"content": content},
        headers=auth_headers,
    )
    return resp


# ── IMAGE ──────────────────────────────────────────────────────────────────────

def test_image_request_returns_coming_soon_without_llm(client, auth_headers):
    with patch("src.services.agent_service._router.complete", new=AsyncMock()) as mock_llm:
        resp = _send(client, auth_headers, "Generate an image of a sunset")

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    done = next((e for e in events if e["type"] == "done"), None)
    assert done is not None, f"No done event. Events: {events}"
    # No image engine is configured in tests: an honest refusal, never a promise.
    assert "available" in done["answer"].lower()
    assert "soon" not in done["answer"].lower()
    assert done["iterations"] == 0
    # LLM must NOT have been called
    mock_llm.assert_not_called()


# ── VIDEO ──────────────────────────────────────────────────────────────────────

def test_video_request_returns_coming_soon_without_llm(client, auth_headers):
    with patch("src.services.agent_service._router.complete", new=AsyncMock()) as mock_llm:
        resp = _send(client, auth_headers, "Create a video of a cat playing")

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    done = next((e for e in events if e["type"] == "done"), None)
    assert done is not None
    assert done["iterations"] == 0
    mock_llm.assert_not_called()


# ── AUDIO ──────────────────────────────────────────────────────────────────────

def test_audio_request_returns_coming_soon_without_llm(client, auth_headers):
    with patch("src.services.agent_service._router.complete", new=AsyncMock()) as mock_llm:
        resp = _send(client, auth_headers, "Transcribe this audio file")

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    done = next((e for e in events if e["type"] == "done"), None)
    assert done is not None
    assert done["iterations"] == 0
    mock_llm.assert_not_called()


# ── Message persisted ─────────────────────────────────────────────────────────

def test_coming_soon_answer_is_saved_in_session_history(client, auth_headers):
    """The coming-soon assistant message must be saved to the DB."""
    session = client.post(
        "/sessions",
        json={"title": "Image test"},
        headers=auth_headers,
    ).json()

    with patch("src.services.agent_service._router.complete", new=AsyncMock()):
        client.post(
            f"/sessions/{session['id']}/message",
            json={"content": "Generate an image of a mountain"},
            headers=auth_headers,
        )

    messages_resp = client.get(
        f"/sessions/{session['id']}",
        headers=auth_headers,
    )
    assert messages_resp.status_code == 200
    messages = messages_resp.json().get("messages", [])
    roles = [m["role"] for m in messages]
    assert "user" in roles
    assert "assistant" in roles
    assistant_msgs = [m for m in messages if m["role"] == "assistant"]
    assert len(assistant_msgs) >= 1
    assert "disponível" in assistant_msgs[-1]["content"].lower() or "available" in assistant_msgs[-1]["content"].lower()


# ── No quota consumed ─────────────────────────────────────────────────────────

def test_coming_soon_does_not_consume_quota(db, client, auth_headers):
    """No tokens on the reply and nothing spent: the refusal leaves no charge behind."""
    from src.models.usage import UsageRecord

    with patch("src.services.agent_service._router.complete", new=AsyncMock()):
        resp = _send(client, auth_headers, "Generate an image of a river")

    events = _parse_sse(resp.text)
    done = next((e for e in events if e["type"] == "done"), None)
    assert done is not None
    assert done.get("usage", {}).get("input_tokens") == 0
    assert all(float(record.tcu or 0) == 0 for record in db.query(UsageRecord).all())
