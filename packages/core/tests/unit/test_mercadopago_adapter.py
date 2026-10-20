"""Unit tests for MercadoPagoAdapter —
packages/core/src/billing/providers/mercadopago_adapter.py.

All tests use a FakeTransport (MercadoPagoTransport) — no real network
call is ever made, same mandatory rule as
test_nowpayments_adapter.py/test_binance_pay_adapter.py.

Full end-to-end wiring (checkout -> processing -> webhook -> plan
activation, duplicate webhook idempotency, concurrent duplicate,
foreign order, expired order) is covered at the HTTP level by
test_billing_checkout_mercadopago.py, reusing this SAME real adapter
registered into the real provider registry — not duplicated here. This
file covers only what's specific to the adapter itself: request
construction, response parsing, status mapping, x-signature
verification, and the registration guard.

Test matrix
-----------
M01) create_intent — well-formed request (path, headers incl.
     Authorization/X-Idempotency-Key, body incl. items/external_reference)
M02) create_intent — success response parsed into PaymentIntent
     (redirect_url = init_point, id = our own deterministic reference)
M03) create_intent — same idempotency_key always yields the same
     external_reference / PaymentIntent.id
M04) create_intent — non-2xx/missing init_point response raises MercadoPagoError
M05) create_intent — transport timeout raises ProviderUnavailable
M06) create_intent — payment_method="pix" narrows excluded_payment_types
     to exclude every OTHER known type, never pix's own ("bank_transfer")
M07) create_intent — payment_method="boleto" excludes credit_card/
     debit_card/bank_transfer, never "ticket"
M08) create_intent — no payment_method in metadata -> no payment_methods
     key sent at all (Checkout Pro shows every enabled method)
M09) get_intent — parses the first Search Payments result into PaymentIntent
M10) get_intent — empty results -> PaymentIntentNotFound
M11) get_intent — non-2xx response raises MercadoPagoError
M12..M15) verify_webhook — status mapping: pending/in_process/authorized
     -> PROCESSING; approved -> CONFIRMED; rejected -> FAILED;
     cancelled -> CANCELLED; refunded -> REFUNDED
M16) verify_webhook — unmapped status (in_mediation, charged_back) ->
     UnknownWebhookEvent, no order ever touched
M17) verify_webhook — non-"payment" notification type -> UnknownWebhookEvent
M18) verify_webhook — missing x-signature header -> InvalidWebhookSignature
M19) verify_webhook — malformed x-signature (no v1) -> InvalidWebhookSignature
M20) verify_webhook — tampered/wrong-secret signature -> InvalidWebhookSignature
     (spoofing attempt — adapter cannot distinguish "wrong secret" from
     "tampered payload", both fail the same HMAC check by design)
M21) verify_webhook — valid signature with x-request-id present matches
     the official manifest template exactly (regression-pins the format)
M22) verify_webhook — valid signature with x-request-id ABSENT still
     verifies (manifest drops that segment, per official guidance)
M23) verify_webhook — provider_ref echoes payment.external_reference
     (fetched via GET, never trusted from the webhook body itself)
M24) verify_webhook — amount/currency mismatch vs. the order is caught
     by the generic webhook route, not this adapter — this adapter only
     reports what the fetched Payment says (covered at HTTP level)
M25) verify_webhook — nonce is the notification's own top-level `id`
     (distinct from data.id)
M26) verify_webhook — GET /v1/payments/{id} non-2xx raises MercadoPagoError
M27) verify_webhook — malformed JSON payload -> caught by the generic
     "adapter raised an unexpected error" path (covered at HTTP level);
     here: _parse_json raises MercadoPagoError directly
M28) refund() — full refund (no amount) request/response mapped correctly
M29) refund() — partial refund includes amount in body, maps to PARTIAL_REFUND
M30) refund() — idempotency key is deterministic per (payment_id, amount)
M31) refund() — non-2xx response raises MercadoPagoError
M32) build_mercadopago_adapter — disabled -> None
M33) build_mercadopago_adapter — enabled but missing credentials -> None
M34) build_mercadopago_adapter — fully configured -> registers into ProviderRegistry
M35) no access token / webhook secret ever appears in log output
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.billing.providers.base import (
    InvalidWebhookSignature,
    PaymentIntentNotFound,
    PaymentMethod,
    PaymentStatus,
    ProviderUnavailable,
    UnknownWebhookEvent,
)
from src.billing.providers.mercadopago_adapter import (
    MercadoPagoAdapter,
    MercadoPagoError,
    _deterministic_reference,
    build_mercadopago_adapter,
)
from src.billing.providers.registry import ProviderRegistry


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
        self.calls.append({"method": "GET", "url": url, "headers": headers, "timeout": timeout})
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _make_adapter(transport: FakeTransport, *, webhook_secret: str = "test-webhook-secret", **overrides) -> MercadoPagoAdapter:
    kwargs = dict(
        access_token="TEST-access-token",
        webhook_secret=webhook_secret,
        base_url="https://api.mercadopago.com",
        transport=transport,
    )
    kwargs.update(overrides)
    return MercadoPagoAdapter(**kwargs)


def _preference_success(*, preference_id: str = "pref-123", init_point: str = "https://mercadopago.com/checkout/pref-123") -> dict:
    return {"id": preference_id, "init_point": init_point, "sandbox_init_point": init_point + "?sandbox=1"}


def _payment(
    *,
    payment_id: int = 987654321,
    status: str = "approved",
    status_detail: str = "accredited",
    transaction_amount: float = 29.99,
    currency_id: str = "BRL",
    external_reference: str = "ref-abc",
) -> dict:
    return {
        "id": payment_id,
        "status": status,
        "status_detail": status_detail,
        "transaction_amount": transaction_amount,
        "currency_id": currency_id,
        "external_reference": external_reference,
        "payment_method_id": "pix",
        "payment_type_id": "bank_transfer",
    }


def _sign(secret: str, *, data_id: str, request_id: str | None, ts: str) -> str:
    """Independent re-implementation of the manifest + HMAC-SHA256
    scheme — deliberately re-derived here (not imported from the
    adapter's own _build_signature_manifest/_verify_signature) so these
    tests would catch a real regression, not just self-agreement."""
    manifest = f"id:{data_id.lower()};"
    if request_id is not None:
        manifest += f"request-id:{request_id};"
    manifest += f"ts:{ts};"
    return hmac.new(secret.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256).hexdigest()


def _webhook_headers(secret: str, *, data_id: str, request_id: str | None = "req-1", ts: str = "1700000000000") -> dict:
    v1 = _sign(secret, data_id=data_id, request_id=request_id, ts=ts)
    headers = {"x-signature": f"ts={ts},v1={v1}"}
    if request_id is not None:
        headers["x-request-id"] = request_id
    return headers


def _notification(*, data_id: str = "987654321", notification_id: int = 555, type_: str = "payment") -> dict:
    return {"id": notification_id, "type": type_, "action": f"{type_}.updated", "data": {"id": data_id}, "live_mode": False}


# ---------------------------------------------------------------------------
# M01-M08 — create_intent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_M01_create_intent_well_formed_request():
    transport = FakeTransport()
    transport.queue_json(201, _preference_success())
    adapter = _make_adapter(transport)

    await adapter.create_intent(
        amount=Decimal("29.99"), currency="BRL", user_id="u1", idempotency_key="idem-1",
    )

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/checkout/preferences")
    assert call["headers"]["Authorization"] == "Bearer TEST-access-token"
    assert call["headers"]["X-Idempotency-Key"] == "idem-1"
    body = json.loads(call["body"])
    assert body["items"][0]["unit_price"] == 29.99
    assert body["items"][0]["currency_id"] == "BRL"
    assert body["external_reference"] == _deterministic_reference("idem-1")


@pytest.mark.asyncio
async def test_M02_create_intent_success_parses_intent():
    transport = FakeTransport()
    transport.queue_json(201, _preference_success(init_point="https://mercadopago.com/checkout/xyz"))
    adapter = _make_adapter(transport)

    intent = await adapter.create_intent(
        amount=Decimal("9.99"), currency="usd", user_id="u1", idempotency_key="idem-2",
    )

    assert intent.redirect_url == "https://mercadopago.com/checkout/xyz"
    assert intent.id == _deterministic_reference("idem-2")
    assert intent.currency == "USD"
    assert intent.status == PaymentStatus.PENDING


@pytest.mark.asyncio
async def test_M03_create_intent_deterministic_reference():
    transport = FakeTransport()
    transport.queue_json(201, _preference_success())
    transport.queue_json(201, _preference_success())
    adapter = _make_adapter(transport)

    i1 = await adapter.create_intent(amount=Decimal("9.99"), currency="USD", user_id="u1", idempotency_key="same-key")
    i2 = await adapter.create_intent(amount=Decimal("9.99"), currency="USD", user_id="u1", idempotency_key="same-key")
    assert i1.id == i2.id


@pytest.mark.asyncio
async def test_M04_create_intent_error_response_raises():
    transport = FakeTransport()
    transport.queue_json(400, {"message": "invalid preference"})
    adapter = _make_adapter(transport)

    with pytest.raises(MercadoPagoError):
        await adapter.create_intent(amount=Decimal("9.99"), currency="USD", user_id="u1", idempotency_key="idem-4")


@pytest.mark.asyncio
async def test_M05_create_intent_transport_timeout_raises_provider_unavailable():
    transport = FakeTransport()
    transport.queue_exception(ProviderUnavailable("timed out"))
    adapter = _make_adapter(transport)

    with pytest.raises(ProviderUnavailable):
        await adapter.create_intent(amount=Decimal("9.99"), currency="USD", user_id="u1", idempotency_key="idem-5")


@pytest.mark.asyncio
async def test_M06_create_intent_pix_excludes_other_types():
    transport = FakeTransport()
    transport.queue_json(201, _preference_success())
    adapter = _make_adapter(transport)

    await adapter.create_intent(
        amount=Decimal("9.99"), currency="BRL", user_id="u1", idempotency_key="idem-6",
        metadata={"payment_method": "pix"},
    )
    body = json.loads(transport.calls[0]["body"])
    excluded_ids = {e["id"] for e in body["payment_methods"]["excluded_payment_types"]}
    assert "bank_transfer" not in excluded_ids  # pix itself never excluded
    assert excluded_ids == {"ticket", "credit_card", "debit_card"}


@pytest.mark.asyncio
async def test_M07_create_intent_boleto_excludes_others():
    transport = FakeTransport()
    transport.queue_json(201, _preference_success())
    adapter = _make_adapter(transport)

    await adapter.create_intent(
        amount=Decimal("9.99"), currency="BRL", user_id="u1", idempotency_key="idem-7",
        metadata={"payment_method": "boleto"},
    )
    body = json.loads(transport.calls[0]["body"])
    excluded_ids = {e["id"] for e in body["payment_methods"]["excluded_payment_types"]}
    assert "ticket" not in excluded_ids  # boleto itself never excluded
    assert excluded_ids == {"bank_transfer", "credit_card", "debit_card"}


@pytest.mark.asyncio
async def test_M08_create_intent_no_payment_method_sends_no_filter():
    transport = FakeTransport()
    transport.queue_json(201, _preference_success())
    adapter = _make_adapter(transport)

    await adapter.create_intent(amount=Decimal("9.99"), currency="BRL", user_id="u1", idempotency_key="idem-8")
    body = json.loads(transport.calls[0]["body"])
    assert "payment_methods" not in body


# ---------------------------------------------------------------------------
# M09-M11 — get_intent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_M09_get_intent_parses_first_result():
    transport = FakeTransport()
    transport.queue_json(200, {"results": [_payment(status="approved")]})
    adapter = _make_adapter(transport)

    intent = await adapter.get_intent("ref-abc")
    assert intent.status == PaymentStatus.CONFIRMED
    assert intent.currency == "BRL"
    assert intent.amount == Decimal("29.99")


@pytest.mark.asyncio
async def test_M10_get_intent_empty_results_not_found():
    transport = FakeTransport()
    transport.queue_json(200, {"results": []})
    adapter = _make_adapter(transport)

    with pytest.raises(PaymentIntentNotFound):
        await adapter.get_intent("ref-none")


@pytest.mark.asyncio
async def test_M11_get_intent_error_response_raises():
    transport = FakeTransport()
    transport.queue_json(401, {"message": "invalid token"})
    adapter = _make_adapter(transport)

    with pytest.raises(MercadoPagoError):
        await adapter.get_intent("ref-abc")


# ---------------------------------------------------------------------------
# M12-M27 — verify_webhook
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mp_status,expected", [
    ("pending", PaymentStatus.PROCESSING),
    ("in_process", PaymentStatus.PROCESSING),
    ("authorized", PaymentStatus.PROCESSING),
    ("approved", PaymentStatus.CONFIRMED),
    ("rejected", PaymentStatus.FAILED),
    ("cancelled", PaymentStatus.CANCELLED),
    ("refunded", PaymentStatus.REFUNDED),
])
@pytest.mark.asyncio
async def test_M12_15_status_mapping(mp_status, expected):
    secret = "wh-secret"
    transport = FakeTransport()
    transport.queue_json(200, _payment(status=mp_status))
    adapter = _make_adapter(transport, webhook_secret=secret)

    notification = _notification(data_id="987654321")
    headers = _webhook_headers(secret, data_id="987654321")

    event = await adapter.verify_webhook(json.dumps(notification).encode("utf-8"), headers)
    assert event.status == expected


@pytest.mark.parametrize("unmapped_status", ["in_mediation", "charged_back", "something_new"])
@pytest.mark.asyncio
async def test_M16_unmapped_status_raises_unknown_event(unmapped_status):
    secret = "wh-secret"
    transport = FakeTransport()
    transport.queue_json(200, _payment(status=unmapped_status))
    adapter = _make_adapter(transport, webhook_secret=secret)

    notification = _notification(data_id="987654321")
    headers = _webhook_headers(secret, data_id="987654321")

    with pytest.raises(UnknownWebhookEvent):
        await adapter.verify_webhook(json.dumps(notification).encode("utf-8"), headers)


@pytest.mark.asyncio
async def test_M17_non_payment_notification_type_unknown_event():
    secret = "wh-secret"
    adapter = _make_adapter(FakeTransport(), webhook_secret=secret)
    notification = _notification(type_="merchant_order")
    headers = _webhook_headers(secret, data_id="987654321")

    with pytest.raises(UnknownWebhookEvent):
        await adapter.verify_webhook(json.dumps(notification).encode("utf-8"), headers)


@pytest.mark.asyncio
async def test_M18_missing_signature_header_invalid():
    adapter = _make_adapter(FakeTransport(), webhook_secret="wh-secret")
    notification = _notification()

    with pytest.raises(InvalidWebhookSignature):
        await adapter.verify_webhook(json.dumps(notification).encode("utf-8"), {})


@pytest.mark.asyncio
async def test_M19_malformed_signature_header_invalid():
    adapter = _make_adapter(FakeTransport(), webhook_secret="wh-secret")
    notification = _notification()

    with pytest.raises(InvalidWebhookSignature):
        await adapter.verify_webhook(json.dumps(notification).encode("utf-8"), {"x-signature": "garbage-no-equals"})


@pytest.mark.asyncio
async def test_M20_tampered_signature_invalid_spoof_attempt():
    """Simulates a spoofing attempt: an attacker forges a notification
    body/headers without knowing the real webhook secret."""
    real_secret = "real-secret"
    adapter = _make_adapter(FakeTransport(), webhook_secret=real_secret)
    notification = _notification(data_id="987654321")
    # Signed with the WRONG secret — a spoofer's best guess.
    forged_headers = _webhook_headers("wrong-secret-guess", data_id="987654321")

    with pytest.raises(InvalidWebhookSignature):
        await adapter.verify_webhook(json.dumps(notification).encode("utf-8"), forged_headers)


@pytest.mark.asyncio
async def test_M21_manifest_matches_official_template_with_request_id():
    secret = "wh-secret"
    transport = FakeTransport()
    transport.queue_json(200, _payment(status="approved"))
    adapter = _make_adapter(transport, webhook_secret=secret)

    ts = "1742505638683"
    data_id = "987654321"
    request_id = "bb56a2f1-6aae-46ac-982e-9dcd3581d08e"
    manifest = f"id:{data_id};request-id:{request_id};ts:{ts};"
    v1 = hmac.new(secret.encode(), manifest.encode(), hashlib.sha256).hexdigest()
    headers = {"x-signature": f"ts={ts},v1={v1}", "x-request-id": request_id}

    event = await adapter.verify_webhook(json.dumps(_notification(data_id=data_id)).encode("utf-8"), headers)
    assert event.status == PaymentStatus.CONFIRMED


@pytest.mark.asyncio
async def test_M22_manifest_without_request_id_still_verifies():
    secret = "wh-secret"
    transport = FakeTransport()
    transport.queue_json(200, _payment(status="approved"))
    adapter = _make_adapter(transport, webhook_secret=secret)

    headers = _webhook_headers(secret, data_id="987654321", request_id=None)
    assert "x-request-id" not in headers

    event = await adapter.verify_webhook(json.dumps(_notification(data_id="987654321")).encode("utf-8"), headers)
    assert event.status == PaymentStatus.CONFIRMED


@pytest.mark.asyncio
async def test_M23_provider_ref_from_fetched_payment_not_webhook_body():
    secret = "wh-secret"
    transport = FakeTransport()
    transport.queue_json(200, _payment(status="approved", external_reference="the-real-reference"))
    adapter = _make_adapter(transport, webhook_secret=secret)

    notification = _notification(data_id="987654321")
    headers = _webhook_headers(secret, data_id="987654321")
    event = await adapter.verify_webhook(json.dumps(notification).encode("utf-8"), headers)

    assert event.provider_ref == "the-real-reference"
    # GET /v1/payments/{id} must have actually been called (never trust body alone)
    assert any(c["method"] == "GET" and "/v1/payments/987654321" in c["url"] for c in transport.calls)


@pytest.mark.asyncio
async def test_M25_nonce_is_notification_own_id():
    secret = "wh-secret"
    transport = FakeTransport()
    transport.queue_json(200, _payment(status="approved"))
    adapter = _make_adapter(transport, webhook_secret=secret)

    notification = _notification(data_id="987654321", notification_id=42424242)
    headers = _webhook_headers(secret, data_id="987654321")
    event = await adapter.verify_webhook(json.dumps(notification).encode("utf-8"), headers)

    assert event.nonce == "42424242"
    assert event.nonce != str(notification["data"]["id"])


@pytest.mark.asyncio
async def test_M26_get_payment_error_response_raises():
    secret = "wh-secret"
    transport = FakeTransport()
    transport.queue_json(500, {"message": "internal error"})
    adapter = _make_adapter(transport, webhook_secret=secret)

    notification = _notification(data_id="987654321")
    headers = _webhook_headers(secret, data_id="987654321")

    with pytest.raises(MercadoPagoError):
        await adapter.verify_webhook(json.dumps(notification).encode("utf-8"), headers)


@pytest.mark.asyncio
async def test_M27_malformed_json_payload_raises():
    adapter = _make_adapter(FakeTransport(), webhook_secret="wh-secret")
    with pytest.raises(MercadoPagoError):
        await adapter.verify_webhook(b"not-json-at-all", {"x-signature": "ts=1,v1=deadbeef"})


# ---------------------------------------------------------------------------
# M28-M31 — refund
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_M28_full_refund():
    transport = FakeTransport()
    transport.queue_json(201, {"id": 111, "payment_id": 987654321, "amount": 29.99, "status": "approved"})
    adapter = _make_adapter(transport)

    result = await adapter.refund("987654321")
    body = json.loads(transport.calls[0]["body"])
    assert "amount" not in body
    assert result.status == PaymentStatus.REFUNDED
    assert result.amount_gross == Decimal("29.99")


@pytest.mark.asyncio
async def test_M29_partial_refund():
    transport = FakeTransport()
    transport.queue_json(201, {"id": 112, "payment_id": 987654321, "amount": 10.00, "status": "approved"})
    adapter = _make_adapter(transport)

    result = await adapter.refund("987654321", amount=Decimal("10.00"))
    body = json.loads(transport.calls[0]["body"])
    assert body["amount"] == 10.00
    assert result.status == PaymentStatus.PARTIAL_REFUND


@pytest.mark.asyncio
async def test_M30_refund_idempotency_key_deterministic():
    transport = FakeTransport()
    transport.queue_json(201, {"id": 1, "payment_id": 1, "amount": 5.0, "status": "approved"})
    transport.queue_json(201, {"id": 1, "payment_id": 1, "amount": 5.0, "status": "approved"})
    adapter = _make_adapter(transport)

    await adapter.refund("p1", amount=Decimal("5.00"))
    await adapter.refund("p1", amount=Decimal("5.00"))
    key1 = transport.calls[0]["headers"]["X-Idempotency-Key"]
    key2 = transport.calls[1]["headers"]["X-Idempotency-Key"]
    assert key1 == key2


@pytest.mark.asyncio
async def test_M31_refund_error_response_raises():
    transport = FakeTransport()
    transport.queue_json(400, {"message": "cannot refund"})
    adapter = _make_adapter(transport)

    with pytest.raises(MercadoPagoError):
        await adapter.refund("p1")


# ---------------------------------------------------------------------------
# M32-M35 — factory / registry / secret hygiene
# ---------------------------------------------------------------------------


def test_M32_build_adapter_disabled_returns_none():
    settings = SimpleNamespace(
        mercadopago_enabled=False, mercadopago_access_token="tok", mercadopago_webhook_secret="sec",
        mercadopago_base_url="https://api.mercadopago.com",
        mercadopago_notification_url="", mercadopago_success_url="", mercadopago_pending_url="", mercadopago_failure_url="",
    )
    assert build_mercadopago_adapter(settings) is None


@pytest.mark.parametrize("access_token,webhook_secret", [("", "sec"), ("tok", "")])
def test_M33_build_adapter_missing_credentials_returns_none(access_token, webhook_secret):
    settings = SimpleNamespace(
        mercadopago_enabled=True, mercadopago_access_token=access_token, mercadopago_webhook_secret=webhook_secret,
        mercadopago_base_url="https://api.mercadopago.com",
        mercadopago_notification_url="", mercadopago_success_url="", mercadopago_pending_url="", mercadopago_failure_url="",
    )
    assert build_mercadopago_adapter(settings) is None


def test_M34_build_adapter_fully_configured_registers():
    settings = SimpleNamespace(
        mercadopago_enabled=True, mercadopago_access_token="real-looking-test-token",
        mercadopago_webhook_secret="real-looking-test-secret",
        mercadopago_base_url="https://api.mercadopago.com",
        mercadopago_notification_url="", mercadopago_success_url="", mercadopago_pending_url="", mercadopago_failure_url="",
    )
    adapter = build_mercadopago_adapter(settings)
    assert adapter is not None
    assert adapter.provider_name == "mercado_pago"

    registry = ProviderRegistry()
    registry.register(adapter)
    assert registry.is_registered("mercado_pago") is True
    assert registry.get("mercado_pago") is adapter


@pytest.mark.asyncio
async def test_M35_no_secret_leaks_to_logs(caplog):
    secret_token = "SUPER-SECRET-ACCESS-TOKEN-VALUE"
    secret_webhook = "SUPER-SECRET-WEBHOOK-VALUE"
    transport = FakeTransport()
    transport.queue_json(201, _preference_success())
    adapter = _make_adapter(transport, webhook_secret=secret_webhook, access_token=secret_token)

    with caplog.at_level(logging.DEBUG):
        await adapter.create_intent(amount=Decimal("9.99"), currency="USD", user_id="u1", idempotency_key="idem-secret")

    for record in caplog.records:
        assert secret_token not in record.getMessage()
        assert secret_webhook not in record.getMessage()
