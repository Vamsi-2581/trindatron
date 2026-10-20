"""Etapa 5C — proves the load_class integration point in sessions.py is
wired into /chat, /agent, and /message without changing any observable
behavior: same status codes, same response bodies as before this etapa;
the only new thing is one INFO log line per request carrying the computed
LoadClass.
"""
from __future__ import annotations

import logging

from trinity_router import RouterStreamChunk


def _make_stream_mock(texts: list[str]):
    chunks = [RouterStreamChunk(text=t) for t in texts]
    chunks.append(RouterStreamChunk(
        is_final=True, provider="anthropic", model="claude-sonnet-test",
        input_tokens=10, output_tokens=sum(len(t) for t in texts),
        cached_tokens=0, cost_usd=0.0, tcu=0.0,
    ))

    async def _stream(request):
        for chunk in chunks:
            yield chunk

    return _stream


def test_chat_endpoint_logs_light_load_class(client, auth_headers, monkeypatch, caplog):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    with monkeypatch.context() as m:
        m.setattr("src.services.chat_service._router.stream", _make_stream_mock(["hi"]))
        with caplog.at_level(logging.INFO, logger="src.api.routes.sessions"):
            resp = client.post(f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=auth_headers)

    assert resp.status_code == 200  # behavior unchanged
    matching = [r for r in caplog.records if "load_class computed" in r.message]
    assert len(matching) == 1
    assert "endpoint=/chat" in matching[0].message
    assert "load_class=light" in matching[0].message


def test_agent_endpoint_logs_heavy_load_class_even_for_plain_text(client, auth_headers, monkeypatch, caplog):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    async def _fake_complete(request):
        class _Result:
            model = "claude-sonnet-test"
            content = "hi there"
            tool_calls = None
            input_tokens = 5
            output_tokens = 5
            cached_tokens = 0
            cost_usd = 0.0
            tcu = 0.0
        return _Result()

    with monkeypatch.context() as m:
        m.setattr("src.services.agent_service._router.complete", _fake_complete)
        with caplog.at_level(logging.INFO, logger="src.api.routes.sessions"):
            # Plain, non-agentic-looking text — /agent still forces HEAVY.
            resp = client.post(f"/sessions/{session_id}/agent", json={"content": "hello there"}, headers=auth_headers)

    assert resp.status_code == 200  # behavior unchanged
    matching = [r for r in caplog.records if "load_class computed" in r.message]
    assert len(matching) == 1
    assert "endpoint=/agent" in matching[0].message
    assert "load_class=heavy" in matching[0].message


def test_message_endpoint_logs_medium_load_class_for_files_capability(client, auth_headers, monkeypatch, caplog):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    async def _fake_complete(request):
        class _Result:
            model = "claude-sonnet-test"
            content = "done reading the file"
            tool_calls = None
            input_tokens = 5
            output_tokens = 5
            cached_tokens = 0
            cost_usd = 0.0
            tcu = 0.0
        return _Result()

    with monkeypatch.context() as m:
        m.setattr("src.services.agent_service._router.complete", _fake_complete)
        with caplog.at_level(logging.INFO, logger="src.api.routes.sessions"):
            resp = client.post(
                f"/sessions/{session_id}/message",
                json={"content": "please read the file config.json"},
                headers=auth_headers,
            )

    assert resp.status_code == 200  # behavior unchanged
    matching = [r for r in caplog.records if "load_class computed" in r.message]
    assert len(matching) == 1
    assert "endpoint=/message" in matching[0].message
    assert "load_class=medium" in matching[0].message
