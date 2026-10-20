"""Nothing is granted without evidence of payment.

Found in the 2026-09-13 economic audit, both exploitable by any account:

1. POST /billing/checkout with a provider that has no adapter ("none")
   created a plan order at the real price, and POST /orders/{id}/confirm
   then activated the plan — MAX, with premium Opus routing, for nothing.
   The same route credited any credit order the client created.
2. POST /billing/orders took credits_amount from the client. A provider
   webhook proves amount_fiat was paid, not that the credits were bought, so
   a real $1 payment could carry a million TCU.

The suite-wide conftest keeps the legacy client confirmation on for the older
billing tests; every test here turns it off, as production has it.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.api.deps import get_provider_registry
from src.billing.providers.registry import ProviderRegistry
from src.billing.unit_economics import max_credits_for_payment
from src.config import Settings, settings
from src.main import app
from src.models.user import User


@pytest.fixture(autouse=True)
def production_defaults(monkeypatch):
    monkeypatch.setattr(settings, "billing_client_confirm_enabled", False)
    app.dependency_overrides[get_provider_registry] = lambda: ProviderRegistry()


def _order(client, headers, *, amount="10.00", fiat="USD", credits="10.00", credits_currency="USD"):
    return client.post("/billing/orders", json={
        "provider": "manual_test_provider",
        "amount_fiat": amount, "currency_fiat": fiat,
        "credits_amount": credits, "currency_credits": credits_currency,
        "idempotency_key": str(uuid.uuid4()),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }, headers=headers)


def test_client_confirmation_is_off_by_default():
    assert Settings.model_fields["billing_client_confirm_enabled"].default is False


def test_an_unpaid_plan_checkout_cannot_be_confirmed_by_its_buyer(client, db, auth_headers, registered_user):
    checkout = client.post("/billing/checkout", json={
        "plan": "max", "provider": "none", "idempotency_key": str(uuid.uuid4()),
    }, headers=auth_headers)
    assert checkout.status_code < 300, checkout.text

    resp = client.post(f"/billing/orders/{checkout.json()['order']['id']}/confirm", headers=auth_headers)

    assert resp.status_code == 409
    user = db.query(User).filter(User.id == registered_user["user_id"]).one()
    db.refresh(user)
    assert user.plan != "max"


def test_an_unpaid_credit_order_cannot_be_confirmed_by_its_buyer(client, auth_headers):
    order = _order(client, auth_headers)
    assert order.status_code == 201, order.text

    resp = client.post(f"/billing/orders/{order.json()['id']}/confirm", headers=auth_headers)

    assert resp.status_code == 409
    balance = client.get("/billing/balance/USD", headers=auth_headers).json()["balance"]
    assert Decimal(balance) == Decimal("0")


def test_an_admin_can_still_confirm_a_manual_order(client, db, auth_headers, registered_user):
    user = db.query(User).filter(User.id == registered_user["user_id"]).one()
    user.is_admin = True
    db.commit()
    order = _order(client, auth_headers)

    resp = client.post(f"/billing/orders/{order.json()['id']}/confirm", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "confirmed"


def test_credits_beyond_what_the_payment_buys_are_refused(client, auth_headers):
    assert _order(client, auth_headers, credits="1000000", credits_currency="TCU").status_code == 422
    assert _order(client, auth_headers, credits="10.01").status_code == 422


def test_credits_up_to_the_cap_are_accepted(client, auth_headers):
    cap = max_credits_for_payment(10.0, "USD", "TCU")
    within = f"{int(cap)}"
    assert _order(client, auth_headers, credits=within, credits_currency="TCU").status_code == 201


def test_a_payment_that_cannot_be_valued_buys_nothing(client, auth_headers):
    assert _order(client, auth_headers, fiat="BRL", credits="1").status_code == 422
    assert _order(client, auth_headers, credits="1", credits_currency="EUR").status_code == 422
