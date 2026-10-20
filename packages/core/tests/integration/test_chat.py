import json
from unittest.mock import patch

from trinity_router import RouterStreamChunk


def _make_stream_mock(texts: list[str], cost_usd: float = 0.0, model: str = "claude-sonnet-test"):
    """Return an async generator function that mocks _router.stream."""
    chunks = [RouterStreamChunk(text=t) for t in texts]
    chunks.append(RouterStreamChunk(
        is_final=True,
        provider="anthropic",
        model=model,
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


def test_chat_requires_auth(client):
    resp = client.post("/sessions/any-id/chat", json={"content": "Hello"})
    assert resp.status_code == 401


def test_chat_session_not_found(client, auth_headers):
    resp = client.post("/sessions/nonexistent/chat", json={"content": "Hello"}, headers=auth_headers)
    assert resp.status_code == 404


def test_chat_streams_sse_format(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    with patch("src.services.chat_service._router.stream", _make_stream_mock(["Hello", ", ", "I'm TRINITY AI!"])):
        resp = client.post(
            f"/sessions/{session_id}/chat",
            json={"content": "Hello!"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]

    body = resp.text
    lines = [l for l in body.strip().split("\n") if l.startswith("data:")]
    assert len(lines) >= 2  # at least text chunks + done + [DONE]


def test_chat_persists_messages(client, auth_headers, db):
    from src.models.message import Message

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    with patch("src.services.chat_service._router.stream", _make_stream_mock(["Hello there!"])):
        client.post(
            f"/sessions/{session_id}/chat",
            json={"content": "Hi TRINITY"},
            headers=auth_headers,
        )

    messages = db.query(Message).filter(Message.session_id == session_id).all()
    assert len(messages) == 2
    assert messages[0].role == "user"
    assert messages[0].content == "Hi TRINITY"
    assert messages[1].role == "assistant"
    assert messages[1].content == "Hello there!"


def test_chat_auto_titles_session(client, auth_headers, db):
    from src.models.session import Session

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    with patch("src.services.chat_service._router.stream", _make_stream_mock(["Sure!"])):
        client.post(
            f"/sessions/{session_id}/chat",
            json={"content": "Explain machine learning to me"},
            headers=auth_headers,
        )

    session = db.query(Session).filter(Session.id == session_id).first()
    db.refresh(session)
    assert session.title == "Explain machine learning to me"


def test_chat_records_usage(client, auth_headers, db):
    from src.models.usage import UsageRecord

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    with patch("src.services.chat_service._router.stream", _make_stream_mock(["Done!"], cost_usd=0.0005)):
        client.post(
            f"/sessions/{session_id}/chat",
            json={"content": "Test usage"},
            headers=auth_headers,
        )

    records = db.query(UsageRecord).filter(UsageRecord.session_id == session_id).all()
    assert len(records) == 1
    assert float(records[0].cost_usd) == pytest.approx(0.0005, abs=1e-9)


def test_chat_sse_contains_done_event(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    with patch("src.services.chat_service._router.stream", _make_stream_mock(["Hi!"])):
        resp = client.post(
            f"/sessions/{session_id}/chat",
            json={"content": "Hello"},
            headers=auth_headers,
        )

    body = resp.text
    data_lines = [l[5:].strip() for l in body.split("\n") if l.startswith("data:")]

    done_event = next((d for d in data_lines if d != "[DONE]" and json.loads(d).get("type") == "done"), None)
    assert done_event is not None

    done_data = json.loads(done_event)
    assert "session_id" in done_data
    assert "message_id" in done_data
    assert "usage" in done_data


def test_chat_done_event_has_nonzero_tokens(client, auth_headers):
    """SSE done event must carry real token counts, not zeros."""
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    with patch("src.services.chat_service._router.stream",
               _make_stream_mock(["Token test!"], cost_usd=0.001)):
        resp = client.post(
            f"/sessions/{session_id}/chat",
            json={"content": "Count tokens"},
            headers=auth_headers,
        )

    data_lines = [l[5:].strip() for l in resp.text.split("\n") if l.startswith("data:")]
    done = next(
        (json.loads(d) for d in data_lines
         if d != "[DONE]" and json.loads(d).get("type") == "done"),
        None,
    )
    assert done is not None
    usage = done["usage"]
    assert usage["input_tokens"] == 10          # hardcoded in _make_stream_mock
    assert usage["output_tokens"] == len("Token test!")
    # Cost and TCU are internal accounting: persisted (next test), never streamed.
    assert "cost_usd" not in usage and "tcu" not in usage


def test_chat_usage_record_has_nonzero_tokens(client, auth_headers, db):
    """UsageRecord in the DB must persist real token counts and cost."""
    from src.models.usage import UsageRecord

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    with patch("src.services.chat_service._router.stream",
               _make_stream_mock(["Verified!"], cost_usd=0.0005)):
        client.post(
            f"/sessions/{session_id}/chat",
            json={"content": "Verify token tracking"},
            headers=auth_headers,
        )

    records = db.query(UsageRecord).filter(
        UsageRecord.session_id == session_id,
        UsageRecord.status == "completed",
    ).all()
    assert len(records) == 1
    r = records[0]
    assert r.input_tokens == 10
    assert r.output_tokens == len("Verified!")
    assert float(r.cost_usd) == pytest.approx(0.0005, abs=1e-9)
    assert float(r.tcu) == pytest.approx(0.05, abs=1e-6)


import pytest
