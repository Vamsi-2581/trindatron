"""Integration tests — Mercado Pago wired end-to-end into the EXISTING
Commercial Checkout V1 billing surface (POST /billing/checkout, POST
/billing/webhook/mercado_pago) over real HTTP.

Same division of labor as test_nowpayments_checkout.py's own docstring:
this file registers the REAL MercadoPagoAdapter with only its HTTP
transport faked out, proving the actual wiring (preference creation
request shape, external_reference round-trip, real x-signature
verification, the GET-payment-after-webhook fetch, status mapping)
works end-to-end against the real PaymentOrderService/ledger/
entitlement stack — not just that the generic route dispatches
correctly (that's test_billing_checkout.py's job, with a minimal fake
adapter).

Test matrix
-----------
MPC01) POST /billing/checkout provider=mercado_pago, payment_method=pix
       -> local PaymentOrder + real (faked-transport) Checkout Pro
       preference; order.external_order_id round-trips as the SAME
       value later echoed as external_reference on the fetched payment.
MPC02) Preference request excludes every payment type except pix's own
       ("bank_transfer") when payment_method=pix was requested.
MPC03) POST /billing/checkout provider=mercado_pago, payment_method=
       credit_card — same flow, proving cards genuinely work through
       this adapter's actual hosted-checkout mechanism (no card data
       ever sent to TRINITY Core).
MPC04) "approved" webhook -> order confirmed, plan activated, entitlement
       reflects it on the very next read.
MPC05) "pending" webhook -> stays 'processing', never activated.
MPC06) "rejected" webhook -> order 'failed', plan never activated.
MPC07) Duplicate "approved" webhook delivered twice (identical body/
       signature) -> exactly one plan activation, one ledger entry pair
       (idempotent via the existing webhook_nonce mechanism, fed by
       this adapter's own per-delivery notification id as nonce).
MPC08) Invalid x-signature -> 400, order untouched.
MPC09) Webhook for an external_reference that does not exist (foreign/
       unknown order) -> 200 (absorbed, idempotent no-op).
MPC10) Webhook for an already-expired order -> 200 (absorbed), plan not
       activated, order left 'expired'.
MPC11) "approved" webhook with a wrong transaction_amount -> 400, never
       activated — amount tampering is caught by the generic route
       BEFORE confirm_order() is ever called.
MPC12) "approved" webhook with a wrong currency_id -> 400, never activated.
MPC13) "approved" webhook whose order belongs to a DIFFERENT provider
       (cross-provider spoofing) -> 400 (provider-mismatch guard).
MPC14) Two "approved" webhook deliveries in the SAME logical operation
       (simulating a concurrent double-delivery) never produce two
       PaymentOrders, two ledger pairs, or two plan activations —
       exercised sequentially here (SQLite has one shared connection;
       see test_billing_postgres_concurrency.py for a real race), but
       proves the idempotency invariant end-to-end through this
       specific adapter.
MPC15) GET /billing/providers reflects mercado_pago once registered.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.api.deps import get_provider_registry
from src.billing.providers.mercadopago_adapter import MercadoPagoAdapter
from src.billing.providers.registry import ProviderRegistry
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

_WEBHOOK_SECRET = "integration-test-mp-webhook-secret"


class _FakeMercadoPagoTransport:
    """Serves both the preference-creation POST and the get-payment GET
    the adapter's verify_webhook() issues. Payments are registered via
    `register_payment()` before a test fires a webhook for them —
    mirrors what a real Mercado Pago payment object would contain."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._payments_by_id: dict[str, dict] = {}

    def register_payment(self, payment_id: str, payment: dict) -> None:
        self._payments_by_id[payment_id] = payment

    async def post(self, url, *, headers, body, timeout):
        self.calls.append({"method": "POST", "url": url, "headers": headers, "body": json.loads(body)})
        parsed = json.loads(body)
        ref = parsed["external_reference"]
        return 201, json.dumps({
            "id": f"pref-{ref}",
            "init_point": f"https://mercadopago.com/checkout/{ref}",
            "sandbox_init_point": f"https://mercadopago.com/checkout/{ref}?sandbox=1",
        }).encode("utf-8")

    async def get(self, url, *, headers, timeout):
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        # url shape: {base}/v1/payments/{payment_id}
        payment_id = url.rsplit("/", 1)[-1]
        payment = self._payments_by_id.get(payment_id)
        if payment is None:
            return 404, json.dumps({"message": "not found"}).encode("utf-8")
        return 200, json.dumps(payment).encode("utf-8")


def _install_registry() -> tuple[ProviderRegistry, _FakeMercadoPagoTransport]:
    transport = _FakeMercadoPagoTransport()
    adapter = MercadoPagoAdapter(
        access_token="TEST-token", webhook_secret=_WEBHOOK_SECRET,
        base_url="https://api.mercadopago.com", transport=transport,
    )
    reg = ProviderRegistry()
    reg.register(adapter)
    _app.dependency_overrides[get_provider_registry] = lambda: reg
    return reg, transport


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
    body = {"plan": plan, "provider": "mercado_pago", "idempotency_key": str(uuid.uuid4())}
    body.update(overrides)
    return body


def _sign(secret: str, *, data_id: str, request_id: str, ts: str) -> str:
    manifest = f"id:{data_id.lower()};request-id:{request_id};ts:{ts};"
    return hmac.new(secret.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256).hexdigest()


def _webhook(*, data_id: str, notification_id: int = 1) -> tuple[bytes, dict]:
    ts = "1700000000000"
    request_id = "req-integration-1"
    payload = {"id": notification_id, "type": "payment", "action": "payment.updated", "data": {"id": data_id}, "live_mode": False}
    body = json.dumps(payload).encode("utf-8")
    v1 = _sign(_WEBHOOK_SECRET, data_id=data_id, request_id=request_id, ts=ts)
    headers = {"x-signature": f"ts={ts},v1={v1}", "x-request-id": request_id}
    return body, headers


def _payment_for_order(order: dict, *, status: str, payment_id: str = "900000001") -> dict:
    return {
        "id": int(payment_id),
        "status": status,
        "status_detail": "accredited" if status == "approved" else status,
        "transaction_amount": float(order["amount_fiat"]),
        "currency_id": order["currency_fiat"],
        "external_reference": order["external_order_id"],
        "payment_method_id": "pix",
        "payment_type_id": "bank_transfer",
    }


def test_MPC01_checkout_creates_real_preference_and_reference_round_trips(client, auth_headers):
    _install_registry()
    resp = client.post(
        "/billing/checkout", json=_checkout_body(plan="pro", payment_method="pix"), headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["redirect_url"] is not None and body["redirect_url"].startswith("https://mercadopago.com/checkout/")
    order = body["order"]
    assert order["status"] == "processing"
    assert order["external_order_id"] is not None
    assert order["payment_method"] == "pix"
    # B2: Mercado Pago never supports USD (see MercadoPagoAdapter.
    # supported_currencies) — an explicit provider=mercado_pago checkout
    # with no client-supplied currency resolves to BRL, not the
    # CHECKOUT_CURRENCY default, so the real preference request never
    # sends a currency Mercado Pago would reject.
    assert order["currency_fiat"] == "BRL"
    assert Decimal(order["amount_fiat"]) == PLAN_PRICES["pro"]["BRL"]


def test_MPC02_pix_checkout_excludes_other_payment_types(client, auth_headers):
    _reg, transport = _install_registry()
    client.post("/billing/checkout", json=_checkout_body(plan="pro", payment_method="pix"), headers=auth_headers)
    post_call = next(c for c in transport.calls if c["method"] == "POST")
    excluded = {e["id"] for e in post_call["body"]["payment_methods"]["excluded_payment_types"]}
    assert "bank_transfer" not in excluded
    assert excluded == {"ticket", "credit_card", "debit_card"}


def test_MPC03_credit_card_checkout_works_through_hosted_flow(client, auth_headers):
    _install_registry()
    resp = client.post(
        "/billing/checkout", json=_checkout_body(plan="max", payment_method="credit_card"), headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    order = resp.json()["order"]
    assert order["payment_method"] == "credit_card"
    assert order["status"] == "processing"


def test_MPC04_approved_webhook_confirms_and_activates_plan(client, auth_headers):
    _reg, transport = _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro"), headers=auth_headers)
    order = checkout.json()["order"]

    transport.register_payment("900000001", _payment_for_order(order, status="approved"))
    body, headers = _webhook(data_id="900000001")
    resp = client.post("/billing/webhook/mercado_pago", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["new_status"] == "confirmed"

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "confirmed"

    entitlement = client.get("/users/me/entitlement", headers=auth_headers)
    assert entitlement.json()["plan"] == "pro"


def test_MPC05_pending_webhook_never_activates(client, auth_headers):
    _reg, transport = _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="max"), headers=auth_headers)
    order = checkout.json()["order"]

    transport.register_payment("900000002", _payment_for_order(order, status="pending", payment_id="900000002"))
    body, headers = _webhook(data_id="900000002")
    resp = client.post("/billing/webhook/mercado_pago", content=body, headers=headers)
    assert resp.status_code == 200

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "processing"

    entitlement = client.get("/users/me/entitlement", headers=auth_headers)
    assert entitlement.json()["plan"] != "max"


def test_MPC06_rejected_webhook_fails_order_never_activates(client, auth_headers):
    _reg, transport = _install_registry()
    # Flat TEAM is no longer sold (TEAM is by seat), so this exercises MAX.
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="max"), headers=auth_headers)
    order = checkout.json()["order"]

    transport.register_payment("900000003", _payment_for_order(order, status="rejected", payment_id="900000003"))
    body, headers = _webhook(data_id="900000003")
    resp = client.post("/billing/webhook/mercado_pago", content=body, headers=headers)
    assert resp.status_code == 200

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "failed"

    entitlement = client.get("/users/me/entitlement", headers=auth_headers)
    assert entitlement.json()["plan"] != "max"


def test_MPC07_duplicate_approved_webhook_activates_once(client, auth_headers, db):
    _reg, transport = _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="start"), headers=auth_headers)
    order = checkout.json()["order"]

    transport.register_payment("900000004", _payment_for_order(order, status="approved", payment_id="900000004"))
    body, headers = _webhook(data_id="900000004", notification_id=777)
    first = client.post("/billing/webhook/mercado_pago", content=body, headers=headers)
    second = client.post("/billing/webhook/mercado_pago", content=body, headers=headers)
    assert first.status_code == 200 and second.status_code == 200

    db.expire_all()
    row = db.query(PaymentOrder).filter(PaymentOrder.id == order["id"]).first()
    assert row.status == "confirmed"
    ledger_rows = db.query(LedgerEntry).filter(LedgerEntry.reference_id == order["id"]).count()
    assert ledger_rows == 2  # exactly one debit+credit pair, not two


def test_MPC08_invalid_signature_rejected_order_untouched(client, auth_headers):
    _reg, transport = _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro"), headers=auth_headers)
    order = checkout.json()["order"]

    transport.register_payment("900000005", _payment_for_order(order, status="approved", payment_id="900000005"))
    body, _headers = _webhook(data_id="900000005")
    resp = client.post(
        "/billing/webhook/mercado_pago", content=body,
        headers={"x-signature": "ts=1700000000000,v1=deadbeef", "x-request-id": "req-integration-1"},
    )
    assert resp.status_code == 400

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "processing"


def test_MPC09_webhook_for_unregistered_external_reference_absorbed(client):
    _reg, transport = _install_registry()
    transport.register_payment("900000100", {
        "id": 900000100, "status": "approved", "status_detail": "accredited",
        "transaction_amount": float(PLAN_PRICES["start"]["USD"]), "currency_id": "USD",
        "external_reference": "never-created-order-reference",
        "payment_method_id": "pix", "payment_type_id": "bank_transfer",
    })
    body, headers = _webhook(data_id="900000100")
    resp = client.post("/billing/webhook/mercado_pago", content=body, headers=headers)
    assert resp.status_code == 200  # idempotent no-op, not an error


def test_MPC10_webhook_for_expired_order_absorbed(client, auth_headers, db):
    _reg, transport = _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro"), headers=auth_headers)
    order = checkout.json()["order"]

    row = db.query(PaymentOrder).filter(PaymentOrder.id == order["id"]).first()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    transport.register_payment("900000006", _payment_for_order(order, status="approved", payment_id="900000006"))
    body, headers = _webhook(data_id="900000006")
    resp = client.post("/billing/webhook/mercado_pago", content=body, headers=headers)
    assert resp.status_code == 200

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "expired"

    entitlement = client.get("/users/me/entitlement", headers=auth_headers)
    assert entitlement.json()["plan"] != "pro"


def test_MPC11_approved_webhook_wrong_amount_rejected(client, auth_headers):
    _reg, transport = _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro"), headers=auth_headers)
    order = checkout.json()["order"]

    tampered = _payment_for_order(order, status="approved", payment_id="900000007")
    tampered["transaction_amount"] = 0.01
    transport.register_payment("900000007", tampered)
    body, headers = _webhook(data_id="900000007")
    resp = client.post("/billing/webhook/mercado_pago", content=body, headers=headers)
    assert resp.status_code == 400

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "processing"

    entitlement = client.get("/users/me/entitlement", headers=auth_headers)
    assert entitlement.json()["plan"] != "pro"


def test_MPC12_approved_webhook_wrong_currency_rejected(client, auth_headers):
    _reg, transport = _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro"), headers=auth_headers)
    order = checkout.json()["order"]

    tampered = _payment_for_order(order, status="approved", payment_id="900000008")
    tampered["currency_id"] = "ARS"
    transport.register_payment("900000008", tampered)
    body, headers = _webhook(data_id="900000008")
    resp = client.post("/billing/webhook/mercado_pago", content=body, headers=headers)
    assert resp.status_code == 400

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "processing"


def test_MPC13_webhook_for_order_owned_by_different_provider_rejected(client, auth_headers, db):
    _reg, transport = _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro"), headers=auth_headers)
    order = checkout.json()["order"]

    row = db.query(PaymentOrder).filter(PaymentOrder.id == order["id"]).first()
    row.provider = "nowpayments"  # simulates an order that really belongs to a different provider
    db.commit()

    transport.register_payment("900000009", _payment_for_order(order, status="approved", payment_id="900000009"))
    body, headers = _webhook(data_id="900000009")
    resp = client.post("/billing/webhook/mercado_pago", content=body, headers=headers)
    assert resp.status_code == 400

    db.expire_all()
    refreshed = db.query(PaymentOrder).filter(PaymentOrder.id == order["id"]).first()
    assert refreshed.status == "processing"


def test_MPC14_repeated_delivery_never_double_activates(client, auth_headers, db):
    """Sequential stand-in for a concurrent double-delivery (SQLite has
    one shared connection — see test_billing_postgres_concurrency.py for
    a real race): proves the idempotency invariant end-to-end through
    this specific adapter, not just generically."""
    _reg, transport = _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro"), headers=auth_headers)
    order = checkout.json()["order"]

    transport.register_payment("900000010", _payment_for_order(order, status="approved", payment_id="900000010"))
    body, headers = _webhook(data_id="900000010", notification_id=888)

    responses = [client.post("/billing/webhook/mercado_pago", content=body, headers=headers) for _ in range(3)]
    assert all(r.status_code == 200 for r in responses)

    db.expire_all()
    row = db.query(PaymentOrder).filter(PaymentOrder.id == order["id"]).first()
    assert row.status == "confirmed"
    ledger_rows = db.query(LedgerEntry).filter(LedgerEntry.reference_id == order["id"]).count()
    assert ledger_rows == 2

    orders_for_ref = db.query(PaymentOrder).filter(PaymentOrder.external_order_id == order["external_order_id"]).count()
    assert orders_for_ref == 1  # never a second PaymentOrder for the same logical operation


def test_MPC15_providers_endpoint_reflects_mercado_pago(client):
    _install_registry()
    resp = client.get("/billing/providers")
    assert resp.json()["providers"] == ["mercado_pago"]
