"""
NOWPaymentsAdapter — concrete PaymentProvider implementation for
NOWPayments, TRINDATRON's first ACTIVE crypto payment gateway.

Docs verified for this implementation (fetched live, not from training
memory alone — see below for exactly which claims came from where):
  API and endpoint description (Zendesk HelpCenter)
    https://nowpayments.zendesk.com/hc/en-us/articles/21345824322717
  IPN and how to setup (Zendesk HelpCenter)
    https://nowpayments.zendesk.com/hc/en-us/articles/21395546303389
  Official OpenAPI spec (mirrored, apis.guru)
    https://api.apis.guru/v2/specs/nowpayments.io/1.0.0/openapi.json
      -> confirmed: base URL https://api.nowpayments.io, auth header
         x-api-key, GET /v1/payment/{payment_id}, payment_status enum
         (waiting/confirming/confirmed/sending/partially_paid/finished/
         failed/refunded/expired), Payment object field names.
  Official Node.js SDK (NowPaymentsIO/nowpayments-sdk-nodejs, GitHub)
    -> confirmed: IPN signature verification is HMAC-SHA512 over a
       RECURSIVELY key-sorted JSON serialization of the callback body
       ("sortObjectDeep"), header x-nowpayments-sig; currency listing via
       GET /v1/merchant/coins (merchant's own enabled currencies) /
       GET /v1/full-currencies (platform-wide).
  Official JS SDK create-invoice source (NowPaymentsIO/nowpayments-api-js,
  GitHub, src/actions/create-invoice/index.ts)
    -> confirmed the exact POST /v1/invoice request/response field names
       used below (price_amount, price_currency, pay_currency,
       ipn_callback_url, order_id, order_description, success_url,
       cancel_url / id, invoice_url, created_at, updated_at).

Hosted invoice, not the direct Payment API — deliberate choice
--------------------------------------------------------------------
NOWPayments offers two integration shapes:
  1. Direct Payment API (POST /v1/payment) — requires `pay_currency`
     UP FRONT (a payment object is tied to one specific blockchain
     address for one specific currency); the customer never sees a
     NOWPayments-hosted page.
  2. Hosted Invoice (POST /v1/invoice) — currency-agnostic at creation
     time; returns `invoice_url`, a NOWPayments-hosted page where the
     customer picks from NOWPayments' own full currency list and pays.

This adapter uses (2) — matching this mission's own explicit flow
("O cliente poderá selecionar a moeda cripto disponível na NOWPayments")
and the SAME "redirect_url, hosted checkout" shape BinancePayAdapter
already established (PaymentIntent.redirect_url, unchanged contract).
This also sidesteps a real integration hazard: the Invoice API's own
`id` is NEVER echoed back in the later payment IPN (only `order_id` and
a NEW `payment_id`, generated once the customer actually picks a
currency, are) — so `order_id` (what WE send at invoice-creation time)
is the only stable identifier that survives round-trip. See
create_intent()'s own docstring for how this is threaded through
PaymentIntent.id / PaymentOrder.external_order_id without any change to
the generic PaymentProvider contract or the generic webhook route's
lookup-by-external_order_id logic.

Nonce substitute for replay protection — deliberate, documented choice
--------------------------------------------------------------------------
Unlike Binance Pay (BinancePay-Nonce header), NOWPayments' IPN carries
NO per-delivery nonce or timestamp of its own — verified against the
official docs above, not assumed. `WebhookEvent.nonce` is set here to
the VERIFIED signature itself (`x-nowpayments-sig`): the signature is a
deterministic HMAC of the (sorted) body, so an identical redelivery
(a genuine replay, or NOWPayments retrying because it never got a 200)
produces the EXACT same signature, while any genuinely new status
transition (waiting -> confirming -> finished) has different body
content and therefore a different signature. This reuses
src/services/webhook_replay_service.py completely unmodified — "menor
solução compatível com o banco existente", not a parallel mechanism.

Money handling
--------------
NOWPayments' `price_amount` is a JSON NUMBER, not a string (verified via
a real example request in the official docs) — the ONE place in this
adapter that converts Decimal -> float. This is safe ONLY because
src/billing/pricing.py's own catalog is always a plain 2-decimal-place
USD value (9.99, 29.99, 89.99, 249.99) — never a value whose float64
round-trip could lose meaningful precision. This adapter does not
attempt to support arbitrary-precision Decimal amounts through this one
JSON field; if that ever changes, this conversion needs revisiting.

Currency selection UI — deliberately NOT built in this phase
------------------------------------------------------------------
`list_supported_currencies()` below (GET /v1/merchant/coins) exists and
is tested, but nothing calls it from a route yet — the hosted invoice
page already shows the customer every currency NOWPayments enables for
this merchant natively; building a SECOND, TRINDATRON-side currency
picker before the checkout literally needs one would be exactly the
"não criar lógica especial desnecessária" this phase was told to avoid.

Known gaps — deliberately NOT implemented (see docs)
--------------------------------------------------------------------
* Recurring/subscription billing — explicitly out of scope this phase.
* Refunds (refund() raises NotImplementedError, matching
  BinancePayAdapter's own — PaymentOrderService.refund_order() already
  reverses credits TRINDATRON-side; PlanPurchaseNotRefundable still
  blocks this for plan-purchase orders regardless of provider).
* `refunded`/`expired` IPN statuses map to PaymentStatus.REFUNDED/EXPIRED
  but receive_webhook() has no dedicated branch for either (matching
  Binance Pay's own PAY_CLOSED->EXPIRED gap) — both fall through to the
  existing generic "unhandled event status" log line, never crash, never
  wrongly activate/credit anything. Order expiration is TRINDATRON's own
  authority (payment_order_service.expire_overdue_orders()), not the
  provider's.
* A TRINDATRON-side currency picker (see above).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Protocol

import httpx

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

_CREATE_INVOICE_PATH = "/v1/invoice"
_PAYMENT_STATUS_PATH = "/v1/payment/{payment_id}"
_MERCHANT_COINS_PATH = "/v1/merchant/coins"

# Official payment_status enum (verified against the live OpenAPI spec —
# see module docstring). Only "finished" means "money has actually
# arrived and settled" — every other value is either still in flight or
# a terminal non-success, and NONE of them may be treated as completed.
_STATUS_MAP: dict[str, PaymentStatus] = {
    "waiting": PaymentStatus.PROCESSING,
    "confirming": PaymentStatus.PROCESSING,
    "confirmed": PaymentStatus.PROCESSING,
    "sending": PaymentStatus.PROCESSING,
    "partially_paid": PaymentStatus.PROCESSING,
    "finished": PaymentStatus.CONFIRMED,
    "failed": PaymentStatus.FAILED,
    "refunded": PaymentStatus.REFUNDED,
    "expired": PaymentStatus.EXPIRED,
}


class NOWPaymentsError(PaymentCreationRejected):
    """A NOWPayments API call completed but returned a non-success result.

    Distinct from ProviderUnavailable (network/5xx/timeout — see
    HttpxNOWPaymentsTransport) — this means NOWPayments' API responded
    and explicitly rejected the request or reported an error body.
    Inherits from PaymentCreationRejected (Payment Router etapa) — see
    that class's own docstring for why: a real response was received,
    so no charge/invoice was created provider-side, making this safe
    for a provider-agnostic caller to treat as fail-over-eligible.
    """

    def __init__(self, status_code: int, message: str | None) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"NOWPayments API error: status={status_code} message={message!r}")


# ---------------------------------------------------------------------------
# Transport — same injectable-seam pattern as BinancePayTransport, so tests
# never open a real socket (mission requirement: no real API calls in tests).
# ---------------------------------------------------------------------------


class NOWPaymentsTransport(Protocol):
    """Minimal HTTP transport contract — one method each for GET/POST,
    raw bytes in and out. No retry/timeout policy baked in here; that is
    the transport's job, kept separate from this adapter's own request-
    building/parsing/mapping logic."""

    async def post(self, url: str, *, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
        ...

    async def get(self, url: str, *, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        ...


class HttpxNOWPaymentsTransport:
    """Default transport — a real network call via httpx. Only ever
    reached if settings.nowpayments_enabled=True AND a real API key is
    configured (see build_nowpayments_adapter()) — never true in this
    repository/environment, so never exercised by the automatic test
    suite."""

    async def post(self, url: str, *, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, content=body)
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(f"NOWPayments request timed out: {exc}") from None
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"NOWPayments request failed: {exc}") from None
        return resp.status_code, resp.content

    async def get(self, url: str, *, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(f"NOWPayments request timed out: {exc}") from None
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"NOWPayments request failed: {exc}") from None
        return resp.status_code, resp.content


# ---------------------------------------------------------------------------
# Pure helpers — independently testable, no I/O.
# ---------------------------------------------------------------------------


def _nowpayments_order_id(idempotency_key: str) -> str:
    """NOWPayments' own documented constraints on order_id length/charset
    were not confirmed against a literal spec field (no maxLength/pattern
    surfaced in the mirrored OpenAPI excerpt this session could reach) —
    rather than assume our idempotency_key (up to 128 chars, may contain
    ':') is always accepted verbatim, this deterministically hashes into
    a fixed-length hex string, exactly the same defensive pattern
    BinancePayAdapter's own _merchant_trade_no() already established for
    the identical reason. Same idempotency_key always yields the same
    order_id — a retried create_intent() call maps to the same value
    NOWPayments-side too, not a second one."""
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]


def _sort_recursive(value: Any) -> Any:
    """Recursively sort dict keys (and, for consistency, leave list order
    untouched — NOWPayments' own sortObjectDeep reference implementation
    sorts nested OBJECT keys, not array element order) — see module
    docstring for why this must be recursive, verified against the
    official Node.js SDK's own sortObjectDeep, not assumed from the
    simpler top-level-only description alone."""
    if isinstance(value, dict):
        return {k: _sort_recursive(value[k]) for k in sorted(value.keys())}
    if isinstance(value, list):
        return [_sort_recursive(v) for v in value]
    return value


def _verify_ipn_signature(*, ipn_secret: str, raw_body: bytes, signature_hex: str) -> bool:
    """HMAC-SHA512 over a recursively key-sorted, compact-JSON
    re-serialization of the callback body — see module docstring.
    Returns False (never raises) for any malformed input."""
    try:
        parsed = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    sorted_body = _sort_recursive(parsed)
    canonical = json.dumps(sorted_body, separators=(",", ":"), ensure_ascii=False)
    expected = hmac.new(ipn_secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature_hex.lower())


def _decimal_or_none(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


def _format_price_amount(amount: Decimal) -> float:
    """See module docstring's "Money handling" section — safe ONLY for
    the plain 2-decimal-place USD values src/billing/pricing.py's own
    catalog ever produces."""
    return float(amount)


def _get_header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup — same rationale as
    BinancePayAdapter's own _get_header()."""
    val = headers.get(name)
    if val is not None:
        return val
    lname = name.lower()
    for k, v in headers.items():
        if k.lower() == lname:
            return v
    return None


def _parse_json(raw: bytes) -> dict:
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise NOWPaymentsError(0, f"Non-JSON response from NOWPayments: {exc}") from None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class NOWPaymentsAdapter(ExchangePaymentProvider):
    """NOWPayments — exchange-hosted crypto payment (custody/conversion
    stay with NOWPayments; TRINDATRON receives an IPN when payment
    settles). See ExchangePaymentProvider's own docstring in base.py."""

    def __init__(
        self,
        *,
        api_key: str,
        ipn_secret: str,
        base_url: str,
        transport: NOWPaymentsTransport,
        success_url: str | None = None,
        cancel_url: str | None = None,
        ipn_callback_url: str | None = None,
        request_timeout_seconds: float = 15.0,
    ) -> None:
        if not api_key:
            raise ValueError("NOWPaymentsAdapter requires a non-empty api_key")
        if not ipn_secret:
            raise ValueError("NOWPaymentsAdapter requires a non-empty ipn_secret")
        if not base_url:
            raise ValueError("NOWPaymentsAdapter requires a non-empty base_url")
        self._api_key = api_key
        self._ipn_secret = ipn_secret
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._success_url = success_url
        self._cancel_url = cancel_url
        self._ipn_callback_url = ipn_callback_url
        self._request_timeout_seconds = request_timeout_seconds

    @property
    def provider_name(self) -> str:
        return "nowpayments"

    @property
    def supported_payment_methods(self) -> frozenset[PaymentMethod]:
        return frozenset({PaymentMethod.CRYPTO})

    @property
    def supported_payment_types(self) -> frozenset[PaymentType]:
        # No real auto-recurring charge exists at NOWPayments (manual
        # re-invoice or custodied-balance debit only, per the payments
        # architecture research) — declare ONE_TIME only, matching
        # actual capability.
        return frozenset({PaymentType.ONE_TIME})

    # ------------------------------------------------------------------
    # Request plumbing
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        # Never logged, never returned to any caller — see module
        # docstring's own security notes and _post()'s debug log below,
        # which logs the path only.
        return {"x-api-key": self._api_key, "Content-Type": "application/json"}

    async def _post(self, path: str, body_dict: dict) -> tuple[int, bytes]:
        body = json.dumps(body_dict, separators=(",", ":"), ensure_ascii=False)
        url = f"{self._base_url}{path}"
        logger.debug("nowpayments: POST %s", path)  # path only — never headers/body (API key/PII)
        try:
            return await self._transport.post(
                url, headers=self._headers(), body=body.encode("utf-8"), timeout=self._request_timeout_seconds,
            )
        except ProviderUnavailable:
            raise
        except Exception as exc:
            raise ProviderUnavailable(f"NOWPayments request to {path} failed: {exc}") from exc

    async def _get(self, path: str) -> tuple[int, bytes]:
        url = f"{self._base_url}{path}"
        logger.debug("nowpayments: GET %s", path)
        try:
            return await self._transport.get(url, headers=self._headers(), timeout=self._request_timeout_seconds)
        except ProviderUnavailable:
            raise
        except Exception as exc:
            raise ProviderUnavailable(f"NOWPayments request to {path} failed: {exc}") from exc

    # ------------------------------------------------------------------
    # PaymentProvider interface
    # ------------------------------------------------------------------

    async def create_intent(
        self, *, amount: Decimal, currency: str, user_id: str, idempotency_key: str, metadata: dict | None = None,
    ) -> PaymentIntent:
        """Creates a NOWPayments hosted invoice. `PaymentIntent.id` is set
        to OUR OWN deterministic order_id (see _nowpayments_order_id()),
        NOT the invoice's own `id` field — the invoice `id` is never
        echoed back in the later payment IPN (only `order_id` is), so
        using it as `PaymentOrder.external_order_id` would make the
        generic webhook route's lookup-by-external_order_id never match
        anything. See module docstring for the full reasoning."""
        order_id = _nowpayments_order_id(idempotency_key)
        goods_name = (metadata or {}).get("goods_name", "TRINDATRON plan")
        body: dict = {
            "price_amount": _format_price_amount(amount),
            "price_currency": currency.lower(),
            "order_id": order_id,
            "order_description": goods_name,
        }
        if self._ipn_callback_url:
            body["ipn_callback_url"] = self._ipn_callback_url
        if self._success_url:
            body["success_url"] = self._success_url
        if self._cancel_url:
            body["cancel_url"] = self._cancel_url
        pay_currency = (metadata or {}).get("pay_currency")
        if pay_currency:
            body["pay_currency"] = pay_currency

        status_code, resp_body = await self._post(_CREATE_INVOICE_PATH, body)
        data = _parse_json(resp_body)
        if status_code >= 400 or "invoice_url" not in data:
            raise NOWPaymentsError(status_code, data.get("message") or data.get("code"))

        return PaymentIntent(
            id=order_id,
            provider=self.provider_name,
            amount=amount,
            currency=currency.upper(),
            status=PaymentStatus.PENDING,
            idempotency_key=idempotency_key,
            redirect_url=data.get("invoice_url"),
            metadata={"invoice_id": data.get("id"), "order_id": order_id},
        )

    async def get_intent(self, intent_id: str) -> PaymentIntent:
        """`intent_id` here is OUR order_id (see create_intent()'s own
        docstring) — NOWPayments has no "get invoice/order by our own
        order_id" endpoint; GET /v1/payment/{payment_id} needs THEIR
        payment_id, which we only learn from the IPN, not at creation
        time. Without a locally-cached payment_id there is nothing to
        query yet — this is a REAL, documented limitation (not silently
        faked): callers needing live status before any IPN has arrived
        get PaymentIntentNotFound, an honest "nothing to report yet"."""
        raise PaymentIntentNotFound(intent_id)

    async def verify_webhook(self, payload: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        signature = _get_header(headers, "x-nowpayments-sig")
        if not signature:
            raise InvalidWebhookSignature("Missing x-nowpayments-sig header")
        if not _verify_ipn_signature(ipn_secret=self._ipn_secret, raw_body=payload, signature_hex=signature):
            raise InvalidWebhookSignature("NOWPayments IPN signature verification failed")

        data = _parse_json(payload)
        external_status = data.get("payment_status")
        mapped_status = _STATUS_MAP.get(external_status)
        if mapped_status is None:
            raise UnknownWebhookEvent(f"Unhandled NOWPayments payment_status: {external_status!r}")

        return WebhookEvent(
            provider=self.provider_name,
            event_type=f"payment_status:{external_status}",
            payment_id=None,
            # order_id — NOT payment_id/invoice id — round-trips exactly
            # what we sent at invoice-creation time. See module docstring.
            provider_ref=data.get("order_id"),
            status=mapped_status,
            amount=_decimal_or_none(data.get("price_amount")),
            currency=data.get("price_currency"),
            raw_payload=data,
            # No native per-delivery nonce exists (verified, not assumed
            # — see module docstring) — the already-verified signature
            # itself is a correct, deterministic substitute.
            nonce=signature,
        )

    async def refund(self, payment_id: str, *, amount: Decimal | None = None, reason: str = "") -> PaymentResult:
        raise NotImplementedError(
            "NOWPayments refunds are explicitly out of scope for this first crypto "
            "integration. PaymentOrderService.refund_order() already reverses credits "
            "on the TRINDATRON side without touching the provider (and is refused "
            "outright for a plan-purchase order via PlanPurchaseNotRefundable — see "
            "docs/CHECKOUT.md); a real NOWPayments-side refund needs its own API call "
            "(not implemented/verified here) plus a decision on how provider-side and "
            "TRINDATRON-side refunds stay in sync."
        )

    # ------------------------------------------------------------------
    # Convenience — NOT part of the generic PaymentProvider ABC, not
    # wired into any route yet. See module docstring's own "Currency
    # selection UI" section for why building one before it's needed
    # would be exactly the "lógica especial desnecessária" this phase
    # was told to avoid.
    # ------------------------------------------------------------------

    async def list_supported_currencies(self) -> list[str]:
        """GET /v1/merchant/coins — currencies enabled for THIS merchant
        account specifically (not NOWPayments' full global list, which
        would include currencies we haven't enabled for payout)."""
        status_code, resp_body = await self._get(_MERCHANT_COINS_PATH)
        data = _parse_json(resp_body)
        if status_code >= 400:
            raise NOWPaymentsError(status_code, data.get("message") or data.get("code"))
        return list(data.get("selectedCurrencies") or [])


# ---------------------------------------------------------------------------
# Factory — the ONLY place that decides whether NOWPayments gets registered.
# ---------------------------------------------------------------------------


def build_nowpayments_adapter(settings) -> NOWPaymentsAdapter | None:
    """Return a configured NOWPaymentsAdapter, or None if it should not be
    registered. Registered ONLY when settings.nowpayments_enabled is True
    AND both nowpayments_api_key and nowpayments_ipn_secret are non-empty
    — same "never registered by default, never raises for missing config"
    contract as build_binance_pay_adapter()."""
    if not settings.nowpayments_enabled:
        return None
    if not settings.nowpayments_api_key or not settings.nowpayments_ipn_secret:
        return None
    return NOWPaymentsAdapter(
        api_key=settings.nowpayments_api_key,
        ipn_secret=settings.nowpayments_ipn_secret,
        base_url=settings.nowpayments_base_url,
        transport=HttpxNOWPaymentsTransport(),
        success_url=settings.nowpayments_success_url or None,
        cancel_url=settings.nowpayments_cancel_url or None,
        ipn_callback_url=settings.nowpayments_ipn_callback_url or None,
    )
