"""
MercadoPagoAdapter — concrete PaymentProvider implementation for Mercado
Pago, TRINDATRON's first ACTIVE FiatPaymentProvider (Payments Foundation
etapa, 2026-09-08).

Docs verified for this implementation (fetched live via web search/fetch
against mercadopago.com.br/.ar/.co/.mx developer portals this session —
not from training memory alone; the portal's own pages are a
JS-rendered SPA that repeatedly 404'd on direct fetch, so claims below
are corroborated across MULTIPLE independent search results rather than
a single page dump — see each section for what was actually confirmed):
  Create preference (Checkout Pro)
    POST https://api.mercadopago.com/checkout/preferences
    -> confirmed: items[]/back_urls/notification_url/auto_return request
       shape, init_point/sandbox_init_point response fields.
  Payment methods configuration (Checkout Pro)
    -> confirmed: payment_methods.excluded_payment_types /
       excluded_payment_methods narrows which methods the hosted page
       shows; "ticket" (boleto) confirmed as a real payment_type_id in
       an official exclusion example; "bank_transfer" confirmed as
       Pix's payment_type_id (Pix payment_method.id="pix",
       type="bank_transfer", in the official Pix integration docs);
       "credit_card"/"debit_card" are standard MP payment_type_id
       values seen across multiple official reference pages (get
       payment / get payment methods) but not independently re-verified
       against a single canonical enum page this session — see
       _EXCLUDABLE_PAYMENT_TYPE_IDS' own comment.
  Webhooks / notifications
    -> confirmed: notification body carries top-level `id` (the
       delivery's own id), `type`/`action`, and `data.id` (the
       resource id — for a payment notification, the MP payment id).
       Official guidance explicitly says to fetch the payment via the
       Payments API and treat only "approved" as money received — never
       trust the webhook body's own fields for the money decision.
  Webhook signature (x-signature / x-request-id)
    -> confirmed: header format `ts=<ms>,v1=<hex>`; manifest template
       `id:{data.id};request-id:{x-request-id};ts:{ts};` (segments for
       any header that is actually absent are dropped, per official
       guidance); HMAC-SHA256 over the manifest with the app's webhook
       secret, hex digest compared to v1. data.id is lowercased before
       use (documented requirement for the newer alphanumeric Orders
       API ids; a no-op for classic numeric Payments API ids, applied
       unconditionally as defense-in-depth).
  Get payment / Search payments
    GET https://api.mercadopago.com/v1/payments/{id}
    GET https://api.mercadopago.com/v1/payments/search?external_reference=...
    -> confirmed both endpoints exist via official reference pages;
       response field names (id, status, status_detail, transaction_amount,
       currency_id, external_reference, payment_method_id, payment_type_id)
       confirmed via official reference/example pages. The Search
       endpoint's exact `{"results": [...]}` envelope follows this API's
       long-established, stable convention rather than being
       independently re-fetched this session (see get_intent()'s own
       docstring).
  Refunds
    POST https://api.mercadopago.com/v1/payments/{id}/refunds
    -> confirmed: optional `amount` body field (partial refund; omitted
       = full refund), X-Idempotency-Key documented as MANDATORY for
       this endpoint, response fields (id, payment_id, amount, status).
  Idempotency (Payments/Refunds API)
    -> confirmed: X-Idempotency-Key header, mandatory for Payments and
       Refunds specifically. NOT confirmed as documented/enforced for
       Checkout Pro's Preferences endpoint — sent anyway (harmless if
       ignored), but see create_intent()'s own docstring for why
       TRINDATRON's own idempotency (already proven — see
       payment_order_service.create_provider_order()) is what actually
       prevents a duplicate preference here, not a provider guarantee.
  Payment status values
    -> confirmed: pending, in_process, approved, rejected, cancelled,
       refunded are all documented main statuses. Two DIFFERENT sources
       described a dispute-in-progress state under two DIFFERENT names
       ("in_mediation" in one payment-status overview, "charged_back"
       with status_detail transitioning in_process->settled/reimbursed
       in the dedicated chargebacks doc) — this session could not
       reconcile whether these are the same concept under different API
       versions or genuinely distinct values. Neither is mapped to a
       canonical PaymentStatus below (see _STATUS_MAP); an event
       reporting either raises UnknownWebhookEvent — logged, 200-acked,
       and no plan/credit is ever touched by it. See docs/CHECKOUT.md
       once chargeback handling is prioritized.
  Countries / currency
    -> confirmed: Mercado Pago operates in Argentina, Brazil, Chile,
       Colombia, Mexico, Peru, Uruguay with currency_id ARS/BRL/CLP/
       COP/MXN/PEN/UYU respectively. This adapter is currency-agnostic
       (currency is whatever src/billing/pricing.py's catalog priced
       the order in) — no BRL-only assumption is hardcoded.
  Subscriptions / recurring
    -> confirmed: real recurring billing exists via a COMPLETELY
       separate resource, POST /preapproval (`auto_recurring` object:
       frequency, frequency_type, transaction_amount, currency_id).
       Explicitly NOT implemented here — see "Known gaps" below and
       Subscription's own module docstring; Subscription rows stay
       inert until a dedicated subscription-engine phase builds this.
  Python SDK
    -> confirmed official `mercadopago` package exists
       (github.com/mercadopago/sdk-python, `pip install mercadopago`,
       Python 3.9+) but is NOT used here — this adapter makes raw HTTP
       calls through an injectable transport, exactly like
       NOWPaymentsAdapter/BinancePayAdapter, so tests never need network
       access or a vendored SDK dependency.

Why Checkout Pro (hosted preference), not the direct Payments API
-------------------------------------------------------------------
The direct Payments API (POST /v1/payments) can create a Pix or boleto
payment with no card data at all, but a CARD payment through that same
endpoint requires a `token` — produced by Mercado Pago's client-side JS
SDK/Card Form tokenizing raw card data in the browser BEFORE it ever
reaches a backend. Building that flow needs a frontend integration this
phase does not touch (packages/web is out of scope here). Checkout Pro
sidesteps this entirely: ONE endpoint (create a preference) covers Pix,
boleto, AND cards, because Mercado Pago's OWN hosted page collects
whatever the payer picks — no card byte ever reaches TRINITY Core. This
is the exact same "hosted checkout, redirect_url, PCI stays with the
provider" shape BinancePayAdapter/NOWPaymentsAdapter already established
(PaymentIntent.redirect_url), not a new pattern.

Because of this, `supported_payment_methods` below genuinely includes
PIX, CREDIT_CARD, DEBIT_CARD, and BOLETO — all four are real payment
methods Checkout Pro's own hosted page presents to a Brazilian payer,
confirmed against official docs (not the commercial-catalog superset
Mercado Pago markets more broadly for methods this adapter's actual
flow does not touch, e.g. BANK_TRANSFER/WALLET as distinct methods,
open finance, in-person QR).

Identity: external_reference, not the preference id
-------------------------------------------------------------------
`PaymentIntent.id` here is a TRINITY-derived deterministic reference
(see `_deterministic_reference()`), set as the preference's own
`external_reference` at creation time — NOT Mercado Pago's preference
`id`. This mirrors NOWPaymentsAdapter's own order_id workaround for the
identical structural problem: the resource a webhook later reports
(a PAYMENT) is a completely different object, created only once the
payer actually completes checkout, from the resource this method
creates (a PREFERENCE). external_reference is Mercado Pago's own
documented mechanism for propagating a merchant-supplied identifier
from the preference onto the resulting payment — confirmed via the Pix/
Orders integration docs' own external_reference usage, and is the only
field guaranteed to round-trip. verify_webhook() reads it back off the
fetched Payment object (never off the webhook body's own fields) to
resolve `WebhookEvent.provider_ref`, which the generic webhook route
matches against `PaymentOrder.external_order_id` — see billing.py's own
receive_webhook() step 4.

Money handling
--------------
Every amount that crosses the wire is converted via `float(Decimal)` —
safe ONLY for the plain 2-decimal-place values src/billing/pricing.py's
own catalog ever produces (same documented caveat as NOWPaymentsAdapter
has for the identical reason).

Known gaps — deliberately NOT implemented (see docs)
--------------------------------------------------------------------
* Recurring/subscription billing (POST /preapproval) — a completely
  separate resource and flow from everything in this file; wiring it up
  is explicitly the next subscription-engine phase's job, not this
  adapter's. `supported_payment_types` below is {ONE_TIME} only.
* Chargeback/dispute handling — "in_mediation"/"charged_back" webhook
  events raise UnknownWebhookEvent (logged, 200-acked, no order
  mutation) — see the module docstring's "Payment status values"
  section for why neither is safely mappable yet.
* refund()'s `payment_id` parameter is documented here as MERCADO
  PAGO'S OWN numeric payment id — NOT `PaymentOrder.id` and NOT
  `PaymentOrder.external_order_id` (which holds the external_reference,
  a different identifier space — see above). Nothing in this codebase
  calls `adapter.refund()` today (PaymentOrderService.refund_order()
  reverses credits TRINDATRON-side only, and is refused outright for a
  plan-purchase order via PlanPurchaseNotRefundable regardless of
  provider — see docs/CHECKOUT.md); this method exists as a real,
  working primitive for a FUTURE caller, not a currently-exercised path.
* Preference-level card-brand/installment restriction, split payments,
  marketplace fees — none of this mission's checkout flow needs them.
* Checkout Pro's `payment_methods.excluded_payment_types` narrowing
  (see `_EXCLUDABLE_PAYMENT_TYPE_IDS`) is best-effort UI guidance, not a
  hard technical guarantee for every account configuration — Mercado
  Pago's own docs note "Cash in account" can never be excluded, and
  this adapter does not attempt to suppress it.
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
    FiatPaymentProvider,
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

_CREATE_PREFERENCE_PATH = "/checkout/preferences"
_GET_PAYMENT_PATH = "/v1/payments/{payment_id}"
_SEARCH_PAYMENTS_PATH = "/v1/payments/search"
_REFUND_PATH = "/v1/payments/{payment_id}/refunds"

# Official main payment statuses (verified — see module docstring).
# "in_mediation"/"charged_back" are deliberately absent — see module
# docstring's "Payment status values" section for why.
_STATUS_MAP: dict[str, PaymentStatus] = {
    "pending": PaymentStatus.PROCESSING,
    "in_process": PaymentStatus.PROCESSING,
    "authorized": PaymentStatus.PROCESSING,
    "approved": PaymentStatus.CONFIRMED,
    "rejected": PaymentStatus.FAILED,
    "cancelled": PaymentStatus.CANCELLED,
    "refunded": PaymentStatus.REFUNDED,
}

# payment_type_id values usable in a preference's
# payment_methods.excluded_payment_types — see module docstring's
# "Why Checkout Pro" / trailing "Known gaps" notes on confidence level.
# Only ONE (PIX -> "bank_transfer", BOLETO -> "ticket") comes from a
# method-specific official doc page found this session; CREDIT_CARD/
# DEBIT_CARD map to their own conventional payment_type_id, seen
# consistently across multiple official reference pages but not pinned
# to one canonical enum listing this session.
_EXCLUDABLE_PAYMENT_TYPE_IDS: dict[PaymentMethod, str] = {
    PaymentMethod.PIX: "bank_transfer",
    PaymentMethod.BOLETO: "ticket",
    PaymentMethod.CREDIT_CARD: "credit_card",
    PaymentMethod.DEBIT_CARD: "debit_card",
}


class MercadoPagoError(PaymentCreationRejected):
    """A Mercado Pago API call completed but returned a non-success result.

    Distinct from ProviderUnavailable (network/5xx/timeout — see
    HttpxMercadoPagoTransport) — this means Mercado Pago's API responded
    and explicitly rejected the request or reported an error body.
    Inherits from PaymentCreationRejected (Payment Router etapa) — see
    that class's own docstring for why this is safe to fail over on.
    """

    def __init__(self, status_code: int, message: str | None) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"Mercado Pago API error: status={status_code} message={message!r}")


# ---------------------------------------------------------------------------
# Transport — same injectable-seam pattern as NOWPaymentsTransport/
# BinancePayTransport, so tests never open a real socket.
# ---------------------------------------------------------------------------


class MercadoPagoTransport(Protocol):
    """Minimal HTTP transport contract — one method each for GET/POST,
    raw bytes in and out."""

    async def post(self, url: str, *, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
        ...

    async def get(self, url: str, *, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        ...


class HttpxMercadoPagoTransport:
    """Default transport — a real network call via httpx. Only ever
    reached if settings.mercadopago_enabled=True AND a real access token
    is configured (see build_mercadopago_adapter()) — never true in this
    repository/environment, so never exercised by the automatic test
    suite."""

    async def post(self, url: str, *, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, content=body)
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(f"Mercado Pago request timed out: {exc}") from None
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Mercado Pago request failed: {exc}") from None
        return resp.status_code, resp.content

    async def get(self, url: str, *, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(f"Mercado Pago request timed out: {exc}") from None
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Mercado Pago request failed: {exc}") from None
        return resp.status_code, resp.content


# ---------------------------------------------------------------------------
# Pure helpers — independently testable, no I/O.
# ---------------------------------------------------------------------------


def _deterministic_reference(idempotency_key: str) -> str:
    """Same defensive rationale as NOWPaymentsAdapter's own
    _nowpayments_order_id(): Mercado Pago's own constraints on
    external_reference length/charset were not pinned to a literal spec
    field this session, so this deterministically hashes our
    idempotency_key (up to 128 chars) into a fixed-length hex string
    rather than assume it is always accepted verbatim. Same
    idempotency_key always yields the same reference — a retried
    create_intent() call maps to the same value Mercado-Pago-side too."""
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]


def _format_amount(amount: Decimal) -> float:
    """See module docstring's "Money handling" section — safe ONLY for
    the plain 2-decimal-place catalog values src/billing/pricing.py ever
    produces."""
    return float(amount)


def _decimal_or_none(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


def _get_header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup — same rationale as
    NOWPaymentsAdapter/BinancePayAdapter's own _get_header()."""
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
        raise MercadoPagoError(0, f"Non-JSON response from Mercado Pago: {exc}") from None


def _parse_x_signature(raw: str) -> dict[str, str]:
    """Parse `ts=<ms>,v1=<hex>` into {"ts": ..., "v1": ...}. Unknown
    parts are kept (forward-compatible); malformed parts (no '=') are
    skipped rather than raising — an unparseable header simply yields a
    dict missing 'v1', which the caller treats as verification failure."""
    parts: dict[str, str] = {}
    for chunk in raw.split(","):
        if "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        parts[key.strip()] = value.strip()
    return parts


def _build_signature_manifest(*, data_id: str, request_id: str | None, ts: str) -> str:
    """`id:{data.id};request-id:{x-request-id};ts:{ts};` — segments for
    an absent value are dropped entirely (official guidance), not left
    as an empty placeholder. data.id is lowercased unconditionally (see
    module docstring)."""
    manifest = f"id:{data_id.lower()};"
    if request_id is not None:
        manifest += f"request-id:{request_id};"
    manifest += f"ts:{ts};"
    return manifest


def _verify_signature(*, secret: str, manifest: str, expected_hex: str) -> bool:
    computed = hmac.new(secret.encode("utf-8"), manifest.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, expected_hex.lower())


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class MercadoPagoAdapter(FiatPaymentProvider):
    """Mercado Pago — hosted Checkout Pro preference/redirect (fiat: Pix,
    boleto, credit card, debit card). See module docstring for the full
    architecture and every source consulted."""

    def __init__(
        self,
        *,
        access_token: str,
        webhook_secret: str,
        base_url: str,
        transport: MercadoPagoTransport,
        notification_url: str | None = None,
        success_url: str | None = None,
        pending_url: str | None = None,
        failure_url: str | None = None,
        request_timeout_seconds: float = 15.0,
    ) -> None:
        if not access_token:
            raise ValueError("MercadoPagoAdapter requires a non-empty access_token")
        if not webhook_secret:
            raise ValueError("MercadoPagoAdapter requires a non-empty webhook_secret")
        if not base_url:
            raise ValueError("MercadoPagoAdapter requires a non-empty base_url")
        self._access_token = access_token
        self._webhook_secret = webhook_secret
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._notification_url = notification_url
        self._success_url = success_url
        self._pending_url = pending_url
        self._failure_url = failure_url
        self._request_timeout_seconds = request_timeout_seconds

    @property
    def provider_name(self) -> str:
        return "mercado_pago"

    @property
    def supported_payment_methods(self) -> frozenset[PaymentMethod]:
        return frozenset({PaymentMethod.PIX, PaymentMethod.CREDIT_CARD, PaymentMethod.DEBIT_CARD, PaymentMethod.BOLETO})

    @property
    def supported_payment_types(self) -> frozenset[PaymentType]:
        # Recurring requires the entirely separate /preapproval resource
        # — not implemented here. See module docstring's "Known gaps".
        return frozenset({PaymentType.ONE_TIME})

    @property
    def supported_currencies(self) -> frozenset[str]:
        # Payment Router etapa — Mercado Pago's real, confirmed market
        # list (see module docstring's "Countries / currency" section):
        # Argentina, Brazil, Chile, Colombia, Mexico, Peru, Uruguay.
        # Narrow and closed enough to declare with confidence, unlike
        # Stripe's much broader (and not exhaustively confirmed) list —
        # see PaymentProvider.supported_currencies' own docstring for why
        # the DEFAULT (unrestricted) is deliberately not used here.
        return frozenset({"ARS", "BRL", "CLP", "COP", "MXN", "PEN", "UYU"})

    # ------------------------------------------------------------------
    # Request plumbing
    # ------------------------------------------------------------------

    def _headers(self, *, idempotency_key: str | None = None) -> dict[str, str]:
        # Never logged, never returned to any caller — see _post()/_get()'s
        # debug log below, which logs the path only.
        headers = {"Authorization": f"Bearer {self._access_token}", "Content-Type": "application/json"}
        if idempotency_key is not None:
            headers["X-Idempotency-Key"] = idempotency_key
        return headers

    async def _post(self, path: str, body_dict: dict, *, idempotency_key: str | None = None) -> tuple[int, bytes]:
        body = json.dumps(body_dict, separators=(",", ":"), ensure_ascii=False)
        url = f"{self._base_url}{path}"
        logger.debug("mercadopago: POST %s", path)  # path only — never headers/body (access token/PII)
        try:
            return await self._transport.post(
                url, headers=self._headers(idempotency_key=idempotency_key),
                body=body.encode("utf-8"), timeout=self._request_timeout_seconds,
            )
        except ProviderUnavailable:
            raise
        except Exception as exc:
            raise ProviderUnavailable(f"Mercado Pago request to {path} failed: {exc}") from exc

    async def _get(self, path: str) -> tuple[int, bytes]:
        url = f"{self._base_url}{path}"
        logger.debug("mercadopago: GET %s", path)
        try:
            return await self._transport.get(url, headers=self._headers(), timeout=self._request_timeout_seconds)
        except ProviderUnavailable:
            raise
        except Exception as exc:
            raise ProviderUnavailable(f"Mercado Pago request to {path} failed: {exc}") from exc

    # ------------------------------------------------------------------
    # PaymentProvider interface
    # ------------------------------------------------------------------

    async def create_intent(
        self, *, amount: Decimal, currency: str, user_id: str, idempotency_key: str, metadata: dict | None = None,
    ) -> PaymentIntent:
        """Creates a Checkout Pro preference. `PaymentIntent.id` is our
        own deterministic external_reference (see
        _deterministic_reference()), NOT the preference's own `id` —
        see module docstring's "Identity" section for why."""
        reference = _deterministic_reference(idempotency_key)
        goods_name = (metadata or {}).get("goods_name", "TRINDATRON plan")
        requested_method_raw = (metadata or {}).get("payment_method")
        requested_method = PaymentMethod(requested_method_raw) if requested_method_raw else None

        body: dict = {
            "items": [{
                "title": goods_name,
                "quantity": 1,
                "unit_price": _format_amount(amount),
                "currency_id": currency.upper(),
            }],
            "external_reference": reference,
        }
        if self._notification_url:
            body["notification_url"] = self._notification_url
        back_urls: dict[str, str] = {}
        if self._success_url:
            back_urls["success"] = self._success_url
        if self._pending_url:
            back_urls["pending"] = self._pending_url
        if self._failure_url:
            back_urls["failure"] = self._failure_url
        if back_urls:
            body["back_urls"] = back_urls
            if "success" in back_urls:
                body["auto_return"] = "approved"

        if requested_method is not None and requested_method in _EXCLUDABLE_PAYMENT_TYPE_IDS:
            wanted_type_id = _EXCLUDABLE_PAYMENT_TYPE_IDS[requested_method]
            excluded = [
                {"id": type_id}
                for type_id in set(_EXCLUDABLE_PAYMENT_TYPE_IDS.values())
                if type_id != wanted_type_id
            ]
            body["payment_methods"] = {"excluded_payment_types": excluded}

        status_code, resp_body = await self._post(_CREATE_PREFERENCE_PATH, body, idempotency_key=idempotency_key)
        data = _parse_json(resp_body)
        if status_code >= 400 or "init_point" not in data:
            raise MercadoPagoError(status_code, data.get("message") or data.get("error"))

        return PaymentIntent(
            id=reference,
            provider=self.provider_name,
            amount=amount,
            currency=currency.upper(),
            status=PaymentStatus.PENDING,
            idempotency_key=idempotency_key,
            redirect_url=data.get("init_point"),
            metadata={"preference_id": data.get("id")},
        )

    async def get_intent(self, intent_id: str) -> PaymentIntent:
        """`intent_id` here is OUR external_reference (see create_intent()'s
        own docstring). Uses the Search Payments endpoint
        (GET /v1/payments/search?external_reference=...) — confirmed to
        exist via the official reference page; the exact `{"results":
        [...]}` envelope follows this API's long-established convention
        (not independently re-fetched this session — see module
        docstring). Returns the most recent matching payment; raises
        PaymentIntentNotFound if none exists yet (e.g. the payer hasn't
        completed checkout)."""
        status_code, resp_body = await self._get(
            f"{_SEARCH_PAYMENTS_PATH}?external_reference={intent_id}&sort=date_created&criteria=desc"
        )
        data = _parse_json(resp_body)
        if status_code >= 400:
            raise MercadoPagoError(status_code, data.get("message") or data.get("error"))
        results = data.get("results") or []
        if not results:
            raise PaymentIntentNotFound(intent_id)
        payment = results[0]
        mapped_status = _STATUS_MAP.get(payment.get("status"), PaymentStatus.PROCESSING)
        return PaymentIntent(
            id=intent_id,
            provider=self.provider_name,
            amount=_decimal_or_none(payment.get("transaction_amount")) or Decimal("0"),
            currency=str(payment.get("currency_id") or "").upper(),
            status=mapped_status,
            idempotency_key=intent_id,
            metadata={"payment_id": payment.get("id"), "status_detail": payment.get("status_detail")},
        )

    async def verify_webhook(self, payload: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        notification = _parse_json(payload)

        event_kind = notification.get("type") or notification.get("action", "").split(".")[0]
        if event_kind != "payment":
            raise UnknownWebhookEvent(f"Unhandled Mercado Pago notification type: {event_kind!r}")

        data_id = (notification.get("data") or {}).get("id")
        if not data_id:
            raise UnknownWebhookEvent("Mercado Pago notification missing data.id")
        data_id = str(data_id)

        x_signature = _get_header(headers, "x-signature")
        if not x_signature:
            raise InvalidWebhookSignature("Missing x-signature header")
        sig_parts = _parse_x_signature(x_signature)
        ts = sig_parts.get("ts")
        v1 = sig_parts.get("v1")
        if not ts or not v1:
            raise InvalidWebhookSignature("Malformed x-signature header")

        request_id = _get_header(headers, "x-request-id")
        manifest = _build_signature_manifest(data_id=data_id, request_id=request_id, ts=ts)
        if not _verify_signature(secret=self._webhook_secret, manifest=manifest, expected_hex=v1):
            raise InvalidWebhookSignature("Mercado Pago x-signature verification failed")

        # Official guidance: never trust the webhook body's own status —
        # fetch the authoritative Payment via the API. See module docstring.
        status_code, resp_body = await self._get(_GET_PAYMENT_PATH.format(payment_id=data_id))
        payment = _parse_json(resp_body)
        if status_code >= 400:
            raise MercadoPagoError(status_code, payment.get("message") or payment.get("error"))

        external_status = payment.get("status")
        mapped_status = _STATUS_MAP.get(external_status)
        if mapped_status is None:
            raise UnknownWebhookEvent(f"Unhandled Mercado Pago payment status: {external_status!r}")

        notification_id = notification.get("id")
        return WebhookEvent(
            provider=self.provider_name,
            event_type=f"payment:{external_status}",
            payment_id=str(payment.get("id")) if payment.get("id") is not None else None,
            provider_ref=payment.get("external_reference"),
            status=mapped_status,
            amount=_decimal_or_none(payment.get("transaction_amount")),
            currency=str(payment.get("currency_id") or "").upper() or None,
            raw_payload=payment,
            # The notification's OWN top-level `id` (distinct from
            # data.id, the payment's id) — a genuine per-delivery unique
            # value Mercado Pago itself generates, unlike NOWPayments
            # (which has none and must reuse its verified signature
            # instead — see that adapter's own docstring).
            nonce=str(notification_id) if notification_id is not None else None,
        )

    async def refund(self, payment_id: str, *, amount: Decimal | None = None, reason: str = "") -> PaymentResult:
        """`payment_id` is Mercado Pago's OWN numeric payment id — see
        module docstring's "Known gaps" section for the identity-space
        caveat (this is NOT PaymentOrder.id/external_order_id).
        X-Idempotency-Key is derived deterministically from
        (payment_id, amount) since the PaymentProvider ABC's refund()
        signature carries no caller-supplied idempotency key — a retry
        of the exact same refund reuses the same key; a second, smaller
        partial refund on the same payment gets a different one, which
        is correct (Mercado Pago supports multiple partial refunds)."""
        idem_seed = f"refund:{payment_id}:{amount if amount is not None else 'full'}"
        idempotency_key = hashlib.sha256(idem_seed.encode("utf-8")).hexdigest()[:32]
        body: dict = {}
        if amount is not None:
            body["amount"] = _format_amount(amount)

        status_code, resp_body = await self._post(
            _REFUND_PATH.format(payment_id=payment_id), body, idempotency_key=idempotency_key,
        )
        data = _parse_json(resp_body)
        if status_code >= 400:
            raise MercadoPagoError(status_code, data.get("message") or data.get("error"))

        refunded_amount = _decimal_or_none(data.get("amount")) or Decimal("0")
        return PaymentResult(
            payment_id=str(data.get("payment_id") or payment_id),
            provider=self.provider_name,
            provider_ref=str(data.get("id")) if data.get("id") is not None else None,
            status=PaymentStatus.PARTIAL_REFUND if amount is not None else PaymentStatus.REFUNDED,
            amount_gross=refunded_amount,
            currency="",  # Mercado Pago's refund response does not echo a currency field.
            provider_fee=Decimal("0"),
            net_amount=refunded_amount,
            metadata={"raw_status": data.get("status")},
        )


# ---------------------------------------------------------------------------
# Factory — the ONLY place that decides whether Mercado Pago gets registered.
# ---------------------------------------------------------------------------


def build_mercadopago_adapter(settings) -> MercadoPagoAdapter | None:
    """Return a configured MercadoPagoAdapter, or None if it should not
    be registered. Registered ONLY when settings.mercadopago_enabled is
    True AND both mercadopago_access_token and mercadopago_webhook_secret
    are non-empty — same "never registered by default, never raises for
    missing config" contract as build_binance_pay_adapter()/
    build_nowpayments_adapter()."""
    if not settings.mercadopago_enabled:
        return None
    if not settings.mercadopago_access_token or not settings.mercadopago_webhook_secret:
        return None
    return MercadoPagoAdapter(
        access_token=settings.mercadopago_access_token,
        webhook_secret=settings.mercadopago_webhook_secret,
        base_url=settings.mercadopago_base_url,
        transport=HttpxMercadoPagoTransport(),
        notification_url=settings.mercadopago_notification_url or None,
        success_url=settings.mercadopago_success_url or None,
        pending_url=settings.mercadopago_pending_url or None,
        failure_url=settings.mercadopago_failure_url or None,
    )
