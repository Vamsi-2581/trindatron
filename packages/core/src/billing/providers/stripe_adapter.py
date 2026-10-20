"""
StripeAdapter — concrete PaymentProvider implementation for Stripe,
TRINDATRON's SECOND ACTIVE FiatPaymentProvider (Payments Foundation
etapa, 2026-09-08), alongside MercadoPagoAdapter.

Docs verified for this implementation (fetched live via web search this
session — not from training memory alone; docs.stripe.com pages were
reachable via search snippets rather than direct fetch in every case,
so claims below are corroborated across MULTIPLE independent official
reference/doc pages rather than a single dump — see each section):
  Checkout Sessions
    POST https://api.stripe.com/v1/checkout/sessions
    GET  https://api.stripe.com/v1/checkout/sessions/{id}
    -> confirmed: mode ("payment"/"subscription"), line_items[] with
       price_data (currency/unit_amount/product_data.name), quantity,
       success_url (REQUIRED)/cancel_url, client_reference_id,
       payment_method_types[]. Response: id ("cs_..."), url (the hosted
       redirect), payment_intent, payment_status, amount_total,
       currency, client_reference_id.
  Request encoding
    -> confirmed: the entire API takes application/x-www-form-urlencoded
       bodies (NOT JSON), with bracket notation for nested objects/
       arrays (`line_items[0][price_data][currency]=...`,
       `payment_method_types[0]=card`) — a materially different wire
       format from Mercado Pago/NOWPayments, implemented here via
       `_flatten_form_params()`.
  Webhook payload & signature
    -> confirmed: `Stripe-Signature` header format
       `t=<unix_ts>,v1=<hex>[,v0=<hex>]`; signed payload is
       `f"{t}.{raw_body}"`, HMAC-SHA256 with the endpoint's webhook
       signing secret, hex digest compared to v1 (v0 is a legacy scheme,
       ignored here). Official libraries default to a 5-minute timestamp
       tolerance (configurable; never disable it entirely) — the
       timestamp is itself part of the signed payload, so it cannot be
       forged without invalidating the signature. Each Event object
       carries a full embedded copy of the affected resource in
       `data.object` — trusted directly once the signature verifies
       (Stripe signs the ENTIRE raw body, unlike Mercado Pago's
       id/request-id/ts-only manifest, which forced a follow-up GET —
       no such GET is needed here).
  Checkout events
    -> confirmed event names: `checkout.session.completed` (payment_status
       is "paid", "unpaid", or "no_payment_required" — delayed methods
       like boleto can complete this event with "unpaid" and resolve
       later), `checkout.session.async_payment_succeeded`,
       `checkout.session.async_payment_failed`, `checkout.session.expired`.
  PaymentIntent status / Retrieve
    GET https://api.stripe.com/v1/payment_intents/{id}
    -> confirmed status enum: requires_payment_method,
       requires_confirmation, requires_action, processing,
       requires_capture, canceled, succeeded. Used here ONLY inside
       refund() to learn a payment's currency before converting a
       partial-refund Decimal into Stripe's minor-unit integer — see
       refund()'s own docstring.
  Refunds
    POST https://api.stripe.com/v1/refunds
    -> confirmed: `payment_intent` identifies what to refund; optional
       `amount` (positive integer, smallest currency unit) for a
       partial refund — omitted means a full refund; can be called
       multiple times until the full charge is refunded.
  Idempotency
    -> confirmed: the `Idempotency-Key` header, sent on POST requests,
       makes Stripe itself return the ORIGINAL result for a retried
       request with the same key (genuinely deduplicates server-side —
       unlike Mercado Pago's Preferences endpoint, where this was NOT
       confirmed and TRINDATRON's own DB-level idempotency was the real
       backstop).
  Disputes
    -> confirmed event `charge.dispute.created` (and `charge.dispute.
       closed`) exist as a distinct event category from ordinary
       payment success/failure. Not mapped to a canonical PaymentStatus
       here — see "Known gaps" below (same honest-gap precedent as
       Mercado Pago's in_mediation/charged_back).
  Currencies
    -> confirmed: 135+ ISO 4217 currencies; amounts are a positive
       integer in the SMALLEST unit of the currency (e.g. cents) EXCEPT
       a documented list of zero-decimal currencies (BIF, CLP, DJF,
       GNF, JPY, KMF, KRW, MGA, PYG, RWF, UGX, VND, VUV, XAF, XOF, XPF)
       charged in whole units — see `_ZERO_DECIMAL_CURRENCIES` and
       `_to_minor_units()`/`_from_minor_units()`.
  Pix (Brazil)
    -> confirmed Pix exists on Stripe ONLY via Link, is INVITE-ONLY for
       Brazil-based accounts, and is only exposed through Payment Links,
       Stripe Checkout (Hosted), and the Payment Element — even though
       our chosen flow (Hosted Checkout) is technically an eligible
       surface, this session found no way to confirm any given account
       actually holds the invite/eligibility. Per this mission's own
       "não declarar capability não confirmada" rule, PIX is
       DELIBERATELY NOT declared in `supported_payment_methods` below.
  Card funding (credit vs. debit)
    -> confirmed: Stripe's Checkout `payment_method_types` has ONE
       "card" entry covering credit, debit, AND prepaid — which kind a
       given card is is a property of the card itself (`card.funding`),
       decided by the customer's bank, not something a Checkout Session
       can independently allow-list or exclude. CREDIT_CARD and
       DEBIT_CARD are still both declared below (this adapter's actual
       flow genuinely can settle either), but see "Known gaps" for why
       there is no method-level exclusion knob the way Mercado Pago has.
  Boleto (Brazil)
    -> confirmed: `boleto` is a real, documented Checkout
       `payment_method_types` entry (voucher-based, ~20% of BR
       transactions), with its own `payment_method_options.boleto`
       block (e.g. `expires_after_days`) — not configured here (see
       "Known gaps").
  Subscriptions / recurring
    -> confirmed: Checkout Session `mode="subscription"` with
       `line_items[].price_data.recurring.interval` is a REAL,
       documented mechanism (no separate resource needed, unlike
       Mercado Pago's /preapproval) — technically reachable from the
       exact same endpoint this adapter already calls. DELIBERATELY NOT
       wired up this phase regardless — see "Known gaps" for why.

Why Stripe Checkout (hosted Checkout Session), not Payment Intents/
SetupIntent directly
-------------------------------------------------------------------
Same rationale as MercadoPagoAdapter's own "Why Checkout Pro" section:
a direct PaymentIntent-based card charge needs a client-side token
(Stripe.js/Elements collecting raw card data in the browser) — a
frontend integration this phase does not touch. Stripe Checkout
sidesteps this entirely: ONE endpoint (create a Checkout Session)
covers cards AND boleto, because Stripe's OWN hosted page collects
whatever the payer picks — no card byte ever reaches TRINITY Core. This
is the exact same "hosted checkout, redirect_url, PCI stays with the
provider" shape MercadoPagoAdapter/BinancePayAdapter/NOWPaymentsAdapter
all already established.

Identity: Stripe's OWN Session id, not a deterministic hash
-------------------------------------------------------------------
Unlike NOWPaymentsAdapter (invoice id vs. later payment id) and
MercadoPagoAdapter (preference id vs. later payment id), a Stripe
Checkout Session's `id` ("cs_...") IS the SAME value at creation time
and inside every later webhook event for that session — there is no
identity split to work around. `PaymentIntent.id` here is therefore
Stripe's own session id directly (no `_deterministic_reference()`
hashing needed for THIS purpose). `client_reference_id` is still set,
to our own deterministic reference, purely for human cross-referencing
in the Stripe Dashboard — it is NOT what the generic webhook route
matches on.

Money handling
--------------
Every amount that crosses the wire is converted via `_to_minor_units()`/
`_from_minor_units()` — Decimal major units <-> Stripe's required
integer minor units, correctly zero-decimal-currency-aware (see module
docstring's "Currencies" section). This is a materially different, more
involved conversion than NOWPaymentsAdapter/MercadoPagoAdapter's plain
`float(Decimal)` (neither of those providers uses minor-unit integers).

Recurring billing (Stripe Recurring etapa, 2026-09-08)
-------------------------------------------------------------------
Now IMPLEMENTED — SubscriptionService (src/services/subscription_service.py)
has real lifecycle management (5-state machine, row-locked concurrency
guards, grace period) that was inert/missing when RECURRING was
originally withheld. `supported_payment_types` below now includes
PaymentType.RECURRING.

Docs verified fresh for THIS addition (same discipline as the rest of
this module — official reference pages, not training memory alone):
  Checkout Session mode="subscription"
    -> confirmed: `price_data.recurring.interval` works INLINE, with NO
       pre-created Stripe Price object required — "you can also use
       price_data to reference information from an external database"
       applies identically in subscription mode. This adapter therefore
       does NOT introduce any Stripe Price ID configuration/mapping —
       src/billing/pricing.py's own catalog (amount + implied monthly
       interval, confirmed via that module's own docstring: "Monthly
       price catalog") is the ONLY source of the recurring price, passed
       inline exactly like the one-time `mode="payment"` path already
       does. The completed Session's own `subscription` field (distinct
       from `payment_intent`, used by mode="payment") carries the newly
       created Stripe Subscription's id.
  Subscription object
    GET    https://api.stripe.com/v1/subscriptions/{id}
    DELETE https://api.stripe.com/v1/subscriptions/{id} (cancels
           IMMEDIATELY — confirmed official behavior)
    POST   https://api.stripe.com/v1/subscriptions/{id} (metadata update)
    -> confirmed status enum: incomplete, incomplete_expired, trialing,
       active, past_due, canceled, unpaid, paused.
    -> `cancel_at_period_end` is a DEPRECATED field (a changelog entry
       confirms its replacement by newer `cancel_at` enum values whose
       exact contract this session did not exhaustively verify) — this
       adapter's update_subscription() therefore does NOT expose
       cancel-at-period-end at all (see "Known gaps"); cancel_subscription()
       uses the confirmed, stable, immediate-cancel DELETE endpoint only.
  Webhook events (subscription-lifecycle)
    -> confirmed real event types: checkout.session.completed (mode=
       subscription; carries the new `subscription` id),
       customer.subscription.created/updated/deleted,
       invoice.payment_succeeded, invoice.payment_failed.
    -> confirmed Invoice object fields used for the renewal-cycle
       identity/amount: id ("in_..."), subscription, created,
       amount_paid, currency, status.
    -> confirmed Event object fields used for the ordering/dedup guard:
       id ("evt_..."), created (Unix timestamp), type, data.object —
       see verify_subscription_webhook()'s own docstring.
  Signature verification for these events reuses the EXACT SAME
  Stripe-Signature HMAC mechanism verify_webhook() already implements —
  never a second, weaker scheme (see verify_subscription_webhook()'s
  own implementation, which literally calls the same helpers).

Known gaps — deliberately NOT implemented (see docs)
--------------------------------------------------------------------
* Pix — invite-only/unconfirmed eligibility, not declared (see above).
* update_subscription() only updates metadata — no plan/price changes,
  no cancel-at-period-end (deprecated field, see above; the newer
  cancel_at enum contract was not confirmed to the rigor this codebase
  requires before relying on it).
* No automatic reconciliation of an AMBIGUOUS create_subscription()
  outcome (ProviderUnavailable) — SubscriptionService leaves
  provider_subscription_id NULL for a human/future job to resolve via
  retrieve_subscription(), never retried automatically.
* Disputes/chargebacks (`charge.dispute.*`) — not mapped to a canonical
  PaymentStatus; an event of this kind raises UnknownWebhookEvent
  (logged, 200-acked, no order mutation) — same honest-gap precedent as
  Mercado Pago's in_mediation/charged_back.
* Per-method exclusion filtering (Mercado Pago's
  `excluded_payment_types` equivalent) — Stripe Checkout only supports
  an allow-list (`payment_method_types`), not an exclude-list from a
  broader default; this adapter WHITELISTS a single resolved type when
  the caller requested one, but cannot narrow "card" down to "debit
  only" or "credit only" (see "Card funding" above — Stripe itself has
  no such knob).
* Boleto-specific configuration (`payment_method_options.boleto.
  expires_after_days`, mandatory payer tax ID/address collection) — not
  configured; a real boleto Checkout Session may need additional setup
  this adapter does not attempt.
* refund()'s `payment_id` parameter is documented here as Stripe's OWN
  PaymentIntent id ("pi_...") — NOT `PaymentOrder.id`/
  `external_order_id` (which holds the Checkout Session id, a different
  identifier space). Nothing in this codebase calls `adapter.refund()`
  today (PaymentOrderService.refund_order() reverses credits
  TRINDATRON-side only, and is refused outright for a plan-purchase
  order via PlanPurchaseNotRefundable regardless of provider); this
  method exists as a real, working primitive for a FUTURE caller.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Mapping, Protocol
from urllib.parse import urlencode

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
    SubscriptionEventKind,
    SubscriptionIntent,
    SubscriptionWebhookEvent,
    UnknownWebhookEvent,
    WebhookEvent,
)

logger = logging.getLogger(__name__)

_CREATE_SESSION_PATH = "/v1/checkout/sessions"
_GET_SESSION_PATH = "/v1/checkout/sessions/{session_id}"
_GET_PAYMENT_INTENT_PATH = "/v1/payment_intents/{payment_intent_id}"
_CREATE_REFUND_PATH = "/v1/refunds"
_GET_SUBSCRIPTION_PATH = "/v1/subscriptions/{subscription_id}"
_UPDATE_SUBSCRIPTION_PATH = "/v1/subscriptions/{subscription_id}"
_CANCEL_SUBSCRIPTION_PATH = "/v1/subscriptions/{subscription_id}"

# Confirmed real Stripe event types for subscription lifecycle (see
# module docstring's "Recurring billing" section). checkout.session.
# completed is shared with the one-time path (verify_webhook()) but ALSO
# handled here when session.mode == "subscription" — see
# verify_subscription_webhook()'s own dispatch.
#
# customer.subscription.created is DELIBERATELY NOT mapped here: its own
# object (the Subscription) carries no reference back to the originating
# Checkout Session, so it cannot be correlated to an internal
# Subscription row via provider_setup_intent_id the way checkout.session.
# completed(mode=subscription) can — see verify_subscription_webhook()'s
# own docstring. An incoming delivery of this type raises
# UnknownWebhookEvent (absorbed, not crashed) until a Stripe `metadata`-
# based correlation is built in a future phase.
_SUBSCRIPTION_EVENT_KIND_MAP: dict[str, SubscriptionEventKind] = {
    "customer.subscription.updated": SubscriptionEventKind.SUBSCRIPTION_UPDATED,
    "customer.subscription.deleted": SubscriptionEventKind.SUBSCRIPTION_CANCELED,
    "invoice.payment_succeeded": SubscriptionEventKind.RENEWAL_SUCCEEDED,
    "invoice.payment_failed": SubscriptionEventKind.RENEWAL_FAILED,
}

# Confirmed Subscription.status enum (see module docstring). Used only by
# retrieve_subscription() for reconciliation — the ordinary path is
# entirely webhook-event-driven (SubscriptionEventKind), never polled.
_SUBSCRIPTION_STATUS_MAP: dict[str, PaymentStatus] = {
    "incomplete": PaymentStatus.PROCESSING,
    "trialing": PaymentStatus.PROCESSING,
    "active": PaymentStatus.CONFIRMED,
    "past_due": PaymentStatus.PROCESSING,
    "unpaid": PaymentStatus.PROCESSING,
    "paused": PaymentStatus.PROCESSING,
    "incomplete_expired": PaymentStatus.EXPIRED,
    "canceled": PaymentStatus.CANCELLED,
}

# Zero-decimal currencies (official list, verified — see module docstring's
# "Currencies" section). Every OTHER supported currency uses 2 decimal
# places (minor units = major units * 100).
_ZERO_DECIMAL_CURRENCIES: frozenset[str] = frozenset({
    "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga",
    "pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
})

# checkout.session.completed's own payment_status field (verified).
_COMPLETED_PAYMENT_STATUS_MAP: dict[str, PaymentStatus] = {
    "paid": PaymentStatus.CONFIRMED,
    "no_payment_required": PaymentStatus.CONFIRMED,
    "unpaid": PaymentStatus.PROCESSING,  # delayed method (e.g. boleto) still in flight
}

# Event types this adapter maps directly to a canonical status (verified
# — see module docstring's "Checkout events" section). checkout.session.
# completed is handled separately via _COMPLETED_PAYMENT_STATUS_MAP
# because its OWN status depends on the embedded payment_status field.
_EVENT_STATUS_MAP: dict[str, PaymentStatus] = {
    "checkout.session.async_payment_succeeded": PaymentStatus.CONFIRMED,
    "checkout.session.async_payment_failed": PaymentStatus.FAILED,
    "checkout.session.expired": PaymentStatus.EXPIRED,
}

# Stripe's card method covers credit AND debit (funding is a card
# property, not a checkout-selectable option — see module docstring's
# "Card funding" section). Boleto has its own real type.
_STRIPE_PAYMENT_METHOD_TYPE: dict[PaymentMethod, str] = {
    PaymentMethod.CREDIT_CARD: "card",
    PaymentMethod.DEBIT_CARD: "card",
    PaymentMethod.BOLETO: "boleto",
}


class StripeError(PaymentCreationRejected):
    """A Stripe API call completed but returned a non-success result.

    Distinct from ProviderUnavailable (network/5xx/timeout — see
    HttpxStripeTransport) — this means Stripe's API responded and
    explicitly rejected the request or reported an error body.
    Inherits from PaymentCreationRejected (Payment Router etapa) — see
    that class's own docstring for why this is safe to fail over on.
    """

    def __init__(self, status_code: int, message: str | None) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(f"Stripe API error: status={status_code} message={message!r}")


# ---------------------------------------------------------------------------
# Transport — same injectable-seam pattern as every other adapter in this
# codebase, so tests never open a real socket.
# ---------------------------------------------------------------------------


class StripeTransport(Protocol):
    """Minimal HTTP transport contract — one method each for GET/POST,
    raw bytes in and out."""

    async def post(self, url: str, *, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
        ...

    async def get(self, url: str, *, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        ...

    async def delete(self, url: str, *, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        ...


class HttpxStripeTransport:
    """Default transport — a real network call via httpx. Only ever
    reached if settings.stripe_enabled=True AND a real secret key is
    configured (see build_stripe_adapter()) — never true in this
    repository/environment, so never exercised by the automatic test
    suite."""

    async def post(self, url: str, *, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, content=body)
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(f"Stripe request timed out: {exc}") from None
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Stripe request failed: {exc}") from None
        return resp.status_code, resp.content

    async def get(self, url: str, *, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(f"Stripe request timed out: {exc}") from None
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Stripe request failed: {exc}") from None
        return resp.status_code, resp.content

    async def delete(self, url: str, *, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.delete(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(f"Stripe request timed out: {exc}") from None
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Stripe request failed: {exc}") from None
        return resp.status_code, resp.content


# ---------------------------------------------------------------------------
# Pure helpers — independently testable, no I/O.
# ---------------------------------------------------------------------------


def _deterministic_reference(idempotency_key: str) -> str:
    """Used ONLY for `client_reference_id` (human/Dashboard
    cross-referencing) — NOT for order matching, see module docstring's
    "Identity" section. Same defensive rationale as the other two
    adapters' own deterministic-reference helpers."""
    return hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]


def _to_minor_units(amount: Decimal, currency: str) -> int:
    """Decimal major units -> Stripe's required integer minor units,
    zero-decimal-currency-aware (see module docstring)."""
    if currency.lower() in _ZERO_DECIMAL_CURRENCIES:
        return int(amount.to_integral_value(rounding=ROUND_HALF_UP))
    return int((amount * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _from_minor_units(minor: int | None, currency: str | None) -> Decimal | None:
    if minor is None:
        return None
    if currency and currency.lower() in _ZERO_DECIMAL_CURRENCIES:
        return Decimal(minor)
    return Decimal(minor) / Decimal(100)


def _decimal_or_none(raw: Any) -> Decimal | None:
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup — same convention as every other
    adapter's own _get_header()."""
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
        raise StripeError(0, f"Non-JSON response from Stripe: {exc}") from None


def _flatten_form_params(prefix: str, value: Any, out: list[tuple[str, str]]) -> None:
    """Recursively flattens a nested dict/list into Stripe's
    x-www-form-urlencoded bracket-notation pairs — see module docstring's
    "Request encoding" section. `bool` is checked before `int` because
    `bool` is a subclass of `int` in Python (True/False would otherwise
    be misencoded as 1/0, which happens to work for Stripe's boolean
    fields too, but this is explicit rather than accidental)."""
    if isinstance(value, dict):
        for k, v in value.items():
            _flatten_form_params(f"{prefix}[{k}]" if prefix else str(k), v, out)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _flatten_form_params(f"{prefix}[{i}]", v, out)
    elif isinstance(value, bool):
        out.append((prefix, "true" if value else "false"))
    elif value is not None:
        out.append((prefix, str(value)))


def _encode_form(body_dict: dict) -> bytes:
    pairs: list[tuple[str, str]] = []
    _flatten_form_params("", body_dict, pairs)
    return urlencode(pairs).encode("utf-8")


def _parse_stripe_signature(raw: str) -> dict[str, str]:
    """Parse `t=<unix_ts>,v1=<hex>[,v0=<hex>]` into a dict. Unknown/
    malformed parts are skipped rather than raising."""
    parts: dict[str, str] = {}
    for chunk in raw.split(","):
        if "=" not in chunk:
            continue
        key, _, val = chunk.partition("=")
        parts[key.strip()] = val.strip()
    return parts


def _verify_stripe_signature(*, secret: str, timestamp: str, raw_body: bytes, expected_hex: str) -> bool:
    signed_payload = f"{timestamp}.".encode("utf-8") + raw_body
    computed = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, expected_hex.lower())


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class StripeAdapter(FiatPaymentProvider):
    """Stripe — hosted Checkout Session/redirect (fiat: credit card,
    debit card, boleto). See module docstring for the full architecture
    and every source consulted."""

    def __init__(
        self,
        *,
        secret_key: str,
        webhook_secret: str,
        base_url: str,
        transport: StripeTransport,
        success_url: str | None = None,
        cancel_url: str | None = None,
        webhook_tolerance_seconds: int = 300,
        request_timeout_seconds: float = 15.0,
    ) -> None:
        if not secret_key:
            raise ValueError("StripeAdapter requires a non-empty secret_key")
        if not webhook_secret:
            raise ValueError("StripeAdapter requires a non-empty webhook_secret")
        if not base_url:
            raise ValueError("StripeAdapter requires a non-empty base_url")
        self._secret_key = secret_key
        self._webhook_secret = webhook_secret
        self._base_url = base_url.rstrip("/")
        self._transport = transport
        self._success_url = success_url
        self._cancel_url = cancel_url
        self._webhook_tolerance_seconds = webhook_tolerance_seconds
        self._request_timeout_seconds = request_timeout_seconds

    @property
    def provider_name(self) -> str:
        return "stripe"

    @property
    def supported_payment_methods(self) -> frozenset[PaymentMethod]:
        # PIX deliberately excluded — invite-only/unconfirmed eligibility.
        # See module docstring's "Pix (Brazil)" section.
        return frozenset({PaymentMethod.CREDIT_CARD, PaymentMethod.DEBIT_CARD, PaymentMethod.BOLETO})

    @property
    def supported_payment_types(self) -> frozenset[PaymentType]:
        # RECURRING is now real — create_subscription()/cancel_subscription()/
        # retrieve_subscription()/update_subscription()/
        # verify_subscription_webhook() are all implemented below (Stripe
        # Recurring etapa). See module docstring's own "Recurring billing"
        # section for exactly what was confirmed before declaring this.
        return frozenset({PaymentType.ONE_TIME, PaymentType.RECURRING})

    # ------------------------------------------------------------------
    # Request plumbing
    # ------------------------------------------------------------------

    def _headers(self, *, idempotency_key: str | None = None) -> dict[str, str]:
        # Never logged, never returned to any caller — see _post()/_get()'s
        # debug log below, which logs the path only.
        headers = {
            "Authorization": f"Bearer {self._secret_key}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def _post(self, path: str, body_dict: dict, *, idempotency_key: str | None = None) -> tuple[int, bytes]:
        body = _encode_form(body_dict)
        url = f"{self._base_url}{path}"
        logger.debug("stripe: POST %s", path)  # path only — never headers/body (secret key/PII)
        try:
            return await self._transport.post(
                url, headers=self._headers(idempotency_key=idempotency_key),
                body=body, timeout=self._request_timeout_seconds,
            )
        except ProviderUnavailable:
            raise
        except Exception as exc:
            raise ProviderUnavailable(f"Stripe request to {path} failed: {exc}") from exc

    async def _get(self, path: str) -> tuple[int, bytes]:
        url = f"{self._base_url}{path}"
        logger.debug("stripe: GET %s", path)
        try:
            return await self._transport.get(url, headers=self._headers(), timeout=self._request_timeout_seconds)
        except ProviderUnavailable:
            raise
        except Exception as exc:
            raise ProviderUnavailable(f"Stripe request to {path} failed: {exc}") from exc

    async def _delete(self, path: str) -> tuple[int, bytes]:
        url = f"{self._base_url}{path}"
        logger.debug("stripe: DELETE %s", path)
        try:
            return await self._transport.delete(url, headers=self._headers(), timeout=self._request_timeout_seconds)
        except ProviderUnavailable:
            raise
        except Exception as exc:
            raise ProviderUnavailable(f"Stripe request to {path} failed: {exc}") from exc

    # ------------------------------------------------------------------
    # PaymentProvider interface
    # ------------------------------------------------------------------

    async def create_intent(
        self, *, amount: Decimal, currency: str, user_id: str, idempotency_key: str, metadata: dict | None = None,
    ) -> PaymentIntent:
        """Creates a Checkout Session in mode="payment". `PaymentIntent.id`
        is Stripe's OWN session id — see module docstring's "Identity"
        section for why no deterministic-hash workaround is needed here,
        unlike NOWPaymentsAdapter/MercadoPagoAdapter."""
        reference = _deterministic_reference(idempotency_key)
        goods_name = (metadata or {}).get("goods_name", "TRINDATRON plan")
        requested_method_raw = (metadata or {}).get("payment_method")
        requested_method = PaymentMethod(requested_method_raw) if requested_method_raw else None

        body: dict = {
            "mode": "payment",
            "client_reference_id": reference,
            "line_items": [{
                "price_data": {
                    "currency": currency.lower(),
                    "unit_amount": _to_minor_units(amount, currency),
                    "product_data": {"name": goods_name},
                },
                "quantity": 1,
            }],
        }
        if self._success_url:
            body["success_url"] = self._success_url
        if self._cancel_url:
            body["cancel_url"] = self._cancel_url
        if requested_method is not None and requested_method in _STRIPE_PAYMENT_METHOD_TYPE:
            body["payment_method_types"] = [_STRIPE_PAYMENT_METHOD_TYPE[requested_method]]

        status_code, resp_body = await self._post(_CREATE_SESSION_PATH, body, idempotency_key=idempotency_key)
        data = _parse_json(resp_body)
        if status_code >= 400 or "url" not in data or "id" not in data:
            raise StripeError(status_code, (data.get("error") or {}).get("message") or data.get("message"))

        return PaymentIntent(
            id=data["id"],
            provider=self.provider_name,
            amount=amount,
            currency=currency.upper(),
            status=PaymentStatus.PENDING,
            idempotency_key=idempotency_key,
            redirect_url=data.get("url"),
            metadata={"client_reference_id": reference},
        )

    async def get_intent(self, intent_id: str) -> PaymentIntent:
        """`intent_id` here is Stripe's own Checkout Session id
        ("cs_..."). No search/workaround needed — see module docstring's
        "Identity" section."""
        status_code, resp_body = await self._get(_GET_SESSION_PATH.format(session_id=intent_id))
        data = _parse_json(resp_body)
        if status_code == 404:
            raise PaymentIntentNotFound(intent_id)
        if status_code >= 400:
            raise StripeError(status_code, (data.get("error") or {}).get("message") or data.get("message"))

        currency = data.get("currency")
        payment_status = data.get("payment_status")
        mapped_status = _COMPLETED_PAYMENT_STATUS_MAP.get(payment_status, PaymentStatus.PROCESSING)
        return PaymentIntent(
            id=intent_id,
            provider=self.provider_name,
            amount=_from_minor_units(data.get("amount_total"), currency) or Decimal("0"),
            currency=str(currency or "").upper(),
            status=mapped_status,
            idempotency_key=intent_id,
            redirect_url=data.get("url"),
            metadata={"payment_intent": data.get("payment_intent")},
        )

    async def verify_webhook(self, payload: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        signature_header = _get_header(headers, "stripe-signature")
        if not signature_header:
            raise InvalidWebhookSignature("Missing Stripe-Signature header")
        sig_parts = _parse_stripe_signature(signature_header)
        timestamp = sig_parts.get("t")
        v1 = sig_parts.get("v1")
        if not timestamp or not v1:
            raise InvalidWebhookSignature("Malformed Stripe-Signature header")

        try:
            ts_int = int(timestamp)
        except ValueError:
            raise InvalidWebhookSignature("Non-numeric timestamp in Stripe-Signature header") from None
        if abs(time.time() - ts_int) > self._webhook_tolerance_seconds:
            raise InvalidWebhookSignature("Stripe-Signature timestamp outside tolerance window")

        if not _verify_stripe_signature(secret=self._webhook_secret, timestamp=timestamp, raw_body=payload, expected_hex=v1):
            raise InvalidWebhookSignature("Stripe webhook signature verification failed")

        event = _parse_json(payload)
        event_type = event.get("type", "")
        session = (event.get("data") or {}).get("object") or {}

        if event_type == "checkout.session.completed":
            mapped_status = _COMPLETED_PAYMENT_STATUS_MAP.get(session.get("payment_status"))
            if mapped_status is None:
                raise UnknownWebhookEvent(f"Unhandled checkout.session payment_status: {session.get('payment_status')!r}")
        elif event_type in _EVENT_STATUS_MAP:
            mapped_status = _EVENT_STATUS_MAP[event_type]
        else:
            # Disputes/chargebacks and anything else not explicitly
            # handled — see module docstring's "Known gaps". Absorbed
            # (200-acked) by the generic route without touching any order.
            raise UnknownWebhookEvent(f"Unhandled Stripe event type: {event_type!r}")

        currency = session.get("currency")
        event_id = event.get("id")
        return WebhookEvent(
            provider=self.provider_name,
            event_type=event_type,
            payment_id=session.get("payment_intent"),
            provider_ref=session.get("id"),
            status=mapped_status,
            amount=_from_minor_units(session.get("amount_total"), currency),
            currency=str(currency or "").upper() or None,
            raw_payload=session,
            # The Event's OWN id ("evt_...") — Stripe redelivers the SAME
            # event id on retry until a 2xx is received, making it a
            # genuine, correct per-delivery replay key (same role as
            # Mercado Pago's notification id / Binance's own nonce).
            nonce=str(event_id) if event_id is not None else None,
        )

    # ------------------------------------------------------------------
    # Subscription Provider Contract (Stripe Recurring etapa)
    # ------------------------------------------------------------------

    async def create_subscription(
        self, *, amount: Decimal, currency: str, user_id: str, idempotency_key: str, metadata: dict | None = None,
    ) -> SubscriptionIntent:
        """Creates a Checkout Session in mode="subscription" — see
        module docstring's "Recurring billing" section for why this
        needs no pre-created Stripe Price (price_data inline, confirmed
        to work identically in this mode). `idempotency_key` is passed
        straight through as Stripe's own Idempotency-Key header — the
        SAME confirmed server-side deduplication create_intent() already
        relies on (see that method's own docstring), never a second key
        derived here.

        `provider_subscription_id` is NOT known yet — a Checkout Session
        only creates the real Stripe Subscription object once the payer
        actually completes it; that id arrives later, in the session's
        own `subscription` field on checkout.session.completed (see
        verify_subscription_webhook()'s own docstring)."""
        reference = _deterministic_reference(idempotency_key)
        goods_name = (metadata or {}).get("goods_name", "TRINDATRON subscription")
        requested_method_raw = (metadata or {}).get("payment_method")
        requested_method = PaymentMethod(requested_method_raw) if requested_method_raw else None

        body: dict = {
            "mode": "subscription",
            "client_reference_id": reference,
            "line_items": [{
                "price_data": {
                    "currency": currency.lower(),
                    "unit_amount": _to_minor_units(amount, currency),
                    "product_data": {"name": goods_name},
                    # Every TRINDATRON plan is billed monthly — see
                    # src/billing/pricing.py's own "Monthly price
                    # catalog" docstring. Not configurable per-call;
                    # there is no other cadence in the domain to express.
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }],
        }
        if self._success_url:
            body["success_url"] = self._success_url
        if self._cancel_url:
            body["cancel_url"] = self._cancel_url
        if requested_method is not None and requested_method in _STRIPE_PAYMENT_METHOD_TYPE:
            body["payment_method_types"] = [_STRIPE_PAYMENT_METHOD_TYPE[requested_method]]

        status_code, resp_body = await self._post(_CREATE_SESSION_PATH, body, idempotency_key=idempotency_key)
        data = _parse_json(resp_body)
        if status_code >= 400 or "url" not in data or "id" not in data:
            raise StripeError(status_code, (data.get("error") or {}).get("message") or data.get("message"))

        return SubscriptionIntent(
            id=data["id"],
            provider=self.provider_name,
            status=PaymentStatus.PENDING,
            idempotency_key=idempotency_key,
            redirect_url=data.get("url"),
            provider_subscription_id=None,
            metadata={"client_reference_id": reference},
        )

    async def cancel_subscription(self, provider_subscription_id: str) -> None:
        """DELETE /v1/subscriptions/{id} — cancels IMMEDIATELY (confirmed
        official behavior, not the deprecated cancel_at_period_end path
        — see module docstring)."""
        status_code, resp_body = await self._delete(
            _CANCEL_SUBSCRIPTION_PATH.format(subscription_id=provider_subscription_id)
        )
        data = _parse_json(resp_body)
        if status_code >= 400:
            raise StripeError(status_code, (data.get("error") or {}).get("message") or data.get("message"))

    async def retrieve_subscription(self, provider_subscription_id: str) -> SubscriptionIntent:
        status_code, resp_body = await self._get(
            _GET_SUBSCRIPTION_PATH.format(subscription_id=provider_subscription_id)
        )
        data = _parse_json(resp_body)
        if status_code == 404:
            raise PaymentIntentNotFound(provider_subscription_id)
        if status_code >= 400:
            raise StripeError(status_code, (data.get("error") or {}).get("message") or data.get("message"))

        mapped_status = _SUBSCRIPTION_STATUS_MAP.get(data.get("status"), PaymentStatus.PROCESSING)
        return SubscriptionIntent(
            id=provider_subscription_id,
            provider=self.provider_name,
            status=mapped_status,
            idempotency_key=provider_subscription_id,
            provider_subscription_id=provider_subscription_id,
            metadata={"raw_status": data.get("status")},
        )

    async def update_subscription(self, provider_subscription_id: str, *, metadata: dict) -> SubscriptionIntent:
        """Updates ONLY provider-side metadata — see module docstring's
        "Known gaps" for why plan/price changes and cancel-at-period-end
        are deliberately not exposed here."""
        body = {f"metadata[{k}]": v for k, v in metadata.items()}
        status_code, resp_body = await self._post(
            _UPDATE_SUBSCRIPTION_PATH.format(subscription_id=provider_subscription_id), body,
        )
        data = _parse_json(resp_body)
        if status_code >= 400:
            raise StripeError(status_code, (data.get("error") or {}).get("message") or data.get("message"))

        mapped_status = _SUBSCRIPTION_STATUS_MAP.get(data.get("status"), PaymentStatus.PROCESSING)
        return SubscriptionIntent(
            id=provider_subscription_id,
            provider=self.provider_name,
            status=mapped_status,
            idempotency_key=provider_subscription_id,
            provider_subscription_id=provider_subscription_id,
            metadata={"raw_status": data.get("status")},
        )

    def is_subscription_event(self, payload: bytes) -> bool:
        """See PaymentProvider.is_subscription_event()'s own docstring —
        an UNVERIFIED, best-effort peek at `payload` used only to pick
        which verify_* method routes/billing.py's receive_webhook()
        calls; the real Stripe-Signature check happens inside whichever
        one actually runs. True for any event.type already mapped in
        _SUBSCRIPTION_EVENT_KIND_MAP, or a checkout.session.completed
        whose embedded session object has mode="subscription" (the
        SAME condition verify_subscription_webhook() itself checks —
        see that method's own dispatch). Malformed JSON (which will
        fail whichever verify_* runs anyway) safely returns False here
        rather than raising."""
        try:
            event = _parse_json(payload)
        except Exception:
            return False
        event_type = event.get("type", "")
        if event_type in _SUBSCRIPTION_EVENT_KIND_MAP:
            return True
        if event_type == "checkout.session.completed":
            obj = (event.get("data") or {}).get("object") or {}
            return obj.get("mode") == "subscription"
        return False

    async def verify_subscription_webhook(self, payload: bytes, headers: Mapping[str, str]) -> SubscriptionWebhookEvent:
        """Verifies using the EXACT SAME Stripe-Signature mechanism as
        verify_webhook() (never a second, weaker scheme — both call the
        same _parse_stripe_signature()/_verify_stripe_signature()
        helpers) then dispatches on event.type for the confirmed
        subscription-lifecycle events (see module docstring's "Recurring
        billing" section for exactly which). Raises UnknownWebhookEvent
        for anything else (e.g. a one-time-payment event delivered to
        this method by mistake, or a genuinely unhandled type like
        customer.subscription.trial_will_end) — same absorbed-not-crashed
        contract as verify_webhook()'s own default branch."""
        signature_header = _get_header(headers, "stripe-signature")
        if not signature_header:
            raise InvalidWebhookSignature("Missing Stripe-Signature header")
        sig_parts = _parse_stripe_signature(signature_header)
        timestamp = sig_parts.get("t")
        v1 = sig_parts.get("v1")
        if not timestamp or not v1:
            raise InvalidWebhookSignature("Malformed Stripe-Signature header")
        try:
            ts_int = int(timestamp)
        except ValueError:
            raise InvalidWebhookSignature("Non-numeric timestamp in Stripe-Signature header") from None
        if abs(time.time() - ts_int) > self._webhook_tolerance_seconds:
            raise InvalidWebhookSignature("Stripe-Signature timestamp outside tolerance window")
        if not _verify_stripe_signature(secret=self._webhook_secret, timestamp=timestamp, raw_body=payload, expected_hex=v1):
            raise InvalidWebhookSignature("Stripe webhook signature verification failed")

        event = _parse_json(payload)
        event_type = event.get("type", "")
        obj = (event.get("data") or {}).get("object") or {}
        event_id = event.get("id")
        occurred_at_raw = event.get("created")
        occurred_at = (
            datetime.fromtimestamp(occurred_at_raw, tz=timezone.utc) if occurred_at_raw is not None else _utcnow()
        )

        if event_type == "checkout.session.completed" and obj.get("mode") == "subscription":
            provider_subscription_id = obj.get("subscription")
            return SubscriptionWebhookEvent(
                kind=SubscriptionEventKind.SUBSCRIPTION_CREATED,
                provider=self.provider_name,
                provider_subscription_id=str(provider_subscription_id) if provider_subscription_id else None,
                event_id=str(event_id) if event_id is not None else "",
                occurred_at=occurred_at,
                # The Session's OWN id — matches Subscription.
                # provider_setup_intent_id, letting
                # apply_subscription_webhook_event() find WHICH internal
                # Subscription this confirms (see SubscriptionWebhookEvent.
                # provider_ref's own docstring).
                provider_ref=str(obj.get("id")) if obj.get("id") is not None else None,
                raw_payload=obj,
            )

        kind = _SUBSCRIPTION_EVENT_KIND_MAP.get(event_type)
        if kind is None:
            raise UnknownWebhookEvent(f"Unhandled Stripe subscription event type: {event_type!r}")

        if kind in (SubscriptionEventKind.RENEWAL_SUCCEEDED, SubscriptionEventKind.RENEWAL_FAILED):
            # obj is an Invoice — id/subscription/amount_paid/currency
            # confirmed fields (see module docstring).
            currency = obj.get("currency")
            return SubscriptionWebhookEvent(
                kind=kind,
                provider=self.provider_name,
                provider_subscription_id=str(obj.get("subscription")) if obj.get("subscription") else None,
                event_id=str(event_id) if event_id is not None else "",
                occurred_at=occurred_at,
                provider_ref=str(obj.get("id")) if obj.get("id") is not None else None,
                amount=_from_minor_units(obj.get("amount_paid"), currency),
                currency=str(currency or "").upper() or None,
                raw_payload=obj,
            )

        # SUBSCRIPTION_CANCELED / SUBSCRIPTION_UPDATED — obj is a Subscription.
        return SubscriptionWebhookEvent(
            kind=kind,
            provider=self.provider_name,
            provider_subscription_id=str(obj.get("id")) if obj.get("id") is not None else None,
            event_id=str(event_id) if event_id is not None else "",
            occurred_at=occurred_at,
            raw_payload=obj,
        )

    async def refund(self, payment_id: str, *, amount: Decimal | None = None, reason: str = "") -> PaymentResult:
        """`payment_id` is Stripe's OWN PaymentIntent id ("pi_...") — see
        module docstring's "Known gaps" section for the identity-space
        caveat. For a PARTIAL refund, the PaymentIntent is fetched FIRST
        (GET /v1/payment_intents/{id}) purely to learn its currency —
        `_to_minor_units()` cannot safely convert a Decimal without
        knowing whether the currency is zero-decimal. A full refund
        (amount=None) needs no such lookup — Stripe refunds the entire
        remaining charge on its own. Idempotency-Key is derived
        deterministically from (payment_id, amount), same convention as
        MercadoPagoAdapter's own refund()."""
        currency: str | None = None
        if amount is not None:
            status_code, resp_body = await self._get(_GET_PAYMENT_INTENT_PATH.format(payment_intent_id=payment_id))
            pi_data = _parse_json(resp_body)
            if status_code >= 400:
                raise StripeError(status_code, (pi_data.get("error") or {}).get("message") or pi_data.get("message"))
            currency = pi_data.get("currency")

        idem_seed = f"refund:{payment_id}:{amount if amount is not None else 'full'}"
        idempotency_key = hashlib.sha256(idem_seed.encode("utf-8")).hexdigest()[:32]

        body: dict = {"payment_intent": payment_id}
        if amount is not None:
            body["amount"] = _to_minor_units(amount, currency or "usd")

        status_code, resp_body = await self._post(_CREATE_REFUND_PATH, body, idempotency_key=idempotency_key)
        data = _parse_json(resp_body)
        if status_code >= 400:
            raise StripeError(status_code, (data.get("error") or {}).get("message") or data.get("message"))

        refund_currency = data.get("currency") or currency
        refunded_amount = _from_minor_units(data.get("amount"), refund_currency) or Decimal("0")
        return PaymentResult(
            payment_id=str(data.get("payment_intent") or payment_id),
            provider=self.provider_name,
            provider_ref=str(data.get("id")) if data.get("id") is not None else None,
            status=PaymentStatus.PARTIAL_REFUND if amount is not None else PaymentStatus.REFUNDED,
            amount_gross=refunded_amount,
            currency=str(refund_currency or "").upper(),
            provider_fee=Decimal("0"),
            net_amount=refunded_amount,
            metadata={"raw_status": data.get("status")},
        )


# ---------------------------------------------------------------------------
# Factory — the ONLY place that decides whether Stripe gets registered.
# ---------------------------------------------------------------------------


def build_stripe_adapter(settings) -> StripeAdapter | None:
    """Return a configured StripeAdapter, or None if it should not be
    registered. Registered ONLY when settings.stripe_enabled is True AND
    both stripe_secret_key and stripe_webhook_secret are non-empty —
    same "never registered by default, never raises for missing config"
    contract as build_mercadopago_adapter()/build_binance_pay_adapter()/
    build_nowpayments_adapter()."""
    if not settings.stripe_enabled:
        return None
    if not settings.stripe_secret_key or not settings.stripe_webhook_secret:
        return None
    return StripeAdapter(
        secret_key=settings.stripe_secret_key,
        webhook_secret=settings.stripe_webhook_secret,
        base_url=settings.stripe_base_url,
        transport=HttpxStripeTransport(),
        success_url=settings.stripe_success_url or None,
        cancel_url=settings.stripe_cancel_url or None,
        webhook_tolerance_seconds=settings.stripe_webhook_tolerance_seconds,
    )
