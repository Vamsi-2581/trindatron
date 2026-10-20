"""Integration tests — NOWPayments wired end-to-end into the EXISTING
Commercial Checkout V1 billing surface (POST /billing/checkout, POST
/billing/webhook/nowpayments) over real HTTP.

Unlike test_billing_checkout.py and test_webhook_replay_protection.py
(which use a minimal `_FakeCheckoutAdapter`/`_FakeReplayAdapter` test
double to prove the GENERIC route logic), this file registers the REAL
`NOWPaymentsAdapter` (src/billing/providers/nowpayments_adapter.py) with
only its HTTP transport faked out — proving the actual NOWPayments
wiring (invoice creation request shape, order_id round-trip, real
HMAC-SHA512 IPN signature verification, real status mapping) works
end-to-end against the real PaymentOrderService/BillingOrchestrator/
ledger/entitlement stack, not just that the generic route dispatches
correctly.

DB-level idempotency/replay mechanics themselves (nonce table,
crash-safety, rate limiting) are already proven generically in
test_webhook_replay_protection.py and are not re-derived here — these
tests confirm NOWPayments' own adapter correctly FEEDS that mechanism
(real signature-as-nonce, correct status branch), matching the same
division of labor test_billing_checkout.py's own docstring describes
for Binance Pay.

Test matrix
-----------
NC01) POST /billing/checkout with provider=nowpayments creates a local
      PaymentOrder AND a real (faked-transport) NOWPayments invoice;
      order.external_order_id round-trips as the SAME value later
      echoed in the IPN's own order_id field.
NC02) "waiting" IPN -> order stays 'processing', plan NOT activated
      ("não tratar 'waiting' como pagamento concluído").
NC03) "confirming" IPN -> still 'processing', not activated.
NC04) "finished" IPN -> order 'confirmed', user.plan updated, entitlement
      reflects it on the very next read.
NC05) Duplicate "finished" IPN delivered twice (identical body/signature)
      -> exactly one plan activation, one ledger entry pair (idempotent
      via the existing webhook_nonce mechanism, fed by this adapter's
      signature-as-nonce).
NC06) "failed" IPN -> order 'failed', plan never activated.
NC07) Invalid IPN signature -> 400, order untouched.
NC08) IPN for an order_id that does not exist (foreign/unknown order) ->
      200 (absorbed, idempotent no-op), nothing activated.
NC09) IPN for an already-expired order -> 200 (absorbed), plan not
      activated, order left 'expired'.
NC10) "partially_paid" IPN -> stays 'processing', never confirmed.
NC10b) "finished" IPN with a wrong price_amount -> 400, never activated.
NC10c) "finished" IPN with a wrong price_currency -> 400, never activated.
NC10d) "finished" IPN whose order_id belongs to a DIFFERENT provider
      (cross-provider spoofing) -> 400 (provider-mismatch guard), never
      activated — "nunca ativar um plano apenas porque alguém conhece o
      order_id".

Genuine multi-connection concurrency (concurrent duplicate webhook,
confirm-vs-expire race) is NOT exercised here — SQLite's single shared
connection can't reproduce a real race. See
tests/integration/test_billing_postgres_concurrency.py (gated by a real
PostgreSQL instance) for that.
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
from src.billing.providers.nowpayments_adapter import NOWPaymentsAdapter
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

_IPN_SECRET = "integration-test-ipn-secret"


class _FakeNOWPaymentsTransport:
    """Only the invoice-creation call is exercised by these tests (the
    webhook path never calls the transport) — one canned success
    response per checkout call is enough."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def post(self, url, *, headers, body, timeout):
        self.calls.append({"url": url, "headers": headers, "body": json.loads(body)})
        request_order_id = json.loads(body)["order_id"]
        return 200, json.dumps({
            "id": 555111,
            "order_id": request_order_id,
            "order_description": "TRINDATRON plan",
            "price_amount": json.loads(body)["price_amount"],
            "price_currency": json.loads(body)["price_currency"],
            "pay_currency": None,
            "invoice_url": f"https://nowpayments.io/payment/?iid={request_order_id}",
            "success_url": None, "cancel_url": None,
            "created_at": "2026-01-01T00:00:00.000Z", "updated_at": "2026-01-01T00:00:00.000Z",
        }).encode("utf-8")

    async def get(self, url, *, headers, timeout):
        raise NotImplementedError


def _install_registry() -> ProviderRegistry:
    reg = ProviderRegistry()
    adapter = NOWPaymentsAdapter(
        api_key="test-api-key", ipn_secret=_IPN_SECRET,
        base_url="https://api.nowpayments.io", transport=_FakeNOWPaymentsTransport(),
    )
    reg.register(adapter)
    _app.dependency_overrides[get_provider_registry] = lambda: reg
    return reg


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
    body = {"plan": plan, "provider": "nowpayments", "idempotency_key": str(uuid.uuid4())}
    body.update(overrides)
    return body


def _sort_recursive(value):
    if isinstance(value, dict):
        return {k: _sort_recursive(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        return [_sort_recursive(v) for v in value]
    return value


def _sign(payload: dict) -> str:
    canonical = json.dumps(_sort_recursive(payload), separators=(",", ":"), ensure_ascii=False)
    return hmac.new(_IPN_SECRET.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha512).hexdigest()


def _ipn(*, order_id: str, payment_status: str, price_amount: float, price_currency: str = "usd") -> tuple[bytes, dict]:
    payload = {
        "payment_id": 900000001,
        "payment_status": payment_status,
        "pay_address": "bc1qexampleaddress",
        "price_amount": price_amount,
        "price_currency": price_currency,
        "pay_amount": 0.0007,
        "actually_paid": 0.0007,
        "pay_currency": "btc",
        "order_id": order_id,
        "order_description": "TRINDATRON plan",
        "purchase_id": "1",
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return body, {"x-nowpayments-sig": _sign(payload)}


def test_NC01_checkout_creates_real_nowpayments_invoice(client, auth_headers):
    _install_registry()
    resp = client.post("/billing/checkout", json=_checkout_body(plan="pro"), headers=auth_headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["redirect_url"] is not None and body["redirect_url"].startswith("https://nowpayments.io/payment/")
    order = body["order"]
    assert order["status"] == "processing"
    assert order["external_order_id"] is not None
    assert Decimal(order["amount_fiat"]) == PLAN_PRICES["pro"]["USD"]


@pytest.mark.parametrize("payment_status", ["waiting", "confirming", "partially_paid"])
def test_NC02_NC03_NC10_in_flight_statuses_never_activate(client, auth_headers, payment_status):
    _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="max"), headers=auth_headers)
    order = checkout.json()["order"]

    body, headers = _ipn(order_id=order["external_order_id"], payment_status=payment_status, price_amount=float(PLAN_PRICES["max"]["USD"]))
    resp = client.post("/billing/webhook/nowpayments", content=body, headers=headers)
    assert resp.status_code == 200

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "processing"  # never 'confirmed'

    entitlement = client.get("/users/me/entitlement", headers=auth_headers)
    assert entitlement.json()["plan"] != "max"


def test_NC04_finished_status_confirms_and_activates_plan(client, auth_headers):
    _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro"), headers=auth_headers)
    order = checkout.json()["order"]

    body, headers = _ipn(order_id=order["external_order_id"], payment_status="finished", price_amount=float(PLAN_PRICES["pro"]["USD"]))
    resp = client.post("/billing/webhook/nowpayments", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["new_status"] == "confirmed"

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "confirmed"

    entitlement = client.get("/users/me/entitlement", headers=auth_headers)
    assert entitlement.json()["plan"] == "pro"


def test_NC05_duplicate_finished_ipn_activates_once(client, auth_headers, db):
    _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="start"), headers=auth_headers)
    order = checkout.json()["order"]

    body, headers = _ipn(order_id=order["external_order_id"], payment_status="finished", price_amount=float(PLAN_PRICES["start"]["USD"]))
    first = client.post("/billing/webhook/nowpayments", content=body, headers=headers)
    second = client.post("/billing/webhook/nowpayments", content=body, headers=headers)
    assert first.status_code == 200 and second.status_code == 200

    db.expire_all()
    row = db.query(PaymentOrder).filter(PaymentOrder.id == order["id"]).first()
    assert row.status == "confirmed"
    # Plan-purchase orders create exactly one ledger entry pair (debit +
    # credit) — a second, wrongly-reprocessed delivery would double this.
    ledger_rows = db.query(LedgerEntry).filter(LedgerEntry.reference_id == order["id"]).count()
    assert ledger_rows == 2


def test_NC06_failed_status_fails_order_never_activates(client, auth_headers):
    _install_registry()
    # TEAM is sold per seat through /teams, so the flat checkout uses the top individual plan.
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="max"), headers=auth_headers)
    order = checkout.json()["order"]

    body, headers = _ipn(order_id=order["external_order_id"], payment_status="failed", price_amount=float(PLAN_PRICES["max"]["USD"]))
    resp = client.post("/billing/webhook/nowpayments", content=body, headers=headers)
    assert resp.status_code == 200

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "failed"

    entitlement = client.get("/users/me/entitlement", headers=auth_headers)
    assert entitlement.json()["plan"] != "max"


def test_NC07_invalid_signature_rejected_order_untouched(client, auth_headers):
    _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro"), headers=auth_headers)
    order = checkout.json()["order"]

    body, _headers = _ipn(order_id=order["external_order_id"], payment_status="finished", price_amount=float(PLAN_PRICES["pro"]["USD"]))
    resp = client.post("/billing/webhook/nowpayments", content=body, headers={"x-nowpayments-sig": "not-a-real-signature"})
    assert resp.status_code == 400

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "processing"


def test_NC08_ipn_for_unknown_order_absorbed(client):
    _install_registry()
    body, headers = _ipn(order_id="never-created-order-id", payment_status="finished", price_amount=1.0)
    resp = client.post("/billing/webhook/nowpayments", content=body, headers=headers)
    assert resp.status_code == 200  # idempotent no-op, not an error


def test_NC10b_finished_ipn_wrong_amount_rejected(client, auth_headers):
    """A 'finished' IPN whose price_amount does not match what this order
    was actually created for must be rejected (400) before any plan
    activation — mission requirement: never trust a client/provider-
    supplied amount blindly (generic route step 6, exercised here through
    the REAL adapter, not a fake)."""
    _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro"), headers=auth_headers)
    order = checkout.json()["order"]

    body, headers = _ipn(order_id=order["external_order_id"], payment_status="finished", price_amount=0.01)
    resp = client.post("/billing/webhook/nowpayments", content=body, headers=headers)
    assert resp.status_code == 400

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "processing"

    entitlement = client.get("/users/me/entitlement", headers=auth_headers)
    assert entitlement.json()["plan"] != "pro"


def test_NC10c_finished_ipn_wrong_currency_rejected(client, auth_headers):
    """A 'finished' IPN reporting a different fiat currency than the order
    was created for must be rejected (400) — the order was priced in USD;
    an IPN claiming EUR (even with the right numeric amount) is a mismatch."""
    _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro"), headers=auth_headers)
    order = checkout.json()["order"]

    body, headers = _ipn(
        order_id=order["external_order_id"], payment_status="finished",
        price_amount=float(PLAN_PRICES["pro"]["USD"]), price_currency="eur",
    )
    resp = client.post("/billing/webhook/nowpayments", content=body, headers=headers)
    assert resp.status_code == 400

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "processing"


def test_NC10d_finished_ipn_for_order_owned_by_different_provider_rejected(client, auth_headers, db):
    """Cross-provider spoofing: an order created under a DIFFERENT
    provider (e.g. binance_pay) must never be confirmable by a NOWPayments
    IPN carrying the same external_order_id — mission requirement: 'nunca
    ativar um plano apenas porque alguém conhece o order_id'. Exercised
    against the real generic route's provider-mismatch guard (step 5),
    with a genuine NOWPayments-signed IPN, not a fake adapter."""
    from src.models.payment_order import PaymentOrder

    _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro"), headers=auth_headers)
    order = checkout.json()["order"]

    row = db.query(PaymentOrder).filter(PaymentOrder.id == order["id"]).first()
    row.provider = "binance_pay"  # simulates an order that really belongs to a different provider
    db.commit()

    body, headers = _ipn(order_id=order["external_order_id"], payment_status="finished", price_amount=float(PLAN_PRICES["pro"]["USD"]))
    resp = client.post("/billing/webhook/nowpayments", content=body, headers=headers)
    assert resp.status_code == 400

    db.expire_all()
    refreshed = db.query(PaymentOrder).filter(PaymentOrder.id == order["id"]).first()
    assert refreshed.status == "processing"


def test_NC09_ipn_for_expired_order_absorbed(client, auth_headers, db):
    _install_registry()
    checkout = client.post("/billing/checkout", json=_checkout_body(plan="pro"), headers=auth_headers)
    order = checkout.json()["order"]

    row = db.query(PaymentOrder).filter(PaymentOrder.id == order["id"]).first()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    body, headers = _ipn(order_id=order["external_order_id"], payment_status="finished", price_amount=float(PLAN_PRICES["pro"]["USD"]))
    resp = client.post("/billing/webhook/nowpayments", content=body, headers=headers)
    assert resp.status_code == 200

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "expired"

    entitlement = client.get("/users/me/entitlement", headers=auth_headers)
    assert entitlement.json()["plan"] != "pro"
