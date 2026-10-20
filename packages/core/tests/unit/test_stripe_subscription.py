"""Unit + integration tests for the Stripe Recurring etapa (2026-09-08) —
the Subscription Provider Contract, StripeAdapter's real implementation
of it, PaymentRouter.route_subscription_creation(), and
SubscriptionService's webhook-driven consumption
(initiate_external_subscription()/confirm_external_subscription()/
apply_subscription_webhook_event()).

Every test uses a FakeTransport (StripeAdapter's own HTTP seam) or a
FakeSubProvider test double — NO real network call, NO real Stripe
credentials, NO real charge, anywhere in this file.

Test matrix
-----------
Adapter (StripeAdapter + FakeTransport) — proves the CONCRETE Stripe
integration genuinely implements the contract, not just declares it:
  A01) create_subscription — mode=subscription, recurring.interval=month,
       Idempotency-Key passed straight through, no pre-created Price.
  A02) create_subscription — success parses SubscriptionIntent (id=session
       id, redirect_url=url, provider_subscription_id=None — not known yet).
  A03) create_subscription — error response raises StripeError.
  A04) create_subscription — transport timeout raises ProviderUnavailable.
  A05) cancel_subscription — DELETE to the right path; error raises StripeError.
  A06) retrieve_subscription — status="active" -> CONFIRMED; 404 -> PaymentIntentNotFound.
  A07) update_subscription — metadata-only body; parses returned status.
  A08) verify_subscription_webhook — checkout.session.completed(mode=subscription)
       -> SUBSCRIPTION_CREATED, provider_ref=session id, provider_subscription_id=
       session.subscription.
  A09) verify_subscription_webhook — invoice.payment_succeeded -> RENEWAL_SUCCEEDED,
       provider_ref=invoice id, amount/currency parsed.
  A10) verify_subscription_webhook — invoice.payment_failed -> RENEWAL_FAILED.
  A11) verify_subscription_webhook — customer.subscription.deleted -> SUBSCRIPTION_CANCELED.
  A12) verify_subscription_webhook — customer.subscription.updated -> SUBSCRIPTION_UPDATED.
  A13) verify_subscription_webhook — customer.subscription.created (deliberately
       unmapped — no back-reference to the setup Checkout Session) -> UnknownWebhookEvent.
  A14) verify_subscription_webhook — invalid signature -> InvalidWebhookSignature
       (same mechanism as verify_webhook() — a spoofing attempt is rejected identically).
  A15) verify_subscription_webhook / create_subscription — no secret ever appears in logs.

Router + Service (FakeSubProvider doubles) — the 22 mission-enumerated scenarios:
  R01) Stripe-like fake declaring ONE_TIME+RECURRING is eligible for a
       RECURRING creation; a Mercado-Pago-like fake declaring ONE_TIME only
       (R02) is excluded.
  R03) Router selects the ONLY RECURRING-capable candidate when multiple
       providers are registered.
  R04) Router raises NoEligibleProvider when NO provider declares RECURRING.
  R05) Subscription row creation itself is idempotent per idempotency_key
       (create_subscription()).
  R06) Two "concurrent" (sequential stand-in) initiate_external_subscription()
       calls for the SAME subscription_id converge to ONE external
       create_subscription() call.
  R07) Retry of external creation AFTER a successful attempt is a no-op
       (provider_setup_intent_id short-circuits it).
  R08) Timeout (ProviderUnavailable) during creation leaves
       provider_setup_intent_id/provider_subscription_id BOTH None —
       never treated as "creation failed", never retried automatically.
  R09) Retry after that same ProviderUnavailable resumes against the
       SAME provider and (once it succeeds) creates exactly ONE external
       subscription — the ambiguous outcome never produced a second one.
  R10) Concurrent cancellation vs. a webhook-driven renewal: once
       cancel_subscription() wins, a subsequent RENEWAL_SUCCEEDED event
       for the same cycle raises SubscriptionNotRenewable — no ledger
       entry, no PaymentOrder, no corrupted state.
  R11) SUBSCRIPTION_UPDATED webhook — logged no-op, ordering markers still
       advance (a later real event isn't misjudged as "out of order").
  R12) Valid SUBSCRIPTION_CREATED webhook -> confirm_external_subscription()
       sets provider_subscription_id exactly once.
  R13) (invalid signature is exercised at the adapter level — A14; nothing
       new to prove at the service layer, which never sees an unverified
       payload).
  R14) Duplicate webhook (identical event_id) is absorbed — no second
       mutation, subscription state byte-for-byte unchanged.
  R15) Out-of-order webhook — an OLDER event arriving AFTER a newer one is
       absorbed; the newer state is never overwritten.
  R16) Renewal payment generates EXACTLY ONE PaymentOrder for the cycle,
       even when the SAME invoice is redelivered under a different
       top-level event_id (Stripe's own at-least-once semantics).
  R17) Confirmation generates EXACTLY ONE ledger debit+credit pair, even
       across a duplicate delivery.
  R18) Confirmation does not duplicate entitlement — user.plan set once,
       stays correct after a duplicate delivery.
  R19) RENEWAL_FAILED webhook -> record_renewal_failure() -> subscription
       moves ACTIVE -> PAST_DUE (explicit rejection, never ambiguous).
  R20) ProviderUnavailable during subscription creation changes NOTHING
       about Subscription.status — see R08, re-asserted from the
       service-level entry point (initiate_external_subscription()).
  R21) Coexistence: the SAME registry serves both an ordinary one-time
       route_checkout() and a route_subscription_creation() with zero
       cross-interference.
  R22) (no-secret-in-logs is exercised at the adapter level — A15; the
       service layer never handles a secret value at all).

Integration
-----------
  I01) Full fake-transport flow through the REAL StripeAdapter (not a
       fake provider): create Subscription -> initiate_external_subscription()
       creates a real-shaped Checkout Session (mode=subscription) ->
       provider_setup_intent_id persisted -> a genuine
       checkout.session.completed(mode=subscription) webhook, parsed by
       StripeAdapter.verify_subscription_webhook(), is applied via
       apply_subscription_webhook_event() -> provider_subscription_id
       persisted -> a genuine invoice.payment_succeeded webhook,
       likewise parsed by the real adapter, reactively creates+confirms
       this cycle's PaymentOrder -> ledger pair (exactly 2 entries) and
       user.plan are correct -> Subscription.status == 'active' and
       current_period_start advanced.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Mapping

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.billing.payment_router import (
    AllSubscriptionProvidersRejected,
    NoEligibleProvider,
    route_subscription_creation,
)
from src.billing.providers.base import (
    InvalidWebhookSignature,
    PaymentCreationRejected,
    PaymentIntent,
    PaymentIntentNotFound,
    PaymentMethod,
    PaymentProvider,
    PaymentResult,
    PaymentStatus,
    PaymentType,
    ProviderUnavailable,
    SubscriptionEventKind,
    SubscriptionIntent,
    SubscriptionWebhookEvent,
    UnknownWebhookEvent,
    WebhookEvent,
)
from src.billing.providers.registry import ProviderRegistry
from src.billing.providers.stripe_adapter import StripeAdapter, StripeError
from src.database import Base
from src.models import PaymentOrder, Subscription  # noqa: F401 — registers tables
from src.models.ledger import LedgerEntry
from src.models.subscription import SubscriptionStatus
from src.models.user import User
from src.services.subscription_service import (
    SubscriptionNotFound,
    SubscriptionNotRenewable,
    SubscriptionService,
)


# ===========================================================================
# Part A — StripeAdapter unit tests (FakeTransport, no fake-provider double)
# ===========================================================================


class FakeTransport:
    """Same FIFO-queue double as test_stripe_adapter.py's own — never
    opens a socket."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._queue: list[tuple[int, bytes] | Exception] = []

    def queue_json(self, status_code: int, body: dict) -> None:
        self._queue.append((status_code, json.dumps(body).encode("utf-8")))

    def queue_exception(self, exc: Exception) -> None:
        self._queue.append(exc)

    async def post(self, url, *, headers, body, timeout):
        self.calls.append({"method": "POST", "url": url, "headers": headers, "body": body})
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def get(self, url, *, headers, timeout):
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def delete(self, url, *, headers, timeout):
        self.calls.append({"method": "DELETE", "url": url, "headers": headers})
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _make_adapter(transport: FakeTransport, *, webhook_secret: str = "whsec_test", **overrides) -> StripeAdapter:
    kwargs = dict(
        secret_key="sk_test_TEST", webhook_secret=webhook_secret,
        base_url="https://api.stripe.com", transport=transport,
    )
    kwargs.update(overrides)
    return StripeAdapter(**kwargs)


def _sign(secret: str, *, ts: str, body: bytes) -> str:
    signed_payload = f"{ts}.".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()


def _webhook_headers(secret: str, *, body: bytes, ts: str | None = None) -> dict:
    ts = ts or str(int(time.time()))
    v1 = _sign(secret, ts=ts, body=body)
    return {"stripe-signature": f"t={ts},v1={v1}"}


def _checkout_completed_subscription_event(
    *, event_id: str = "evt_sub_1", session_id: str = "cs_test_sub_1", subscription_id: str = "sub_test_1",
    created: int | None = None,
) -> dict:
    return {
        "id": event_id, "type": "checkout.session.completed",
        "created": created if created is not None else int(time.time()),
        "data": {"object": {
            "id": session_id, "object": "checkout.session", "mode": "subscription",
            "subscription": subscription_id, "payment_status": "paid",
        }},
    }


def _invoice_event(
    *, event_id: str = "evt_inv_1", event_type: str = "invoice.payment_succeeded",
    invoice_id: str = "in_test_1", subscription_id: str = "sub_test_1",
    amount_paid: int = 2999, currency: str = "usd", created: int | None = None,
) -> dict:
    return {
        "id": event_id, "type": event_type,
        "created": created if created is not None else int(time.time()),
        "data": {"object": {
            "id": invoice_id, "object": "invoice", "subscription": subscription_id,
            "amount_paid": amount_paid, "currency": currency,
        }},
    }


def _subscription_lifecycle_event(
    *, event_id: str = "evt_cs_1", event_type: str = "customer.subscription.deleted",
    subscription_id: str = "sub_test_1", created: int | None = None,
) -> dict:
    return {
        "id": event_id, "type": event_type,
        "created": created if created is not None else int(time.time()),
        "data": {"object": {"id": subscription_id, "object": "subscription", "status": "canceled"}},
    }


# ---------------------------------------------------------------------------
# A01-A04 — create_subscription
# ---------------------------------------------------------------------------


async def test_A01_create_subscription_well_formed_request():
    from urllib.parse import parse_qs

    transport = FakeTransport()
    transport.queue_json(200, {"id": "cs_test_sub_1", "url": "https://checkout.stripe.com/c/pay/cs_test_sub_1"})
    adapter = _make_adapter(transport)

    await adapter.create_subscription(
        amount=Decimal("29.99"), currency="USD", user_id="u1", idempotency_key="sub-idem-1",
    )

    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/v1/checkout/sessions")
    assert call["headers"]["Idempotency-Key"] == "sub-idem-1"
    body = {k: v[0] for k, v in parse_qs(call["body"].decode("utf-8")).items()}
    assert body["mode"] == "subscription"
    assert body["line_items[0][price_data][recurring][interval]"] == "month"
    assert body["line_items[0][price_data][unit_amount]"] == "2999"
    assert "price" not in body  # never a pre-created Stripe Price ID


async def test_A02_create_subscription_success_parses_intent():
    transport = FakeTransport()
    transport.queue_json(200, {"id": "cs_test_sub_2", "url": "https://checkout.stripe.com/c/pay/cs_test_sub_2"})
    adapter = _make_adapter(transport)

    intent = await adapter.create_subscription(
        amount=Decimal("9.99"), currency="usd", user_id="u1", idempotency_key="sub-idem-2",
    )
    assert intent.id == "cs_test_sub_2"
    assert intent.redirect_url == "https://checkout.stripe.com/c/pay/cs_test_sub_2"
    assert intent.provider_subscription_id is None  # not known until webhook confirms
    assert intent.status == PaymentStatus.PENDING


async def test_A03_create_subscription_error_response_raises():
    transport = FakeTransport()
    transport.queue_json(400, {"error": {"message": "invalid request"}})
    adapter = _make_adapter(transport)
    with pytest.raises(StripeError):
        await adapter.create_subscription(amount=Decimal("9.99"), currency="USD", user_id="u1", idempotency_key="k")


async def test_A04_create_subscription_timeout_raises_provider_unavailable():
    transport = FakeTransport()
    transport.queue_exception(ProviderUnavailable("timed out"))
    adapter = _make_adapter(transport)
    with pytest.raises(ProviderUnavailable):
        await adapter.create_subscription(amount=Decimal("9.99"), currency="USD", user_id="u1", idempotency_key="k")


# ---------------------------------------------------------------------------
# A05-A07 — cancel / retrieve / update
# ---------------------------------------------------------------------------


async def test_A05_cancel_subscription_deletes_and_raises_on_error():
    transport = FakeTransport()
    transport.queue_json(200, {"id": "sub_1", "status": "canceled"})
    adapter = _make_adapter(transport)
    await adapter.cancel_subscription("sub_1")
    assert transport.calls[0]["method"] == "DELETE"
    assert transport.calls[0]["url"].endswith("/v1/subscriptions/sub_1")

    transport2 = FakeTransport()
    transport2.queue_json(404, {"error": {"message": "no such subscription"}})
    adapter2 = _make_adapter(transport2)
    with pytest.raises(StripeError):
        await adapter2.cancel_subscription("sub_missing")


async def test_A06_retrieve_subscription_status_mapping_and_not_found():
    transport = FakeTransport()
    transport.queue_json(200, {"id": "sub_1", "status": "active"})
    adapter = _make_adapter(transport)
    intent = await adapter.retrieve_subscription("sub_1")
    assert intent.status == PaymentStatus.CONFIRMED
    assert intent.provider_subscription_id == "sub_1"

    transport2 = FakeTransport()
    transport2.queue_json(404, {"error": {"message": "missing"}})
    adapter2 = _make_adapter(transport2)
    with pytest.raises(PaymentIntentNotFound):
        await adapter2.retrieve_subscription("sub_missing")


async def test_A07_update_subscription_metadata_only():
    from urllib.parse import parse_qs

    transport = FakeTransport()
    transport.queue_json(200, {"id": "sub_1", "status": "active"})
    adapter = _make_adapter(transport)
    intent = await adapter.update_subscription("sub_1", metadata={"internal_note": "vip"})
    call = transport.calls[0]
    body = {k: v[0] for k, v in parse_qs(call["body"].decode("utf-8")).items()}
    assert body["metadata[internal_note]"] == "vip"
    assert intent.status == PaymentStatus.CONFIRMED


# ---------------------------------------------------------------------------
# A08-A15 — verify_subscription_webhook
# ---------------------------------------------------------------------------


async def test_A08_checkout_completed_subscription_creates_event():
    secret = "whsec_test"
    adapter = _make_adapter(FakeTransport(), webhook_secret=secret)
    body = json.dumps(_checkout_completed_subscription_event(
        session_id="cs_abc", subscription_id="sub_abc",
    )).encode("utf-8")
    headers = _webhook_headers(secret, body=body)

    event = await adapter.verify_subscription_webhook(body, headers)
    assert event.kind == SubscriptionEventKind.SUBSCRIPTION_CREATED
    assert event.provider_ref == "cs_abc"
    assert event.provider_subscription_id == "sub_abc"


async def test_A09_invoice_payment_succeeded():
    secret = "whsec_test"
    adapter = _make_adapter(FakeTransport(), webhook_secret=secret)
    body = json.dumps(_invoice_event(
        invoice_id="in_abc", subscription_id="sub_abc", amount_paid=2999, currency="usd",
    )).encode("utf-8")
    headers = _webhook_headers(secret, body=body)

    event = await adapter.verify_subscription_webhook(body, headers)
    assert event.kind == SubscriptionEventKind.RENEWAL_SUCCEEDED
    assert event.provider_ref == "in_abc"
    assert event.provider_subscription_id == "sub_abc"
    assert event.amount == Decimal("29.99")
    assert event.currency == "USD"


async def test_A10_invoice_payment_failed():
    secret = "whsec_test"
    adapter = _make_adapter(FakeTransport(), webhook_secret=secret)
    body = json.dumps(_invoice_event(event_type="invoice.payment_failed", invoice_id="in_fail")).encode("utf-8")
    headers = _webhook_headers(secret, body=body)

    event = await adapter.verify_subscription_webhook(body, headers)
    assert event.kind == SubscriptionEventKind.RENEWAL_FAILED
    assert event.provider_ref == "in_fail"


async def test_A11_subscription_deleted_canceled():
    secret = "whsec_test"
    adapter = _make_adapter(FakeTransport(), webhook_secret=secret)
    body = json.dumps(_subscription_lifecycle_event(
        event_type="customer.subscription.deleted", subscription_id="sub_abc",
    )).encode("utf-8")
    headers = _webhook_headers(secret, body=body)

    event = await adapter.verify_subscription_webhook(body, headers)
    assert event.kind == SubscriptionEventKind.SUBSCRIPTION_CANCELED
    assert event.provider_subscription_id == "sub_abc"


async def test_A12_subscription_updated():
    secret = "whsec_test"
    adapter = _make_adapter(FakeTransport(), webhook_secret=secret)
    body = json.dumps(_subscription_lifecycle_event(
        event_type="customer.subscription.updated", subscription_id="sub_abc",
    )).encode("utf-8")
    headers = _webhook_headers(secret, body=body)

    event = await adapter.verify_subscription_webhook(body, headers)
    assert event.kind == SubscriptionEventKind.SUBSCRIPTION_UPDATED


async def test_A13_subscription_created_deliberately_unmapped():
    secret = "whsec_test"
    adapter = _make_adapter(FakeTransport(), webhook_secret=secret)
    body = json.dumps(_subscription_lifecycle_event(
        event_type="customer.subscription.created", subscription_id="sub_abc",
    )).encode("utf-8")
    headers = _webhook_headers(secret, body=body)

    with pytest.raises(UnknownWebhookEvent):
        await adapter.verify_subscription_webhook(body, headers)


async def test_A14_invalid_signature_rejected():
    real_secret = "whsec_real"
    adapter = _make_adapter(FakeTransport(), webhook_secret=real_secret)
    body = json.dumps(_checkout_completed_subscription_event()).encode("utf-8")
    forged_headers = _webhook_headers("whsec_wrong_guess", body=body)

    with pytest.raises(InvalidWebhookSignature):
        await adapter.verify_subscription_webhook(body, forged_headers)


async def test_A15_no_secret_leaks_to_logs(caplog):
    secret_key = "SUPER-SECRET-STRIPE-KEY"
    webhook_secret = "SUPER-SECRET-WEBHOOK"
    transport = FakeTransport()
    transport.queue_json(200, {"id": "cs_1", "url": "https://checkout.stripe.com/c/pay/cs_1"})
    adapter = _make_adapter(transport, webhook_secret=webhook_secret, secret_key=secret_key)

    with caplog.at_level(logging.DEBUG):
        await adapter.create_subscription(amount=Decimal("9.99"), currency="USD", user_id="u1", idempotency_key="k")
        body = json.dumps(_checkout_completed_subscription_event()).encode("utf-8")
        headers = _webhook_headers(webhook_secret, body=body)
        await adapter.verify_subscription_webhook(body, headers)

    for record in caplog.records:
        assert secret_key not in record.getMessage()
        assert webhook_secret not in record.getMessage()


# ===========================================================================
# Part B — Router + Service tests (FakeSubProvider doubles)
# ===========================================================================


class _FakeRejected(PaymentCreationRejected):
    pass


class FakeSubProvider(PaymentProvider):
    """A Stripe-like fake declaring ONE_TIME+RECURRING and implementing
    the full Subscription Provider Contract, with a controllable
    behavior for create_subscription()."""

    def __init__(
        self, name: str = "fake_stripe", *,
        methods: frozenset[PaymentMethod] = frozenset({PaymentMethod.CREDIT_CARD}),
        types: frozenset[PaymentType] = frozenset({PaymentType.ONE_TIME, PaymentType.RECURRING}),
        behavior: str = "success",
        cancel_behavior: str = "success",
    ) -> None:
        self._name = name
        self._methods = methods
        self._types = types
        self.behavior = behavior
        self.cancel_behavior = cancel_behavior
        self.create_subscription_calls: list[dict] = []
        self.create_intent_calls: list[dict] = []
        self.cancel_subscription_calls: list[str] = []

    @property
    def provider_name(self) -> str:
        return self._name

    @property
    def supported_payment_methods(self) -> frozenset[PaymentMethod]:
        return self._methods

    @property
    def supported_payment_types(self) -> frozenset[PaymentType]:
        return self._types

    async def create_intent(self, *, amount, currency, user_id, idempotency_key, metadata=None) -> PaymentIntent:
        self.create_intent_calls.append({"idempotency_key": idempotency_key})
        return PaymentIntent(
            id=f"ext-{self._name}-{idempotency_key}", provider=self._name, amount=amount,
            currency=currency.upper(), status=PaymentStatus.PENDING, idempotency_key=idempotency_key,
        )

    async def get_intent(self, intent_id: str) -> PaymentIntent:
        raise NotImplementedError

    async def verify_webhook(self, payload: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        raise NotImplementedError

    async def refund(self, payment_id: str, **kwargs) -> PaymentResult:
        raise NotImplementedError

    async def create_subscription(self, *, amount, currency, user_id, idempotency_key, metadata=None) -> SubscriptionIntent:
        self.create_subscription_calls.append({"idempotency_key": idempotency_key, "amount": amount})
        if self.behavior == "rejected":
            raise _FakeRejected(f"{self._name} rejected the subscription creation")
        if self.behavior == "unavailable":
            raise ProviderUnavailable(f"{self._name} timed out")
        return SubscriptionIntent(
            id=f"setup-{self._name}-{idempotency_key}", provider=self._name, status=PaymentStatus.PENDING,
            idempotency_key=idempotency_key, provider_subscription_id=None,
        )

    async def cancel_subscription(self, provider_subscription_id: str) -> None:
        self.cancel_subscription_calls.append(provider_subscription_id)
        if self.cancel_behavior == "rejected":
            raise _FakeRejected(f"{self._name} rejected the cancellation")
        if self.cancel_behavior == "unavailable":
            raise ProviderUnavailable(f"{self._name} timed out during cancellation")
        return None

    async def retrieve_subscription(self, provider_subscription_id: str) -> SubscriptionIntent:
        raise NotImplementedError

    async def update_subscription(self, provider_subscription_id: str, *, metadata: dict) -> SubscriptionIntent:
        raise NotImplementedError

    async def verify_subscription_webhook(self, payload: bytes, headers: Mapping[str, str]) -> SubscriptionWebhookEvent:
        raise NotImplementedError


class FakeOneTimeOnlyProvider(FakeSubProvider):
    """Mercado-Pago-like fake — ONE_TIME only, no RECURRING (used to
    prove the capability filter genuinely excludes a non-recurring
    provider from subscription creation)."""

    def __init__(self, name: str = "fake_mercadopago") -> None:
        super().__init__(name=name, methods=frozenset({PaymentMethod.PIX}), types=frozenset({PaymentType.ONE_TIME}))

    async def create_subscription(self, **kwargs) -> SubscriptionIntent:
        raise AssertionError("create_subscription() must never be called on a non-RECURRING provider")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def user(db):
    u = User(email=f"stripe-sub-{uuid.uuid4()}@test.com", hashed_password="x", plan="start")
    db.add(u)
    db.commit()
    return u


@pytest.fixture
def svc(db):
    return SubscriptionService(db)


def _registry(*providers: PaymentProvider) -> ProviderRegistry:
    reg = ProviderRegistry()
    for p in providers:
        reg.register(p)
    return reg


def _make_subscription(svc, user, *, provider="fake_stripe", plan="pro", amount=Decimal("29.99")) -> Subscription:
    return svc.create_subscription(
        user_id=user.id, plan=plan, provider=provider, payment_method=PaymentMethod.CREDIT_CARD,
        currency="USD", amount=amount,
    )


def _created_event(
    *, provider_ref: str, provider_subscription_id: str, event_id: str = "evt_created_1",
    occurred_at: datetime | None = None,
) -> SubscriptionWebhookEvent:
    return SubscriptionWebhookEvent(
        kind=SubscriptionEventKind.SUBSCRIPTION_CREATED, provider="fake_stripe",
        provider_subscription_id=provider_subscription_id, event_id=event_id,
        occurred_at=occurred_at or datetime.now(timezone.utc), provider_ref=provider_ref,
    )


def _renewal_event(
    *, kind: SubscriptionEventKind, provider_subscription_id: str, invoice_ref: str,
    event_id: str = "evt_renewal_1", occurred_at: datetime | None = None,
    amount: Decimal | None = Decimal("29.99"), currency: str | None = "USD",
) -> SubscriptionWebhookEvent:
    return SubscriptionWebhookEvent(
        kind=kind, provider="fake_stripe", provider_subscription_id=provider_subscription_id,
        event_id=event_id, occurred_at=occurred_at or datetime.now(timezone.utc), provider_ref=invoice_ref,
        amount=amount, currency=currency,
    )


# ---------------------------------------------------------------------------
# R01-R04 — capability + eligibility
# ---------------------------------------------------------------------------


async def test_R01_R02_R03_router_selects_only_recurring_capable_provider(db, svc, user):
    stripe_like = FakeSubProvider(name="fake_stripe")
    mp_like = FakeOneTimeOnlyProvider(name="fake_mercadopago")
    registry = _registry(mp_like, stripe_like)

    sub = _make_subscription(svc, user, provider="fake_stripe")
    result_sub, intent = await route_subscription_creation(db, registry, subscription_id=sub.id, idempotency_key="k1")

    assert result_sub.provider == "fake_stripe"
    assert len(stripe_like.create_subscription_calls) == 1
    # FakeOneTimeOnlyProvider.create_subscription() raises AssertionError if
    # ever called — it wasn't, so mercado-pago-like never touched.


async def test_R04_no_eligible_provider_when_none_declare_recurring(db, svc, user):
    mp_like = FakeOneTimeOnlyProvider()
    registry = _registry(mp_like)
    sub = _make_subscription(svc, user, provider="fake_mercadopago")

    with pytest.raises(NoEligibleProvider):
        await route_subscription_creation(db, registry, subscription_id=sub.id, idempotency_key="k1")


# ---------------------------------------------------------------------------
# R05 — Subscription row creation idempotency
# ---------------------------------------------------------------------------


def test_R05_create_subscription_row_idempotent(db, svc, user):
    key = str(uuid.uuid4())
    first = svc.create_subscription(
        user_id=user.id, plan="pro", provider="fake_stripe", payment_method=PaymentMethod.CREDIT_CARD,
        currency="USD", amount=Decimal("29.99"), idempotency_key=key,
    )
    second = svc.create_subscription(
        user_id=user.id, plan="pro", provider="fake_stripe", payment_method=PaymentMethod.CREDIT_CARD,
        currency="USD", amount=Decimal("29.99"), idempotency_key=key,
    )
    assert first.id == second.id
    assert db.query(Subscription).filter(Subscription.user_id == user.id).count() == 1


# ---------------------------------------------------------------------------
# R06-R09 — external creation idempotency / concurrency / timeout
# ---------------------------------------------------------------------------


async def test_R06_concurrent_creation_converges_to_one_external_call(db, svc, user):
    provider = FakeSubProvider()
    registry = _registry(provider)
    sub = _make_subscription(svc, user)

    # Sequential stand-in for "concurrent" — same precedent as every
    # other MPC/SC test in this codebase (see subscription_service.py's
    # own module docstring).
    r1, i1 = await svc.initiate_external_subscription(registry, sub.id, idempotency_key="create-1")
    r2, i2 = await svc.initiate_external_subscription(registry, sub.id, idempotency_key="create-1")

    assert len(provider.create_subscription_calls) == 1
    assert r1.provider_setup_intent_id == r2.provider_setup_intent_id == i1.id


async def test_R07_retry_after_success_is_noop(db, svc, user):
    provider = FakeSubProvider()
    registry = _registry(provider)
    sub = _make_subscription(svc, user)

    await svc.initiate_external_subscription(registry, sub.id, idempotency_key="create-1")
    await svc.initiate_external_subscription(registry, sub.id, idempotency_key="create-1")
    await svc.initiate_external_subscription(registry, sub.id, idempotency_key="create-1")

    assert len(provider.create_subscription_calls) == 1


async def test_R08_R20_timeout_leaves_identities_null_and_status_unchanged(db, svc, user):
    provider = FakeSubProvider(behavior="unavailable")
    registry = _registry(provider)
    sub = _make_subscription(svc, user)
    original_status = sub.status

    with pytest.raises(ProviderUnavailable):
        await svc.initiate_external_subscription(registry, sub.id, idempotency_key="create-1")

    db.refresh(sub)
    assert sub.provider_setup_intent_id is None
    assert sub.provider_subscription_id is None
    assert sub.status == original_status  # completely untouched


async def test_R09_retry_after_ambiguous_outcome_creates_exactly_one(db, svc, user):
    provider = FakeSubProvider(behavior="unavailable")
    registry = _registry(provider)
    sub = _make_subscription(svc, user)

    with pytest.raises(ProviderUnavailable):
        await svc.initiate_external_subscription(registry, sub.id, idempotency_key="create-1")

    provider.behavior = "success"  # simulate the provider recovering
    result, intent = await svc.initiate_external_subscription(registry, sub.id, idempotency_key="create-1")

    # The failed attempt DID reach the provider (ambiguous outcome, never
    # auto-retried) and the recovered retry reaches it again — 2 calls
    # total — but only ONE of them ever SUCCEEDED, so exactly one
    # external subscription setup identity is ever recorded.
    assert len(provider.create_subscription_calls) == 2
    assert result.provider_setup_intent_id == intent.id

    # A THIRD call (another retry) must now be a pure no-op — the
    # successful setup intent is already persisted.
    await svc.initiate_external_subscription(registry, sub.id, idempotency_key="create-1")
    assert len(provider.create_subscription_calls) == 2


# ---------------------------------------------------------------------------
# R10 — cancellation vs. webhook-driven renewal
# ---------------------------------------------------------------------------


async def test_R10_cancelled_subscription_rejects_late_renewal_webhook(db, svc, user):
    provider = FakeSubProvider()
    registry = _registry(provider)
    sub = _make_subscription(svc, user)
    sub.provider_subscription_id = "sub_ext_1"
    db.commit()

    await svc.cancel_subscription(registry, sub.id, user_id=user.id)
    assert provider.cancel_subscription_calls == ["sub_ext_1"]

    event = _renewal_event(
        kind=SubscriptionEventKind.RENEWAL_SUCCEEDED, provider_subscription_id="sub_ext_1", invoice_ref="in_1",
    )
    with pytest.raises(SubscriptionNotRenewable):
        svc.apply_subscription_webhook_event(event)

    assert db.query(LedgerEntry).count() == 0
    assert db.query(PaymentOrder).count() == 0


# ---------------------------------------------------------------------------
# R11-R12 — SUBSCRIPTION_UPDATED no-op / SUBSCRIPTION_CREATED confirmation
# ---------------------------------------------------------------------------


def test_R11_subscription_updated_is_logged_noop(db, svc, user):
    sub = _make_subscription(svc, user)
    sub.provider_subscription_id = "sub_ext_1"
    db.commit()

    event = SubscriptionWebhookEvent(
        kind=SubscriptionEventKind.SUBSCRIPTION_UPDATED, provider="fake_stripe",
        provider_subscription_id="sub_ext_1", event_id="evt_upd_1", occurred_at=datetime.now(timezone.utc),
    )
    result = svc.apply_subscription_webhook_event(event)
    assert result.status == SubscriptionStatus.ACTIVE.value
    assert result.last_subscription_event_id == "evt_upd_1"


def test_R12_subscription_created_confirms_exactly_once(db, svc, user):
    sub = _make_subscription(svc, user)
    sub.provider_setup_intent_id = "cs_setup_1"
    db.commit()

    event = _created_event(provider_ref="cs_setup_1", provider_subscription_id="sub_ext_1")
    result = svc.confirm_external_subscription(event)
    assert result.provider_subscription_id == "sub_ext_1"

    # A second, DIFFERENT delivery must never overwrite it.
    event2 = _created_event(
        provider_ref="cs_setup_1", provider_subscription_id="sub_ext_DIFFERENT", event_id="evt_created_2",
    )
    result2 = svc.confirm_external_subscription(event2)
    assert result2.provider_subscription_id == "sub_ext_1"  # unchanged


# ---------------------------------------------------------------------------
# R14-R15 — duplicate / out-of-order webhook
# ---------------------------------------------------------------------------


def test_R14_duplicate_webhook_absorbed(db, svc, user):
    sub = _make_subscription(svc, user)
    sub.provider_setup_intent_id = "cs_setup_1"
    db.commit()

    event = _created_event(provider_ref="cs_setup_1", provider_subscription_id="sub_ext_1", event_id="evt_dup")
    r1 = svc.confirm_external_subscription(event)
    r2 = svc.confirm_external_subscription(event)  # EXACT same event_id, redelivered

    assert r1.provider_subscription_id == r2.provider_subscription_id == "sub_ext_1"
    assert r2.last_subscription_event_id == "evt_dup"


def test_R15_out_of_order_webhook_absorbed(db, svc, user):
    sub = _make_subscription(svc, user)
    sub.provider_subscription_id = "sub_ext_1"
    db.commit()

    now = datetime.now(timezone.utc)
    newer = SubscriptionWebhookEvent(
        kind=SubscriptionEventKind.SUBSCRIPTION_UPDATED, provider="fake_stripe",
        provider_subscription_id="sub_ext_1", event_id="evt_newer", occurred_at=now,
    )
    older = SubscriptionWebhookEvent(
        kind=SubscriptionEventKind.SUBSCRIPTION_UPDATED, provider="fake_stripe",
        provider_subscription_id="sub_ext_1", event_id="evt_older", occurred_at=now - timedelta(minutes=5),
    )

    svc.apply_subscription_webhook_event(newer)
    result = svc.apply_subscription_webhook_event(older)  # arrives LATE, SAME stream

    assert result.last_subscription_event_id == "evt_newer"  # NOT overwritten by the older one


def test_R15b_cross_stream_ordering_regression(db, svc, user):
    """Regression test for the functional-audit finding: a single GLOBAL
    ordering timestamp let a more-recently-applied SUBSCRIPTION_UPDATED
    (Subscription-object stream) silently absorb a genuine, never-before-
    seen RENEWAL_SUCCEEDED (Invoice-object stream) just because the
    invoice event's OWN timestamp was earlier. Reproduced with a
    throwaway script during the audit; the fix splits the ordering guard
    into two independent per-stream columns (see Subscription model's
    own docstring and migration 0028).

    Scenario exactly as specified by the audit:
      1. SUBSCRIPTION_UPDATED with timestamp T2.
      2. RENEWAL_SUCCEEDED, legitimate, timestamp T1 < T2, DIFFERENT
         event_id, from the OTHER (invoice) stream.
      3. BOTH must be processed correctly — the invoice event must NOT
         be absorbed as stale.
      4. A genuinely stale event from the SAME (invoice) stream must
         still be correctly rejected/absorbed.
    """
    sub = _make_subscription(svc, user, plan="pro")
    sub.provider_subscription_id = "sub_ext_1"
    db.commit()

    t_later = datetime.now(timezone.utc)
    t_earlier = t_later - timedelta(seconds=5)

    # 1. SUBSCRIPTION_UPDATED at T2 (later), subscription-object stream.
    updated_event = SubscriptionWebhookEvent(
        kind=SubscriptionEventKind.SUBSCRIPTION_UPDATED, provider="fake_stripe",
        provider_subscription_id="sub_ext_1", event_id="evt_updated", occurred_at=t_later,
    )
    svc.apply_subscription_webhook_event(updated_event)

    # 2. RENEWAL_SUCCEEDED at T1 < T2, invoice-object stream, genuinely new.
    renewal_event = _renewal_event(
        kind=SubscriptionEventKind.RENEWAL_SUCCEEDED, provider_subscription_id="sub_ext_1",
        invoice_ref="in_real_charge_1", event_id="evt_renewal_real", occurred_at=t_earlier,
    )
    result = svc.apply_subscription_webhook_event(renewal_event)

    # 3. Must have been processed — NOT absorbed as stale.
    assert result.status == SubscriptionStatus.ACTIVE.value
    assert db.query(PaymentOrder).count() == 1
    assert db.query(LedgerEntry).count() == 2
    db.refresh(user)
    assert user.plan == "pro"
    # Both streams' markers independently reflect what was actually applied.
    assert result.last_subscription_event_id == "evt_updated"
    assert result.last_invoice_event_id == "evt_renewal_real"

    # 4. A genuinely STALE event of the SAME (invoice) stream — arriving
    # even earlier than the one just applied — must still be rejected.
    stale_renewal = _renewal_event(
        kind=SubscriptionEventKind.RENEWAL_SUCCEEDED, provider_subscription_id="sub_ext_1",
        invoice_ref="in_stale_charge", event_id="evt_renewal_stale", occurred_at=t_earlier - timedelta(seconds=5),
    )
    svc.apply_subscription_webhook_event(stale_renewal)
    assert db.query(PaymentOrder).count() == 1  # unchanged — stale event absorbed
    assert db.query(LedgerEntry).count() == 2


# ---------------------------------------------------------------------------
# R16-R19 — renewal: order/ledger/entitlement singularity, explicit failure
# ---------------------------------------------------------------------------


def test_R16_R17_R18_renewal_succeeded_singular_order_ledger_entitlement(db, svc, user):
    sub = _make_subscription(svc, user, plan="pro")
    sub.provider_subscription_id = "sub_ext_1"
    db.commit()

    event1 = _renewal_event(
        kind=SubscriptionEventKind.RENEWAL_SUCCEEDED, provider_subscription_id="sub_ext_1",
        invoice_ref="in_cycle_1", event_id="evt_r1",
    )
    result1 = svc.apply_subscription_webhook_event(event1)
    assert result1.status == SubscriptionStatus.ACTIVE.value
    db.refresh(user)
    assert user.plan == "pro"
    assert db.query(PaymentOrder).count() == 1
    assert db.query(LedgerEntry).count() == 2

    # SAME invoice, a DIFFERENT top-level event_id (Stripe's own
    # at-least-once redelivery under a new delivery id) — must still
    # resolve to the SAME PaymentOrder, no new ledger pair.
    event2 = _renewal_event(
        kind=SubscriptionEventKind.RENEWAL_SUCCEEDED, provider_subscription_id="sub_ext_1",
        invoice_ref="in_cycle_1", event_id="evt_r1_redelivered", occurred_at=datetime.now(timezone.utc),
    )
    svc.apply_subscription_webhook_event(event2)
    assert db.query(PaymentOrder).count() == 1
    assert db.query(LedgerEntry).count() == 2
    db.refresh(user)
    assert user.plan == "pro"


def test_R19_renewal_failed_moves_to_past_due(db, svc, user):
    sub = _make_subscription(svc, user)
    sub.provider_subscription_id = "sub_ext_1"
    db.commit()

    event = _renewal_event(
        kind=SubscriptionEventKind.RENEWAL_FAILED, provider_subscription_id="sub_ext_1", invoice_ref="in_fail_1",
    )
    result = svc.apply_subscription_webhook_event(event)
    assert result.status == SubscriptionStatus.PAST_DUE.value
    assert result.consecutive_failed_cycles == 1


# ---------------------------------------------------------------------------
# R21 — coexistence with the ordinary one-time checkout path
# ---------------------------------------------------------------------------


async def test_R21_coexists_with_one_time_checkout(db, svc, user):
    from src.billing.payment_router import route_checkout

    provider = FakeSubProvider()
    registry = _registry(provider)
    sub = _make_subscription(svc, user)

    order, intent = await route_checkout(
        db, registry, user_id=user.id, plan=None, amount_fiat=Decimal("5.00"), currency_fiat="USD",
        credits_amount=Decimal("5.00"), currency_credits="USD", idempotency_key="onetime-1",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        payment_method=PaymentMethod.CREDIT_CARD, payment_type=PaymentType.ONE_TIME,
    )
    assert order.provider == "fake_stripe"
    assert len(provider.create_intent_calls) == 1

    result_sub, sub_intent = await route_subscription_creation(
        db, registry, subscription_id=sub.id, idempotency_key="sub-1",
    )
    assert result_sub.provider == "fake_stripe"
    assert len(provider.create_subscription_calls) == 1
    # Each capability used its OWN method — no cross-contamination.


# ---------------------------------------------------------------------------
# R22-R26 — real external cancellation (post-commit hardening)
# ---------------------------------------------------------------------------


async def test_R22_cancel_success_calls_provider_before_local_state_change(db, svc, user):
    provider = FakeSubProvider()
    registry = _registry(provider)
    sub = _make_subscription(svc, user)
    sub.provider_subscription_id = "sub_ext_1"
    db.commit()

    result = await svc.cancel_subscription(registry, sub.id, user_id=user.id)

    assert provider.cancel_subscription_calls == ["sub_ext_1"]
    assert result.status == SubscriptionStatus.CANCELLED.value
    assert result.cancelled_at is not None


async def test_R23_cancel_provider_unavailable_never_fakes_success(db, svc, user):
    provider = FakeSubProvider(cancel_behavior="unavailable")
    registry = _registry(provider)
    sub = _make_subscription(svc, user)
    sub.provider_subscription_id = "sub_ext_1"
    db.commit()
    original_status = sub.status

    with pytest.raises(ProviderUnavailable):
        await svc.cancel_subscription(registry, sub.id, user_id=user.id)

    db.refresh(sub)
    assert sub.status == original_status  # NEVER faked as cancelled
    assert sub.cancelled_at is None

    # Retry after the provider recovers succeeds normally — no duplicate
    # external call is required to be avoided here (cancelling is
    # naturally idempotent on the provider's own side; this proves OUR
    # side doesn't get stuck).
    provider.cancel_behavior = "success"
    result = await svc.cancel_subscription(registry, sub.id, user_id=user.id)
    assert result.status == SubscriptionStatus.CANCELLED.value


async def test_R24_cancel_explicit_rejection_never_fakes_success(db, svc, user):
    provider = FakeSubProvider(cancel_behavior="rejected")
    registry = _registry(provider)
    sub = _make_subscription(svc, user)
    sub.provider_subscription_id = "sub_ext_1"
    db.commit()

    with pytest.raises(PaymentCreationRejected):
        await svc.cancel_subscription(registry, sub.id, user_id=user.id)

    db.refresh(sub)
    assert sub.status == SubscriptionStatus.ACTIVE.value  # unchanged


async def test_R25_cancel_without_provider_subscription_id_is_local_only(db, svc, user):
    """A Subscription whose external mandate was never confirmed (still
    just a setup attempt, or never even attempted) has nothing to cancel
    at the provider — cancellation must succeed locally WITHOUT ever
    calling the adapter."""
    provider = FakeSubProvider()
    registry = _registry(provider)
    sub = _make_subscription(svc, user)  # provider_subscription_id is None
    assert sub.provider_subscription_id is None

    result = await svc.cancel_subscription(registry, sub.id, user_id=user.id)

    assert provider.cancel_subscription_calls == []  # adapter never called
    assert result.status == SubscriptionStatus.CANCELLED.value


async def test_R26_cancel_idempotent_and_later_webhook_confirmation(db, svc, user):
    """Retry/idempotency: cancelling twice never calls the provider
    twice. A LATER webhook confirming the same external cancellation
    (SUBSCRIPTION_CANCELED) must be absorbed as a no-op, never raising
    and never re-deriving a different cancelled_at."""
    provider = FakeSubProvider()
    registry = _registry(provider)
    sub = _make_subscription(svc, user)
    sub.provider_subscription_id = "sub_ext_1"
    db.commit()

    first = await svc.cancel_subscription(registry, sub.id, user_id=user.id)
    second = await svc.cancel_subscription(registry, sub.id, user_id=user.id)

    assert provider.cancel_subscription_calls == ["sub_ext_1"]  # only ONE external call
    assert second.cancelled_at == first.cancelled_at

    # A later-arriving webhook for the SAME cancellation is absorbed.
    event = SubscriptionWebhookEvent(
        kind=SubscriptionEventKind.SUBSCRIPTION_CANCELED, provider="fake_stripe",
        provider_subscription_id="sub_ext_1", event_id="evt_cancel_confirm",
        occurred_at=datetime.now(timezone.utc),
    )
    result = svc.apply_subscription_webhook_event(event)
    assert result.status == SubscriptionStatus.CANCELLED.value
    assert result.cancelled_at == first.cancelled_at  # not overwritten


# ===========================================================================
# Part C — Integration: full fake-transport flow with the REAL StripeAdapter
# ===========================================================================


def _hosted_checkout_transport(*, session_id: str = "cs_int_1") -> FakeTransport:
    t = FakeTransport()
    t.queue_json(200, {"id": session_id, "url": f"https://checkout.stripe.com/c/pay/{session_id}"})
    return t


async def test_I01_full_stripe_recurring_flow_with_fake_transport(db, svc, user):
    webhook_secret = "whsec_integration_test"
    transport = _hosted_checkout_transport(session_id="cs_int_1")
    adapter = StripeAdapter(
        secret_key="sk_test_TEST", webhook_secret=webhook_secret,
        base_url="https://api.stripe.com", transport=transport,
    )
    registry = _registry(adapter)

    sub = svc.create_subscription(
        user_id=user.id, plan="pro", provider="stripe", payment_method=PaymentMethod.CREDIT_CARD,
        currency="USD", amount=Decimal("29.99"),
    )

    result_sub, intent = await svc.initiate_external_subscription(registry, sub.id, idempotency_key="int-create-1")
    assert result_sub.provider_setup_intent_id == "cs_int_1"
    assert result_sub.provider_subscription_id is None

    created_body = json.dumps(_checkout_completed_subscription_event(
        event_id="evt_int_created", session_id="cs_int_1", subscription_id="sub_int_1",
    )).encode("utf-8")
    created_headers = _webhook_headers(webhook_secret, body=created_body)
    created_event = await adapter.verify_subscription_webhook(created_body, created_headers)

    confirmed_sub = svc.apply_subscription_webhook_event(created_event)
    assert confirmed_sub.provider_subscription_id == "sub_int_1"

    invoice_body = json.dumps(_invoice_event(
        event_id="evt_int_invoice", invoice_id="in_int_1", subscription_id="sub_int_1",
        amount_paid=2999, currency="usd",
    )).encode("utf-8")
    invoice_headers = _webhook_headers(webhook_secret, body=invoice_body)
    invoice_event = await adapter.verify_subscription_webhook(invoice_body, invoice_headers)

    final_sub = svc.apply_subscription_webhook_event(invoice_event)
    assert final_sub.status == SubscriptionStatus.ACTIVE.value
    assert final_sub.current_period_start is not None

    db.refresh(user)
    assert user.plan == "pro"
    assert db.query(PaymentOrder).filter(PaymentOrder.subscription_id == sub.id).count() == 1
    assert db.query(LedgerEntry).count() == 2

    order = db.query(PaymentOrder).filter(PaymentOrder.subscription_id == sub.id).first()
    assert order.status == "confirmed"
    assert order.payment_type == PaymentType.RECURRING.value
