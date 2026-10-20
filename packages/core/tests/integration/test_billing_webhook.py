"""Integration tests for POST /billing/webhook/{provider} — Billing-2.

All tests use conftest.py fixtures (db, client, registered_user, auth_headers).
The FakeAdapter test double controls verify_webhook() behavior per test.

Test matrix
-----------
W01) Valid CONFIRMED event → order confirmed, credit balance applied
W02) Valid PROCESSING event → pending order transitions to processing
W03) Valid FAILED event → processing order transitions to failed
W04) InvalidWebhookSignature from adapter → 400 Bad Request
W05) Unknown provider (not in registry) → 400 Bad Request
W06) UnknownWebhookEvent from adapter → 200, event_type="unknown", no order change
W07) provider_ref not in DB (no matching order) → 200, order_id=None
W08) Provider mismatch (order.provider != path provider) → 400
W09) CONFIRMED event on already-confirmed order → 200, idempotent, no double-credit
W10) CONFIRMED event on expired order → 200, absorbed (OrderExpired caught)
W11) Missing X-Signature header → adapter receives empty string; processes normally
W12) CONFIRMED event credits balance atomically (DB state verified)
W13) Empty ProviderRegistry → 400 for any provider name
W14) WebhookEvent.provider_ref = None → 200, no order found, order_id=None
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Mapping

import pytest

from src.api.deps import get_provider_registry
from src.billing.providers.base import (
    InvalidWebhookSignature,
    PaymentIntent,
    PaymentProvider,
    PaymentResult,
    PaymentStatus,
    UnknownWebhookEvent,
    WebhookEvent,
)
from src.billing.providers.registry import ProviderRegistry
from src.main import app as _app
from src.services.payment_order_service import PaymentOrderService


# ---------------------------------------------------------------------------
# Test double — FakeAdapter
# ---------------------------------------------------------------------------


class FakeAdapter(PaymentProvider):
    """Minimal PaymentProvider for webhook tests.

    configure(response=...) — next verify_webhook() returns event.
    configure(exc=...)      — next verify_webhook() raises exception.
    """

    _name = "test_provider"

    def __init__(self) -> None:
        self._response: WebhookEvent | None = None
        self._exc: Exception | None = None

    def configure(
        self,
        *,
        response: WebhookEvent | None = None,
        exc: Exception | None = None,
    ) -> None:
        self._response = response
        self._exc = exc

    @property
    def provider_name(self) -> str:
        return self._name

    async def verify_webhook(self, payload: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        if self._exc is not None:
            raise self._exc
        return self._response

    async def create_intent(self, **kwargs) -> PaymentIntent:
        raise NotImplementedError

    async def get_intent(self, intent_id: str) -> PaymentIntent:
        raise NotImplementedError

    async def refund(self, payment_id: str, **kwargs) -> PaymentResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_registry(adapter: FakeAdapter | None = None) -> ProviderRegistry:
    """Override get_provider_registry with a registry containing adapter (if given)."""
    reg = ProviderRegistry()
    if adapter is not None:
        reg.register(adapter)
    _app.dependency_overrides[get_provider_registry] = lambda: reg
    return reg


def _make_webhook_event(
    *,
    status: PaymentStatus | None,
    provider_ref: str | None = "prov-ref-001",
    event_type: str = "payment.event",
) -> WebhookEvent:
    return WebhookEvent(
        provider="test_provider",
        event_type=event_type,
        payment_id=None,
        provider_ref=provider_ref,
        status=status,
        amount=Decimal("10.00"),
        currency="USD",
        raw_payload={"event": event_type},
    )


def _make_order_in_processing(
    db,
    user_id: str,
    *,
    provider: str = "test_provider",
    ext_ref: str = "prov-ref-001",
    credits: str = "10.00",
):
    """Create a PaymentOrder in processing state with external_order_id set."""
    svc = PaymentOrderService(db)
    order = svc.create_order(
        user_id=user_id,
        provider=provider,
        amount_fiat=Decimal("10.00"),
        currency_fiat="USD",
        credits_amount=Decimal(credits),
        currency_credits="USD",
        idempotency_key=str(uuid.uuid4()),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    svc.mark_processing(order.id, user_id, external_order_id=ext_ref)
    db.expire_all()
    return order


def _post_webhook(
    client,
    *,
    provider: str = "test_provider",
    body: bytes = b'{"event":"test"}',
    signature: str = "sig",
    extra_headers: dict | None = None,
):
    headers = {"X-Signature": signature, "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    return client.post(f"/billing/webhook/{provider}", content=body, headers=headers)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_W01_confirmed_event_transitions_order_and_credits_balance(
    client, auth_headers, db, registered_user
):
    order = _make_order_in_processing(db, registered_user["user_id"])
    adapter = FakeAdapter()
    adapter.configure(response=_make_webhook_event(status=PaymentStatus.CONFIRMED))
    _install_registry(adapter)

    resp = _post_webhook(client)

    assert resp.status_code == 200
    data = resp.json()
    assert data["received"] is True
    assert data["new_status"] == PaymentStatus.CONFIRMED.value
    assert data["order_id"] == order.id

    bal = client.get("/billing/balance/USD", headers=auth_headers)
    assert Decimal(bal.json()["balance"]) == Decimal("10.00")


def test_W02_processing_event_transitions_pending_to_processing(
    client, db, registered_user
):
    svc = PaymentOrderService(db)
    order = svc.create_order(
        user_id=registered_user["user_id"],
        provider="test_provider",
        amount_fiat=Decimal("5.00"),
        currency_fiat="USD",
        credits_amount=Decimal("5.00"),
        currency_credits="USD",
        idempotency_key=str(uuid.uuid4()),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    order.external_order_id = "prov-proc-001"
    db.commit()
    db.expire_all()

    adapter = FakeAdapter()
    adapter.configure(response=_make_webhook_event(
        status=PaymentStatus.PROCESSING, provider_ref="prov-proc-001"
    ))
    _install_registry(adapter)

    resp = _post_webhook(client)

    assert resp.status_code == 200
    assert resp.json()["new_status"] == PaymentStatus.PROCESSING.value


def test_W03_failed_event_transitions_order_to_failed(
    client, db, registered_user
):
    _make_order_in_processing(db, registered_user["user_id"])
    adapter = FakeAdapter()
    adapter.configure(response=_make_webhook_event(status=PaymentStatus.FAILED))
    _install_registry(adapter)

    resp = _post_webhook(client)

    assert resp.status_code == 200
    assert resp.json()["new_status"] == PaymentStatus.FAILED.value


def test_W04_invalid_signature_returns_400(client, db, registered_user):
    _make_order_in_processing(db, registered_user["user_id"])
    adapter = FakeAdapter()
    adapter.configure(exc=InvalidWebhookSignature("bad sig"))
    _install_registry(adapter)

    resp = _post_webhook(client, signature="bad_sig")

    assert resp.status_code == 400
    assert "signature" in resp.json()["detail"].lower()


def test_W05_unknown_provider_returns_400(client, db):
    adapter = FakeAdapter()
    _install_registry(adapter)

    resp = _post_webhook(client, provider="nonexistent")

    assert resp.status_code == 400
    assert "nonexistent" in resp.json()["detail"]


def test_W06_unknown_event_type_returns_200_no_order_change(
    client, db, registered_user
):
    _make_order_in_processing(db, registered_user["user_id"])
    adapter = FakeAdapter()
    adapter.configure(exc=UnknownWebhookEvent("not handled"))
    _install_registry(adapter)

    resp = _post_webhook(client)

    assert resp.status_code == 200
    data = resp.json()
    assert data["event_type"] == "unknown"
    assert data.get("order_id") is None


def test_W07_provider_ref_not_in_db_returns_200(client, db):
    adapter = FakeAdapter()
    adapter.configure(response=_make_webhook_event(
        status=PaymentStatus.CONFIRMED, provider_ref="ghost-ref-999"
    ))
    _install_registry(adapter)

    resp = _post_webhook(client)

    assert resp.status_code == 200
    assert resp.json()["received"] is True
    assert resp.json().get("order_id") is None


def test_W08_provider_mismatch_returns_400(client, db, registered_user):
    # Order recorded under provider="stripe", webhook arrives at /test_provider
    _make_order_in_processing(db, registered_user["user_id"], provider="stripe")

    # FakeAdapter is registered as "test_provider", finds the order by ext_ref,
    # then the path "test_provider" != order.provider "stripe" → 400
    adapter = FakeAdapter()
    adapter.configure(response=_make_webhook_event(status=PaymentStatus.CONFIRMED))
    _install_registry(adapter)

    resp = _post_webhook(client)

    assert resp.status_code == 400
    assert "mismatch" in resp.json()["detail"].lower()


def test_W09_confirmed_event_on_already_confirmed_order_is_idempotent(
    client, auth_headers, db, registered_user
):
    _make_order_in_processing(db, registered_user["user_id"])
    adapter = FakeAdapter()
    adapter.configure(response=_make_webhook_event(status=PaymentStatus.CONFIRMED))
    _install_registry(adapter)

    resp1 = _post_webhook(client)
    assert resp1.status_code == 200

    resp2 = _post_webhook(client)
    assert resp2.status_code == 200
    assert resp2.json()["new_status"] == PaymentStatus.CONFIRMED.value

    # Balance credited only once
    bal = client.get("/billing/balance/USD", headers=auth_headers)
    assert Decimal(bal.json()["balance"]) == Decimal("10.00")


def test_W10_confirmed_event_on_expired_order_absorbed(client, db, registered_user):
    svc = PaymentOrderService(db)
    order = svc.create_order(
        user_id=registered_user["user_id"],
        provider="test_provider",
        amount_fiat=Decimal("10.00"),
        currency_fiat="USD",
        credits_amount=Decimal("10.00"),
        currency_credits="USD",
        idempotency_key=str(uuid.uuid4()),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    order.external_order_id = "prov-expired-001"
    order.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    db.expire_all()

    adapter = FakeAdapter()
    adapter.configure(response=_make_webhook_event(
        status=PaymentStatus.CONFIRMED, provider_ref="prov-expired-001"
    ))
    _install_registry(adapter)

    resp = _post_webhook(client)

    assert resp.status_code == 200
    assert resp.json()["received"] is True


def test_W11_missing_signature_header_falls_through_to_adapter(
    client, db, registered_user
):
    _make_order_in_processing(db, registered_user["user_id"])
    adapter = FakeAdapter()
    adapter.configure(response=_make_webhook_event(status=PaymentStatus.CONFIRMED))
    _install_registry(adapter)

    # No X-Signature header — adapter receives the full headers mapping without
    # that key (FakeAdapter ignores headers entirely) and returns the event
    resp = client.post(
        "/billing/webhook/test_provider",
        content=b'{}',
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 200
    assert resp.json()["new_status"] == PaymentStatus.CONFIRMED.value


def test_W12_confirmed_event_credits_balance_atomically(
    client, auth_headers, db, registered_user
):
    _make_order_in_processing(db, registered_user["user_id"], credits="25.00")
    adapter = FakeAdapter()
    adapter.configure(response=WebhookEvent(
        provider="test_provider",
        event_type="payment.confirmed",
        payment_id=None,
        provider_ref="prov-ref-001",
        status=PaymentStatus.CONFIRMED,
        # The real money paid (order.amount_fiat, fixed at 10.00 by
        # _make_order_in_processing) — NOT credits_amount (25.00, a
        # different figure by design: pay $10, receive 25.00 USD-
        # denominated credits at some rate). Webhook amount validation
        # (Commercial Checkout V1) checks this against order.amount_fiat.
        amount=Decimal("10.00"),
        currency="USD",
        raw_payload={},
    ))
    _install_registry(adapter)

    resp = _post_webhook(client)
    assert resp.status_code == 200

    bal = client.get("/billing/balance/USD", headers=auth_headers)
    assert Decimal(bal.json()["balance"]) == Decimal("25.00")

    order_resp = client.get(f"/billing/orders/{resp.json()['order_id']}", headers=auth_headers)
    assert order_resp.json()["status"] == "confirmed"


def test_W13_empty_registry_returns_400_for_any_provider(client, db):
    _install_registry()  # no adapter registered

    resp = _post_webhook(client, provider="any_provider_name")

    assert resp.status_code == 400


def test_W14_provider_ref_none_returns_200_without_order_id(client, db, registered_user):
    adapter = FakeAdapter()
    adapter.configure(response=_make_webhook_event(
        status=PaymentStatus.CONFIRMED, provider_ref=None
    ))
    _install_registry(adapter)

    resp = _post_webhook(client)

    assert resp.status_code == 200
    assert resp.json().get("order_id") is None
