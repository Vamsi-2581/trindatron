"""Demand Engine — HTTP-level proof the wiring is actually connected.

Unit coverage for the counters themselves lives in
tests/unit/test_demand_engine.py; what belongs here is the fact that a
real request through a real endpoint reaches them, and that a plan
refusal is recorded as unmet demand rather than silently disappearing.
"""
from __future__ import annotations

from unittest.mock import patch

from trinity_router import RouterStreamChunk

from src.models.user import User
from src.services.demand_engine import demand_snapshot


def _make_stream_mock(texts: list[str]):
    chunks = [RouterStreamChunk(text=t) for t in texts]
    chunks.append(RouterStreamChunk(
        is_final=True, provider="anthropic", model="claude-sonnet-test",
        input_tokens=10, output_tokens=5, cached_tokens=0,
        cost_usd=0.001, tcu=0.1,
    ))

    async def _stream(request):
        for chunk in chunks:
            yield chunk

    return _stream


def _count(rows: list[dict], **match) -> int:
    return sum(
        row["count"] for row in rows
        if all(row.get(k) == v for k, v in match.items())
    )


def _downgrade_to_free(db, user_id: str) -> None:
    user = db.query(User).filter(User.id == user_id).first()
    user.plan = "free"
    db.commit()


def test_chat_request_records_demand(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    with patch("src.services.chat_service._router.stream", _make_stream_mock(["ok"])):
        resp = client.post(
            f"/sessions/{session_id}/chat", json={"content": "Escreva um resumo curto"},
            headers=auth_headers,
        )
    assert resp.status_code == 200

    rows = demand_snapshot()["demand"]
    assert _count(rows, endpoint="/chat") >= 1, rows
    assert all(row["plan"] for row in rows), "plan label must be populated from the real user"


def test_agent_refusal_on_free_plan_records_unmet_demand(client, db, registered_user, auth_headers):
    """The monetization signal: demand a plan turned away. FREE has no
    agent mode (PlanEntitlement.supports_agent_mode)."""
    _downgrade_to_free(db, registered_user["user_id"])
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    resp = client.post(
        f"/sessions/{session_id}/agent", json={"content": "Pesquise e resuma o mercado"},
        headers=auth_headers,
    )
    assert resp.status_code == 403

    snap = demand_snapshot()
    assert _count(snap["unmet"], reason="agent_mode_not_entitled", plan="free") >= 1, snap
    # Recorded as asked BEFORE the gate refused it, so unmet stays a subset.
    assert _count(snap["demand"], endpoint="/agent", plan="free") >= 1, snap


def test_demand_counters_reach_the_metrics_endpoint(client, db, auth_headers):
    """Aggregates are surfaced through the existing admin metrics endpoint,
    not a second reporting path."""
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    with patch("src.services.chat_service._router.stream", _make_stream_mock(["ok"])):
        client.post(
            f"/sessions/{session_id}/chat", json={"content": "Oi, tudo bem?"},
            headers=auth_headers,
        )

    reg = client.post("/auth/register", json={
        "email": "demand-admin@example.com", "password": "securepass123", "full_name": "A",
    }).json()
    admin = db.query(User).filter(User.id == reg["user_id"]).first()
    admin.is_admin = True
    db.commit()

    body = client.get(
        "/health/metrics", headers={"Authorization": f"Bearer {reg['access_token']}"},
    ).json()
    assert "demand_capability_total" in body["counters"]


def test_metrics_endpoint_never_exposes_message_content_via_demand(client, db, auth_headers):
    """The Demand Engine records a closed-set capability label, never the
    text that produced it."""
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    secret = "minha senha e hunter2 e a chave sk-ant-api03-neverleak"
    with patch("src.services.chat_service._router.stream", _make_stream_mock(["ok"])):
        client.post(
            f"/sessions/{session_id}/chat", json={"content": secret}, headers=auth_headers,
        )

    reg = client.post("/auth/register", json={
        "email": "demand-admin2@example.com", "password": "securepass123", "full_name": "A",
    }).json()
    admin = db.query(User).filter(User.id == reg["user_id"]).first()
    admin.is_admin = True
    db.commit()

    text = client.get(
        "/health/metrics", headers={"Authorization": f"Bearer {reg['access_token']}"},
    ).text
    assert "hunter2" not in text
    assert "sk-ant-api03-neverleak" not in text
