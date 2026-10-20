"""
BinancePayAdapter — concrete PaymentProvider implementation for Binance Pay.

SANDBOX/TEST PHASE ONLY. See docs/BINANCE_PAY_SANDBOX.md for the full picture,
including what this adapter does NOT do yet.

Binance Pay has NO documented public sandbox environment (checked against
developers.binance.com and merchant.binance.com — only one base URL is
documented anywhere: https://bpay.binanceapi.com, the real production
endpoint real merchants use). Per the task constraint "não inventar um
endpoint" when no sandbox exists, this adapter:
  - defaults `base_url` to that one documented URL — there is no alternative
    to point to instead, and inventing a fake sandbox hostname would be
    worse (silent failure) than being honest that this is the real endpoint;
  - is NEVER auto-registered — see build_binance_pay_adapter() below — unless
    BOTH binance_pay_enabled=True AND real credentials are configured, which
    is true in NO environment in this repository (verified: no BINANCE_PAY_*
    value appears in any committed .env.example or config default);
  - takes its HTTP transport as an injected dependency (BinancePayTransport),
    so the entire request/response/signature/webhook lifecycle is fully
    unit-testable with a fake transport that never opens a socket.

Endpoints implemented — official docs only, no third-party blogs/SDKs used
as authority:
  Create Order (v3)   POST /binancepay/openapi/v3/order
    https://developers.binance.com/en/docs/products/binance-pay-merchant/api-order-create-v3
  Query Order (v2)    POST /binancepay/openapi/v2/order/query
    https://developers.binance.com/docs/binance-pay/api-order-query-v2
    (no v3 query-order page was found; v2 carries no deprecation notice)
  Query Certificate   POST /binancepay/openapi/certificates
    https://developers.binance.com/docs/binance-pay/webhook-query-certificate
  Request signing / common headers
    https://developers.binance.com/docs/binance-pay/api-common
  Webhook signature verification
    https://developers.binance.com/docs/binance-pay/webhook-common
  Webhook payload shape (bizStatus values)
    https://developers.binance.com/docs/binance-pay/order-notification

Request signing (Create Order / Query Order / Query Certificate)
------------------------------------------------------------------
  payload   = f"{timestamp}\\n{nonce}\\n{body}\\n"  (body = compact JSON —
              Binance recomputes the signature over these exact bytes, so
              any reformatting after signing invalidates it)
  signature = hex(HMAC_SHA512(payload, secret_key)).upper()
  Headers:  Content-Type: application/json
            BinancePay-Timestamp: <timestamp>        (ms; same value as payload)
            BinancePay-Nonce: <nonce>                 (same value as payload)
            BinancePay-Certificate-SN: <api_key>      (merchant's OWN API key —
                                                         confusingly the same
                                                         header name means
                                                         something different
                                                         on inbound webhooks)
            BinancePay-Signature: <signature>

Webhook verification (inbound: Binance -> our /billing/webhook/binance_pay)
------------------------------------------------------------------------------
  Headers Binance sends: BinancePay-Timestamp, BinancePay-Nonce,
                          BinancePay-Certificate-SN (here: which of Binance's
                          OWN public certs signed this — not our API key),
                          BinancePay-Signature (base64, not hex)
  payload = f"{timestamp}\\n{nonce}\\n{body}\\n"  (body = the exact raw
            bytes we received, before any JSON parsing)
  Verified via RSA-SHA256 (PKCS1v15) against Binance's public key, fetched
  via the Query Certificate API and cached — never hardcoded, since Binance
  can rotate it. A pinned key can also be supplied via
  settings.binance_pay_public_key_pem to skip that fetch.

Money handling
--------------
Every amount that crosses this module is a Decimal, built from a string
(never a float) both when constructing the outbound request body and when
parsing a response/webhook. Binance's own JSON amounts are already decimal
strings (e.g. "10.00000000"); Decimal(str(x)) is applied uniformly so a
value that happened to arrive as a bare JSON number is never silently
routed through a binary float first.

Webhook acknowledgment format
------------------------------
webhook_ack_body() overrides PaymentProvider's default (None) to return
Binance's documented ack shape ({"returnCode": "SUCCESS", "returnMessage":
null}) — see api/routes/billing.py's _webhook_ack() helper, which every 200
response in receive_webhook() goes through. Without this, Binance would
keep retrying (up to 6 times per order-notification docs) a webhook this
adapter already processed correctly.

Checkout integration
---------------------
POST /billing/orders calls create_provider_order() (payment_order_service.py)
whenever body.provider is registered — see that route's docstring. The
frontend checkout itself (plans/page.tsx) is unchanged, still the manual
mailto flow; only the backend order-creation endpoint gained real
provider integration, ready for a future frontend to use.

Known gaps — deliberately NOT implemented (see docs)
--------------------------------------------------------------------
* Refunds (refund() raises NotImplementedError — Binance-side refund needs
  its own API call this task explicitly excludes).
* Certificate rotation mid-process (the fetched public key is cached for
  the adapter's lifetime; a serial mismatch on a later webhook is not
  detected or auto-refetched).
* The exact Binance error-code string for "order not found" on Query Order
  was not confirmed against a literal doc page — get_intent() only treats
  an HTTP 404 as PaymentIntentNotFound (a safe, generic signal); any other
  non-SUCCESS response raises BinancePayError instead of guessing a code.

Resolved (no longer gaps)
--------------------------
* Nonce-reuse tracking for webhook replay defense — CLOSED (Production
  Payment Activation hardening): `nonce` is now part of the returned
  `WebhookEvent`, checked/recorded via
  src/services/webhook_replay_service.py, DB-backed (webhook_nonces
  table, migration 0024), multi-process-safe by construction (UNIQUE
  constraint is the arbiter). See that module's own docstring for the
  full crash-safety analysis — the pre-existing order-state idempotency
  this note used to lean on entirely is now a SECOND, still-intact layer
  of defense, not the only one.
* PaymentOrder confirmation connected to user.plan/entitlements — CLOSED
  (Commercial Checkout V1, docs/CHECKOUT.md) — a plan-purchase order
  (PaymentOrder.plan set) activates user.plan atomically on confirmation.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import string
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from src.billing.providers.base import (
    ExchangePaymentProvider,
    InvalidWebhookSignature,
    PaymentCreationRejected,
    PaymentIntent,
    PaymentIntentNotFound,
    PaymentMethod,
    PaymentResult,
    PaymentStatus,
    PaymentType,
    ProviderUnavailable,
    UnknownWebhookEvent,
    WebhookEvent,
)

logger = logging.getLogger(__name__)

_CREATE_ORDER_PATH = "/binancepay/openapi/v3/order"
_QUERY_ORDER_PATH = "/binancepay/openapi/v2/order/query"
_QUERY_CERTIFICATE_PATH = "/binancepay/openapi/certificates"

# a-z, A-Z only, 32 chars — per api-common docs for BinancePay-Nonce.
_NONCE_ALPHABET = string.ascii_letters
_NONCE_LENGTH = 32

# Create/Query Order status -> canonical PaymentStatus.
# Binance's own enum (per api-order-query-v2): INITIAL, PENDING, PAID, CANCELED.
_ORDER_STATUS_MAP: dict[str, PaymentStatus] = {
    "INITIAL": PaymentStatus.PENDING,
    "PENDING": PaymentStatus.PENDING,
    "PAID": PaymentStatus.CONFIRMED,
    "CANCELED": PaymentStatus.CANCELLED,
}

# Webhook bizStatus -> canonical PaymentStatus (per order-notification docs).
# PAY_CLOSED is documented as "closed/expired" — mapped to EXPIRED rather
# than CANCELLED as the closer semantic match for a timed-out checkout link;
# this is an interpretation choice, not something the docs state explicitly.
_WEBHOOK_BIZ_STATUS_MAP: dict[str, PaymentStatus] = {
    "PAY_SUCCESS": PaymentStatus.CONFIRMED,
    "PAY_CLOSED": PaymentStatus.EXPIRED,
    "PAY_FAIL": PaymentStatus.FAILED,
}


class BinancePayError(PaymentCreationRejected):
    """A Binance Pay API call completed but returned a non-SUCCESS result.

    Distinct from ProviderUnavailable (network/5xx failure — see
    HttpxBinancePayTransport) — this means Binance's API responded and
    explicitly rejected the request/reported a business-logic failure.
    Inherits from PaymentCreationRejected (Payment Router etapa) — see
    that class's own docstring for why this is safe to fail over on.
    """

    def __init__(self, code: str | None, message: str | None) -> None:
        self.code = code
        self.message = message
        super().__init__(f"Binance Pay API error: code={code!r} message={message!r}")


# ---------------------------------------------------------------------------
# Transport — the one network-touching seam. Injected so tests never hit
# a real socket; production wiring uses HttpxBinancePayTransport.
# ---------------------------------------------------------------------------


class BinancePayTransport(Protocol):
    """Minimal HTTP transport contract. One method, raw bytes in and out —
    no retry/timeout policy baked in here; that is the transport's job, kept
    separate from this adapter's signing/parsing/mapping logic.
    """

    async def post(
        self, url: str, *, headers: dict[str, str], body: bytes, timeout: float
    ) -> tuple[int, bytes]:
        """POST body to url; return (status_code, response_body).

        Must not raise for a non-2xx HTTP response — the caller inspects
        status_code itself. May raise for a network-level failure (timeout,
        connection error, DNS) — the adapter wraps those as ProviderUnavailable.
        """
        ...


class HttpxBinancePayTransport:
    """Default transport — a real network call via httpx.

    Only ever reached if settings.binance_pay_enabled=True AND both
    credentials are configured (see build_binance_pay_adapter()) — neither
    is true in this repository or environment, so this class is never
    exercised by the automatic test suite.
    """

    async def post(
        self, url: str, *, headers: dict[str, str], body: bytes, timeout: float
    ) -> tuple[int, bytes]:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, content=body)
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(f"Binance Pay request timed out: {exc}") from None
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Binance Pay request failed: {exc}") from None
        return resp.status_code, resp.content


# ---------------------------------------------------------------------------
# Signing / verification helpers — pure functions, independently testable.
# ---------------------------------------------------------------------------


def _generate_nonce() -> str:
    return "".join(secrets.choice(_NONCE_ALPHABET) for _ in range(_NONCE_LENGTH))


def _sign_request(*, timestamp: str, nonce: str, body: str, secret_key: str) -> str:
    """hex(HMAC-SHA512(timestamp + '\\n' + nonce + '\\n' + body + '\\n', secret_key)).upper()."""
    payload = f"{timestamp}\n{nonce}\n{body}\n".encode("utf-8")
    digest = hmac.new(secret_key.encode("utf-8"), payload, hashlib.sha512).hexdigest()
    return digest.upper()


def _verify_rsa_signature(
    *, public_key_pem: str, timestamp: str, nonce: str, body: bytes, signature_b64: str
) -> bool:
    """RSA-SHA256/PKCS1v15 verification of an inbound webhook signature.

    Returns False (never raises) for any malformed input — signature
    verification failure is always just "not valid", not an exceptional
    condition the caller needs to distinguish by exception type.
    """
    payload = timestamp.encode("utf-8") + b"\n" + nonce.encode("utf-8") + b"\n" + body + b"\n"
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except Exception:
        return False
    try:
        public_key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
    except Exception:
        return False
    try:
        public_key.verify(signature, payload, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return False
    except Exception:
        # Wrong key type (e.g. an EC key loaded where RSA was expected) or
        # any other verify()-time failure — treated the same as "invalid",
        # never surfaced as a 500.
        return False
    return True


def _merchant_trade_no(idempotency_key: str) -> str:
    """Binance requires merchantTradeNo: string(32), alphanumeric only.

    Our idempotency keys can be longer and contain non-alphanumeric
    characters (e.g. "po:<uuid>:confirm:credit" uses colons), so this
    deterministically hashes into a 32-char hex string. Same idempotency_key
    always yields the same merchantTradeNo — a retried create_intent() call
    maps to the same value Binance-side too, not a second one.
    """
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]


def _format_amount(amount: Decimal) -> str:
    """Plain fixed-point decimal string — never scientific notation, never a float."""
    return format(amount, "f")


def _decimal_or_none(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


def _parse_json(raw: bytes) -> dict:
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BinancePayError(None, f"Non-JSON response from Binance Pay: {exc}") from None


def _get_header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup.

    FastAPI's Request.headers is already case-insensitive (.get() suffices).
    The manual scan below only matters for a plain dict passed directly in
    tests that don't use FastAPI's Headers type.
    """
    val = headers.get(name)
    if val is not None:
        return val
    lname = name.lower()
    for k, v in headers.items():
        if k.lower() == lname:
            return v
    return None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class BinancePayAdapter(ExchangePaymentProvider):
    """Binance Pay — exchange-hosted payment (custody/conversion/compliance
    stay with Binance; TRINDATRON receives a webhook when payment is
    confirmed). See ExchangePaymentProvider's own docstring in base.py.
    """

    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        base_url: str,
        transport: BinancePayTransport,
        public_key_pem: str | None = None,
        webhook_max_clock_skew_seconds: int = 300,
        request_timeout_seconds: float = 15.0,
        terminal_type: str = "WEB",
        goods_category: str = "D000",
    ) -> None:
        if not api_key or not secret_key:
            raise ValueError("BinancePayAdapter requires a non-empty api_key and secret_key")
        if not base_url:
            raise ValueError("BinancePayAdapter requires a non-empty base_url")
        self._api_key = api_key
        self._secret_key = secret_key
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._webhook_max_clock_skew_seconds = webhook_max_clock_skew_seconds
        self._request_timeout_seconds = request_timeout_seconds
        self._terminal_type = terminal_type
        self._goods_category = goods_category
        # Cached webhook public key — see module docstring's "Known gaps"
        # re: certificate rotation not being detected mid-process.
        self._cached_public_key_pem: str | None = public_key_pem
        self._cached_certificate_serial: str | None = None

    @property
    def provider_name(self) -> str:
        return "binance_pay"

    @property
    def supported_payment_methods(self) -> frozenset[PaymentMethod]:
        return frozenset({PaymentMethod.CRYPTO})

    @property
    def supported_payment_types(self) -> frozenset[PaymentType]:
        # Binance Pay has no recurring/auto-charge mechanism in this
        # integration — one-time hosted checkout only.
        return frozenset({PaymentType.ONE_TIME})

    # ------------------------------------------------------------------
    # Signed request plumbing
    # ------------------------------------------------------------------

    async def _post_signed(self, path: str, body_dict: dict) -> tuple[int, bytes]:
        body = json.dumps(body_dict, separators=(",", ":"), ensure_ascii=False)
        timestamp = str(int(time.time() * 1000))
        nonce = _generate_nonce()
        signature = _sign_request(
            timestamp=timestamp, nonce=nonce, body=body, secret_key=self._secret_key
        )
        headers = {
            "Content-Type": "application/json",
            "BinancePay-Timestamp": timestamp,
            "BinancePay-Nonce": nonce,
            "BinancePay-Certificate-SN": self._api_key,
            "BinancePay-Signature": signature,
        }
        url = f"{self._base_url}{path}"
        logger.debug("binance_pay: POST %s", path)  # path only — never headers/body (secrets/PII)
        try:
            status_code, resp_body = await self._transport.post(
                url, headers=headers, body=body.encode("utf-8"), timeout=self._request_timeout_seconds
            )
        except ProviderUnavailable:
            raise
        except Exception as exc:
            raise ProviderUnavailable(f"Binance Pay request to {path} failed: {exc}") from exc
        return status_code, resp_body

    # ------------------------------------------------------------------
    # PaymentProvider interface
    # ------------------------------------------------------------------

    async def create_intent(
        self,
        *,
        amount: Decimal,
        currency: str,
        user_id: str,
        idempotency_key: str,
        metadata: dict | None = None,
    ) -> PaymentIntent:
        merchant_trade_no = _merchant_trade_no(idempotency_key)
        goods_name = (metadata or {}).get("goods_name", "TRINDATRON credits")
        body_dict = {
            "env": {"terminalType": self._terminal_type},
            "merchantTradeNo": merchant_trade_no,
            "orderAmount": _format_amount(amount),
            "currency": currency.upper(),
            "description": goods_name,
            "goodsDetails": [
                {
                    "goodsType": "02",  # virtual good — credits are not a physical product
                    "goodsCategory": self._goods_category,
                    "referenceGoodsId": merchant_trade_no,
                    "goodsName": goods_name,
                }
            ],
        }
        status_code, resp_body = await self._post_signed(_CREATE_ORDER_PATH, body_dict)
        data = _parse_json(resp_body)
        if data.get("status") != "SUCCESS":
            raise BinancePayError(data.get("code"), data.get("errorMessage"))

        order_data = data.get("data") or {}
        expire_time_ms = order_data.get("expireTime")
        expires_at = (
            datetime.fromtimestamp(int(expire_time_ms) / 1000, tz=timezone.utc)
            if expire_time_ms is not None
            else datetime.now(timezone.utc) + timedelta(hours=1)
        )
        return PaymentIntent(
            id=merchant_trade_no,
            provider=self.provider_name,
            amount=amount,
            currency=currency.upper(),
            status=PaymentStatus.PENDING,
            idempotency_key=idempotency_key,
            redirect_url=order_data.get("checkoutUrl"),
            expires_at=expires_at,
            created_at=datetime.now(timezone.utc),
            metadata={
                "prepay_id": order_data.get("prepayId"),
                "qr_content": order_data.get("qrContent"),
                "merchant_trade_no": merchant_trade_no,
            },
        )

    async def get_intent(self, intent_id: str) -> PaymentIntent:
        status_code, resp_body = await self._post_signed(
            _QUERY_ORDER_PATH, {"merchantTradeNo": intent_id}
        )
        data = _parse_json(resp_body)
        if data.get("status") != "SUCCESS":
            # See module docstring's "Known gaps": the precise Binance error
            # code for "order not found" wasn't confirmed against a literal
            # doc page, so only a generic HTTP 404 is treated as not-found.
            if status_code == 404:
                raise PaymentIntentNotFound(intent_id)
            raise BinancePayError(data.get("code"), data.get("errorMessage"))

        order_data = data.get("data") or {}
        external_status = order_data.get("status", "")
        mapped_status = _ORDER_STATUS_MAP.get(external_status)
        if mapped_status is None:
            logger.warning(
                "binance_pay: unmapped order status %r for merchantTradeNo=%s",
                external_status, intent_id,
            )
            mapped_status = PaymentStatus.PROCESSING
        amount = _decimal_or_none(order_data.get("orderAmount")) or Decimal("0")

        return PaymentIntent(
            id=intent_id,
            provider=self.provider_name,
            amount=amount,
            currency=order_data.get("currency", ""),
            status=mapped_status,
            idempotency_key=intent_id,
            metadata={
                "transaction_id": order_data.get("transactionId"),
                "merchant_trade_no": intent_id,
            },
        )

    async def verify_webhook(self, payload: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        timestamp = _get_header(headers, "BinancePay-Timestamp")
        nonce = _get_header(headers, "BinancePay-Nonce")
        certificate_sn = _get_header(headers, "BinancePay-Certificate-SN")
        signature_b64 = _get_header(headers, "BinancePay-Signature")

        if not timestamp or not nonce or not signature_b64:
            raise InvalidWebhookSignature("Missing required Binance Pay webhook header(s)")

        # Replay defense: reject a stale timestamp even if the signature is
        # genuinely valid. Does NOT track nonce reuse (see module docstring's
        # "Known gaps") — order-state idempotency in PaymentOrderService is
        # the actual backstop against double-crediting a replayed payload.
        try:
            ts_seconds = int(timestamp) / 1000
        except ValueError:
            raise InvalidWebhookSignature("Malformed BinancePay-Timestamp header") from None
        if abs(time.time() - ts_seconds) > self._webhook_max_clock_skew_seconds:
            raise InvalidWebhookSignature("BinancePay-Timestamp outside the allowed clock-skew window")

        public_key_pem = await self._get_public_key(certificate_sn)
        if not _verify_rsa_signature(
            public_key_pem=public_key_pem,
            timestamp=timestamp,
            nonce=nonce,
            body=payload,
            signature_b64=signature_b64,
        ):
            raise InvalidWebhookSignature("Binance Pay webhook signature verification failed")

        data = _parse_json(payload)
        biz_status = data.get("bizStatus")
        mapped_status = _WEBHOOK_BIZ_STATUS_MAP.get(biz_status)
        if mapped_status is None:
            raise UnknownWebhookEvent(f"Unhandled Binance Pay bizStatus: {biz_status!r}")

        inner_raw = data.get("data")
        inner: dict[str, Any] = {}
        if isinstance(inner_raw, str):
            try:
                inner = json.loads(inner_raw)
            except json.JSONDecodeError:
                inner = {}
        elif isinstance(inner_raw, dict):
            inner = inner_raw

        return WebhookEvent(
            provider=self.provider_name,
            event_type=f"PAY:{biz_status}",
            payment_id=None,
            provider_ref=inner.get("merchantTradeNo"),
            status=mapped_status,
            amount=_decimal_or_none(inner.get("totalFee")),
            currency=inner.get("currency"),
            raw_payload=data,
            # Already extracted and cryptographically verified above (it's
            # part of what BinancePay-Signature covers) — reused here for
            # replay detection (see webhook_replay_service.py), not a
            # second, separate verification.
            nonce=nonce,
        )

    def webhook_ack_body(self) -> dict | None:
        """Binance's documented webhook ack shape — required or Binance keeps
        retrying a notification that was already processed correctly (up to
        6 times, per order-notification docs).
        """
        return {"returnCode": "SUCCESS", "returnMessage": None}

    async def refund(
        self, payment_id: str, *, amount: Decimal | None = None, reason: str = ""
    ) -> PaymentResult:
        raise NotImplementedError(
            "Binance Pay refunds are explicitly out of scope for this sandbox/test "
            "phase. PaymentOrderService.refund_order() already reverses credits on "
            "the TRINDATRON side without touching the provider; a real Binance-side "
            "refund needs its own API call (not implemented/verified here) plus a "
            "decision on how provider-side and TRINDATRON-side refunds stay in sync."
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _get_public_key(self, certificate_sn: str | None) -> str:
        if self._cached_public_key_pem is not None:
            return self._cached_public_key_pem

        status_code, resp_body = await self._post_signed(_QUERY_CERTIFICATE_PATH, {})
        data = _parse_json(resp_body)
        if data.get("status") != "SUCCESS":
            raise ProviderUnavailable(
                f"Could not fetch Binance Pay certificate: {data.get('errorMessage')}"
            )
        cert_data = data.get("data") or {}
        public_key_pem = cert_data.get("certPublic")
        if not public_key_pem:
            raise ProviderUnavailable("Binance Pay certificate response missing certPublic")
        self._cached_public_key_pem = public_key_pem
        self._cached_certificate_serial = cert_data.get("certSerial")
        return public_key_pem


# ---------------------------------------------------------------------------
# Factory — the ONLY place that decides whether Binance Pay gets registered.
# ---------------------------------------------------------------------------


def build_binance_pay_adapter(settings) -> BinancePayAdapter | None:
    """Return a configured BinancePayAdapter, or None if it should not be registered.

    Registered ONLY when settings.binance_pay_enabled is True AND both
    binance_pay_api_key and binance_pay_secret_key are non-empty. Any other
    combination — including every default in this repository — returns None,
    so main.py's lifespan simply does not call registry.register() for it.
    Absence of Binance Pay must never prevent the Core from booting; this
    function raising would defeat that, so it deliberately cannot raise for
    "not configured" — only ValueError from BinancePayAdapter's own
    constructor guard would propagate, and that guard only fires if this
    function's own condition already let an empty credential through, which
    it cannot.
    """
    if not settings.binance_pay_enabled:
        return None
    if not settings.binance_pay_api_key or not settings.binance_pay_secret_key:
        return None
    return BinancePayAdapter(
        api_key=settings.binance_pay_api_key,
        secret_key=settings.binance_pay_secret_key,
        base_url=settings.binance_pay_base_url,
        transport=HttpxBinancePayTransport(),
        public_key_pem=settings.binance_pay_public_key_pem or None,
        webhook_max_clock_skew_seconds=settings.binance_pay_webhook_max_clock_skew_seconds,
    )
