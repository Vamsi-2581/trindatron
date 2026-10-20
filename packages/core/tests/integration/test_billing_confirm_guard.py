"""Integration tests for the confirm-order evidence-of-payment guard — Billing-2.

Audit finding this closes: POST /billing/orders/{id}/confirm previously let
any authenticated user confirm (and get credited for) ANY of their own
pending/processing orders directly — no proof a payment ever happened. For
a provider with NO adapter registered that was already the only path
available (fine for a manual/legacy flow), but for a provider WITH a real
adapter registered, that adapter's webhook is the actual evidence of
payment — the client calling this endpoint is not.

This guard: POST /orders/{id}/confirm now returns 409 for any order whose
provider is registered in the ProviderRegistry; confirmation for those
orders can only happen via POST /billing/webhook/{provider}, which requires
a signature the adapter verifies. Orders whose provider has no adapter
registered are unaffected — see test_C02 below, and every pre-existing
test_billing_routes.py confirm scenario (provider="test_provider", never
registered in the default empty registry), which is why none of those
needed to change for this guard to land.

POST /billing/orders now also calls a registered provider's create_intent()
(create_provider_order(), see payment_order_service.py) — so
_MinimalRegisteredAdapter.create_intent() below must return a usable
PaymentIntent, not raise, or order creation itself would fail before these
tests ever reach the confirm guard they exist to check.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.api.deps import get_provider_registry
from src.billing.providers.base import PaymentIntent, PaymentProvider, PaymentStatus
from src.billing.providers.registry import ProviderRegistry
from src.main import app as _app


class _MinimalRegisteredAdapter(PaymentProvider):
    """A PaymentProvider that does the minimum to exist in the registry AND
    let order creation succeed — enough to prove the confirm guard keys off
    registration, not off any specific adapter's real payment behavior."""

    @property
    def provider_name(self) -> str:
        return "binance_pay"

    async def create_intent(self, *, amount, currency, user_id, idempotency_key, metadata=None):
        return PaymentIntent(
            id=f"fake-external-{idempotency_key}",
            provider=self.provider_name,
            amount=amount,
            currency=currency,
            status=PaymentStatus.PENDING,
            idempotency_key=idempotency_key,
        )

    async def get_intent(self, intent_id: str):
        raise NotImplementedError

    async def verify_webhook(self, payload: bytes, headers):
        raise NotImplementedError

    async def refund(self, payment_id: str, **kwargs):
        raise NotImplementedError


def _install_registry(*, with_adapter: bool) -> ProviderRegistry:
    reg = ProviderRegistry()
    if with_adapter:
        reg.register(_MinimalRegisteredAdapter())
    _app.dependency_overrides[get_provider_registry] = lambda: reg
    return reg


def _create_order(client, headers: dict, *, provider: str) -> dict:
    body = {
        "provider": provider,
        "amount_fiat": "10.00",
        "currency_fiat": "USD",
        "credits_amount": "10.00",
        "currency_credits": "USD",
        "idempotency_key": str(uuid.uuid4()),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    resp = client.post("/billing/orders", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_C01_client_confirm_refused_for_registered_provider(client, auth_headers):
    _install_registry(with_adapter=True)
    order = _create_order(client, auth_headers, provider="binance_pay")

    resp = client.post(f"/billing/orders/{order['id']}/confirm", headers=auth_headers)

    assert resp.status_code == 409
    assert "webhook" in resp.json()["detail"].lower()

    # And the balance was never touched.
    bal = client.get("/billing/balance/USD", headers=auth_headers)
    assert Decimal(bal.json()["balance"]) == Decimal("0")


def test_C02_client_confirm_still_allowed_for_unregistered_provider(client, auth_headers):
    _install_registry(with_adapter=True)  # binance_pay registered, NOT "manual_test_provider"
    order = _create_order(client, auth_headers, provider="manual_test_provider")

    resp = client.post(f"/billing/orders/{order['id']}/confirm", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "confirmed"


def test_C03_empty_registry_never_blocks_confirm(client, auth_headers):
    _install_registry(with_adapter=False)  # nothing registered at all
    order = _create_order(client, auth_headers, provider="binance_pay")

    resp = client.post(f"/billing/orders/{order['id']}/confirm", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "confirmed"


def test_C04_registered_provider_order_confirms_via_webhook_path_service_layer(client, db, auth_headers, registered_user):
    """The webhook route calls PaymentOrderService.confirm_order() directly
    (not through the guarded HTTP route) — confirming this stays reachable
    for the one path that DOES carry provider evidence (a verified webhook),
    exactly the asymmetry this guard is meant to create.
    """
    from src.services.payment_order_service import PaymentOrderService

    _install_registry(with_adapter=True)
    order = _create_order(client, auth_headers, provider="binance_pay")

    svc = PaymentOrderService(db)
    confirmed = svc.confirm_order(order["id"], registered_user["user_id"])

    assert confirmed.status == "confirmed"
