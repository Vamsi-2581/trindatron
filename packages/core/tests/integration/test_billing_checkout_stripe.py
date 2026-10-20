"""Integration tests — Stripe wired end-to-end into the EXISTING
Commercial Checkout V1 billing surface (POST /billing/checkout, POST
/billing/webhook/stripe) over real HTTP.

Same division of labor as test_billing_checkout_mercadopago.py's own
docstring: this file registers the REAL StripeAdapter with only its
HTTP transport faked out, proving the actual wiring (Checkout Session
creation request shape, session-id round-trip, real Stripe-Signature
verification, status mapping) works end-to-end against the real
PaymentOrderService/ledger/entitlement stack — not just that the
generic route dispatches correctly.

Test matrix
-----------
SC01) POST /billing/checkout provider=stripe, payment_method=credit_card
      -> local PaymentOrder + real (faked-transport) Checkout Session;
      order.external_order_id round-trips as the SAME value later
      reported as the session id in the webhook.
SC02) POST /billing/checkout provider=stripe, payment_method=boleto —
      same flow, proving boleto genuinely works through this adapter.
SC03) "checkout.session.completed" (paid) webhook -> order confirmed,
      plan activated, entitlement reflects it on the very next read.
SC04) "checkout.session.completed" (unpaid — delayed method) webhook ->
      stays 'processing', never activated.
SC05) "checkout.session.async_payment_succeeded" webhook after an
      earlier unpaid completed event -> confirms and activates.
SC06) "checkout.session.async_payment_failed" webhook -> order 'failed',
      plan never activated.
SC07) Duplicate "checkout.session.completed" (paid) webhook delivered
      twice (identical body/signature) -> exactly one plan activation,
      one ledger entry pair (idempotent via the existing webhook_nonce
      mechanism, fed by this adapter's own Event id as nonce).
SC08) Invalid Stripe-Signature -> 400, order untouched.
SC09) Webhook for a session id that does not exist (foreign/unknown
      order) -> 200 (absorbed, idempotent no-op).
SC10) Webhook for an already-expired order -> 200 (absorbed), plan not
      activated, order left 'expired'.
SC11) "paid" webhook with a wrong amount_total -> 400, never activated —
      amount tampering caught by the generic route BEFORE confirm_order().
SC12) "paid" webhook with a wrong currency -> 400, never activated.
SC13) "paid" webhook whose order belongs to a DIFFERENT provider
      (cross-provider spoofing) -> 400 (provider-mismatch guard).
SC14) Repeated delivery (3x) of the same "paid" webhook never produces
      two PaymentOrders, two ledger pairs, or two plan activations.
SC15) GET /billing/providers reflects BOTH mercado_pago and stripe when
      both are registered — two independent adapters coexisting.
SC16) A dispute event ("charge.dispute.created") targeting a confirmed
      order is absorbed (200) without altering the order's status.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.api.deps import get_provider_registry
from src.billing.providers.mercadopago_adapter import MercadoPagoAdapter
from src.billing.providers.registry import ProviderRegistry
from src.billing.providers.stripe_adapter import StripeAdapter
from src.main import app as _app
from src.models.ledger import LedgerEntry
from src.models.payment_order import PaymentOrder
from src.services.request_control import InMemoryRateLimiter
from src.billing.pricing import PLAN_PRICES
# Amounts below come from the catalog rather than from literals: what these
# tests check is that an order, a webhook or an invoice carries the SAME
# price the server published, and a repeated literal quietly stops testing
# that the moment prices move. The prices themselves are argued for in
# tests/unit/test_plan_economics.py.

_WEBHOOK_SECRET = "integration-test-stripe-webhook-secret"


class _FakeStripeTransport:
    """Serves the Checkout Session creation POST. verify_webhook() never
    calls the transport (Stripe signs the full body — no follow-up GET
    needed, unlike Mercado Pago) — see stripe_adapter.py's own docstring."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def post(self, url, *, headers, body, timeout):
        self.calls.append({"method": "POST", "url": url, "headers": headers, "body": body})
        from urllib.parse import parse_qs
        parsed = {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}
        session_id = f"cs_test_{parsed.get('client_reference_id', 'unknown')}"
        return 200, json.dumps({
            "id": session_id, "object": "checkout.session",
            "url": f"https://checkout.stripe.com/c/pay/{session_id}",
        }).encode("utf-8")

    async def get(self, url, *, headers, timeout):
        raise NotImplementedError


def _install_registry(*, with_mercadopago: bool = False) -> tuple[ProviderRegistry, _FakeStripeTransport]:
    transport = _FakeStripeTransport()
    stripe_adapter = StripeAdapter(
        secret_key="sk_test_TEST", webhook_secret=_WEBHOOK_SECRET,
        base_url="https://api.stripe.com", transport=transport,
    )
    reg = ProviderRegistry()
    reg.register(stripe_adapter)
    if with_mercadopago:
        mp_adapter = MercadoPagoAdapter(
            access_token="TEST-token", webhook_secret="mp-secret",
            base_url="https://api.mercadopago.com", transport=_UnusedTransport(),
        )
        reg.register(mp_adapter)
    _app.dependency_overrides[get_provider_registry] = lambda: reg
    return reg, transport


class _UnusedTransport:
    async def post(self, *a, **k):
        raise NotImplementedError

    async def get(self, *a, **k):
        raise NotImplementedError


@pytest.fixture(autouse=True)
def _cleanup_registry():
    yield
    _app.dependency_overrides.pop(get_provider_registry, None)


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    from src.api.routes import billing as billing_routes
    billing_routes._order_rate_limiter = InMemoryRateLimiter()
    billing_routes._webhook_rate_limiter = InMemoryRateLimiter()
    yield


def _checkout_body(plan: str = "pro", **overrides) -> dict:
    body = {"plan": plan, "provider": "stripe", "idempotency_key": str(uuid.uuid4())}
    body.update(overrides)
    return body


def _sign(secret: str, *, ts: str, body: bytes) -> str:
    signed_payload = f"{ts}.".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()


def _webhook(*, event_type: str, session: dict, event_id: str = "evt_1") -> tuple[bytes, dict]:
    ts = str(int(time.time()))
    payload = {"id": event_id, "type": event_type, "data": {"object": session}}
    body = json.dumps(payload).encode("utf-8")
    v1 = _sign(_WEBHOOK_SECRET, ts=ts, body=body)
    return body, {"stripe-signature": f"t={ts},v1={v1}"}


def _session_for_order(order: dict, *, payment_status: str = "paid", payment_intent: str = "pi_test_1") -> dict:
    return {
        "id": order["external_order_id"],
        "object": "checkout.session",
        "payment_status": payment_status,
        "amount_total": int(round(float(order["amount_fiat"]) * 100)),
        "currency": order["currency_fiat"].lower(),
        "payment_intent": payment_intent,
    }


def test_SC01_checkout_creates_real_session_and_id_round_trips(client, auth_headers):
    _install_registry()
    resp = client.post(
        "/billing/checkout", json=_checkout_body(plan="pro", payment_method="credit_card"), headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["redirect_url"] is not None and body["redirect_url"].startswith("https://checkout.stripe.com/")
    order = body["order"]
    assert order["status"] == "processing"
    assert order["external_order_id"] is not None and order["external_order_id"].startswith("cs_test_")
    assert order["payment_method"] == "credit_card"
    assert Decimal(order["amount_fiat"]) == PLAN_PRICES["pro"]["USD"]


def test_SC02_boleto_checkout_works(client, auth_headers):
    _install_registry()
    resp = client.post(
        "/billing/checkout", json=_checkout_body(plan="max", payment_method="boleto"), headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    order = resp.json()["order"]
    assert order["payment_method"] == "boleto"
    assert order["status"] == "processing"


def test_SC03_completed_paid_webhook_confirms_and_activates(client, auth_headers):
    _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro"), headers=auth_headers)
    order = checkout.json()["order"]

    session = _session_for_order(order, payment_status="paid")
    body, headers = _webhook(event_type="checkout.session.completed", session=session)
    resp = client.post("/billing/webhook/stripe", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["new_status"] == "confirmed"

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "confirmed"

    entitlement = client.get("/users/me/entitlement", headers=auth_headers)
    assert entitlement.json()["plan"] == "pro"


def test_SC04_completed_unpaid_webhook_never_activates(client, auth_headers):
    _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="max"), headers=auth_headers)
    order = checkout.json()["order"]

    session = _session_for_order(order, payment_status="unpaid")
    body, headers = _webhook(event_type="checkout.session.completed", session=session)
    resp = client.post("/billing/webhook/stripe", content=body, headers=headers)
    assert resp.status_code == 200

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "processing"

    entitlement = client.get("/users/me/entitlement", headers=auth_headers)
    assert entitlement.json()["plan"] != "max"


def test_SC05_async_payment_succeeded_after_unpaid_confirms(client, auth_headers):
    _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro"), headers=auth_headers)
    order = checkout.json()["order"]

    unpaid_session = _session_for_order(order, payment_status="unpaid")
    body1, headers1 = _webhook(event_type="checkout.session.completed", session=unpaid_session, event_id="evt_1")
    client.post("/billing/webhook/stripe", content=body1, headers=headers1)

    succeeded_session = _session_for_order(order, payment_status="paid")
    body2, headers2 = _webhook(event_type="checkout.session.async_payment_succeeded", session=succeeded_session, event_id="evt_2")
    resp = client.post("/billing/webhook/stripe", content=body2, headers=headers2)
    assert resp.status_code == 200
    assert resp.json()["new_status"] == "confirmed"

    entitlement = client.get("/users/me/entitlement", headers=auth_headers)
    assert entitlement.json()["plan"] == "pro"


def test_SC06_async_payment_failed_fails_order(client, auth_headers):
    _install_registry()
    # Flat TEAM is no longer sold (TEAM is by seat), so this exercises MAX.
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="max"), headers=auth_headers)
    order = checkout.json()["order"]

    session = _session_for_order(order, payment_status="unpaid")
    body, headers = _webhook(event_type="checkout.session.async_payment_failed", session=session)
    resp = client.post("/billing/webhook/stripe", content=body, headers=headers)
    assert resp.status_code == 200

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "failed"

    entitlement = client.get("/users/me/entitlement", headers=auth_headers)
    assert entitlement.json()["plan"] != "max"


def test_SC06b_flat_team_is_not_purchasable(client, auth_headers):
    _install_registry()
    assert "team" not in [p["plan"] for p in client.get("/billing/plans").json()]
    assert client.post("/billing/checkout", json=_checkout_body(plan="team"), headers=auth_headers).status_code == 404


def test_SC07_duplicate_paid_webhook_activates_once(client, auth_headers, db):
    _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="start"), headers=auth_headers)
    order = checkout.json()["order"]

    session = _session_for_order(order, payment_status="paid")
    body, headers = _webhook(event_type="checkout.session.completed", session=session, event_id="evt_dup")
    first = client.post("/billing/webhook/stripe", content=body, headers=headers)
    second = client.post("/billing/webhook/stripe", content=body, headers=headers)
    assert first.status_code == 200 and second.status_code == 200

    db.expire_all()
    row = db.query(PaymentOrder).filter(PaymentOrder.id == order["id"]).first()
    assert row.status == "confirmed"
    ledger_rows = db.query(LedgerEntry).filter(LedgerEntry.reference_id == order["id"]).count()
    assert ledger_rows == 2


def test_SC08_invalid_signature_rejected_order_untouched(client, auth_headers):
    _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro"), headers=auth_headers)
    order = checkout.json()["order"]

    session = _session_for_order(order, payment_status="paid")
    ts = str(int(time.time()))
    body = json.dumps({"id": "evt_x", "type": "checkout.session.completed", "data": {"object": session}}).encode("utf-8")
    resp = client.post(
        "/billing/webhook/stripe", content=body,
        headers={"stripe-signature": f"t={ts},v1=deadbeef"},
    )
    assert resp.status_code == 400

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "processing"


def test_SC09_webhook_for_unknown_session_absorbed(client):
    _install_registry()
    session = {
        "id": "cs_test_never_created", "object": "checkout.session", "payment_status": "paid",
        "amount_total": int(PLAN_PRICES["start"]["USD"] * 100), "currency": "usd", "payment_intent": "pi_x",
    }
    body, headers = _webhook(event_type="checkout.session.completed", session=session)
    resp = client.post("/billing/webhook/stripe", content=body, headers=headers)
    assert resp.status_code == 200


def test_SC10_webhook_for_expired_order_absorbed(client, auth_headers, db):
    _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro"), headers=auth_headers)
    order = checkout.json()["order"]

    row = db.query(PaymentOrder).filter(PaymentOrder.id == order["id"]).first()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    session = _session_for_order(order, payment_status="paid")
    body, headers = _webhook(event_type="checkout.session.completed", session=session)
    resp = client.post("/billing/webhook/stripe", content=body, headers=headers)
    assert resp.status_code == 200

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "expired"

    entitlement = client.get("/users/me/entitlement", headers=auth_headers)
    assert entitlement.json()["plan"] != "pro"


def test_SC11_paid_webhook_wrong_amount_rejected(client, auth_headers):
    _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro"), headers=auth_headers)
    order = checkout.json()["order"]

    session = _session_for_order(order, payment_status="paid")
    session["amount_total"] = 1  # tampered
    body, headers = _webhook(event_type="checkout.session.completed", session=session)
    resp = client.post("/billing/webhook/stripe", content=body, headers=headers)
    assert resp.status_code == 400

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "processing"

    entitlement = client.get("/users/me/entitlement", headers=auth_headers)
    assert entitlement.json()["plan"] != "pro"


def test_SC12_paid_webhook_wrong_currency_rejected(client, auth_headers):
    _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro"), headers=auth_headers)
    order = checkout.json()["order"]

    session = _session_for_order(order, payment_status="paid")
    session["currency"] = "eur"  # tampered
    body, headers = _webhook(event_type="checkout.session.completed", session=session)
    resp = client.post("/billing/webhook/stripe", content=body, headers=headers)
    assert resp.status_code == 400

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "processing"


def test_SC13_webhook_for_order_owned_by_different_provider_rejected(client, auth_headers, db):
    _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro"), headers=auth_headers)
    order = checkout.json()["order"]

    row = db.query(PaymentOrder).filter(PaymentOrder.id == order["id"]).first()
    row.provider = "mercado_pago"  # simulates an order that really belongs to a different provider
    db.commit()

    session = _session_for_order(order, payment_status="paid")
    body, headers = _webhook(event_type="checkout.session.completed", session=session)
    resp = client.post("/billing/webhook/stripe", content=body, headers=headers)
    assert resp.status_code == 400

    db.expire_all()
    refreshed = db.query(PaymentOrder).filter(PaymentOrder.id == order["id"]).first()
    assert refreshed.status == "processing"


def test_SC14_repeated_delivery_never_double_activates(client, auth_headers, db):
    _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro"), headers=auth_headers)
    order = checkout.json()["order"]

    session = _session_for_order(order, payment_status="paid")
    body, headers = _webhook(event_type="checkout.session.completed", session=session, event_id="evt_repeat")

    responses = [client.post("/billing/webhook/stripe", content=body, headers=headers) for _ in range(3)]
    assert all(r.status_code == 200 for r in responses)

    db.expire_all()
    row = db.query(PaymentOrder).filter(PaymentOrder.id == order["id"]).first()
    assert row.status == "confirmed"
    ledger_rows = db.query(LedgerEntry).filter(LedgerEntry.reference_id == order["id"]).count()
    assert ledger_rows == 2

    orders_for_ref = db.query(PaymentOrder).filter(PaymentOrder.external_order_id == order["external_order_id"]).count()
    assert orders_for_ref == 1


def test_SC15_providers_endpoint_reflects_both_adapters(client):
    _install_registry(with_mercadopago=True)
    resp = client.get("/billing/providers")
    assert set(resp.json()["providers"]) == {"stripe", "mercado_pago"}


def test_SC16_dispute_event_on_confirmed_order_absorbed(client, auth_headers):
    _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro"), headers=auth_headers)
    order = checkout.json()["order"]

    paid_session = _session_for_order(order, payment_status="paid")
    body1, headers1 = _webhook(event_type="checkout.session.completed", session=paid_session, event_id="evt_paid")
    client.post("/billing/webhook/stripe", content=body1, headers=headers1)

    dispute_body, dispute_headers = _webhook(
        event_type="charge.dispute.created",
        session={"id": order["external_order_id"], "payment_intent": "pi_test_1"},
        event_id="evt_dispute",
    )
    resp = client.post("/billing/webhook/stripe", content=dispute_body, headers=dispute_headers)
    # UnknownWebhookEvent from the adapter -> the generic route absorbs it
    # idempotently (200-acked, "unknown" event_type, no order lookup at all).
    assert resp.status_code == 200

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "confirmed"  # untouched
