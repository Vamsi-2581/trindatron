"""Etapa 5I — HTTP-level Idempotency-Key wiring for POST /chat, /agent,
/message.

Covers the required scenarios that are best proven at the HTTP layer
(header presence/absence, 409 mapping, replay short-circuit, retry-after-
failure). Pure claim/reconcile logic is covered directly against
idempotency_service.py in tests/unit/test_idempotency_service.py; Agent
Execution Recovery state is covered in
tests/integration/test_agent_execution_recovery.py.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from trinity_router import RouterStreamChunk, RoutingResult

from src.models.idempotency_key import IdempotencyKey
from src.models.message import Message
from src.models.usage import UsageRecord


def _make_stream_mock(texts: list[str], cost_usd: float = 0.0):
    chunks = [RouterStreamChunk(text=t) for t in texts]
    chunks.append(RouterStreamChunk(
        is_final=True, provider="anthropic", model="claude-sonnet-test",
        input_tokens=10, output_tokens=sum(len(t) for t in texts),
        cached_tokens=0, cost_usd=cost_usd, tcu=cost_usd * 100.0,
    ))

    async def _stream(request):
        for chunk in chunks:
            yield chunk

    return _stream


def _failing_stream(*_args, **_kwargs):
    async def _stream(request):
        raise ConnectionError("provider unreachable")
        yield  # pragma: no cover — makes this an async generator function

    return _stream


def _direct_answer(content: str = "The answer is 42.", cost_usd: float = 0.0) -> RoutingResult:
    return RoutingResult(
        content=content, tool_calls=None, provider="anthropic", model="claude-sonnet-test",
        input_tokens=10, output_tokens=5, cached_tokens=0, cost_usd=cost_usd, tcu=cost_usd * 100.0,
        failover_used=False, failover_reason=None,
    )


def _new_key() -> str:
    return f"idem-{uuid.uuid4()}"


# ── Test 7 — absence of the header is explicit, tested, opt-in behavior ────

def test_no_idempotency_key_header_allows_independent_duplicate_effects(client, auth_headers, db):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    with patch("src.services.chat_service._router.stream", _make_stream_mock(["Hi!"])):
        r1 = client.post(f"/sessions/{session_id}/chat", json={"content": "Hello"}, headers=auth_headers)
        r2 = client.post(f"/sessions/{session_id}/chat", json={"content": "Hello"}, headers=auth_headers)

    assert r1.status_code == 200
    assert r2.status_code == 200
    # No header -> idempotency_service is never consulted -> both requests
    # run the full pipeline independently, exactly as before this etapa.
    assert db.query(IdempotencyKey).count() == 0
    messages = db.query(Message).filter(Message.session_id == session_id).all()
    assert len(messages) == 4  # 2 user + 2 assistant — two genuinely independent effects


# ── Test 1/3 — same key + same request → single effect, retry replays ──────

def test_same_key_same_request_replays_without_duplicate_effect(client, auth_headers, db):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    key = _new_key()
    headers = {**auth_headers, "Idempotency-Key": key}

    with patch("src.services.chat_service._router.stream", _make_stream_mock(["Hi!"])):
        r1 = client.post(f"/sessions/{session_id}/chat", json={"content": "Hello"}, headers=headers)
    r2 = client.post(f"/sessions/{session_id}/chat", json={"content": "Hello"}, headers=headers)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json()["idempotent_replay"] is True
    assert r2.json()["session_id"] == session_id

    messages = db.query(Message).filter(Message.session_id == session_id).all()
    assert len(messages) == 2  # exactly one user+assistant pair — never duplicated

    row = db.query(IdempotencyKey).filter(IdempotencyKey.key == key).first()
    assert row.status == "completed"


# ── Test 5 — same key, different content → rejection, never silent reuse ───

def test_same_key_incompatible_payload_returns_409(client, auth_headers, db):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    key = _new_key()
    headers = {**auth_headers, "Idempotency-Key": key}

    with patch("src.services.chat_service._router.stream", _make_stream_mock(["Hi!"])):
        r1 = client.post(f"/sessions/{session_id}/chat", json={"content": "Hello"}, headers=headers)
    r2 = client.post(f"/sessions/{session_id}/chat", json={"content": "Something else entirely"}, headers=headers)

    assert r1.status_code == 200
    assert r2.status_code == 409


# ── Test 6 — different keys are fully independent ───────────────────────────

def test_different_keys_produce_independent_effects(client, auth_headers, db):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    with patch("src.services.chat_service._router.stream", _make_stream_mock(["Hi!"])):
        r1 = client.post(
            f"/sessions/{session_id}/chat", json={"content": "Hello"},
            headers={**auth_headers, "Idempotency-Key": _new_key()},
        )
        r2 = client.post(
            f"/sessions/{session_id}/chat", json={"content": "Hello"},
            headers={**auth_headers, "Idempotency-Key": _new_key()},
        )

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert db.query(IdempotencyKey).count() == 2
    messages = db.query(Message).filter(Message.session_id == session_id).all()
    assert len(messages) == 4


# ── Test 4 — retry while genuinely in progress (fresh) is deterministic ────

def test_retry_while_fresh_in_progress_returns_409(client, auth_headers, db):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    key = _new_key()

    from src.services.idempotency_service import make_fingerprint
    fp = make_fingerprint(endpoint="chat", session_id=session_id, content="Hello")
    # user_id must be the actual authenticated user's id — resolved via the
    # session we just created instead of a second lookup endpoint.
    from src.models.session import Session as SessionModel
    session = db.query(SessionModel).filter(SessionModel.id == session_id).one()
    row = IdempotencyKey(
        user_id=session.user_id, key=key, endpoint="chat", fingerprint=fp, status="in_progress",
    )
    db.add(row)
    db.commit()

    resp = client.post(
        f"/sessions/{session_id}/chat", json={"content": "Hello"},
        headers={**auth_headers, "Idempotency-Key": key},
    )

    assert resp.status_code == 409


# ── Test 11 + REGRA FINANCEIRA CRÍTICA — retry after failure, one effect ───

def test_retry_after_failure_produces_exactly_one_completed_effect(client, auth_headers, db):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    key = _new_key()
    headers = {**auth_headers, "Idempotency-Key": key}

    with patch("src.services.chat_service._router.stream", _failing_stream()):
        r1 = client.post(f"/sessions/{session_id}/chat", json={"content": "Hello"}, headers=headers)
    # SSE streaming: the response itself is already 200 by the time the
    # provider call fails mid-stream — the failure surfaces as an "error"
    # event inside the body (existing pre-5I behavior), not an HTTP 500.
    assert r1.status_code == 200
    assert '"type": "error"' in r1.text

    row = db.query(IdempotencyKey).filter(IdempotencyKey.key == key).first()
    assert row.status == "failed"

    with patch("src.services.chat_service._router.stream", _make_stream_mock(["Hi!"])):
        r2 = client.post(f"/sessions/{session_id}/chat", json={"content": "Hello"}, headers=headers)
    assert r2.status_code == 200

    db.refresh(row)
    assert row.status == "completed"
    assert db.query(IdempotencyKey).filter(IdempotencyKey.key == key).count() == 1  # same row throughout

    completed = db.query(UsageRecord).filter(
        UsageRecord.session_id == session_id, UsageRecord.status == "completed",
    ).all()
    assert len(completed) == 1  # exactly one economic effect for one logical operation

    # The failed attempt persisted its user message (existing pre-5I
    # behavior — chat_service persists the user turn before calling the
    # provider) but no assistant reply; the retry adds a fresh user+
    # assistant pair. Idempotency governs the ECONOMIC effect (asserted
    # above via the UsageRecord count), not message persistence.
    messages = db.query(Message).filter(Message.session_id == session_id).all()
    assert len(messages) == 3
    assert [m.role for m in messages] == ["user", "user", "assistant"]


# ── /agent — same replay contract as /chat ──────────────────────────────────

def test_agent_endpoint_same_key_retry_returns_replay(client, auth_headers, db):
    from src.models.agent_execution import AgentExecution

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    key = _new_key()
    headers = {**auth_headers, "Idempotency-Key": key}

    direct = _direct_answer("The answer is 42.")
    with patch("src.services.agent_service._router.complete", new=AsyncMock(return_value=direct)):
        r1 = client.post(f"/sessions/{session_id}/agent", json={"content": "What is 6x7?"}, headers=headers)
    r2 = client.post(f"/sessions/{session_id}/agent", json={"content": "What is 6x7?"}, headers=headers)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json()["idempotent_replay"] is True

    executions = db.query(AgentExecution).filter(AgentExecution.session_id == session_id).all()
    assert len(executions) == 1  # the replay never created a second AgentExecution
    assert executions[0].status == "completed"


# ── /message — same replay contract, CHAT-routed capability ────────────────

def test_message_endpoint_same_key_retry_returns_replay(client, auth_headers, db):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    key = _new_key()
    headers = {**auth_headers, "Idempotency-Key": key}

    with patch("src.services.chat_service._router.stream", _make_stream_mock(["Hello!"])):
        r1 = client.post(f"/sessions/{session_id}/message", json={"content": "Hello"}, headers=headers)
    r2 = client.post(f"/sessions/{session_id}/message", json={"content": "Hello"}, headers=headers)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json()["idempotent_replay"] is True

    messages = db.query(Message).filter(Message.session_id == session_id).all()
    assert len(messages) == 2
