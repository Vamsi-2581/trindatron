"""Unit tests for StripeAdapter —
packages/core/src/billing/providers/stripe_adapter.py.

All tests use a FakeTransport (StripeTransport) — no real network call
is ever made, same mandatory rule as every other adapter's own test
file in this codebase.

Full end-to-end wiring (checkout -> processing -> webhook -> plan
activation, duplicate webhook idempotency, concurrent duplicate,
foreign order, expired order) is covered at the HTTP level by
test_billing_checkout_stripe.py — not duplicated here. This file covers
only what's specific to the adapter itself: request construction
(including form-urlencoded bracket notation and minor-unit conversion),
response parsing, status mapping, Stripe-Signature verification, and
the registration guard.

Test matrix
-----------
S01) instantiation — missing secret_key/webhook_secret/base_url raise ValueError
S02) provider_name == "stripe"
S03) supported_payment_methods == {CREDIT_CARD, DEBIT_CARD, BOLETO} (no PIX)
S04) supported_payment_types == {ONE_TIME} (no RECURRING — see module docstring)
S05) create_intent — well-formed form-encoded request (mode=payment,
     Idempotency-Key header, line_items minor-unit amount, client_reference_id)
S06) create_intent — success response parsed (id = Stripe's own session id,
     redirect_url = url)
S07) create_intent — non-2xx/missing url|id response raises StripeError
S08) create_intent — transport timeout raises ProviderUnavailable
S09) create_intent — payment_method="credit_card"/"debit_card" both map
     to payment_method_types=["card"] (Stripe has no separate debit type)
S10) create_intent — payment_method="boleto" maps to payment_method_types=["boleto"]
S11) create_intent — no payment_method in metadata -> no payment_method_types sent
S12) create_intent — zero-decimal currency (JPY) never multiplied by 100
S13) get_intent — parses session response (payment_status="paid" -> CONFIRMED)
S14) get_intent — payment_status="unpaid" -> PROCESSING
S15) get_intent — 404 -> PaymentIntentNotFound
S16) get_intent — non-2xx response raises StripeError
S17) verify_webhook — checkout.session.completed paid -> CONFIRMED
S18) verify_webhook — checkout.session.completed unpaid -> PROCESSING
S19) verify_webhook — async_payment_succeeded -> CONFIRMED
S20) verify_webhook — async_payment_failed -> FAILED
S21) verify_webhook — session.expired -> EXPIRED
S22) verify_webhook — unmapped/dispute event type -> UnknownWebhookEvent,
     no order ever touched
S23) verify_webhook — missing Stripe-Signature header -> InvalidWebhookSignature
S24) verify_webhook — malformed signature header (no v1) -> InvalidWebhookSignature
S25) verify_webhook — tampered/wrong-secret signature -> InvalidWebhookSignature
     (spoofing attempt)
S26) verify_webhook — timestamp outside tolerance window -> InvalidWebhookSignature
     (replay-window rejection)
S27) verify_webhook — provider_ref = session.id, payment_id = session.payment_intent
S28) verify_webhook — nonce is the Event's own top-level id
S29) verify_webhook — malformed JSON payload raises StripeError
S30) refund() — full refund (no GET needed, no amount in body)
S31) refund() — partial refund fetches PaymentIntent for currency, converts
     Decimal to minor units correctly
S32) refund() — idempotency key deterministic per (payment_id, amount)
S33) refund() — non-2xx response raises StripeError
S34) build_stripe_adapter — disabled -> None
S35) build_stripe_adapter — missing credentials -> None
S36) build_stripe_adapter — fully configured -> registers into ProviderRegistry
S37) no secret key / webhook secret ever appears in log output
S38) _flatten_form_params — nested dict/list bracket notation round-trips exactly
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from src.billing.providers.base import (
    InvalidWebhookSignature,
    PaymentIntentNotFound,
    PaymentMethod,
    PaymentStatus,
    PaymentType,
    ProviderUnavailable,
    UnknownWebhookEvent,
)
from src.billing.providers.registry import ProviderRegistry
from src.billing.providers.stripe_adapter import (
    StripeAdapter,
    StripeError,
    _encode_form,
    build_stripe_adapter,
)


# ---------------------------------------------------------------------------
# Test doubles / helpers
# ---------------------------------------------------------------------------


class FakeTransport:
    """FIFO queue of canned responses/exceptions — never opens a socket."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._queue: list[tuple[int, bytes] | Exception] = []

    def queue_json(self, status_code: int, body: dict) -> None:
        self._queue.append((status_code, json.dumps(body).encode("utf-8")))

    def queue_raw(self, status_code: int, raw: bytes) -> None:
        self._queue.append((status_code, raw))

    def queue_exception(self, exc: Exception) -> None:
        self._queue.append(exc)

    async def post(self, url, *, headers, body, timeout):
        self.calls.append({"method": "POST", "url": url, "headers": headers, "body": body, "timeout": timeout})
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


def _decoded_body(call: dict) -> dict:
    """Decode a form-urlencoded call body into a flat {key: value} map
    (bracket-notation keys kept literal, e.g. "line_items[0][quantity]")."""
    parsed = parse_qs(call["body"].decode("utf-8"))
    return {k: v[0] for k, v in parsed.items()}


def _make_adapter(transport: FakeTransport, *, webhook_secret: str = "whsec_test", **overrides) -> StripeAdapter:
    kwargs = dict(
        secret_key="sk_test_TEST",
        webhook_secret=webhook_secret,
        base_url="https://api.stripe.com",
        transport=transport,
    )
    kwargs.update(overrides)
    return StripeAdapter(**kwargs)


def _session_success(*, session_id: str = "cs_test_123", url: str = "https://checkout.stripe.com/c/pay/cs_test_123") -> dict:
    return {"id": session_id, "url": url, "object": "checkout.session"}


def _session_object(
    *,
    session_id: str = "cs_test_123",
    payment_status: str = "paid",
    amount_total: int = 2999,
    currency: str = "usd",
    payment_intent: str = "pi_test_123",
) -> dict:
    return {
        "id": session_id, "object": "checkout.session", "payment_status": payment_status,
        "amount_total": amount_total, "currency": currency, "payment_intent": payment_intent,
        "url": "https://checkout.stripe.com/c/pay/cs_test_123",
    }


def _sign(secret: str, *, ts: str, body: bytes) -> str:
    signed_payload = f"{ts}.".encode("utf-8") + body
    return hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()


def _webhook_headers(secret: str, *, body: bytes, ts: str | None = None) -> dict:
    ts = ts or str(int(time.time()))
    v1 = _sign(secret, ts=ts, body=body)
    return {"stripe-signature": f"t={ts},v1={v1}"}


def _event(*, event_id: str = "evt_123", event_type: str = "checkout.session.completed", session: dict | None = None) -> dict:
    return {"id": event_id, "type": event_type, "data": {"object": session or _session_object()}}


# ---------------------------------------------------------------------------
# S01-S04 — instantiation / capabilities
# ---------------------------------------------------------------------------


def test_S01_missing_required_fields_raise():
    transport = FakeTransport()
    with pytest.raises(ValueError):
        StripeAdapter(secret_key="", webhook_secret="whsec", base_url="https://api.stripe.com", transport=transport)
    with pytest.raises(ValueError):
        StripeAdapter(secret_key="sk", webhook_secret="", base_url="https://api.stripe.com", transport=transport)
    with pytest.raises(ValueError):
        StripeAdapter(secret_key="sk", webhook_secret="whsec", base_url="", transport=transport)


def test_S02_provider_name():
    adapter = _make_adapter(FakeTransport())
    assert adapter.provider_name == "stripe"


def test_S03_supported_payment_methods_no_pix():
    adapter = _make_adapter(FakeTransport())
    assert adapter.supported_payment_methods == frozenset({
        PaymentMethod.CREDIT_CARD, PaymentMethod.DEBIT_CARD, PaymentMethod.BOLETO,
    })
    assert PaymentMethod.PIX not in adapter.supported_payment_methods


def test_S04_supported_payment_types_one_time_and_recurring():
    """Stripe Recurring etapa: RECURRING is now real —
    create_subscription()/cancel_subscription()/retrieve_subscription()/
    update_subscription()/verify_subscription_webhook() are all
    implemented (see test_stripe_subscription.py) — see this file's own
    module docstring for the confirmed mode="subscription" contract."""
    adapter = _make_adapter(FakeTransport())
    assert adapter.supported_payment_types == frozenset({PaymentType.ONE_TIME, PaymentType.RECURRING})


# ---------------------------------------------------------------------------
# S05-S12 — create_intent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_S05_create_intent_well_formed_request():
    transport = FakeTransport()
    transport.queue_json(200, _session_success())
    adapter = _make_adapter(transport, success_url="https://trindatron.example/success")

    await adapter.create_intent(amount=Decimal("29.99"), currency="USD", user_id="u1", idempotency_key="idem-1")

    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/v1/checkout/sessions")
    assert call["headers"]["Authorization"] == "Bearer sk_test_TEST"
    assert call["headers"]["Idempotency-Key"] == "idem-1"
    assert call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    body = _decoded_body(call)
    assert body["mode"] == "payment"
    assert body["line_items[0][price_data][unit_amount]"] == "2999"
    assert body["line_items[0][price_data][currency]"] == "usd"
    assert body["success_url"] == "https://trindatron.example/success"
    assert "client_reference_id" in body


@pytest.mark.asyncio
async def test_S06_create_intent_success_parses_intent():
    transport = FakeTransport()
    transport.queue_json(200, _session_success(session_id="cs_test_abc", url="https://checkout.stripe.com/c/pay/cs_test_abc"))
    adapter = _make_adapter(transport)

    intent = await adapter.create_intent(amount=Decimal("9.99"), currency="usd", user_id="u1", idempotency_key="idem-2")

    assert intent.id == "cs_test_abc"
    assert intent.redirect_url == "https://checkout.stripe.com/c/pay/cs_test_abc"
    assert intent.currency == "USD"
    assert intent.status == PaymentStatus.PENDING


@pytest.mark.asyncio
async def test_S07_create_intent_error_response_raises():
    transport = FakeTransport()
    transport.queue_json(400, {"error": {"message": "invalid request"}})
    adapter = _make_adapter(transport)

    with pytest.raises(StripeError):
        await adapter.create_intent(amount=Decimal("9.99"), currency="USD", user_id="u1", idempotency_key="idem-3")


@pytest.mark.asyncio
async def test_S08_create_intent_transport_timeout_raises_provider_unavailable():
    transport = FakeTransport()
    transport.queue_exception(ProviderUnavailable("timed out"))
    adapter = _make_adapter(transport)

    with pytest.raises(ProviderUnavailable):
        await adapter.create_intent(amount=Decimal("9.99"), currency="USD", user_id="u1", idempotency_key="idem-4")


@pytest.mark.parametrize("method", ["credit_card", "debit_card"])
@pytest.mark.asyncio
async def test_S09_card_methods_map_to_single_card_type(method):
    transport = FakeTransport()
    transport.queue_json(200, _session_success())
    adapter = _make_adapter(transport)

    await adapter.create_intent(
        amount=Decimal("9.99"), currency="USD", user_id="u1", idempotency_key="idem-5",
        metadata={"payment_method": method},
    )
    body = _decoded_body(transport.calls[0])
    assert body["payment_method_types[0]"] == "card"


@pytest.mark.asyncio
async def test_S10_boleto_maps_to_boleto_type():
    transport = FakeTransport()
    transport.queue_json(200, _session_success())
    adapter = _make_adapter(transport)

    await adapter.create_intent(
        amount=Decimal("9.99"), currency="BRL", user_id="u1", idempotency_key="idem-6",
        metadata={"payment_method": "boleto"},
    )
    body = _decoded_body(transport.calls[0])
    assert body["payment_method_types[0]"] == "boleto"


@pytest.mark.asyncio
async def test_S11_no_payment_method_sends_no_filter():
    transport = FakeTransport()
    transport.queue_json(200, _session_success())
    adapter = _make_adapter(transport)

    await adapter.create_intent(amount=Decimal("9.99"), currency="USD", user_id="u1", idempotency_key="idem-7")
    body = _decoded_body(transport.calls[0])
    assert not any(k.startswith("payment_method_types") for k in body)


@pytest.mark.asyncio
async def test_S12_zero_decimal_currency_not_multiplied():
    transport = FakeTransport()
    transport.queue_json(200, _session_success())
    adapter = _make_adapter(transport)

    await adapter.create_intent(amount=Decimal("500"), currency="JPY", user_id="u1", idempotency_key="idem-8")
    body = _decoded_body(transport.calls[0])
    assert body["line_items[0][price_data][unit_amount]"] == "500"  # NOT 50000


# ---------------------------------------------------------------------------
# S13-S16 — get_intent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_S13_get_intent_paid_confirmed():
    transport = FakeTransport()
    transport.queue_json(200, _session_object(payment_status="paid"))
    adapter = _make_adapter(transport)

    intent = await adapter.get_intent("cs_test_123")
    assert intent.status == PaymentStatus.CONFIRMED
    assert intent.amount == Decimal("29.99")
    assert intent.currency == "USD"


@pytest.mark.asyncio
async def test_S14_get_intent_unpaid_processing():
    transport = FakeTransport()
    transport.queue_json(200, _session_object(payment_status="unpaid"))
    adapter = _make_adapter(transport)

    intent = await adapter.get_intent("cs_test_123")
    assert intent.status == PaymentStatus.PROCESSING


@pytest.mark.asyncio
async def test_S15_get_intent_not_found():
    transport = FakeTransport()
    transport.queue_json(404, {"error": {"message": "No such checkout session"}})
    adapter = _make_adapter(transport)

    with pytest.raises(PaymentIntentNotFound):
        await adapter.get_intent("cs_test_missing")


@pytest.mark.asyncio
async def test_S16_get_intent_error_response_raises():
    transport = FakeTransport()
    transport.queue_json(401, {"error": {"message": "invalid api key"}})
    adapter = _make_adapter(transport)

    with pytest.raises(StripeError):
        await adapter.get_intent("cs_test_123")


# ---------------------------------------------------------------------------
# S17-S29 — verify_webhook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_S17_completed_paid_confirmed():
    secret = "whsec_test"
    adapter = _make_adapter(FakeTransport(), webhook_secret=secret)
    session = _session_object(payment_status="paid")
    body = json.dumps(_event(session=session)).encode("utf-8")
    headers = _webhook_headers(secret, body=body)

    event = await adapter.verify_webhook(body, headers)
    assert event.status == PaymentStatus.CONFIRMED


@pytest.mark.asyncio
async def test_S18_completed_unpaid_processing():
    secret = "whsec_test"
    adapter = _make_adapter(FakeTransport(), webhook_secret=secret)
    session = _session_object(payment_status="unpaid")
    body = json.dumps(_event(session=session)).encode("utf-8")
    headers = _webhook_headers(secret, body=body)

    event = await adapter.verify_webhook(body, headers)
    assert event.status == PaymentStatus.PROCESSING


@pytest.mark.asyncio
async def test_S19_async_payment_succeeded_confirmed():
    secret = "whsec_test"
    adapter = _make_adapter(FakeTransport(), webhook_secret=secret)
    body = json.dumps(_event(event_type="checkout.session.async_payment_succeeded")).encode("utf-8")
    headers = _webhook_headers(secret, body=body)

    event = await adapter.verify_webhook(body, headers)
    assert event.status == PaymentStatus.CONFIRMED


@pytest.mark.asyncio
async def test_S20_async_payment_failed():
    secret = "whsec_test"
    adapter = _make_adapter(FakeTransport(), webhook_secret=secret)
    body = json.dumps(_event(event_type="checkout.session.async_payment_failed")).encode("utf-8")
    headers = _webhook_headers(secret, body=body)

    event = await adapter.verify_webhook(body, headers)
    assert event.status == PaymentStatus.FAILED


@pytest.mark.asyncio
async def test_S21_session_expired():
    secret = "whsec_test"
    adapter = _make_adapter(FakeTransport(), webhook_secret=secret)
    body = json.dumps(_event(event_type="checkout.session.expired")).encode("utf-8")
    headers = _webhook_headers(secret, body=body)

    event = await adapter.verify_webhook(body, headers)
    assert event.status == PaymentStatus.EXPIRED


@pytest.mark.parametrize("event_type", ["charge.dispute.created", "charge.dispute.closed", "something.unmapped"])
@pytest.mark.asyncio
async def test_S22_unmapped_event_raises_unknown(event_type):
    secret = "whsec_test"
    adapter = _make_adapter(FakeTransport(), webhook_secret=secret)
    body = json.dumps(_event(event_type=event_type)).encode("utf-8")
    headers = _webhook_headers(secret, body=body)

    with pytest.raises(UnknownWebhookEvent):
        await adapter.verify_webhook(body, headers)


@pytest.mark.asyncio
async def test_S23_missing_signature_header_invalid():
    adapter = _make_adapter(FakeTransport(), webhook_secret="whsec_test")
    body = json.dumps(_event()).encode("utf-8")

    with pytest.raises(InvalidWebhookSignature):
        await adapter.verify_webhook(body, {})


@pytest.mark.asyncio
async def test_S24_malformed_signature_header_invalid():
    adapter = _make_adapter(FakeTransport(), webhook_secret="whsec_test")
    body = json.dumps(_event()).encode("utf-8")

    with pytest.raises(InvalidWebhookSignature):
        await adapter.verify_webhook(body, {"stripe-signature": "garbage-no-equals"})


@pytest.mark.asyncio
async def test_S25_tampered_signature_spoof_attempt():
    """Simulates a spoofing attempt: an attacker forges a webhook without
    knowing the real endpoint signing secret."""
    real_secret = "whsec_real"
    adapter = _make_adapter(FakeTransport(), webhook_secret=real_secret)
    body = json.dumps(_event()).encode("utf-8")
    forged_headers = _webhook_headers("whsec_wrong_guess", body=body)

    with pytest.raises(InvalidWebhookSignature):
        await adapter.verify_webhook(body, forged_headers)


@pytest.mark.asyncio
async def test_S26_stale_timestamp_outside_tolerance_rejected():
    secret = "whsec_test"
    adapter = _make_adapter(FakeTransport(), webhook_secret=secret, webhook_tolerance_seconds=300)
    body = json.dumps(_event()).encode("utf-8")
    stale_ts = str(int(time.time()) - 3600)  # 1 hour old — a captured, replayed payload
    headers = _webhook_headers(secret, body=body, ts=stale_ts)

    with pytest.raises(InvalidWebhookSignature):
        await adapter.verify_webhook(body, headers)


@pytest.mark.asyncio
async def test_S27_provider_ref_and_payment_id_from_session():
    secret = "whsec_test"
    adapter = _make_adapter(FakeTransport(), webhook_secret=secret)
    session = _session_object(session_id="cs_test_xyz", payment_intent="pi_test_xyz")
    body = json.dumps(_event(session=session)).encode("utf-8")
    headers = _webhook_headers(secret, body=body)

    event = await adapter.verify_webhook(body, headers)
    assert event.provider_ref == "cs_test_xyz"
    assert event.payment_id == "pi_test_xyz"


@pytest.mark.asyncio
async def test_S28_nonce_is_event_own_id():
    secret = "whsec_test"
    adapter = _make_adapter(FakeTransport(), webhook_secret=secret)
    body = json.dumps(_event(event_id="evt_unique_42")).encode("utf-8")
    headers = _webhook_headers(secret, body=body)

    event = await adapter.verify_webhook(body, headers)
    assert event.nonce == "evt_unique_42"


@pytest.mark.asyncio
async def test_S29_malformed_json_payload_raises():
    secret = "whsec_test"
    adapter = _make_adapter(FakeTransport(), webhook_secret=secret)
    body = b"not-json-at-all"
    headers = _webhook_headers(secret, body=body)

    with pytest.raises(StripeError):
        await adapter.verify_webhook(body, headers)


# ---------------------------------------------------------------------------
# S30-S33 — refund
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_S30_full_refund_no_currency_lookup():
    transport = FakeTransport()
    transport.queue_json(200, {"id": "re_1", "payment_intent": "pi_1", "amount": 2999, "currency": "usd", "status": "succeeded"})
    adapter = _make_adapter(transport)

    result = await adapter.refund("pi_1")
    assert len(transport.calls) == 1  # no GET for currency lookup
    body = _decoded_body(transport.calls[0])
    assert "amount" not in body
    assert result.status == PaymentStatus.REFUNDED
    assert result.amount_gross == Decimal("29.99")


@pytest.mark.asyncio
async def test_S31_partial_refund_fetches_currency_and_converts():
    transport = FakeTransport()
    transport.queue_json(200, {"id": "pi_2", "currency": "brl", "status": "succeeded"})  # GET payment_intent
    transport.queue_json(200, {"id": "re_2", "payment_intent": "pi_2", "amount": 1000, "currency": "brl", "status": "succeeded"})
    adapter = _make_adapter(transport)

    result = await adapter.refund("pi_2", amount=Decimal("10.00"))
    assert transport.calls[0]["method"] == "GET"
    post_call = transport.calls[1]
    body = _decoded_body(post_call)
    assert body["amount"] == "1000"
    assert result.status == PaymentStatus.PARTIAL_REFUND
    assert result.currency == "BRL"


@pytest.mark.asyncio
async def test_S32_refund_idempotency_key_deterministic():
    transport = FakeTransport()
    transport.queue_json(200, {"id": "re_1", "payment_intent": "pi_1", "amount": 500, "currency": "usd", "status": "succeeded"})
    transport.queue_json(200, {"id": "re_1", "payment_intent": "pi_1", "amount": 500, "currency": "usd", "status": "succeeded"})
    adapter = _make_adapter(transport)

    await adapter.refund("pi_1")
    await adapter.refund("pi_1")
    key1 = transport.calls[0]["headers"]["Idempotency-Key"]
    key2 = transport.calls[1]["headers"]["Idempotency-Key"]
    assert key1 == key2


@pytest.mark.asyncio
async def test_S33_refund_error_response_raises():
    transport = FakeTransport()
    transport.queue_json(400, {"error": {"message": "charge already refunded"}})
    adapter = _make_adapter(transport)

    with pytest.raises(StripeError):
        await adapter.refund("pi_1")


# ---------------------------------------------------------------------------
# S34-S37 — factory / registry / secret hygiene
# ---------------------------------------------------------------------------


def test_S34_build_adapter_disabled_returns_none():
    settings = SimpleNamespace(
        stripe_enabled=False, stripe_secret_key="sk", stripe_webhook_secret="whsec",
        stripe_base_url="https://api.stripe.com", stripe_success_url="", stripe_cancel_url="",
        stripe_webhook_tolerance_seconds=300,
    )
    assert build_stripe_adapter(settings) is None


@pytest.mark.parametrize("secret_key,webhook_secret", [("", "whsec"), ("sk", "")])
def test_S35_build_adapter_missing_credentials_returns_none(secret_key, webhook_secret):
    settings = SimpleNamespace(
        stripe_enabled=True, stripe_secret_key=secret_key, stripe_webhook_secret=webhook_secret,
        stripe_base_url="https://api.stripe.com", stripe_success_url="", stripe_cancel_url="",
        stripe_webhook_tolerance_seconds=300,
    )
    assert build_stripe_adapter(settings) is None


def test_S36_build_adapter_fully_configured_registers():
    settings = SimpleNamespace(
        stripe_enabled=True, stripe_secret_key="sk_test_real-looking",
        stripe_webhook_secret="whsec_real-looking",
        stripe_base_url="https://api.stripe.com", stripe_success_url="", stripe_cancel_url="",
        stripe_webhook_tolerance_seconds=300,
    )
    adapter = build_stripe_adapter(settings)
    assert adapter is not None
    assert adapter.provider_name == "stripe"

    registry = ProviderRegistry()
    registry.register(adapter)
    assert registry.is_registered("stripe") is True
    assert registry.get("stripe") is adapter


@pytest.mark.asyncio
async def test_S37_no_secret_leaks_to_logs(caplog):
    secret_key = "SUPER-SECRET-STRIPE-KEY-VALUE"
    webhook_secret = "SUPER-SECRET-WEBHOOK-VALUE"
    transport = FakeTransport()
    transport.queue_json(200, _session_success())
    adapter = _make_adapter(transport, webhook_secret=webhook_secret, secret_key=secret_key)

    with caplog.at_level(logging.DEBUG):
        await adapter.create_intent(amount=Decimal("9.99"), currency="USD", user_id="u1", idempotency_key="idem-secret")

    for record in caplog.records:
        assert secret_key not in record.getMessage()
        assert webhook_secret not in record.getMessage()


# ---------------------------------------------------------------------------
# S38 — pure form-encoding helper
# ---------------------------------------------------------------------------


def test_S38_flatten_form_params_nested_round_trip():
    body = {
        "mode": "payment",
        "line_items": [
            {"price_data": {"currency": "usd", "unit_amount": 999}, "quantity": 1},
        ],
        "payment_method_types": ["card", "boleto"],
    }
    encoded = _encode_form(body).decode()
    decoded = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(encoded).items()}
    assert decoded["mode"] == "payment"
    assert decoded["line_items[0][price_data][currency]"] == "usd"
    assert decoded["line_items[0][price_data][unit_amount]"] == "999"
    assert decoded["line_items[0][quantity]"] == "1"
    assert decoded["payment_method_types[0]"] == "card"
    assert decoded["payment_method_types[1]"] == "boleto"
