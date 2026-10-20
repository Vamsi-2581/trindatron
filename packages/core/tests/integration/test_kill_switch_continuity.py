"""Etapa 5H — global Continuity kill switch (continuity_enabled).

Covers: enabled (default) -> Etapa 5F behavior unaffected (a real
activation still succeeds); disabled -> Continuity never activates, falls
through to the ORIGINAL 429, no debit, no provider call, no
activate_continuity_or_raise() call at all.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import patch

from src.models.ledger import CreditTransaction
from src.models.user import User
from src.observability.metrics import core_metrics
from src.services.credit_service import CreditService
from src.services.runtime_settings import CONTINUITY_ENABLED, set_setting


def _register_and_set_plan(client, db, email: str, plan: str) -> dict:
    resp = client.post("/auth/register", json={
        "email": email, "password": "securepass123", "full_name": "Kill Switch Test",
    })
    assert resp.status_code == 201
    data = resp.json()
    user = db.query(User).filter(User.id == data["user_id"]).first()
    user.plan = plan
    db.commit()
    return {"token": data["access_token"], "user_id": data["user_id"]}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _grant_credits(db, user_id: str, amount: str) -> None:
    CreditService(db).credit(
        user_id=user_id, currency="TCU", amount=Decimal(amount),
        idempotency_key=f"test-grant-{uuid.uuid4()}", transaction_type="grant", note="test setup",
    )


def _exhausted_quota_exception():
    from fastapi import HTTPException
    return HTTPException(
        status_code=429,
        detail={"error": "daily_limit_exceeded", "message": "Limite diário de TCU atingido.",
                "daily_used": 100.0, "daily_limit": 100.0},
    )


def _counter_sum(snapshot: dict, **label_filter: str) -> float:
    total = 0.0
    for labels, value in snapshot.items():
        d = dict(labels)
        if all(d.get(k) == v for k, v in label_filter.items()):
            total += value
    return total


def _fake_continuity_model():
    from trinity_router.providers import ProviderModel
    return "ks-test-model", ProviderModel(
        provider="anthropic", model_id="ks-test-model", context_window=100_000,
        cost_per_1m_input_usd=0.1, cost_per_1m_output_usd=0.3,
        supports_tools=True, supports_streaming=True, modality="text",
    )


def _stream_mock(texts, cost_usd=0.04):
    from trinity_router import RouterStreamChunk

    chunks = [RouterStreamChunk(text=t) for t in texts]
    chunks.append(RouterStreamChunk(
        is_final=True, provider="anthropic", model="ks-test-model",
        input_tokens=10, output_tokens=sum(len(t) for t in texts),
        cached_tokens=0, cost_usd=cost_usd, tcu=cost_usd * 100.0,
    ))

    async def _stream(request):
        for c in chunks:
            yield c

    return _stream


# ═════════════════════════════════════════════════════════════════════════
# Enabled (default) — Etapa 5F behavior unaffected
# ═════════════════════════════════════════════════════════════════════════

def test_continuity_enabled_default_still_activates(client, db):
    user = _register_and_set_plan(client, db, "ks_enabled@test.com", "start")
    _grant_credits(db, user["user_id"], "1000")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]

    with patch("src.api.routes.sessions.check_and_reserve", side_effect=_exhausted_quota_exception()), \
         patch("src.services.continuity_mode.select_continuity_model", return_value=_fake_continuity_model()), \
         patch("src.services.chat_service._router.stream", _stream_mock(["ok"])):
        resp = client.post(f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=_auth(user["token"]))

    assert resp.status_code == 200


# ═════════════════════════════════════════════════════════════════════════
# Disabled — never activates, no debit, no provider call
# ═════════════════════════════════════════════════════════════════════════

def test_continuity_disabled_falls_through_to_original_429(client, db):
    user = _register_and_set_plan(client, db, "ks_disabled@test.com", "start")
    _grant_credits(db, user["user_id"], "1000")
    set_setting(db, CONTINUITY_ENABLED, False, admin_user_id="test-admin")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]

    with patch("src.api.routes.sessions.check_and_reserve", side_effect=_exhausted_quota_exception()), \
         patch("src.services.continuity_mode.activate_continuity_or_raise") as activate_spy, \
         patch("src.services.chat_service._router.stream") as stream_spy:
        resp = client.post(f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=_auth(user["token"]))

    assert resp.status_code == 429
    activate_spy.assert_not_called()  # not even the eligibility/model checks ran
    stream_spy.assert_not_called()


def test_continuity_disabled_debits_nothing(client, db):
    user = _register_and_set_plan(client, db, "ks_disabled_nodebit@test.com", "start")
    _grant_credits(db, user["user_id"], "1000")
    set_setting(db, CONTINUITY_ENABLED, False, admin_user_id="test-admin")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]

    with patch("src.api.routes.sessions.check_and_reserve", side_effect=_exhausted_quota_exception()):
        resp = client.post(f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=_auth(user["token"]))

    assert resp.status_code == 429
    assert db.query(CreditTransaction).filter(
        CreditTransaction.user_id == user["user_id"], CreditTransaction.transaction_type == "consume",
    ).count() == 0


def test_continuity_disabled_records_kill_switch_metric(client, db):
    user = _register_and_set_plan(client, db, "ks_disabled_metric@test.com", "start")
    _grant_credits(db, user["user_id"], "1000")
    set_setting(db, CONTINUITY_ENABLED, False, admin_user_id="test-admin")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]

    with patch("src.api.routes.sessions.check_and_reserve", side_effect=_exhausted_quota_exception()):
        client.post(f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=_auth(user["token"]))

    rejections = core_metrics.counter("kill_switch_rejections_total").snapshot()
    assert _counter_sum(rejections, component="continuity", reason="continuity_disabled") >= 1

    activations = core_metrics.counter("continuity_activations_total").snapshot()
    assert _counter_sum(activations, plan="start", outcome="continuity_disabled") >= 1


def test_continuity_disabled_does_not_affect_normal_quota_available_path(client, db):
    """continuity_enabled=False must not touch the NORMAL path at all —
    quota is genuinely available here, Continuity is never even
    considered regardless of the switch."""
    user = _register_and_set_plan(client, db, "ks_disabled_normal@test.com", "start")
    set_setting(db, CONTINUITY_ENABLED, False, admin_user_id="test-admin")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]

    with patch("src.services.chat_service._router.stream", _stream_mock(["ok"])):
        resp = client.post(f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=_auth(user["token"]))

    assert resp.status_code == 200
