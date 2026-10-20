"""
Tests for the Chat Retention Policy (migration 0008).

Policy:
- Regular conversations: soft-deleted (UI removes immediately); permanent backend
  deletion within 30 days.
- Temporary conversations: auto-deleted 30 days after creation; not listed in
  permanent history (is_temporary flag).
- Usage records: retained independently when a session is deleted (SET NULL on
  session_id) for billing, TCU accounting, and auditing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from trinity_router import RouterStreamChunk


def _stream_mock(texts: list[str] = None, tcu: float = 0.1, cost_usd: float = 0.001):
    chunks = [RouterStreamChunk(text=t) for t in (texts or ["Hello"])]
    chunks.append(RouterStreamChunk(
        is_final=True, provider="anthropic", model="claude-test",
        input_tokens=10, output_tokens=5, cached_tokens=0,
        cost_usd=cost_usd, tcu=tcu,
    ))

    async def _stream(request):
        for chunk in chunks:
            yield chunk

    return _stream


# ── Soft delete ───────────────────────────────────────────────────────────────

def test_delete_session_hides_from_list(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    assert any(
        s["id"] == session_id
        for s in client.get("/sessions", headers=auth_headers).json()["sessions"]
    )

    client.delete(f"/sessions/{session_id}", headers=auth_headers)

    assert not any(
        s["id"] == session_id
        for s in client.get("/sessions", headers=auth_headers).json()["sessions"]
    )


def test_delete_session_blocks_get_detail(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    client.delete(f"/sessions/{session_id}", headers=auth_headers)
    assert client.get(f"/sessions/{session_id}", headers=auth_headers).status_code == 404


def test_delete_session_blocks_chat(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    client.delete(f"/sessions/{session_id}", headers=auth_headers)
    resp = client.post(f"/sessions/{session_id}/chat", json={"content": "hi"}, headers=auth_headers)
    assert resp.status_code == 404


# ── Usage record retention ────────────────────────────────────────────────────

def test_usage_records_survive_session_deletion(client, auth_headers, db):
    from src.models.usage import UsageRecord

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    with patch("src.services.chat_service._router.stream", _stream_mock(tcu=0.5)):
        client.post(f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=auth_headers)

    # Confirm usage record exists before deletion
    before = db.query(UsageRecord).filter(UsageRecord.session_id == session_id).count()
    assert before >= 1

    client.delete(f"/sessions/{session_id}", headers=auth_headers)

    # After soft-delete the session_id on UsageRecord should still reference the
    # session row (it's soft-deleted, not hard-deleted yet).
    after = db.query(UsageRecord).filter(UsageRecord.session_id == session_id).count()
    assert after >= 1


def test_usage_records_survive_hard_delete(client, auth_headers, db):
    """After cleanup_expired_sessions() permanently removes the session row,
    usage records must remain with session_id = NULL."""
    from src.models.usage import UsageRecord
    from src.services.session_service import cleanup_expired_sessions

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    with patch("src.services.chat_service._router.stream", _stream_mock(tcu=0.5)):
        client.post(f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=auth_headers)

    # Soft-delete then advance time past 30-day window to trigger hard deletion
    client.delete(f"/sessions/{session_id}", headers=auth_headers)

    future = datetime.now(timezone.utc) + timedelta(days=31)
    with patch("src.services.session_service._utcnow", return_value=future):
        removed = cleanup_expired_sessions(db)

    assert removed >= 1
    # Usage records must still exist with session_id = NULL
    orphaned = db.query(UsageRecord).filter(
        UsageRecord.session_id.is_(None),
        UsageRecord.tcu > 0,
    ).count()
    assert orphaned >= 1


# ── Temporary conversations ───────────────────────────────────────────────────

def test_temporary_session_flag_in_response(client, auth_headers):
    resp = client.post("/sessions", json={"is_temporary": True}, headers=auth_headers)
    assert resp.status_code == 201
    assert resp.json()["is_temporary"] is True


def test_regular_session_is_not_temporary(client, auth_headers):
    resp = client.post("/sessions", json={}, headers=auth_headers)
    assert resp.json()["is_temporary"] is False


def test_temporary_session_expires_after_30_days(client, auth_headers, db):
    from src.services.session_service import cleanup_expired_sessions
    from src.models.session import Session

    resp = client.post("/sessions", json={"is_temporary": True}, headers=auth_headers)
    session_id = resp.json()["id"]

    future = datetime.now(timezone.utc) + timedelta(days=31)
    with patch("src.services.session_service._utcnow", return_value=future):
        removed = cleanup_expired_sessions(db)

    assert removed >= 1
    assert db.query(Session).filter(Session.id == session_id).first() is None


def test_regular_session_not_expired_without_soft_delete(client, auth_headers, db):
    """A regular session that has NOT been soft-deleted must NOT be removed by cleanup,
    even after 31 days."""
    from src.services.session_service import cleanup_expired_sessions
    from src.models.session import Session

    resp = client.post("/sessions", json={}, headers=auth_headers)
    session_id = resp.json()["id"]

    future = datetime.now(timezone.utc) + timedelta(days=31)
    with patch("src.services.session_service._utcnow", return_value=future):
        cleanup_expired_sessions(db)

    assert db.query(Session).filter(Session.id == session_id).first() is not None


def test_soft_deleted_regular_session_expires_after_30_days(client, auth_headers, db):
    from src.services.session_service import cleanup_expired_sessions
    from src.models.session import Session

    resp = client.post("/sessions", json={}, headers=auth_headers)
    session_id = resp.json()["id"]
    client.delete(f"/sessions/{session_id}", headers=auth_headers)

    # Before 30 days: still present (soft-deleted row retained)
    future_29 = datetime.now(timezone.utc) + timedelta(days=29)
    with patch("src.services.session_service._utcnow", return_value=future_29):
        removed = cleanup_expired_sessions(db)
    assert removed == 0
    assert db.query(Session).filter(Session.id == session_id).first() is not None

    # After 30 days: permanently removed
    future_31 = datetime.now(timezone.utc) + timedelta(days=31)
    with patch("src.services.session_service._utcnow", return_value=future_31):
        removed = cleanup_expired_sessions(db)
    assert removed >= 1
    assert db.query(Session).filter(Session.id == session_id).first() is None
