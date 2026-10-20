"""Integration tests — HTTP surface for Stripe Recurring subscriptions
(Stripe Recurring hardening pass, 2026-09-08).

Registers the REAL StripeAdapter with only its HTTP transport faked out
(same division of labor as test_billing_checkout_stripe.py), proving the
actual wiring end-to-end over real HTTP: POST /billing/subscriptions,
GET /billing/subscriptions/{id}, POST /billing/subscriptions/{id}/cancel,
and POST /billing/webhook/stripe now also routing subscription-lifecycle
events through the SAME endpoint used for one-time payments.

Test matrix
-----------
HT01) POST /billing/subscriptions -> 201, local Subscription +
      real (faked-transport) Checkout Session (mode=subscription);
      provider_setup_intent_id round-trips.
HT02) checkout.session.completed(mode=subscription) webhook (valid
      signature) -> 200, provider_subscription_id persisted, GET
      reflects it.
HT03) Invalid Stripe-Signature on a subscription webhook -> 400,
      subscription untouched.
HT04) Duplicate delivery (identical body + signature) of the SAME
      event -> processed once (webhook_nonce replay protection, the
      SAME table/mechanism the one-time path uses).
HT05) invoice.payment_succeeded webhook -> renewal PaymentOrder
      created + confirmed, exactly one ledger debit+credit pair,
      entitlement reflects the plan, Subscription period advances.
HT06) POST /billing/subscriptions/{id}/cancel -> 200, REAL DELETE
      reaches the (faked-transport) provider before local status
      flips to 'cancelled'.
HT07) customer.subscription.deleted webhook -> 200, Subscription
      status 'cancelled' via the webhook path (no explicit
      cancel_subscription() call from the user).
HT08) invoice.payment_failed webhook -> 200, record_renewal_failure()
      applied (status ACTIVE -> PAST_DUE at the default
      subscription_max_past_due_cycles=1 threshold), NO PaymentOrder
      created, replay-safe on redelivery (audit gap closed,
      2026-09-09 — added test-only, no production code changed).
HT09) customer.subscription.updated webhook -> 200, domain no-op (no
      confirmed consumer contract yet — see SubscriptionService's own
      docstring): every business-facing field is byte-for-byte
      unchanged; the ONLY mutation is the per-stream ordering marker
      (last_subscription_event_id/_at), verified directly against the
      row since it is not exposed over HTTP (audit gap closed,
      2026-09-09).
HT10) A real Stripe event type that LOOKS subscription-related
      (customer.subscription.trial_will_end) but is NOT one of the
      four types StripeAdapter.is_subscription_event() classifies into
      the subscription vocabulary -> falls through to the SAME
      UnknownWebhookEvent handling the one-time path already uses ->
      200, event_type="unknown", Subscription completely untouched, no
      PaymentOrder/ledger side effect (audit gap closed, 2026-09-09;
      see that test's own docstring for why verify_subscription_
      webhook()'s OWN UnknownWebhookEvent branch is not reachable via
      the real HTTP route today — it stays covered only at the unit
      level, test_stripe_subscription.py's A13).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from decimal import Decimal
from urllib.parse import parse_qs

import pytest

from src.api.deps import get_provider_registry
from src.billing.providers.registry import ProviderRegistry
from src.billing.providers.stripe_adapter import StripeAdapter
from src.main import app as _app
from src.models.ledger import LedgerEntry
from src.models.payment_order import PaymentOrder
from src.models.subscription import Subscription
from src.services.request_control import InMemoryRateLimiter
from src.billing.pricing import PLAN_PRICES
# Amounts below come from the catalog rather than from literals: what these
# tests check is that an order, a webhook or an invoice carries the SAME
# price the server published, and a repeated literal quietly stops testing
# that the moment prices move. The prices themselves are argued for in
# tests/unit/test_plan_economics.py.

_WEBHOOK_SECRET = "integration-test-stripe-recurring-webhook-secret"


class _FakeStripeTransport:
    """Serves both the Checkout Session creation POST (mode=subscription)
    and the cancellation DELETE — verify_subscription_webhook() never
    calls the transport (Stripe signs the full body, no follow-up GET
    needed, same as the one-time verify_webhook())."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def post(self, url, *, headers, body, timeout):
        self.calls.append({"method": "POST", "url": url, "headers": headers, "body": body})
        parsed = {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}
        session_id = f"cs_test_{parsed.get('client_reference_id', 'unknown')}"
        return 200, json.dumps({
            "id": session_id, "object": "checkout.session",
            "url": f"https://checkout.stripe.com/c/pay/{session_id}",
        }).encode("utf-8")

    async def get(self, url, *, headers, timeout):
        raise NotImplementedError

    async def delete(self, url, *, headers, timeout):
        self.calls.append({"method": "DELETE", "url": url, "headers": headers})
        return 200, json.dumps({"id": url.rsplit("/", 1)[-1], "object": "subscription", "status": "canceled"}).encode("utf-8")


def _install_registry() -> tuple[ProviderRegistry, _FakeStripeTransport]:
    transport = _FakeStripeTransport()
    stripe_adapter = StripeAdapter(
        secret_key="sk_test_TEST", webhook_secret=_WEBHOOK_SECRET,
        base_url="https://api.stripe.com", transport=transport,
    )
    reg = ProviderRegistry()
    reg.register(stripe_adapter)
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


def _subscription_body(plan: str = "pro", **overrides) -> dict:
    body = {"plan": plan, "payment_method": "credit_card", "idempotency_key": str(uuid.uuid4())}
    body.update(overrides)
    return body


def _sign(secret: str, *, ts: str, body: bytes) -> str:
    signed_payload = f"{ts}.".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()


def _webhook(*, event_type: str, obj: dict, event_id: str = "evt_1", secret: str = _WEBHOOK_SECRET) -> tuple[bytes, dict]:
    ts = str(int(time.time()))
    payload = {"id": event_id, "type": event_type, "created": int(time.time()), "data": {"object": obj}}
    body = json.dumps(payload).encode("utf-8")
    v1 = _sign(secret, ts=ts, body=body)
    return body, {"stripe-signature": f"t={ts},v1={v1}"}


def _checkout_completed_subscription(*, session_id: str, subscription_id: str, payment_status: str = "paid") -> dict:
    return {
        "id": session_id, "object": "checkout.session", "mode": "subscription",
        "subscription": subscription_id, "payment_status": payment_status,
    }


def _invoice(*, invoice_id: str, subscription_id: str, amount_paid: int = int(PLAN_PRICES["pro"]["USD"] * 100), currency: str = "usd") -> dict:
    return {
        "id": invoice_id, "object": "invoice", "subscription": subscription_id,
        "amount_paid": amount_paid, "currency": currency,
    }


def _subscription_object(*, subscription_id: str, sub_status: str = "canceled") -> dict:
    return {"id": subscription_id, "object": "subscription", "status": sub_status}


# ---------------------------------------------------------------------------
# HT01 — creation
# ---------------------------------------------------------------------------


def test_HT01_create_subscription_creates_real_session(client, auth_headers):
    _install_registry()
    resp = client.post("/billing/subscriptions", json=_subscription_body(plan="pro"), headers=auth_headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["redirect_url"] is not None and body["redirect_url"].startswith("https://checkout.stripe.com/")
    sub = body["subscription"]
    assert sub["status"] == "active"
    assert sub["provider"] == "stripe"
    assert sub["provider_setup_intent_id"] is not None and sub["provider_setup_intent_id"].startswith("cs_test_")
    assert sub["provider_subscription_id"] is None  # not confirmed yet
    assert Decimal(sub["amount"]) == PLAN_PRICES["pro"]["USD"]

    get_resp = client.get(f"/billing/subscriptions/{sub['id']}", headers=auth_headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == sub["id"]


# ---------------------------------------------------------------------------
# HT02-HT04 — subscription-lifecycle webhook: valid / invalid sig / duplicate
# ---------------------------------------------------------------------------


def test_HT02_checkout_completed_subscription_webhook_confirms(client, auth_headers):
    _install_registry()
    created = client.post("/billing/subscriptions", json=_subscription_body(plan="pro"), headers=auth_headers)
    sub = created.json()["subscription"]

    session_obj = _checkout_completed_subscription(
        session_id=sub["provider_setup_intent_id"], subscription_id="sub_ext_ht02",
    )
    body, headers = _webhook(event_type="checkout.session.completed", obj=session_obj, event_id="evt_ht02")
    resp = client.post("/billing/webhook/stripe", content=body, headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["subscription_id"] == sub["id"]
    assert resp.json()["order_id"] is None

    get_resp = client.get(f"/billing/subscriptions/{sub['id']}", headers=auth_headers)
    assert get_resp.json()["provider_subscription_id"] == "sub_ext_ht02"


def test_HT03_invalid_signature_rejected(client, auth_headers):
    _install_registry()
    created = client.post("/billing/subscriptions", json=_subscription_body(plan="pro"), headers=auth_headers)
    sub = created.json()["subscription"]

    session_obj = _checkout_completed_subscription(
        session_id=sub["provider_setup_intent_id"], subscription_id="sub_ext_ht03",
    )
    body, headers = _webhook(
        event_type="checkout.session.completed", obj=session_obj, event_id="evt_ht03", secret="wrong-secret",
    )
    resp = client.post("/billing/webhook/stripe", content=body, headers=headers)
    assert resp.status_code == 400

    get_resp = client.get(f"/billing/subscriptions/{sub['id']}", headers=auth_headers)
    assert get_resp.json()["provider_subscription_id"] is None  # untouched


def test_HT04_duplicate_webhook_delivery_processed_once(client, auth_headers):
    _install_registry()
    created = client.post("/billing/subscriptions", json=_subscription_body(plan="pro"), headers=auth_headers)
    sub = created.json()["subscription"]

    session_obj = _checkout_completed_subscription(
        session_id=sub["provider_setup_intent_id"], subscription_id="sub_ext_ht04",
    )
    body, headers = _webhook(event_type="checkout.session.completed", obj=session_obj, event_id="evt_ht04_dup")

    resp1 = client.post("/billing/webhook/stripe", content=body, headers=headers)
    resp2 = client.post("/billing/webhook/stripe", content=body, headers=headers)  # IDENTICAL redelivery
    assert resp1.status_code == 200
    assert resp2.status_code == 200

    get_resp = client.get(f"/billing/subscriptions/{sub['id']}", headers=auth_headers)
    assert get_resp.json()["provider_subscription_id"] == "sub_ext_ht04"


# ---------------------------------------------------------------------------
# HT05 — invoice/renewal webhook
# ---------------------------------------------------------------------------


def test_HT05_invoice_payment_succeeded_confirms_renewal(client, auth_headers):
    _install_registry()
    created = client.post("/billing/subscriptions", json=_subscription_body(plan="pro"), headers=auth_headers)
    sub = created.json()["subscription"]

    session_obj = _checkout_completed_subscription(
        session_id=sub["provider_setup_intent_id"], subscription_id="sub_ext_ht05",
    )
    body, headers = _webhook(event_type="checkout.session.completed", obj=session_obj, event_id="evt_ht05_created")
    client.post("/billing/webhook/stripe", content=body, headers=headers)

    invoice_obj = _invoice(invoice_id="in_ht05", subscription_id="sub_ext_ht05", amount_paid=int(PLAN_PRICES["pro"]["USD"] * 100), currency="usd")
    body2, headers2 = _webhook(event_type="invoice.payment_succeeded", obj=invoice_obj, event_id="evt_ht05_invoice")
    resp = client.post("/billing/webhook/stripe", content=body2, headers=headers2)
    assert resp.status_code == 200, resp.text
    assert resp.json()["new_status"] == "active"

    entitlement = client.get("/users/me/entitlement", headers=auth_headers)
    assert entitlement.json()["plan"] == "pro"


def test_HT05b_invoice_payment_succeeded_writes_ledger_and_order(client, auth_headers, db):
    _install_registry()
    created = client.post("/billing/subscriptions", json=_subscription_body(plan="pro"), headers=auth_headers)
    sub = created.json()["subscription"]

    session_obj = _checkout_completed_subscription(
        session_id=sub["provider_setup_intent_id"], subscription_id="sub_ext_ht05b",
    )
    body, headers = _webhook(event_type="checkout.session.completed", obj=session_obj, event_id="evt_ht05b_created")
    client.post("/billing/webhook/stripe", content=body, headers=headers)

    invoice_obj = _invoice(invoice_id="in_ht05b", subscription_id="sub_ext_ht05b", amount_paid=int(PLAN_PRICES["pro"]["USD"] * 100), currency="usd")
    body2, headers2 = _webhook(event_type="invoice.payment_succeeded", obj=invoice_obj, event_id="evt_ht05b_invoice")
    resp = client.post("/billing/webhook/stripe", content=body2, headers=headers2)
    assert resp.status_code == 200

    orders = db.query(PaymentOrder).filter(PaymentOrder.subscription_id == sub["id"]).all()
    assert len(orders) == 1
    assert orders[0].status == "confirmed"
    assert db.query(LedgerEntry).count() == 2


# ---------------------------------------------------------------------------
# HT06-HT07 — cancellation, user-initiated and webhook-confirmed
# ---------------------------------------------------------------------------


def test_HT06_cancel_route_calls_real_provider_before_local_state(client, auth_headers):
    _reg, transport = _install_registry()
    created = client.post("/billing/subscriptions", json=_subscription_body(plan="pro"), headers=auth_headers)
    sub = created.json()["subscription"]

    session_obj = _checkout_completed_subscription(
        session_id=sub["provider_setup_intent_id"], subscription_id="sub_ext_ht06",
    )
    body, headers = _webhook(event_type="checkout.session.completed", obj=session_obj, event_id="evt_ht06")
    client.post("/billing/webhook/stripe", content=body, headers=headers)

    resp = client.post(f"/billing/subscriptions/{sub['id']}/cancel", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    assert any(c["method"] == "DELETE" and c["url"].endswith("sub_ext_ht06") for c in transport.calls)


def test_HT07_subscription_canceled_webhook_updates_status(client, auth_headers):
    _install_registry()
    created = client.post("/billing/subscriptions", json=_subscription_body(plan="pro"), headers=auth_headers)
    sub = created.json()["subscription"]

    session_obj = _checkout_completed_subscription(
        session_id=sub["provider_setup_intent_id"], subscription_id="sub_ext_ht07",
    )
    body, headers = _webhook(event_type="checkout.session.completed", obj=session_obj, event_id="evt_ht07_created")
    client.post("/billing/webhook/stripe", content=body, headers=headers)

    cancel_obj = _subscription_object(subscription_id="sub_ext_ht07")
    body2, headers2 = _webhook(event_type="customer.subscription.deleted", obj=cancel_obj, event_id="evt_ht07_cancel")
    resp = client.post("/billing/webhook/stripe", content=body2, headers=headers2)
    assert resp.status_code == 200
    assert resp.json()["new_status"] == "cancelled"

    get_resp = client.get(f"/billing/subscriptions/{sub['id']}", headers=auth_headers)
    assert get_resp.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# HT08-HT10 — audit gaps closed 2026-09-09 (invoice.payment_failed,
# customer.subscription.updated, and an unmapped subscription-shaped event,
# all exercised through the real HTTP route rather than only at the unit
# level). Test-only change — no production code modified for these.
# ---------------------------------------------------------------------------


def test_HT08_invoice_payment_failed_records_renewal_failure(client, auth_headers, db):
    """invoice.payment_failed (valid Stripe-Signature, real HTTP route) ->
    200, record_renewal_failure() applied via SubscriptionService.
    apply_subscription_webhook_event()'s RENEWAL_FAILED branch. At the
    default settings.subscription_max_past_due_cycles=1, the FIRST
    failure does not yet exceed the threshold, so status goes
    ACTIVE -> PAST_DUE (not straight to GRACE_PERIOD) — see
    record_renewal_failure()'s own docstring for the exact
    entering_grace comparison this asserts against. A failed cycle
    NEVER creates a PaymentOrder (only RENEWAL_SUCCEEDED reactively
    does) — asserted directly against the DB. Replay: an identical
    redelivery (same event_id) must not double-increment the failure
    counter — the SAME webhook_nonce table the one-time path uses,
    checked before apply_subscription_webhook_event() is ever called
    again."""
    _install_registry()
    created = client.post("/billing/subscriptions", json=_subscription_body(plan="pro"), headers=auth_headers)
    sub = created.json()["subscription"]

    session_obj = _checkout_completed_subscription(
        session_id=sub["provider_setup_intent_id"], subscription_id="sub_ext_ht08",
    )
    body, headers = _webhook(event_type="checkout.session.completed", obj=session_obj, event_id="evt_ht08_created")
    client.post("/billing/webhook/stripe", content=body, headers=headers)

    invoice_obj = _invoice(invoice_id="in_ht08_fail", subscription_id="sub_ext_ht08", amount_paid=int(PLAN_PRICES["pro"]["USD"] * 100), currency="usd")
    body2, headers2 = _webhook(event_type="invoice.payment_failed", obj=invoice_obj, event_id="evt_ht08_failed")
    resp = client.post("/billing/webhook/stripe", content=body2, headers=headers2)
    assert resp.status_code == 200, resp.text
    assert resp.json()["subscription_id"] == sub["id"]
    assert resp.json()["new_status"] == "past_due"

    get_resp = client.get(f"/billing/subscriptions/{sub['id']}", headers=auth_headers)
    body_after = get_resp.json()
    assert body_after["status"] == "past_due"
    assert body_after["consecutive_failed_cycles"] == 1

    # A FAILED cycle never creates a PaymentOrder — only a SUCCESSFUL one does.
    orders = db.query(PaymentOrder).filter(PaymentOrder.subscription_id == sub["id"]).all()
    assert orders == []

    # Replay: identical redelivery of the SAME event must be absorbed, not
    # counted a second time.
    resp_dup = client.post("/billing/webhook/stripe", content=body2, headers=headers2)
    assert resp_dup.status_code == 200
    get_resp2 = client.get(f"/billing/subscriptions/{sub['id']}", headers=auth_headers)
    body_after2 = get_resp2.json()
    assert body_after2["consecutive_failed_cycles"] == 1
    assert body_after2["status"] == "past_due"


def test_HT09_subscription_updated_webhook_is_domain_noop(client, auth_headers, db):
    """customer.subscription.updated (valid Stripe-Signature, real HTTP
    route) -> 200, reaches SubscriptionService.
    apply_subscription_webhook_event()'s SUBSCRIPTION_UPDATED branch,
    which is a deliberate no-op (no confirmed consumer contract yet —
    see that module's own docstring: 'não invente eventos específicos
    sem confirmar o contrato existente'). Every business-facing field
    (plan/amount/currency/status/payment_method/provider/
    provider_subscription_id/consecutive_failed_cycles) is asserted
    byte-for-byte unchanged over the HTTP response. The ONLY mutation
    the current design makes is the per-stream ordering/replay marker
    (last_subscription_event_id/_at) — not exposed on
    SubscriptionResponse, so asserted directly against the ORM row
    instead of inventing a field the API doesn't return."""
    _install_registry()
    created = client.post("/billing/subscriptions", json=_subscription_body(plan="pro"), headers=auth_headers)
    sub = created.json()["subscription"]

    session_obj = _checkout_completed_subscription(
        session_id=sub["provider_setup_intent_id"], subscription_id="sub_ext_ht09",
    )
    body, headers = _webhook(event_type="checkout.session.completed", obj=session_obj, event_id="evt_ht09_created")
    client.post("/billing/webhook/stripe", content=body, headers=headers)

    before = client.get(f"/billing/subscriptions/{sub['id']}", headers=auth_headers).json()

    updated_obj = _subscription_object(subscription_id="sub_ext_ht09", sub_status="active")
    body2, headers2 = _webhook(event_type="customer.subscription.updated", obj=updated_obj, event_id="evt_ht09_updated")
    resp = client.post("/billing/webhook/stripe", content=body2, headers=headers2)
    assert resp.status_code == 200, resp.text
    assert resp.json()["subscription_id"] == sub["id"]
    assert resp.json()["new_status"] == before["status"]  # unchanged — no-op

    after = client.get(f"/billing/subscriptions/{sub['id']}", headers=auth_headers).json()
    for field in (
        "plan", "amount", "currency", "status", "payment_method", "provider",
        "provider_subscription_id", "consecutive_failed_cycles",
    ):
        assert after[field] == before[field], field

    row = db.query(Subscription).filter(Subscription.id == sub["id"]).first()
    assert row.last_subscription_event_id == "evt_ht09_updated"
    assert row.last_subscription_event_at is not None


def test_HT10_unmapped_subscription_shaped_event_absorbed(client, auth_headers, db):
    """A real, documented Stripe event type that LOOKS subscription-
    related (customer.subscription.trial_will_end) but is NOT one of
    the four types StripeAdapter.is_subscription_event() classifies
    into the subscription vocabulary (only customer.subscription.
    updated/deleted and invoice.payment_succeeded/failed qualify — see
    that method's own dict, _SUBSCRIPTION_EVENT_KIND_MAP). Because the
    classification gate returns False for it, receive_webhook() never
    calls _handle_subscription_webhook() at all — it takes the SAME
    path as any other unrecognized ONE-TIME event: adapter.
    verify_webhook() (NOT verify_subscription_webhook()) raises
    UnknownWebhookEvent, absorbed as a 200 ack with event_type="unknown"
    BEFORE any order/subscription lookup ever happens (receive_
    webhook()'s own step-3 `except UnknownWebhookEvent` branch).

    This is the REAL, current contract, confirmed by reading the code
    before writing this assertion (mission instruction #4): every event
    type that DOES pass is_subscription_event() always resolves to a
    real SubscriptionEventKind inside verify_subscription_webhook() (the
    same four-entry map backs both), so THAT method's own
    UnknownWebhookEvent branch is not reachable through the HTTP route
    today — it stays covered only at the unit level (test_stripe_
    subscription.py's A13, which calls the adapter method directly).
    This test therefore honestly exercises the one-time path's existing,
    already-covered UnknownWebhookEvent handling for a subscription-
    shaped payload, and proves it leaves the Subscription/PaymentOrder/
    ledger completely untouched — the property the audit needed."""
    _install_registry()
    created = client.post("/billing/subscriptions", json=_subscription_body(plan="pro"), headers=auth_headers)
    sub = created.json()["subscription"]

    session_obj = _checkout_completed_subscription(
        session_id=sub["provider_setup_intent_id"], subscription_id="sub_ext_ht10",
    )
    body, headers = _webhook(event_type="checkout.session.completed", obj=session_obj, event_id="evt_ht10_created")
    client.post("/billing/webhook/stripe", content=body, headers=headers)

    before = client.get(f"/billing/subscriptions/{sub['id']}", headers=auth_headers).json()

    trial_obj = _subscription_object(subscription_id="sub_ext_ht10", sub_status="active")
    body2, headers2 = _webhook(
        event_type="customer.subscription.trial_will_end", obj=trial_obj, event_id="evt_ht10_trial",
    )
    resp = client.post("/billing/webhook/stripe", content=body2, headers=headers2)
    assert resp.status_code == 200, resp.text
    assert resp.json()["event_type"] == "unknown"
    assert resp.json()["subscription_id"] is None
    assert resp.json()["order_id"] is None

    after = client.get(f"/billing/subscriptions/{sub['id']}", headers=auth_headers).json()
    assert after == before  # completely untouched

    orders = db.query(PaymentOrder).filter(PaymentOrder.subscription_id == sub["id"]).all()
    assert orders == []
    assert db.query(LedgerEntry).count() == 0
