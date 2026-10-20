"""Forged value is refused on every path, with production billing settings.

Each test is an attack an ordinary logged-in account can attempt from its
own client: pay nothing or $1, and walk away with credits or a paid plan.
Every one must be refused, and the positive control at the end proves the
refusals are specific — a real payment at the real price still activates.

Production settings: client confirmation of an order with no registered
adapter is OFF (billing_client_confirm_enabled), and the only confirmation
that counts is a verified provider webhook whose amount matches the order.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.api.deps import get_provider_registry
from src.billing.pricing import PLAN_PRICES
from src.billing.providers.base import PaymentIntent, PaymentProvider, PaymentStatus, WebhookEvent
from src.billing.providers.registry import ProviderRegistry
from src.config import Settings, settings
from src.main import app
from src.models.user import User
from src.services.request_control import InMemoryRateLimiter


class _VerifiedProvider(PaymentProvider):
    """Stands in for a real adapter: creates provider-side orders and returns
    whatever webhook event the test queues, as if its signature verified."""

    def __init__(self) -> None:
        self._event: WebhookEvent | None = None

    def queue(self, event: WebhookEvent) -> None:
        self._event = event

    @property
    def provider_name(self) -> str:
        return "verified_provider"

    async def create_intent(self, *, amount, currency, user_id, idempotency_key, metadata=None):
        return PaymentIntent(
            id=f"ext-{idempotency_key}", provider=self.provider_name, amount=amount,
            currency=currency, status=PaymentStatus.PENDING, idempotency_key=idempotency_key,
        )

    async def get_intent(self, intent_id: str):
        raise NotImplementedError

    async def verify_webhook(self, payload: bytes, headers):
        return self._event

    async def refund(self, payment_id: str, **kwargs):
        raise NotImplementedError


@pytest.fixture(autouse=True)
def production_billing(monkeypatch):
    from src.api.routes import billing as billing_routes

    monkeypatch.setattr(settings, "billing_client_confirm_enabled", False)
    monkeypatch.setattr(billing_routes, "_order_rate_limiter", InMemoryRateLimiter())
    if hasattr(billing_routes, "_webhook_rate_limiter"):
        monkeypatch.setattr(billing_routes, "_webhook_rate_limiter", InMemoryRateLimiter())
    yield
    app.dependency_overrides.pop(get_provider_registry, None)


def _registry(adapter: PaymentProvider | None = None) -> None:
    registry = ProviderRegistry()
    if adapter is not None:
        registry.register(adapter)
    app.dependency_overrides[get_provider_registry] = lambda: registry


def _order(client, headers, *, amount: str, credits: str, credits_currency: str = "USD",
           provider: str = "manual_provider", **extra):
    body = {
        "provider": provider, "amount_fiat": amount, "currency_fiat": "USD",
        "credits_amount": credits, "currency_credits": credits_currency,
        "idempotency_key": str(uuid.uuid4()),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    body.update(extra)
    return client.post("/billing/orders", json=body, headers=headers)


def _checkout(client, headers, *, plan: str, provider: str):
    return client.post("/billing/checkout", json={
        "plan": plan, "provider": provider, "idempotency_key": str(uuid.uuid4()),
    }, headers=headers)


def _plan(db, user_id: str) -> str:
    user = db.query(User).filter(User.id == user_id).one()
    db.refresh(user)
    return user.plan


def _balance(client, headers, currency: str = "USD") -> Decimal:
    return Decimal(client.get(f"/billing/balance/{currency}", headers=headers).json()["balance"])


def test_production_default_keeps_client_confirmation_off():
    assert Settings.model_fields["billing_client_confirm_enabled"].default is False


def test_one_dollar_for_one_credit_is_not_granted_without_payment(client, auth_headers):
    _registry()
    order = _order(client, auth_headers, amount="1.00", credits="1.00")
    assert order.status_code == 201, order.text

    confirm = client.post(f"/billing/orders/{order.json()['id']}/confirm", headers=auth_headers)

    assert confirm.status_code == 409
    assert _balance(client, auth_headers) == Decimal("0")


@pytest.mark.parametrize("currency", ["USD", "TCU"])
def test_one_dollar_for_a_thousand_credits_is_refused_at_creation(client, auth_headers, currency):
    _registry()
    resp = _order(client, auth_headers, amount="1.00", credits="1000", credits_currency=currency)
    assert resp.status_code == 422
    assert _balance(client, auth_headers, currency) == Decimal("0")


def test_one_dollar_does_not_buy_max_by_confirming_it_yourself(client, db, auth_headers, registered_user):
    _registry()
    checkout = _checkout(client, auth_headers, plan="max", provider="none")
    assert checkout.status_code < 300, checkout.text
    order = checkout.json()["order"]
    assert Decimal(order["amount_fiat"]) == PLAN_PRICES["max"]["USD"], "the price is the server's, never the client's"

    confirm = client.post(f"/billing/orders/{order['id']}/confirm", headers=auth_headers)

    assert confirm.status_code == 409
    assert _plan(db, registered_user["user_id"]) != "max"


def test_one_dollar_does_not_buy_max_through_a_provider_webhook(client, db, auth_headers, registered_user):
    provider = _VerifiedProvider()
    _registry(provider)
    order = _checkout(client, auth_headers, plan="max", provider="verified_provider").json()["order"]
    provider.queue(WebhookEvent(
        provider="verified_provider", event_type="payment.confirmed", payment_id=None,
        provider_ref=order["external_order_id"], status=PaymentStatus.CONFIRMED,
        amount=Decimal("1.00"), currency="USD", raw_payload={},
    ))

    resp = client.post("/billing/webhook/verified_provider", content=b"{}")

    assert resp.status_code == 400
    assert client.get(f"/billing/orders/{order['id']}", headers=auth_headers).json()["status"] != "confirmed"
    assert _plan(db, registered_user["user_id"]) != "max"


@pytest.mark.parametrize("provider", ["does-not-exist", "none"])
def test_an_unregistered_provider_confirms_nothing(client, db, auth_headers, registered_user, provider):
    _registry(_VerifiedProvider())
    plan_order = _checkout(client, auth_headers, plan="max", provider=provider).json()["order"]
    credit_order = _order(client, auth_headers, amount="1.00", credits="1.00", provider=provider).json()

    for order_id in (plan_order["id"], credit_order["id"]):
        assert client.post(f"/billing/orders/{order_id}/confirm", headers=auth_headers).status_code == 409
    assert client.post(f"/billing/webhook/{provider}", content=b"{}").status_code == 400
    assert _plan(db, registered_user["user_id"]) != "max"
    assert _balance(client, auth_headers) == Decimal("0")


def test_self_confirmation_is_refused_for_a_registered_provider_too(client, db, auth_headers, registered_user):
    _registry(_VerifiedProvider())
    order = _checkout(client, auth_headers, plan="max", provider="verified_provider").json()["order"]

    assert client.post(f"/billing/orders/{order['id']}/confirm", headers=auth_headers).status_code == 409
    assert _plan(db, registered_user["user_id"]) != "max"


def test_the_client_cannot_smuggle_a_plan_or_a_status_into_an_order(client, auth_headers):
    _registry()
    resp = _order(client, auth_headers, amount="1.00", credits="1.00",
                  plan="max", status="confirmed", confirmed_at=datetime.now(timezone.utc).isoformat())
    assert resp.status_code in (201, 422)
    if resp.status_code == 201:
        assert resp.json()["status"] == "pending"
        assert resp.json().get("plan") is None
        assert client.post(f"/billing/orders/{resp.json()['id']}/confirm", headers=auth_headers).status_code == 409


def test_positive_control_the_real_price_through_the_provider_activates(client, db, auth_headers, registered_user):
    provider = _VerifiedProvider()
    _registry(provider)
    order = _checkout(client, auth_headers, plan="max", provider="verified_provider").json()["order"]
    provider.queue(WebhookEvent(
        provider="verified_provider", event_type="payment.confirmed", payment_id=None,
        provider_ref=order["external_order_id"], status=PaymentStatus.CONFIRMED,
        amount=PLAN_PRICES["max"]["USD"], currency="USD", raw_payload={},
    ))

    assert client.post("/billing/webhook/verified_provider", content=b"{}").status_code == 200
    assert _plan(db, registered_user["user_id"]) == "max"
