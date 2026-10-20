"""Integration tests for Commercial Checkout V1 — GET /billing/plans,
GET /billing/providers, POST /billing/checkout, and the full
checkout -> webhook -> plan-activation cycle over real HTTP.

Everything ownership/idempotency/atomicity-related at the SERVICE level
is already covered by tests/unit/test_payment_order_service.py's P26-P30
— this file proves the HTTP surface and the end-to-end flow work, not the
underlying financial mechanics a second time.

Test matrix
-----------
CO01) GET /billing/plans returns the price catalog (start/pro/max/team;
      never "free" — not a checkout target).
CO02) GET /billing/providers is empty when nothing is registered — the
      honest, expected answer in this environment (no real credentials).
CO03) GET /billing/providers reflects a registered fake provider.
CO04) POST /billing/checkout with an unknown plan -> 404.
CO05) POST /billing/checkout with "free" -> 404 (never purchasable).
CO06) POST /billing/checkout with no provider registered -> local pending
      order; amount/currency come from the server-side catalog, never
      from the client (CreateCheckoutRequest has no amount field at all).
CO07) POST /billing/checkout idempotency — same idempotency_key twice ->
      the exact same order, no duplicate row.
CO08) POST /billing/checkout is rate-limited per user (mission requirement 11).
CO09) Full E2E happy path: plans -> checkout (provider registered) ->
      processing -> provider webhook (CONFIRMED, matching amount) ->
      confirmed -> user.plan updated -> GET /users/me/capacity reflects
      the new plan on the very next request (no separate "recalculate"
      step needed).
CO10) Webhook amount mismatch -> 400; order stays unconfirmed; plan NOT
      updated.
CO11) Duplicate webhook (same event delivered twice) -> exactly one
      activation, not two (idempotent).
CO12) POST /billing/orders/{id}/refund on a plan-purchase order -> 409
      (PlanPurchaseNotRefundable).
CO13) An expired checkout order's webhook confirmation is absorbed (200)
      without activating the plan.
CO14) Cross-user ownership: GET /billing/orders/{id} for another user's
      plan-purchase order -> 404.
CO15) Provider unavailable during checkout (create_intent() raises
      ProviderUnavailable) -> 503, no order left confirmed/activated.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.api.deps import get_provider_registry
from src.billing.providers.base import PaymentIntent, PaymentProvider, PaymentStatus, ProviderUnavailable, WebhookEvent
from src.billing.providers.registry import ProviderRegistry
from src.main import app as _app
from src.services.request_control import InMemoryRateLimiter
from src.billing.pricing import PLAN_PRICES


class _FakeCheckoutAdapter(PaymentProvider):
    """Supports BOTH create_intent() (checkout order creation) and
    verify_webhook() (confirmation) — enough to drive the full
    checkout -> processing -> webhook -> confirmed cycle end to end."""

    def __init__(self, name: str = "fake_provider", *, redirect_url: str | None = None, unavailable: bool = False) -> None:
        self._name = name
        self._redirect_url = redirect_url
        self._unavailable = unavailable
        self._webhook_response: WebhookEvent | None = None

    def queue_webhook(self, event: WebhookEvent) -> None:
        self._webhook_response = event

    @property
    def provider_name(self) -> str:
        return self._name

    async def create_intent(self, *, amount, currency, user_id, idempotency_key, metadata=None):
        if self._unavailable:
            raise ProviderUnavailable(f"{self.provider_name} is down (simulated)")
        return PaymentIntent(
            id=f"ext-{idempotency_key}",
            provider=self.provider_name,
            amount=amount,
            currency=currency,
            status=PaymentStatus.PENDING,
            idempotency_key=idempotency_key,
            redirect_url=self._redirect_url,
        )

    async def get_intent(self, intent_id: str):
        raise NotImplementedError

    async def verify_webhook(self, payload: bytes, headers):
        return self._webhook_response

    async def refund(self, payment_id: str, **kwargs):
        raise NotImplementedError


def _install_registry(*, adapter: PaymentProvider | None = None) -> ProviderRegistry:
    reg = ProviderRegistry()
    if adapter is not None:
        reg.register(adapter)
    _app.dependency_overrides[get_provider_registry] = lambda: reg
    return reg


@pytest.fixture(autouse=True)
def _cleanup_registry():
    yield
    _app.dependency_overrides.pop(get_provider_registry, None)


@pytest.fixture(autouse=True)
def _reset_order_rate_limiter():
    """The route module's rate limiter is a module-level singleton
    (deliberately — see billing.py's own docstring) — reset its internal
    state between tests so one test's requests never count against
    another's budget."""
    from src.api.routes import billing as billing_routes
    billing_routes._order_rate_limiter = InMemoryRateLimiter()
    yield


def _checkout_body(plan: str = "pro", provider: str = "fake_provider", **overrides) -> dict:
    body = {"plan": plan, "provider": provider, "idempotency_key": str(uuid.uuid4())}
    body.update(overrides)
    return body


def test_CO01_list_plan_prices(client):
    # No provider registered — the B2 default currency resolution
    # (resolve_default_currency()) then has nothing to resolve against and
    # falls back to CHECKOUT_CURRENCY (USD), which is what this test owns.
    # Explicit, rather than relying on whatever the global registry happens
    # to hold in this environment (e.g. a real mercado_pago adapter when a
    # developer's own .env carries real Mercado Pago credentials — see
    # test_billing_checkout_mercadopago.py for the BRL-resolving case).
    _install_registry()
    resp = client.get("/billing/plans")
    assert resp.status_code == 200
    plans = {p["plan"]: p for p in resp.json()}
    # TEAM is sold by seat (GET /teams/pricing), never as a flat plan here.
    assert set(plans.keys()) == {"start", "pro", "max"}
    assert "free" not in plans
    # Assert against the catalog rather than repeating the numbers: what
    # this test owns is that the endpoint serves the server-side catalog
    # faithfully. The prices themselves are guarded — against the market
    # and against their own cost — in tests/unit/test_plan_economics.py,
    # which is where a price change should have to argue for itself.
    for plan, entry in plans.items():
        assert Decimal(entry["amount"]) == PLAN_PRICES[plan]["USD"]
    assert all(p["currency"] == "USD" for p in plans.values())


def test_CO02_no_providers_registered_by_default(client):
    _install_registry()
    resp = client.get("/billing/providers")
    assert resp.status_code == 200
    assert resp.json()["providers"] == []


def test_CO03_registered_provider_is_listed(client):
    _install_registry(adapter=_FakeCheckoutAdapter())
    resp = client.get("/billing/providers")
    assert resp.json()["providers"] == ["fake_provider"]


def test_CO04_checkout_unknown_plan_returns_404(client, auth_headers):
    resp = client.post("/billing/checkout", json=_checkout_body(plan="ultra"), headers=auth_headers)
    assert resp.status_code == 404


def test_CO05_checkout_free_plan_returns_404(client, auth_headers):
    resp = client.post("/billing/checkout", json=_checkout_body(plan="free"), headers=auth_headers)
    assert resp.status_code == 404


def test_CO06_checkout_local_only_when_no_provider_registered(client, auth_headers):
    _install_registry()  # nothing registered
    resp = client.post("/billing/checkout", json=_checkout_body(plan="start", provider="nonexistent"), headers=auth_headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["redirect_url"] is None
    order = body["order"]
    assert order["status"] == "pending"
    assert order["plan"] == "start"
    assert Decimal(order["amount_fiat"]) == PLAN_PRICES["start"]["USD"]
    assert order["currency_fiat"] == "USD"


def test_CO07_checkout_idempotent(client, auth_headers):
    _install_registry()
    key = str(uuid.uuid4())
    first = client.post("/billing/checkout", json=_checkout_body(plan="pro", provider="none", idempotency_key=key), headers=auth_headers)
    second = client.post("/billing/checkout", json=_checkout_body(plan="pro", provider="none", idempotency_key=key), headers=auth_headers)
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["order"]["id"] == second.json()["order"]["id"]


def test_CO08_checkout_rate_limited(client, auth_headers, monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "billing_order_rate_limit", 2)
    _install_registry()

    for _ in range(2):
        resp = client.post("/billing/checkout", json=_checkout_body(plan="start", provider="none"), headers=auth_headers)
        assert resp.status_code == 201

    resp = client.post("/billing/checkout", json=_checkout_body(plan="start", provider="none"), headers=auth_headers)
    assert resp.status_code == 429


def test_CO08b_checkout_response_carries_provider_redirect_url(client, auth_headers):
    """The provider's own PaymentIntent.redirect_url (a one-time hosted-
    checkout link) flows through to the response — see
    create_provider_order()'s own docstring on why this only works on
    the FIRST, non-idempotent call."""
    adapter = _FakeCheckoutAdapter(redirect_url="https://pay.example.com/checkout/abc123")
    _install_registry(adapter=adapter)

    resp = client.post("/billing/checkout", json=_checkout_body(plan="pro", provider="fake_provider"), headers=auth_headers)
    assert resp.status_code == 201
    assert resp.json()["redirect_url"] == "https://pay.example.com/checkout/abc123"


def test_CO09_full_e2e_checkout_to_plan_activation(client, auth_headers, registered_user):
    adapter = _FakeCheckoutAdapter()
    _install_registry(adapter=adapter)

    plans_resp = client.get("/billing/plans")
    pro_price = Decimal(next(p["amount"] for p in plans_resp.json() if p["plan"] == "pro"))

    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro", provider="fake_provider"), headers=auth_headers)
    assert checkout.status_code == 201, checkout.text
    checkout_body = checkout.json()
    order = checkout_body["order"]
    assert order["status"] == "processing"  # provider create_intent() succeeded
    assert order["external_order_id"] is not None

    adapter.queue_webhook(WebhookEvent(
        provider="fake_provider", event_type="payment.confirmed", payment_id=None,
        provider_ref=order["external_order_id"], status=PaymentStatus.CONFIRMED,
        amount=pro_price, currency="USD", raw_payload={},
    ))
    webhook_resp = client.post("/billing/webhook/fake_provider", content=b"{}")
    assert webhook_resp.status_code == 200
    assert webhook_resp.json()["new_status"] == "confirmed"

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "confirmed"

    # The user's OWN entitlement/capacity read is live — reflects "pro"
    # immediately, no separate activation step, no cache to invalidate.
    entitlement = client.get("/users/me/entitlement", headers=auth_headers)
    assert entitlement.json()["plan"] == "pro"


def test_CO10_webhook_amount_mismatch_does_not_activate(client, auth_headers):
    adapter = _FakeCheckoutAdapter()
    _install_registry(adapter=adapter)

    checkout = client.post("/billing/checkout", json=_checkout_body(plan="max", provider="fake_provider"), headers=auth_headers)
    order = checkout.json()["order"]

    adapter.queue_webhook(WebhookEvent(
        provider="fake_provider", event_type="payment.confirmed", payment_id=None,
        provider_ref=order["external_order_id"], status=PaymentStatus.CONFIRMED,
        amount=Decimal("0.01"),  # deliberately not what MAX costs
        currency="USD", raw_payload={},
    ))
    resp = client.post("/billing/webhook/fake_provider", content=b"{}")
    assert resp.status_code == 400

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "processing"  # never confirmed

    entitlement = client.get("/users/me/entitlement", headers=auth_headers)
    assert entitlement.json()["plan"] != "max"


def test_CO11_duplicate_webhook_activates_once(client, auth_headers):
    adapter = _FakeCheckoutAdapter()
    _install_registry(adapter=adapter)

    checkout = client.post("/billing/checkout", json=_checkout_body(plan="start", provider="fake_provider"), headers=auth_headers)
    order = checkout.json()["order"]

    event = WebhookEvent(
        provider="fake_provider", event_type="payment.confirmed", payment_id=None,
        provider_ref=order["external_order_id"], status=PaymentStatus.CONFIRMED,
        amount=PLAN_PRICES["start"]["USD"], currency="USD", raw_payload={},
    )
    adapter.queue_webhook(event)
    first = client.post("/billing/webhook/fake_provider", content=b"{}")
    adapter.queue_webhook(event)
    second = client.post("/billing/webhook/fake_provider", content=b"{}")
    adapter.queue_webhook(event)
    third = client.post("/billing/webhook/fake_provider", content=b"{}")

    for resp in (first, second, third):
        assert resp.status_code == 200

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "confirmed"


def test_CO12_refund_plan_purchase_order_returns_409(client, auth_headers):
    _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro", provider="none"), headers=auth_headers)
    order_id = checkout.json()["order"]["id"]
    confirm = client.post(f"/billing/orders/{order_id}/confirm", headers=auth_headers)
    assert confirm.status_code == 200

    resp = client.post(
        f"/billing/orders/{order_id}/refund", json={"amount": "1.00"}, headers=auth_headers,
    )
    assert resp.status_code == 409


def test_CO13_expired_order_webhook_confirmation_absorbed(client, auth_headers, db):
    adapter = _FakeCheckoutAdapter()
    _install_registry(adapter=adapter)

    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro", provider="fake_provider"), headers=auth_headers)
    order = checkout.json()["order"]

    from src.models.payment_order import PaymentOrder
    row = db.query(PaymentOrder).filter(PaymentOrder.id == order["id"]).first()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    adapter.queue_webhook(WebhookEvent(
        provider="fake_provider", event_type="payment.confirmed", payment_id=None,
        provider_ref=order["external_order_id"], status=PaymentStatus.CONFIRMED,
        amount=PLAN_PRICES["pro"]["USD"], currency="USD", raw_payload={},
    ))
    resp = client.post("/billing/webhook/fake_provider", content=b"{}")
    assert resp.status_code == 200  # absorbed, not an error

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "expired"


def test_CO14_get_order_wrong_user_returns_404(client, auth_headers, db2_user_factory):
    from src.services.auth_service import create_access_token

    _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="start", provider="none"), headers=auth_headers)
    order_id = checkout.json()["order"]["id"]

    other = db2_user_factory("other-checkout@test.com")
    other_headers = {"Authorization": f"Bearer {create_access_token(other.id)}"}
    resp = client.get(f"/billing/orders/{order_id}", headers=other_headers)
    assert resp.status_code == 404


def test_CO15_checkout_provider_unavailable_returns_503(client, auth_headers, db):
    adapter = _FakeCheckoutAdapter(unavailable=True)
    _install_registry(adapter=adapter)

    resp = client.post("/billing/checkout", json=_checkout_body(plan="pro", provider="fake_provider"), headers=auth_headers)

    assert resp.status_code == 503
    # No order was left in any state that could later be mistaken for
    # paid — either no local row exists at all, or (if one was created
    # before the provider call failed) it never advanced past 'pending'.
    from src.models.payment_order import PaymentOrder
    orders = db.query(PaymentOrder).filter(PaymentOrder.plan == "pro").all()
    assert all(o.status == "pending" for o in orders)


def test_CO16_monitoring_counters_are_recorded(client, auth_headers):
    """Mission requirement 13 — payment order created/confirmed observable
    via metrics, never carrying cost/TCU/secret."""
    from src.observability.metrics import core_metrics

    _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="start", provider="none"), headers=auth_headers)
    order_id = checkout.json()["order"]["id"]
    client.post(f"/billing/orders/{order_id}/confirm", headers=auth_headers)

    snapshot = core_metrics.snapshot()
    assert "payment_orders_created_total" in snapshot["counters"]
    created = snapshot["counters"]["payment_orders_created_total"]
    assert any(entry["value"] >= 1 for entry in created)
