"""Unit tests for BinancePayAdapter — packages/core/src/billing/providers/binance_pay_adapter.py.

All tests use a FakeTransport (BinancePayTransport) — no real network call is
ever made. Webhook signature tests use a real, freshly-generated RSA keypair
(never a real Binance key) so the RSA-SHA256/PKCS1v15 verification path is
exercised with genuine cryptography, not mocked out.

DB-level idempotency (does a replayed CONFIRMED webhook double-credit a
user?) is already covered by test_billing_webhook.py's W09 via the generic
webhook route + PaymentOrderService — not duplicated here. This file covers
only what's specific to the adapter itself: request construction/signing,
response parsing, status mapping, webhook signature verification, replay
(clock-skew) rejection, error handling, and the registration guard.

Test matrix
-----------
A01) create_intent — well-formed signed request (headers, path, body)
A02) create_intent — success response parsed into PaymentIntent
A03) create_intent — non-SUCCESS response raises BinancePayError
A04) create_intent — transport failure raises ProviderUnavailable
A05) get_intent — PENDING/INITIAL -> PaymentStatus.PENDING
A06) get_intent — PAID -> PaymentStatus.CONFIRMED
A07) get_intent — CANCELED -> PaymentStatus.CANCELLED
A08) get_intent — unmapped status -> PROCESSING (logged, not raised)
A09) get_intent — HTTP 404 -> PaymentIntentNotFound
A10) verify_webhook — valid PAY_SUCCESS -> CONFIRMED WebhookEvent
A11) verify_webhook — invalid signature -> InvalidWebhookSignature
A12) verify_webhook — missing headers -> InvalidWebhookSignature
A13) verify_webhook — stale timestamp (replay window) -> InvalidWebhookSignature
A14) verify_webhook — unmapped bizStatus -> UnknownWebhookEvent
A15) verify_webhook — same valid payload verified twice is stateless/consistent
A16) verify_webhook — public key fetched once via Query Certificate, then cached
A17) BinancePayAdapter(...) — missing api_key/secret_key -> ValueError
A18) build_binance_pay_adapter — disabled -> None
A19) build_binance_pay_adapter — enabled but missing credentials -> None
A20) build_binance_pay_adapter — fully configured -> adapter registers into ProviderRegistry
A21) no secret (api_key/secret_key/signature) ever appears in log output
A22) refund() raises NotImplementedError (explicitly out of scope this phase)
A23) _merchant_trade_no is deterministic, <=32 chars, alphanumeric
A24) _format_amount never produces scientific notation / float artifacts
"""
from __future__ import annotations

import base64
import json
import logging
import secrets
import string
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.billing.providers.base import (
    InvalidWebhookSignature,
    PaymentIntentNotFound,
    PaymentStatus,
    ProviderUnavailable,
    UnknownWebhookEvent,
)
from src.billing.providers.binance_pay_adapter import (
    BinancePayAdapter,
    BinancePayError,
    _format_amount,
    _merchant_trade_no,
    build_binance_pay_adapter,
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
        self.calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _rsa_keypair() -> tuple[rsa.RSAPrivateKey, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_key, public_pem


def _make_adapter(
    transport: FakeTransport,
    *,
    public_key_pem: str | None = None,
    webhook_max_clock_skew_seconds: int = 300,
) -> BinancePayAdapter:
    return BinancePayAdapter(
        api_key="test-api-key",
        secret_key="test-secret-key",
        base_url="https://bpay.binanceapi.com",
        transport=transport,
        public_key_pem=public_key_pem,
        webhook_max_clock_skew_seconds=webhook_max_clock_skew_seconds,
    )


def _create_order_success(
    *,
    prepay_id: str = "1234567890123456789",
    checkout_url: str = "https://pay.binance.com/checkout/abc",
    expire_time_ms: int | None = None,
) -> dict:
    if expire_time_ms is None:
        expire_time_ms = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp() * 1000)
    return {
        "status": "SUCCESS",
        "code": "000000",
        "data": {
            "prepayId": prepay_id,
            "checkoutUrl": checkout_url,
            "qrContent": "https://qr.example/abc",
            "expireTime": expire_time_ms,
        },
        "errorMessage": None,
    }


def _query_order_response(*, ext_status: str, order_amount: str = "25.00000000", currency: str = "USDT") -> dict:
    return {
        "status": "SUCCESS",
        "code": "000000",
        "data": {
            "merchantTradeNo": "mtno",
            "transactionId": "tx-1",
            "status": ext_status,
            "currency": currency,
            "orderAmount": order_amount,
        },
    }


def _certificate_success(public_key_pem: str, cert_serial: str = "cert-serial-123") -> dict:
    return {
        "status": "SUCCESS",
        "code": "000000",
        "data": {"certSerial": cert_serial, "certPublic": public_key_pem},
    }


def _sign_webhook(
    private_key: rsa.RSAPrivateKey,
    *,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> str:
    payload = timestamp.encode("utf-8") + b"\n" + nonce.encode("utf-8") + b"\n" + body + b"\n"
    signature = private_key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("utf-8")


def _make_webhook(
    private_key: rsa.RSAPrivateKey,
    *,
    biz_status: str = "PAY_SUCCESS",
    merchant_trade_no: str = "abc123def456",
    total_fee: str = "25.00000000",
    currency: str = "USDT",
    timestamp: str | None = None,
    nonce: str | None = None,
    certificate_sn: str = "cert-serial-123",
) -> tuple[bytes, dict[str, str]]:
    if timestamp is None:
        timestamp = str(int(time.time() * 1000))
    if nonce is None:
        nonce = "".join(secrets.choice(string.ascii_letters) for _ in range(32))
    inner = {
        "merchantTradeNo": merchant_trade_no,
        "totalFee": total_fee,
        "currency": currency,
        "transactTime": int(time.time() * 1000),
    }
    outer = {
        "bizType": "PAY",
        "bizId": 123456789,
        "bizIdStr": "123456789",
        "bizStatus": biz_status,
        "data": json.dumps(inner, separators=(",", ":")),
    }
    body = json.dumps(outer, separators=(",", ":")).encode("utf-8")
    signature_b64 = _sign_webhook(private_key, timestamp=timestamp, nonce=nonce, body=body)
    headers = {
        "BinancePay-Timestamp": timestamp,
        "BinancePay-Nonce": nonce,
        "BinancePay-Certificate-SN": certificate_sn,
        "BinancePay-Signature": signature_b64,
    }
    return body, headers


# ---------------------------------------------------------------------------
# create_intent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_A01_create_intent_sends_well_formed_signed_request():
    transport = FakeTransport()
    transport.queue_json(200, _create_order_success())
    adapter = _make_adapter(transport)

    await adapter.create_intent(
        amount=Decimal("25.00"),
        currency="usdt",
        user_id="user-1",
        idempotency_key="po:order-1:confirm:credit",
    )

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == "https://bpay.binanceapi.com/binancepay/openapi/v3/order"
    headers = call["headers"]
    for name in (
        "Content-Type", "BinancePay-Timestamp", "BinancePay-Nonce",
        "BinancePay-Certificate-SN", "BinancePay-Signature",
    ):
        assert name in headers
    assert headers["BinancePay-Certificate-SN"] == "test-api-key"
    # Signature is a valid uppercase hex string (HMAC-SHA512 digest length = 128 hex chars).
    assert len(headers["BinancePay-Signature"]) == 128
    assert headers["BinancePay-Signature"] == headers["BinancePay-Signature"].upper()

    body = json.loads(call["body"])
    assert body["currency"] == "USDT"  # uppercased
    assert body["orderAmount"] == "25.00"
    assert body["goodsDetails"][0]["goodsType"] == "02"


@pytest.mark.asyncio
async def test_A02_create_intent_parses_success_response():
    transport = FakeTransport()
    transport.queue_json(200, _create_order_success(checkout_url="https://pay.binance.com/checkout/xyz"))
    adapter = _make_adapter(transport)

    intent = await adapter.create_intent(
        amount=Decimal("10.50"), currency="USD", user_id="user-1",
        idempotency_key="idem-abc",
    )

    assert intent.provider == "binance_pay"
    assert intent.status == PaymentStatus.PENDING
    assert intent.amount == Decimal("10.50")
    assert intent.redirect_url == "https://pay.binance.com/checkout/xyz"
    assert intent.id == _merchant_trade_no("idem-abc")
    assert intent.expires_at is not None


@pytest.mark.asyncio
async def test_A03_create_intent_non_success_raises_binance_pay_error():
    transport = FakeTransport()
    transport.queue_json(200, {"status": "FAIL", "code": "400001", "errorMessage": "bad params"})
    adapter = _make_adapter(transport)

    with pytest.raises(BinancePayError) as exc_info:
        await adapter.create_intent(
            amount=Decimal("1.00"), currency="USD", user_id="u", idempotency_key="k",
        )
    assert exc_info.value.code == "400001"


@pytest.mark.asyncio
async def test_A04_create_intent_transport_failure_raises_provider_unavailable():
    transport = FakeTransport()
    transport.queue_exception(TimeoutError("connection timed out"))
    adapter = _make_adapter(transport)

    with pytest.raises(ProviderUnavailable):
        await adapter.create_intent(
            amount=Decimal("1.00"), currency="USD", user_id="u", idempotency_key="k",
        )


# ---------------------------------------------------------------------------
# get_intent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ext_status,expected",
    [
        ("INITIAL", PaymentStatus.PENDING),
        ("PENDING", PaymentStatus.PENDING),
        ("PAID", PaymentStatus.CONFIRMED),
        ("CANCELED", PaymentStatus.CANCELLED),
    ],
)
async def test_A05_A06_A07_get_intent_maps_status(ext_status, expected):
    transport = FakeTransport()
    transport.queue_json(200, _query_order_response(ext_status=ext_status))
    adapter = _make_adapter(transport)

    intent = await adapter.get_intent("mtno")

    assert intent.status == expected
    assert intent.amount == Decimal("25.00000000")
    assert intent.currency == "USDT"


@pytest.mark.asyncio
async def test_A08_get_intent_unmapped_status_falls_back_to_processing(caplog):
    transport = FakeTransport()
    transport.queue_json(200, _query_order_response(ext_status="SOME_NEW_STATUS_NOT_IN_DOCS"))
    adapter = _make_adapter(transport)

    with caplog.at_level(logging.WARNING):
        intent = await adapter.get_intent("mtno")

    assert intent.status == PaymentStatus.PROCESSING
    assert any("unmapped order status" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_A09_get_intent_http_404_raises_not_found():
    transport = FakeTransport()
    transport.queue_json(404, {"status": "FAIL", "code": "404", "errorMessage": "not found"})
    adapter = _make_adapter(transport)

    with pytest.raises(PaymentIntentNotFound):
        await adapter.get_intent("ghost-mtno")


# ---------------------------------------------------------------------------
# verify_webhook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_A10_verify_webhook_valid_pay_success():
    private_key, public_pem = _rsa_keypair()
    transport = FakeTransport()
    adapter = _make_adapter(transport, public_key_pem=public_pem)
    body, headers = _make_webhook(private_key, biz_status="PAY_SUCCESS", merchant_trade_no="order-abc")

    event = await adapter.verify_webhook(body, headers)

    assert event.status == PaymentStatus.CONFIRMED
    assert event.provider == "binance_pay"
    assert event.provider_ref == "order-abc"
    assert event.amount == Decimal("25.00000000")
    assert event.currency == "USDT"
    assert len(transport.calls) == 0  # public key was pinned — no certificate fetch


@pytest.mark.asyncio
async def test_A10b_verify_webhook_returns_the_verified_nonce():
    """Production Payment Activation hardening — the exact
    BinancePay-Nonce value that was cryptographically verified as part
    of the signature is also returned on WebhookEvent.nonce, for
    src/services/webhook_replay_service.py to check/record. Not a second,
    separate verification — the same value already used above."""
    private_key, public_pem = _rsa_keypair()
    transport = FakeTransport()
    adapter = _make_adapter(transport, public_key_pem=public_pem)
    body, headers = _make_webhook(private_key, biz_status="PAY_SUCCESS", nonce="deterministic-test-nonce-001")

    event = await adapter.verify_webhook(body, headers)

    assert event.nonce == "deterministic-test-nonce-001"


@pytest.mark.asyncio
async def test_A11_verify_webhook_invalid_signature_rejected():
    private_key, public_pem = _rsa_keypair()
    _wrong_key, wrong_public_pem = _rsa_keypair()
    transport = FakeTransport()
    # Adapter trusts a DIFFERENT public key than the one that actually signed —
    # simulates a forged/tampered signature.
    adapter = _make_adapter(transport, public_key_pem=wrong_public_pem)
    body, headers = _make_webhook(private_key)

    with pytest.raises(InvalidWebhookSignature):
        await adapter.verify_webhook(body, headers)


@pytest.mark.asyncio
async def test_A11b_verify_webhook_tampered_body_rejected():
    private_key, public_pem = _rsa_keypair()
    transport = FakeTransport()
    adapter = _make_adapter(transport, public_key_pem=public_pem)
    body, headers = _make_webhook(private_key, total_fee="25.00000000")
    tampered_body = body.replace(b"25.00000000", b"99999.00000000")

    with pytest.raises(InvalidWebhookSignature):
        await adapter.verify_webhook(tampered_body, headers)


@pytest.mark.asyncio
async def test_A12_verify_webhook_missing_headers_rejected():
    transport = FakeTransport()
    adapter = _make_adapter(transport, public_key_pem="irrelevant")

    with pytest.raises(InvalidWebhookSignature):
        await adapter.verify_webhook(b'{"bizStatus":"PAY_SUCCESS"}', {})


@pytest.mark.asyncio
async def test_A13_verify_webhook_stale_timestamp_rejected():
    private_key, public_pem = _rsa_keypair()
    transport = FakeTransport()
    adapter = _make_adapter(transport, public_key_pem=public_pem, webhook_max_clock_skew_seconds=60)
    stale_timestamp = str(int((time.time() - 3600) * 1000))  # 1 hour old, way past the 60s window
    body, headers = _make_webhook(private_key, timestamp=stale_timestamp)

    with pytest.raises(InvalidWebhookSignature):
        await adapter.verify_webhook(body, headers)


@pytest.mark.asyncio
async def test_A14_verify_webhook_unmapped_biz_status_raises_unknown_event():
    private_key, public_pem = _rsa_keypair()
    transport = FakeTransport()
    adapter = _make_adapter(transport, public_key_pem=public_pem)
    body, headers = _make_webhook(private_key, biz_status="SOME_FUTURE_STATUS")

    with pytest.raises(UnknownWebhookEvent):
        await adapter.verify_webhook(body, headers)


@pytest.mark.asyncio
async def test_A15_verify_webhook_same_payload_verified_twice_is_consistent():
    private_key, public_pem = _rsa_keypair()
    transport = FakeTransport()
    adapter = _make_adapter(transport, public_key_pem=public_pem)
    body, headers = _make_webhook(private_key, merchant_trade_no="dup-order")

    event1 = await adapter.verify_webhook(body, headers)
    event2 = await adapter.verify_webhook(body, headers)

    assert event1.status == event2.status == PaymentStatus.CONFIRMED
    assert event1.provider_ref == event2.provider_ref == "dup-order"
    # Adapter-level parsing is stateless by design — actual double-credit
    # prevention lives in PaymentOrderService (see test_billing_webhook.py W09).


@pytest.mark.asyncio
async def test_A16_verify_webhook_fetches_and_caches_certificate():
    private_key, public_pem = _rsa_keypair()
    transport = FakeTransport()
    transport.queue_json(200, _certificate_success(public_pem))
    adapter = _make_adapter(transport, public_key_pem=None)  # not pinned — must fetch
    body, headers = _make_webhook(private_key)

    event1 = await adapter.verify_webhook(body, headers)
    assert event1.status == PaymentStatus.CONFIRMED
    assert len(transport.calls) == 1
    assert transport.calls[0]["url"].endswith("/binancepay/openapi/certificates")

    # Second verification reuses the cached key — no second fetch.
    body2, headers2 = _make_webhook(private_key, merchant_trade_no="second-order")
    event2 = await adapter.verify_webhook(body2, headers2)
    assert event2.status == PaymentStatus.CONFIRMED
    assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# Construction / registration guard
# ---------------------------------------------------------------------------


def test_A17_adapter_requires_credentials():
    with pytest.raises(ValueError):
        BinancePayAdapter(
            api_key="", secret_key="", base_url="https://bpay.binanceapi.com",
            transport=FakeTransport(),
        )


def test_A18_build_adapter_disabled_returns_none():
    settings = SimpleNamespace(
        binance_pay_enabled=False,
        binance_pay_api_key="k", binance_pay_secret_key="s",
        binance_pay_base_url="https://bpay.binanceapi.com",
        binance_pay_public_key_pem="",
        binance_pay_webhook_max_clock_skew_seconds=300,
    )
    assert build_binance_pay_adapter(settings) is None


@pytest.mark.parametrize("api_key,secret_key", [("", "s"), ("k", ""), ("", "")])
def test_A19_build_adapter_missing_credentials_returns_none(api_key, secret_key):
    settings = SimpleNamespace(
        binance_pay_enabled=True,
        binance_pay_api_key=api_key, binance_pay_secret_key=secret_key,
        binance_pay_base_url="https://bpay.binanceapi.com",
        binance_pay_public_key_pem="",
        binance_pay_webhook_max_clock_skew_seconds=300,
    )
    assert build_binance_pay_adapter(settings) is None


def test_A20_build_adapter_fully_configured_registers():
    settings = SimpleNamespace(
        binance_pay_enabled=True,
        binance_pay_api_key="real-looking-test-key",
        binance_pay_secret_key="real-looking-test-secret",
        binance_pay_base_url="https://bpay.binanceapi.com",
        binance_pay_public_key_pem="",
        binance_pay_webhook_max_clock_skew_seconds=300,
    )
    adapter = build_binance_pay_adapter(settings)
    assert adapter is not None
    assert adapter.provider_name == "binance_pay"

    registry = ProviderRegistry()
    registry.register(adapter)
    assert registry.is_registered("binance_pay") is True
    assert registry.get("binance_pay") is adapter


def test_A21_declares_crypto_one_time_capabilities():
    """Payments Foundation — Binance Pay declares CRYPTO + ONE_TIME only,
    matching its actual integration (hosted checkout, no recurring)."""
    from src.billing.providers.base import PaymentMethod, PaymentType

    adapter = _make_adapter(FakeTransport())
    assert adapter.supported_payment_methods == frozenset({PaymentMethod.CRYPTO})
    assert adapter.supported_payment_types == frozenset({PaymentType.ONE_TIME})


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_A21_no_secret_ever_logged(caplog):
    transport = FakeTransport()
    transport.queue_json(200, _create_order_success())
    api_key = "SUPER-SECRET-API-KEY-MUST-NOT-LOG"
    secret_key = "SUPER-SECRET-SECRET-KEY-MUST-NOT-LOG"
    adapter = BinancePayAdapter(
        api_key=api_key, secret_key=secret_key,
        base_url="https://bpay.binanceapi.com", transport=transport,
    )

    with caplog.at_level(logging.DEBUG):
        await adapter.create_intent(
            amount=Decimal("1.00"), currency="USD", user_id="u", idempotency_key="k",
        )

    full_log_text = "\n".join(r.message for r in caplog.records)
    assert api_key not in full_log_text
    assert secret_key not in full_log_text
    # The HMAC signature itself is a derived, non-reversible value — not a
    # secret in the same sense — but it's still not expected in a log line
    # that only prints the request path (see _post_signed's logger.debug call).
    sent_signature = transport.calls[0]["headers"]["BinancePay-Signature"]
    assert sent_signature not in full_log_text


# ---------------------------------------------------------------------------
# refund() — explicitly out of scope this phase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_A22_refund_not_implemented():
    transport = FakeTransport()
    adapter = _make_adapter(transport)

    with pytest.raises(NotImplementedError):
        await adapter.refund("some-payment-id")


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


def test_A23_merchant_trade_no_deterministic_and_valid():
    a = _merchant_trade_no("po:order-1:confirm:credit")
    b = _merchant_trade_no("po:order-1:confirm:credit")
    c = _merchant_trade_no("po:order-2:confirm:credit")

    assert a == b
    assert a != c
    assert len(a) <= 32
    assert a.isalnum()


@pytest.mark.parametrize(
    "amount,expected",
    [
        (Decimal("25.00"), "25.00"),
        (Decimal("0.00000001"), "0.00000001"),
        (Decimal("1000000"), "1000000"),
    ],
)
def test_A24_format_amount_never_scientific_notation(amount, expected):
    formatted = _format_amount(amount)
    assert formatted == expected
    assert "E" not in formatted.upper()
