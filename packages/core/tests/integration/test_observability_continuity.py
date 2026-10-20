"""Etapa 5G (Observability) — Continuity Mode metrics.

Complements tests/integration/test_continuity_execution.py (which proves
Continuity's real execution wiring) and
tests/integration/test_continuity_entitlement.py (which proves the
financial/budget gate in isolation) — this file only proves the NEW
observability layer sessions.py/continuity_entitlement_service.py gained
in this etapa: continuity_activations_total (the one unified outcome
counter every activation attempt converges into) and the
continuity_tcu_debited/continuity_tcu_true_up histograms.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import patch

from src.models.user import User
from src.observability.metrics import core_metrics
from src.services.credit_service import CreditService


def _register_and_set_plan(client, db, email: str, plan: str) -> dict:
    resp = client.post("/auth/register", json={
        "email": email, "password": "securepass123", "full_name": "Observability Test",
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


def _stream_mock(texts, cost_usd=0.04):
    from trinity_router import RouterStreamChunk

    chunks = [RouterStreamChunk(text=t) for t in texts]
    chunks.append(RouterStreamChunk(
        is_final=True, provider="anthropic", model="continuity-test-model",
        input_tokens=10, output_tokens=sum(len(t) for t in texts),
        cached_tokens=0, cost_usd=cost_usd, tcu=cost_usd * 100.0,
    ))

    async def _stream(request):
        for c in chunks:
            yield c

    return _stream


def _fake_continuity_model():
    from trinity_router.providers import ProviderModel
    return "obs-test-model", ProviderModel(
        provider="anthropic", model_id="obs-test-model", context_window=100_000,
        cost_per_1m_input_usd=0.1, cost_per_1m_output_usd=0.3,
        supports_tools=True, supports_streaming=True, modality="text",
    )


def test_activated_continuity_increments_activations_total_with_outcome_activated(client, db):
    user = _register_and_set_plan(client, db, "obs_activated@test.com", "start")
    _grant_credits(db, user["user_id"], "1000")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]

    with patch("src.api.routes.sessions.check_and_reserve", side_effect=_exhausted_quota_exception()), \
         patch("src.services.continuity_mode.select_continuity_model", return_value=_fake_continuity_model()), \
         patch("src.services.chat_service._router.stream", _stream_mock(["ok"], cost_usd=0.01)):
        resp = client.post(f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=_auth(user["token"]))

    assert resp.status_code == 200
    activations = core_metrics.counter("continuity_activations_total").snapshot()
    assert _counter_sum(activations, plan="start", outcome="activated") >= 1

    debits = core_metrics.counter("continuity_debits_total").snapshot()
    assert _counter_sum(debits, plan="start", outcome="debited") >= 1

    debited_hist = core_metrics.histogram("continuity_tcu_debited").snapshot()
    assert sum(v["count"] for v in debited_hist.values()) >= 1

    # The debit is min(max_tcu_per_request, continuity_daily_tcu_limit) — on
    # START, 4 TCU — so the mocked stream costs 1 TCU deliberately: without a
    # gap between estimate and reality there is nothing to true up, and this
    # assertion would be measuring the mock rather than the reconciliation.
    trueup_hist = core_metrics.histogram("continuity_tcu_true_up").snapshot()
    assert sum(v["count"] for v in trueup_hist.values()) >= 1  # refund of the over-estimate


def test_free_plan_decline_increments_activations_total_with_outcome_not_enabled(client, db):
    user = _register_and_set_plan(client, db, "obs_declined@test.com", "free")
    _grant_credits(db, user["user_id"], "1000")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]

    with patch("src.api.routes.sessions.check_and_reserve", side_effect=_exhausted_quota_exception()):
        resp = client.post(f"/sessions/{session_id}/chat", json={"content": "hello"}, headers=_auth(user["token"]))

    assert resp.status_code == 429
    activations = core_metrics.counter("continuity_activations_total").snapshot()
    assert _counter_sum(activations, plan="free", outcome="not_enabled") >= 1


def test_multimodal_decline_increments_activations_total_with_rejected_reason(client, db):
    user = _register_and_set_plan(client, db, "obs_multimodal@test.com", "start")
    _grant_credits(db, user["user_id"], "1000")
    session_id = client.post("/sessions", json={}, headers=_auth(user["token"])).json()["id"]

    with patch("src.api.routes.sessions.check_and_reserve", side_effect=_exhausted_quota_exception()):
        resp = client.post(
            f"/sessions/{session_id}/message",
            json={"content": "generate an image of a cat"},
            headers=_auth(user["token"]),
        )

    assert resp.status_code == 429
    activations = core_metrics.counter("continuity_activations_total").snapshot()
    assert _counter_sum(activations, plan="start", outcome="rejected_multimodal_capability") >= 1
