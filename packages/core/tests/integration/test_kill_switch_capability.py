"""Etapa 5H — per-capability kill switch (disabled_capabilities).

Covers: enabled (default) -> normal flow; disabled -> 503 before session
lookup/TCU/scheduler/provider, applied UNIFORMLY across /chat, /agent,
/message (see _reject_if_capability_disabled's own docstring in
sessions.py for why all three, not just /message); no TCU consumed;
disabling one capability never affects another.
"""
from __future__ import annotations

from unittest.mock import patch

from src.models.usage import UsageRecord
from src.services.runtime_settings import DISABLED_CAPABILITIES, set_setting


def _disable_capability(db, capability_value: str) -> None:
    set_setting(db, DISABLED_CAPABILITIES, [capability_value], admin_user_id="test-admin")


# ═════════════════════════════════════════════════════════════════════════
# Enabled (default) — unaffected
# ═════════════════════════════════════════════════════════════════════════

def test_capability_enabled_default_message_normal_flow(client, auth_headers):
    from trinity_router import RouterStreamChunk

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    chunks = [RouterStreamChunk(text="hi")]
    chunks.append(RouterStreamChunk(is_final=True, provider="anthropic", model="claude-sonnet-test",
                                     input_tokens=5, output_tokens=5, cached_tokens=0, cost_usd=0.0, tcu=0.0))

    async def _stream(request):
        for c in chunks:
            yield c

    with patch("src.services.chat_service._router.stream", _stream):
        resp = client.post(f"/sessions/{session_id}/message", json={"content": "hello there"}, headers=auth_headers)
    assert resp.status_code == 200


# ═════════════════════════════════════════════════════════════════════════
# Disabled — rejected before provider/TCU, on every endpoint
# ═════════════════════════════════════════════════════════════════════════

def test_message_rejects_disabled_capability_before_provider(client, db, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    _disable_capability(db, "image")

    before = db.query(UsageRecord).count()
    with patch("src.services.chat_service._router.stream") as stream_spy, \
         patch("src.services.agent_service._router.complete") as complete_spy:
        resp = client.post(
            f"/sessions/{session_id}/message",
            json={"content": "generate an image of a cat"},
            headers=auth_headers,
        )
    after = db.query(UsageRecord).count()

    assert resp.status_code == 503
    stream_spy.assert_not_called()
    complete_spy.assert_not_called()
    assert after == before


def test_chat_rejects_disabled_capability(client, db, auth_headers):
    """Uniform gate: even though /chat never DISPATCHES by capability
    (it always runs stream_chat), the classified capability is still
    checked against the kill switch before anything else."""
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    _disable_capability(db, "image")

    with patch("src.services.chat_service._router.stream") as stream_spy:
        resp = client.post(
            f"/sessions/{session_id}/chat",
            json={"content": "generate an image of a cat"},
            headers=auth_headers,
        )

    assert resp.status_code == 503
    stream_spy.assert_not_called()


def test_agent_rejects_disabled_capability(client, db, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    _disable_capability(db, "automation")

    with patch("src.services.agent_service._router.complete") as complete_spy:
        resp = client.post(
            f"/sessions/{session_id}/agent",
            json={"content": "automate this workflow for me"},
            headers=auth_headers,
        )

    assert resp.status_code == 503
    complete_spy.assert_not_called()


def test_disabling_one_capability_does_not_affect_another(client, db, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    _disable_capability(db, "image")

    from trinity_router import RouterStreamChunk
    chunks = [RouterStreamChunk(text="hi")]
    chunks.append(RouterStreamChunk(is_final=True, provider="anthropic", model="claude-sonnet-test",
                                     input_tokens=5, output_tokens=5, cached_tokens=0, cost_usd=0.0, tcu=0.0))

    async def _stream(request):
        for c in chunks:
            yield c

    with patch("src.services.chat_service._router.stream", _stream):
        resp = client.post(f"/sessions/{session_id}/message", json={"content": "just chatting here"}, headers=auth_headers)
    assert resp.status_code == 200
