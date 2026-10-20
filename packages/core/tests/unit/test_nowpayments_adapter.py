"""Unit tests for NOWPaymentsAdapter —
packages/core/src/billing/providers/nowpayments_adapter.py.

All tests use a FakeTransport (NOWPaymentsTransport) — no real network
call is ever made, per this phase's own mandatory rule (mission
requirement 14: "usar um fake HTTP transport; NUNCA acessar a API real
da NOWPayments em testes automatizados").

Full end-to-end wiring (checkout -> processing -> webhook -> plan
activation, duplicate webhook idempotency, concurrent duplicate,
foreign order, expired order) is covered by
tests/integration/test_nowpayments_checkout.py, reusing this SAME real
adapter registered into the real provider registry over real HTTP —
not duplicated here. This file covers only what's specific to the
adapter itself: request construction, response parsing, status
mapping, IPN signature verification, and the registration guard —
exactly the same split BinancePayAdapter's own test file already
established (see test_binance_pay_adapter.py's own docstring).

Test matrix (mission requirement 14's mandatory list, mapped to
adapter-level unit tests where the concern is adapter-local; DB/HTTP
concerns are integration-level — see the docstring above)
-----------------------------------------------------------------
N01) create_intent — well-formed request (path, headers, body incl.
     price_amount as a JSON number)
N02) create_intent — success response parsed into PaymentIntent
     (redirect_url, id = our own deterministic order_id)
N03) create_intent — invalid/error API response raises NOWPaymentsError
N04) create_intent — transport timeout raises ProviderUnavailable
N05) create_intent — transport unavailable (connection error) raises
     ProviderUnavailable
N06) create_intent — same idempotency_key always yields the same
     order_id / PaymentIntent.id
N07..N15) verify_webhook — payment_status mapping: waiting, confirming,
     confirmed, sending, partially_paid -> PROCESSING (never treated as
     completed); finished -> CONFIRMED; failed -> FAILED; refunded ->
     REFUNDED; expired -> EXPIRED
N16) verify_webhook — invalid signature -> InvalidWebhookSignature
N17) verify_webhook — missing x-nowpayments-sig header ->
     InvalidWebhookSignature
N18) verify_webhook — wrong IPN secret -> InvalidWebhookSignature
     (adapter has no way to distinguish "wrong secret" from "tampered
     body" — both fail the same HMAC check, by design)
N19) verify_webhook — unmapped payment_status -> UnknownWebhookEvent
N20) verify_webhook — provider_ref echoes exactly data["order_id"] (the
     id NOWPayments' own IPN payload actually carries — never the
     invoice's own numeric id)
N21) verify_webhook — nonce is set to the verified signature itself (no
     native nonce/timestamp exists — see module docstring)
N22) verify_webhook — recursive key-sort is order-independent: two
     payloads with identical data but different key order / nested key
     order produce the SAME signature
N23) get_intent — always raises PaymentIntentNotFound (documented gap —
     see adapter's own docstring)
N24) refund() raises NotImplementedError (explicitly out of scope this
     phase)
N25) build_nowpayments_adapter — disabled -> None
N26) build_nowpayments_adapter — enabled but missing credentials -> None
N27) build_nowpayments_adapter — fully configured -> registers into
     ProviderRegistry
N28) no API key / IPN secret ever appears in log output
N29) list_supported_currencies — parses merchant coins response
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
    PaymentStatus,
    ProviderUnavailable,
    UnknownWebhookEvent,
)
from src.billing.providers.nowpayments_adapter import (
    NOWPaymentsAdapter,
    NOWPaymentsError,
    _nowpayments_order_id,
    build_nowpayments_adapter,
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


def _make_adapter(transport: FakeTransport, *, ipn_secret: str = "test-ipn-secret") -> NOWPaymentsAdapter:
    return NOWPaymentsAdapter(
        api_key="test-api-key",
        ipn_secret=ipn_secret,
        base_url="https://api.nowpayments.io",
        transport=transport,
    )


def _invoice_success(*, invoice_id: int = 123456, invoice_url: str = "https://nowpayments.io/payment/?iid=123456") -> dict:
    return {
        "id": invoice_id,
        "order_id": "will-be-overwritten-by-caller-if-needed",
        "order_description": "TRINDATRON plan",
        "price_amount": 29.99,
        "price_currency": "usd",
        "pay_currency": None,
        "ipn_callback_url": None,
        "invoice_url": invoice_url,
        "success_url": None,
        "cancel_url": None,
        "created_at": "2026-01-01T00:00:00.000Z",
        "updated_at": "2026-01-01T00:00:00.000Z",
    }


def _sign(ipn_secret: str, payload: dict) -> str:
    """Independent re-implementation of the recursive-sort + HMAC-SHA512
    signing scheme — deliberately re-derived here (not imported from the
    adapter's own _sort_recursive/_verify_ipn_signature) so these tests
    would catch a real regression in either implementation, not just
    confirm they agree with themselves."""

    def sort_recursive(value):
        if isinstance(value, dict):
            return {k: sort_recursive(value[k]) for k in sorted(value.keys())}
        if isinstance(value, list):
            return [sort_recursive(v) for v in value]
        return value

    canonical = json.dumps(sort_recursive(payload), separators=(",", ":"), ensure_ascii=False)
    return hmac.new(ipn_secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha512).hexdigest()


def _ipn_payload(
    *,
    payment_status: str = "finished",
    order_id: str = "order-abc",
    price_amount: float = 29.99,
    price_currency: str = "usd",
    payment_id: int = 987654321,
) -> dict:
    return {
        "payment_id": payment_id,
        "payment_status": payment_status,
        "pay_address": "bc1qexampleaddress",
        "price_amount": price_amount,
        "price_currency": price_currency,
        "pay_amount": 0.00071,
        "actually_paid": 0.00071,
        "pay_currency": "btc",
        "order_id": order_id,
        "order_description": "TRINDATRON plan",
        "purchase_id": "555",
        "outcome_amount": 0.0007,
        "outcome_currency": "btc",
    }


def _signed_ipn(ipn_secret: str, **kwargs) -> tuple[bytes, dict[str, str]]:
    payload = _ipn_payload(**kwargs)
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    signature = _sign(ipn_secret, payload)
    return body, {"x-nowpayments-sig": signature}


# ---------------------------------------------------------------------------
# create_intent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_N01_create_intent_sends_well_formed_request():
    transport = FakeTransport()
    transport.queue_json(200, _invoice_success())
    adapter = _make_adapter(transport)

    await adapter.create_intent(
        amount=Decimal("29.99"), currency="usd", user_id="user-1",
        idempotency_key="po:order-1:checkout",
    )

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == "https://api.nowpayments.io/v1/invoice"
    assert call["headers"]["x-api-key"] == "test-api-key"

    body = json.loads(call["body"])
    assert body["price_amount"] == 29.99
    assert isinstance(body["price_amount"], float)  # JSON number, not string — see module docstring
    assert body["price_currency"] == "usd"
    assert body["order_id"] == _nowpayments_order_id("po:order-1:checkout")
    assert "pay_currency" not in body  # never pre-specified — customer picks it on the hosted page


@pytest.mark.asyncio
async def test_N02_create_intent_parses_success_response():
    transport = FakeTransport()
    transport.queue_json(200, _invoice_success(invoice_url="https://nowpayments.io/payment/?iid=999"))
    adapter = _make_adapter(transport)

    intent = await adapter.create_intent(
        amount=Decimal("9.99"), currency="USD", user_id="user-1", idempotency_key="idem-xyz",
    )

    assert intent.provider == "nowpayments"
    assert intent.status == PaymentStatus.PENDING
    assert intent.amount == Decimal("9.99")
    assert intent.currency == "USD"
    assert intent.redirect_url == "https://nowpayments.io/payment/?iid=999"
    assert intent.id == _nowpayments_order_id("idem-xyz")  # NOT the invoice's own numeric id


@pytest.mark.asyncio
async def test_N03_create_intent_error_response_raises_nowpayments_error():
    transport = FakeTransport()
    transport.queue_json(400, {"statusCode": 400, "code": "INVALID_REQUEST_PARAMS", "message": "price_amount must be positive"})
    adapter = _make_adapter(transport)

    with pytest.raises(NOWPaymentsError) as exc_info:
        await adapter.create_intent(amount=Decimal("1.00"), currency="USD", user_id="u", idempotency_key="k")
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_N04_create_intent_timeout_raises_provider_unavailable():
    transport = FakeTransport()
    transport.queue_exception(ProviderUnavailable("NOWPayments request timed out"))
    adapter = _make_adapter(transport)

    with pytest.raises(ProviderUnavailable):
        await adapter.create_intent(amount=Decimal("1.00"), currency="USD", user_id="u", idempotency_key="k")


@pytest.mark.asyncio
async def test_N05_create_intent_connection_error_raises_provider_unavailable():
    transport = FakeTransport()
    transport.queue_exception(ConnectionError("connection refused"))
    adapter = _make_adapter(transport)

    with pytest.raises(ProviderUnavailable):
        await adapter.create_intent(amount=Decimal("1.00"), currency="USD", user_id="u", idempotency_key="k")


def test_N06_order_id_deterministic_and_stable():
    a = _nowpayments_order_id("po:order-1:confirm:credit")
    b = _nowpayments_order_id("po:order-1:confirm:credit")
    c = _nowpayments_order_id("po:order-2:confirm:credit")

    assert a == b
    assert a != c
    assert len(a) <= 32
    assert all(ch in "0123456789abcdef" for ch in a)


# ---------------------------------------------------------------------------
# verify_webhook — status mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payment_status,expected",
    [
        ("waiting", PaymentStatus.PROCESSING),
        ("confirming", PaymentStatus.PROCESSING),
        ("confirmed", PaymentStatus.PROCESSING),
        ("sending", PaymentStatus.PROCESSING),
        ("partially_paid", PaymentStatus.PROCESSING),
        ("finished", PaymentStatus.CONFIRMED),
        ("failed", PaymentStatus.FAILED),
        ("refunded", PaymentStatus.REFUNDED),
        ("expired", PaymentStatus.EXPIRED),
    ],
)
async def test_N07_N15_verify_webhook_status_mapping(payment_status, expected):
    transport = FakeTransport()
    adapter = _make_adapter(transport)
    body, headers = _signed_ipn("test-ipn-secret", payment_status=payment_status)

    event = adapter_event = await adapter.verify_webhook(body, headers)

    assert event.status == expected
    if payment_status != "finished":
        # Mission requirement: "não tratar 'waiting' como pagamento
        # concluído" — extended to every non-finished status.
        assert event.status != PaymentStatus.CONFIRMED


@pytest.mark.asyncio
async def test_N16_verify_webhook_invalid_signature_rejected():
    transport = FakeTransport()
    adapter = _make_adapter(transport, ipn_secret="real-secret")
    body, headers = _signed_ipn("a-completely-different-secret")

    with pytest.raises(InvalidWebhookSignature):
        await adapter.verify_webhook(body, headers)


@pytest.mark.asyncio
async def test_N17_verify_webhook_missing_signature_header_rejected():
    transport = FakeTransport()
    adapter = _make_adapter(transport)

    with pytest.raises(InvalidWebhookSignature):
        await adapter.verify_webhook(b'{"payment_status":"finished"}', {})


@pytest.mark.asyncio
async def test_N18_verify_webhook_wrong_ipn_secret_rejected():
    transport = FakeTransport()
    adapter = _make_adapter(transport, ipn_secret="the-real-secret")
    body, headers = _signed_ipn("an-attackers-guessed-secret")

    with pytest.raises(InvalidWebhookSignature):
        await adapter.verify_webhook(body, headers)


@pytest.mark.asyncio
async def test_N19_verify_webhook_unmapped_status_raises_unknown_event():
    transport = FakeTransport()
    adapter = _make_adapter(transport)
    body, headers = _signed_ipn("test-ipn-secret", payment_status="some_future_status")

    with pytest.raises(UnknownWebhookEvent):
        await adapter.verify_webhook(body, headers)


@pytest.mark.asyncio
async def test_N20_verify_webhook_provider_ref_is_order_id():
    transport = FakeTransport()
    adapter = _make_adapter(transport)
    body, headers = _signed_ipn("test-ipn-secret", order_id="order-specific-ref-001")

    event = await adapter.verify_webhook(body, headers)

    assert event.provider_ref == "order-specific-ref-001"
    assert event.amount == Decimal("29.99")
    assert event.currency == "usd"


@pytest.mark.asyncio
async def test_N21_verify_webhook_nonce_is_the_verified_signature():
    transport = FakeTransport()
    adapter = _make_adapter(transport)
    body, headers = _signed_ipn("test-ipn-secret")

    event = await adapter.verify_webhook(body, headers)

    assert event.nonce == headers["x-nowpayments-sig"]


@pytest.mark.asyncio
async def test_N22_signature_verification_is_key_order_independent():
    """NOWPayments' own reference implementation recursively sorts
    object keys before signing — a payload delivered with keys in a
    different order (same data) must still verify, since JSON object key
    order carries no semantic meaning and NOWPayments' own client library
    may not preserve it byte-for-byte."""
    transport = FakeTransport()
    adapter = _make_adapter(transport)
    payload = _ipn_payload()
    signature = _sign("test-ipn-secret", payload)

    reordered = dict(reversed(list(payload.items())))
    body = json.dumps(reordered, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    event = await adapter.verify_webhook(body, {"x-nowpayments-sig": signature})
    assert event.status == PaymentStatus.CONFIRMED


# ---------------------------------------------------------------------------
# get_intent / refund — documented gaps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_N23_get_intent_raises_payment_intent_not_found():
    transport = FakeTransport()
    adapter = _make_adapter(transport)

    with pytest.raises(PaymentIntentNotFound):
        await adapter.get_intent("some-order-id")


@pytest.mark.asyncio
async def test_N24_refund_not_implemented():
    transport = FakeTransport()
    adapter = _make_adapter(transport)

    with pytest.raises(NotImplementedError):
        await adapter.refund("some-payment-id")


# ---------------------------------------------------------------------------
# Construction / registration guard
# ---------------------------------------------------------------------------


def test_N25_build_adapter_disabled_returns_none():
    settings = SimpleNamespace(
        nowpayments_enabled=False,
        nowpayments_api_key="k", nowpayments_ipn_secret="s",
        nowpayments_base_url="https://api.nowpayments.io",
        nowpayments_success_url="", nowpayments_cancel_url="", nowpayments_ipn_callback_url="",
    )
    assert build_nowpayments_adapter(settings) is None


@pytest.mark.parametrize("api_key,ipn_secret", [("", "s"), ("k", ""), ("", "")])
def test_N26_build_adapter_missing_credentials_returns_none(api_key, ipn_secret):
    settings = SimpleNamespace(
        nowpayments_enabled=True,
        nowpayments_api_key=api_key, nowpayments_ipn_secret=ipn_secret,
        nowpayments_base_url="https://api.nowpayments.io",
        nowpayments_success_url="", nowpayments_cancel_url="", nowpayments_ipn_callback_url="",
    )
    assert build_nowpayments_adapter(settings) is None


def test_N27_build_adapter_fully_configured_registers():
    settings = SimpleNamespace(
        nowpayments_enabled=True,
        nowpayments_api_key="real-looking-test-key",
        nowpayments_ipn_secret="real-looking-test-ipn-secret",
        nowpayments_base_url="https://api.nowpayments.io",
        nowpayments_success_url="", nowpayments_cancel_url="", nowpayments_ipn_callback_url="",
    )
    adapter = build_nowpayments_adapter(settings)
    assert adapter is not None
    assert adapter.provider_name == "nowpayments"

    registry = ProviderRegistry()
    registry.register(adapter)
    assert registry.is_registered("nowpayments") is True


def test_N28_declares_crypto_one_time_capabilities():
    """Payments Foundation — NOWPayments declares CRYPTO + ONE_TIME only,
    matching its actual integration (no real auto-recurring charge)."""
    from src.billing.providers.base import PaymentMethod, PaymentType

    adapter = NOWPaymentsAdapter(
        api_key="k", ipn_secret="s", base_url="https://api.nowpayments.io",
        transport=FakeTransport(),
    )
    assert adapter.supported_payment_methods == frozenset({PaymentMethod.CRYPTO})
    assert adapter.supported_payment_types == frozenset({PaymentType.ONE_TIME})


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_N28_no_secret_ever_logged(caplog):
    transport = FakeTransport()
    transport.queue_json(200, _invoice_success())
    api_key = "SUPER-SECRET-API-KEY-MUST-NOT-LOG"
    ipn_secret = "SUPER-SECRET-IPN-SECRET-MUST-NOT-LOG"
    adapter = NOWPaymentsAdapter(
        api_key=api_key, ipn_secret=ipn_secret,
        base_url="https://api.nowpayments.io", transport=transport,
    )

    with caplog.at_level(logging.DEBUG):
        await adapter.create_intent(amount=Decimal("1.00"), currency="USD", user_id="u", idempotency_key="k")
        body, headers = _signed_ipn(ipn_secret)
        await adapter.verify_webhook(body, headers)

    full_log_text = "\n".join(r.message for r in caplog.records)
    assert api_key not in full_log_text
    assert ipn_secret not in full_log_text


# ---------------------------------------------------------------------------
# list_supported_currencies — convenience, not wired into any route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_N29_list_supported_currencies_parses_merchant_coins():
    transport = FakeTransport()
    transport.queue_json(200, {"selectedCurrencies": ["btc", "eth", "usdttrc20", "usdc", "sol", "xrp", "trx"]})
    adapter = _make_adapter(transport)

    currencies = await adapter.list_supported_currencies()

    assert currencies == ["btc", "eth", "usdttrc20", "usdc", "sol", "xrp", "trx"]
    assert transport.calls[0]["url"].endswith("/v1/merchant/coins")
