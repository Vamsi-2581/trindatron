"""
Payment Provider Abstractions — Billing-0.

TRINITY Core must never depend on a specific payment SDK (Stripe, Binance, PayPal, etc.).
All provider-specific code lives in concrete adapter classes that implement
one of the ABCs defined here.

Hierarchy:
  PaymentProvider (ABC)
    ├── FiatPaymentProvider      — Stripe, PayPal, Mercado Pago, etc.
    ├── CryptoPaymentProvider    — native crypto: BTC, ETH, SOL
    │     └── StablecoinPaymentProvider  — USDT, USDC (fewer confirmations needed)
    └── ExchangePaymentProvider  — Binance Pay, Coinbase Commerce, etc.

Adding a new provider = implement one of these ABCs + register in config.
Zero changes to the TRINITY Core are required.

Nothing in this file performs I/O or imports any third-party SDK.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Mapping


# ---------------------------------------------------------------------------
# Shared enums and data classes
# ---------------------------------------------------------------------------


class PaymentStatus(str, Enum):
    """Canonical payment lifecycle states — independent of any provider's terminology."""

    PENDING = "pending"       # intent created, awaiting user action
    PROCESSING = "processing" # provider is processing (e.g. awaiting blockchain conf.)
    CONFIRMED = "confirmed"   # payment received and verified
    FAILED = "failed"         # payment attempt failed
    CANCELLED = "cancelled"   # cancelled before completion
    REFUNDED = "refunded"     # fully refunded
    PARTIAL_REFUND = "partial_refund"  # partially refunded
    EXPIRED = "expired"       # intent expired without payment


class PaymentMethod(str, Enum):
    """Canonical payment method — HOW a user pays, independent of WHICH
    provider processes it. A method is never a provider: e.g. a Pix
    payment routed through Mercado Pago is
    (payment_method=PIX, provider="mercado_pago"), never
    payment_method="mercado_pago". Only methods that are actually part of
    the TRINDATRON global payments architecture (see docs/CHECKOUT.md) —
    not a speculative superset of every method any provider offers."""

    PIX = "pix"
    CREDIT_CARD = "credit_card"
    DEBIT_CARD = "debit_card"
    BOLETO = "boleto"
    BANK_TRANSFER = "bank_transfer"
    WALLET = "wallet"
    CRYPTO = "crypto"


class PaymentType(str, Enum):
    """Whether a payment is a single charge or part of a recurring
    schedule. Independent of `plan`/`Subscription` — e.g. a one-time
    plan purchase (PaymentOrder.plan set) still has payment_type=ONE_TIME;
    only a Subscription-backed charge uses RECURRING."""

    ONE_TIME = "one_time"
    RECURRING = "recurring"


@dataclass
class PaymentIntent:
    """Provider-agnostic representation of a payment intent.

    Created by PaymentProvider.create_intent(). The provider fills in
    whichever fields are relevant to its mechanism:
      - Fiat providers: client_secret for widget embedding
      - Crypto providers: address + network for on-chain transfer
      - Exchange providers: redirect_url for hosted checkout
    """

    id: str                        # TRINITY-internal intent ID
    provider: str                  # e.g. "stripe", "binance_pay", "btc_onchain"
    amount: Decimal                # amount in the requested currency
    currency: str                  # "USD", "BRL", "BTC", "USDT", "USDC"
    status: PaymentStatus
    idempotency_key: str           # caller-provided; prevents duplicate intents
    # Fiat / hosted checkout
    client_secret: str | None = None     # Stripe widget token or equivalent
    redirect_url: str | None = None      # for provider-hosted checkout pages
    # Crypto on-chain
    address: str | None = None           # receiving wallet address (unique per intent)
    network: str | None = None           # "bitcoin", "ethereum", "tron", etc.
    expected_confirmations: int | None = None
    # Timing
    expires_at: datetime | None = None
    created_at: datetime | None = None
    # Arbitrary provider metadata (never contains secrets)
    metadata: dict = field(default_factory=dict)


@dataclass
class PaymentResult:
    """Outcome of a confirmed or attempted payment.

    Returned by get_intent() after a webhook or polling confirms the payment.
    All monetary amounts use Decimal to preserve precision.
    """

    payment_id: str               # TRINITY-internal ID
    provider: str
    provider_ref: str | None      # provider's own transaction ID
    status: PaymentStatus
    amount_gross: Decimal         # amount the user sent
    currency: str
    provider_fee: Decimal         # fee retained by the provider
    net_amount: Decimal           # amount_gross - provider_fee (Decimal arithmetic)
    confirmed_at: datetime | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class WebhookEvent:
    """Parsed and verified webhook payload from a provider.

    verify_webhook() validates the HMAC/signature and returns this
    normalized structure — the caller never touches raw bytes/signatures.
    """

    provider: str
    event_type: str               # provider's event name (e.g. "payment_intent.succeeded")
    payment_id: str | None        # TRINITY intent ID if determinable
    provider_ref: str | None      # provider's own event/transaction ID
    status: PaymentStatus | None
    amount: Decimal | None
    currency: str | None
    raw_payload: dict = field(default_factory=dict)  # parsed JSON (no secrets)
    # Production Payment Activation hardening — a per-delivery unique
    # value the provider itself generates (e.g. Binance Pay's
    # BinancePay-Nonce header), used ONLY for replay detection
    # (src/services/webhook_replay_service.py) — never a secret, never
    # logged as sensitive. None for a provider/test-double that doesn't
    # supply one; replay tracking is then simply skipped for that event
    # (no regression — the pre-existing timestamp/idempotency backstops
    # still apply unchanged).
    nonce: str | None = None


# ---------------------------------------------------------------------------
# Subscription Provider Contract — Stripe Recurring etapa (2026-09-08)
# ---------------------------------------------------------------------------
#
# Deliberately SEPARATE from PaymentIntent/WebhookEvent above: a
# PaymentIntent/create_intent() represents ONE charge (one-time, or a
# single already-decided renewal cycle a caller — SubscriptionService.
# attempt_renewal() — explicitly initiates through the SAME PaymentRouter
# eligibility/failover machinery every other checkout already uses).
# A SubscriptionIntent/create_subscription() represents the RECURRING
# AGREEMENT itself — for Stripe, a hosted Checkout Session in
# mode="subscription" that establishes a payment-method-on-file mandate
# and (once completed) a real, provider-managed Subscription object that
# bills itself automatically. These are genuinely different provider
# operations with different lifecycles; conflating them into one
# create_intent() signature would either force every one-time-only
# adapter (NOWPayments, Binance Pay) to reason about recurring concepts,
# or force Stripe's own one-time path to carry subscription-only fields.
#
# NOT abstract, deliberately (same rationale as supported_payment_methods/
# supported_payment_types below): most current adapters do not, and may
# never, support recurring billing. Raising SubscriptionOperationNotSupported
# by default means an adapter that never overrides these methods is
# simply never asked to run them — SubscriptionService only ever calls
# them on a provider PaymentRouter.eligible_providers() already filtered
# for PaymentType.RECURRING support.


class SubscriptionOperationNotSupported(NotImplementedError):
    """Raised by the DEFAULT (non-overridden) create_subscription()/
    cancel_subscription()/retrieve_subscription()/update_subscription()/
    verify_subscription_webhook() — a provider that doesn't declare
    PaymentType.RECURRING in supported_payment_types should never have
    any of these called on it by SubscriptionService (the Payment
    Router's own capability filter prevents that), so reaching this is
    itself a signal of a caller bug, not a normal runtime outcome."""


@dataclass
class SubscriptionIntent:
    """Provider-agnostic representation of a recurring-billing mandate,
    returned by create_subscription(). Mirrors PaymentIntent's own shape
    deliberately (id/provider/redirect_url/metadata) but adds
    provider_subscription_id — NULL until the provider's own webhook
    confirms the mandate actually completed (e.g. Stripe's
    checkout.session.completed for a mode="subscription" session reports
    the newly-created Subscription's own id in its `subscription` field
    — see module docstring's own "Identity" note in stripe_adapter.py).
    """

    id: str                          # TRINITY-internal intent ID (e.g. our Checkout Session id)
    provider: str
    status: PaymentStatus            # PENDING until the provider confirms the mandate
    idempotency_key: str
    redirect_url: str | None = None  # hosted setup page, if any
    provider_subscription_id: str | None = None  # set only once genuinely known
    metadata: dict = field(default_factory=dict)


class SubscriptionEventKind(str, Enum):
    """Internal vocabulary a per-provider webhook translation layer
    (verify_subscription_webhook()) maps external events onto —
    SubscriptionService consumes ONLY this vocabulary, never a
    provider's own JSON shape (see module docstring's own "Webhook
    abstraction" note, and stripe_adapter.py's verify_subscription_webhook()
    for the concrete Stripe event-name mapping, confirmed against
    official docs, not assumed)."""

    SUBSCRIPTION_CREATED = "subscription_created"
    RENEWAL_SUCCEEDED = "renewal_succeeded"
    RENEWAL_FAILED = "renewal_failed"
    SUBSCRIPTION_CANCELED = "subscription_canceled"
    SUBSCRIPTION_UPDATED = "subscription_updated"


@dataclass
class SubscriptionWebhookEvent:
    """Normalized subscription-lifecycle event — the ONLY shape
    SubscriptionService.apply_subscription_webhook_event() ever
    consumes. `event_id`/`occurred_at` are the provider's OWN,
    authoritative values (e.g. Stripe's Event.id/Event.created) — used
    for the out-of-order/duplicate guard; NEVER inferred from HTTP
    arrival order (see module docstring's own "Webhook ordering" note).
    """

    kind: SubscriptionEventKind
    provider: str
    # NULL only for SUBSCRIPTION_CREATED when the provider hasn't
    # assigned the real subscription id yet at event time (never true
    # for Stripe's own checkout.session.completed, which always carries
    # it — see StripeAdapter's own docstring; kept nullable for a FUTURE
    # provider whose creation event might genuinely lack it).
    provider_subscription_id: str | None
    event_id: str                     # provider's own per-delivery unique id
    occurred_at: datetime             # provider's own event timestamp (NOT arrival time)
    # The provider's own reference for whatever object THIS event is
    # about — mirrors WebhookEvent.provider_ref's naming/role exactly.
    # For SUBSCRIPTION_CREATED: the setup attempt's own id (e.g. Stripe's
    # Checkout Session "cs_..." — matches Subscription.
    # provider_setup_intent_id, letting apply_subscription_webhook_event()
    # find WHICH internal Subscription this confirms). For
    # RENEWAL_SUCCEEDED/RENEWAL_FAILED: the provider's own identifier for
    # THIS billing cycle's charge (e.g. Stripe's Invoice.id) — the
    # deterministic identity a reactively-created PaymentOrder's
    # idempotency_key is derived from (see subscription_service.py).
    provider_ref: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    raw_payload: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Base ABC
# ---------------------------------------------------------------------------


class PaymentProvider(ABC):
    """Abstract base for all payment providers.

    Concrete implementations live outside the TRINITY Core:
      billing/providers/adapters/stripe_adapter.py
      billing/providers/adapters/binance_pay_adapter.py
      etc.

    The Core only imports PaymentProvider, never the concrete adapters.
    Providers are registered via config (e.g. payment_providers_enabled=["stripe"]).
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Unique identifier for this provider, e.g. "stripe", "binance_pay"."""

    @abstractmethod
    async def create_intent(
        self,
        *,
        amount: Decimal,
        currency: str,
        user_id: str,
        idempotency_key: str,
        metadata: dict | None = None,
    ) -> PaymentIntent:
        """Create a new payment intent.

        idempotency_key must be unique per logical operation. Repeated calls
        with the same key return the original intent without creating a duplicate.
        """

    @abstractmethod
    async def get_intent(self, intent_id: str) -> PaymentIntent:
        """Retrieve current state of a payment intent."""

    @abstractmethod
    async def verify_webhook(
        self,
        payload: bytes,
        headers: Mapping[str, str],
    ) -> WebhookEvent:
        """Verify webhook signature and return a normalized event.

        Takes the full header mapping (not a single signature string) because
        real providers vary in how many headers their scheme needs — a single
        HMAC provider needs one header, but e.g. Binance Pay's RSA scheme
        needs four (timestamp, nonce, certificate serial, signature) to
        reconstruct the signed payload and pick the right public key. Header
        lookups inside implementations must be case-insensitive (HTTP header
        names are case-insensitive; FastAPI's Request.headers already is).

        Raises:
            InvalidWebhookSignature: if the signature does not match.
            UnknownWebhookEvent: if the event type is not recognized.
        """

    @abstractmethod
    async def refund(
        self,
        payment_id: str,
        *,
        amount: Decimal | None = None,
        reason: str = "",
    ) -> PaymentResult:
        """Initiate a refund. amount=None means full refund."""

    @property
    def supported_payment_methods(self) -> frozenset[PaymentMethod]:
        """Which PaymentMethod values this provider can actually process.

        NOT abstract, deliberately: most existing adapters/test doubles
        predate this concept, and inventing capabilities for a provider
        that was never asked about it would be a fabrication, not a
        declaration. Empty by default — a provider makes a real claim
        about itself only by overriding this. Checkout dispatch (see
        routes/billing.py) uses this to filter candidate providers when
        the caller supplies `payment_method` without an explicit
        `provider`; it is a plain capability filter, not a cost/priority
        router (no such router exists yet — see docs/CHECKOUT.md).
        """
        return frozenset()

    @property
    def supported_payment_types(self) -> frozenset[PaymentType]:
        """Which PaymentType values (ONE_TIME / RECURRING) this provider
        can actually process. Same non-abstract, empty-by-default
        rationale as supported_payment_methods above."""
        return frozenset()

    @property
    def supported_currencies(self) -> frozenset[str]:
        """Which ISO 4217 currency codes (uppercase) this provider can
        price and charge in.

        Payment Router etapa — DELIBERATELY THE OPPOSITE default
        semantic from supported_payment_methods/supported_payment_types
        above: empty here means UNRESTRICTED (matches any currency), not
        "matches nothing". Those two are genuinely gateable, closed,
        per-provider concepts where a provider that hasn't been asked
        should be assumed capable of nothing until it says otherwise.
        Currency is different: a provider with no CONFIRMED narrow
        market list (e.g. Stripe's own 135+ currencies, whose exact
        boundary depends on account/settlement details this session
        could not exhaustively verify) is far more likely to be broadly
        flexible than to support NOTHING — declaring a narrow allowlist
        without real confirmation would be a FALSE restriction, worse
        than declaring none. Override this ONLY when a provider's real,
        confirmed market list is genuinely narrow and closed (e.g.
        Mercado Pago's 7 LATAM currencies — see that adapter's own
        override and module docstring for the citation).
        """
        return frozenset()

    # ------------------------------------------------------------------
    # Subscription Provider Contract — see this module's own section
    # above (SubscriptionIntent/SubscriptionWebhookEvent) for the full
    # rationale. NOT abstract — default raises
    # SubscriptionOperationNotSupported; only a provider whose
    # supported_payment_types includes PaymentType.RECURRING overrides
    # these (Stripe, as of this etapa — see stripe_adapter.py).
    # ------------------------------------------------------------------

    async def create_subscription(
        self,
        *,
        amount: Decimal,
        currency: str,
        user_id: str,
        idempotency_key: str,
        metadata: dict | None = None,
    ) -> SubscriptionIntent:
        """Establish a new recurring-billing mandate. `idempotency_key`
        must be unique per logical subscription-creation intent —
        repeated calls with the same key must return the SAME mandate,
        never create a second one (see each override's own docstring
        for how that provider's real idempotency mechanism achieves
        this)."""
        raise SubscriptionOperationNotSupported(f"{self.provider_name} does not support recurring subscriptions")

    async def cancel_subscription(self, provider_subscription_id: str) -> None:
        """Cancel an existing provider-side subscription immediately."""
        raise SubscriptionOperationNotSupported(f"{self.provider_name} does not support recurring subscriptions")

    async def retrieve_subscription(self, provider_subscription_id: str) -> SubscriptionIntent:
        """Fetch the current state of a provider-side subscription —
        used for reconciliation, never for the ordinary webhook-driven
        path (see SubscriptionService's own module docstring)."""
        raise SubscriptionOperationNotSupported(f"{self.provider_name} does not support recurring subscriptions")

    async def update_subscription(self, provider_subscription_id: str, *, metadata: dict) -> SubscriptionIntent:
        """Update provider-side metadata on an existing subscription.
        Deliberately narrow (metadata only) — see stripe_adapter.py's
        own docstring for why broader mutation (plan/price changes,
        cancel-at-period-end) is explicitly NOT built this etapa."""
        raise SubscriptionOperationNotSupported(f"{self.provider_name} does not support recurring subscriptions")

    async def verify_subscription_webhook(
        self, payload: bytes, headers: Mapping[str, str],
    ) -> SubscriptionWebhookEvent:
        """Verify a subscription-lifecycle webhook (signature mechanism
        is provider-specific but MUST reuse the exact same verification
        as verify_webhook() — never a second, weaker scheme) and return
        a normalized SubscriptionWebhookEvent. Raises
        InvalidWebhookSignature/UnknownWebhookEvent, same contract as
        verify_webhook()."""
        raise SubscriptionOperationNotSupported(f"{self.provider_name} does not support recurring subscriptions")

    def is_subscription_event(self, payload: bytes) -> bool:
        """Best-effort, UNVERIFIED classification of a raw incoming
        webhook payload as belonging to the subscription-lifecycle
        event vocabulary (SubscriptionEventKind) rather than an
        ordinary one-time payment event — used ONLY by routes/billing.py's
        shared receive_webhook() to decide whether to call
        verify_subscription_webhook() or verify_webhook() for THIS
        delivery. Reading `payload` here happens BEFORE signature
        verification, so this carries NO trust of its own: whichever
        verify_* method is chosen still independently checks the exact
        same HMAC signature over the exact same raw bytes, so nothing
        about routing this way can be exploited to bypass verification
        or reach different business logic without a valid signature —
        a forged payload with no valid signature is rejected by
        whichever verify_* method actually runs.

        Stripe Recurring etapa (2026-09-08 hardening pass) — added so
        ONE webhook endpoint can serve both one-time and recurring
        Stripe events without two parallel webhook systems (see
        routes/billing.py's own receive_webhook() docstring). Default:
        False — a provider that doesn't declare PaymentType.RECURRING
        has no subscription-lifecycle event vocabulary at all, and
        routes/billing.py never even calls this for such a provider
        (gated on `PaymentType.RECURRING in adapter.supported_payment_types`
        first)."""
        return False

    def webhook_ack_body(self) -> dict | None:
        """Return this provider's required HTTP 200 webhook acknowledgment
        body, or None to use TRINITY's own default response shape.

        NOT abstract, deliberately: most providers don't require a specific
        ack format, so the default (None) preserves the existing generic
        WebhookEventResponse for every provider that doesn't override this —
        including every FakeAdapter/test double already in this codebase.
        Only override this when a provider's own documentation requires a
        specific body or it keeps retrying a webhook that was already
        processed correctly (e.g. Binance Pay expects exactly
        {"returnCode": "SUCCESS", "returnMessage": null}, not TRINITY's own
        JSON shape — see receive_webhook()'s use of this in routes/billing.py).
        """
        return None


# ---------------------------------------------------------------------------
# Specialized ABCs — extend PaymentProvider with domain-specific contracts
# ---------------------------------------------------------------------------


class FiatPaymentProvider(PaymentProvider):
    """Abstract for fiat-currency providers: Stripe, PayPal, Mercado Pago, etc.

    Fiat intents return client_secret or redirect_url for the user to
    complete payment on the provider's interface (PCI compliance stays
    with the provider, not TRINITY).
    """


class CryptoPaymentProvider(PaymentProvider):
    """Abstract for native-crypto providers: BTC on-chain, ETH on-chain, etc.

    Key contract additions:
      - Each intent gets a UNIQUE receiving address (never reuse addresses).
      - Funds are only credited after >= min_confirmations blocks.
      - Exchange-rate lock: amount_in_crypto fixed at intent creation time.
    """

    @property
    @abstractmethod
    def min_confirmations(self) -> int:
        """Minimum block confirmations before crediting funds.

        Recommended defaults:
          BTC: 3 (≈ 30 minutes)
          ETH: 12 (≈ 2.5 minutes)
          Tron/USDT-TRC20: 1 (≈ 3 seconds)
        """

    @abstractmethod
    async def get_confirmations(self, txid: str) -> int:
        """Return current on-chain confirmation count for a transaction."""

    @abstractmethod
    async def generate_address(self, *, user_id: str, payment_id: str) -> str:
        """Generate a fresh receiving address unique to this intent.

        Addresses must NEVER be reused across intents.
        """


class StablecoinPaymentProvider(CryptoPaymentProvider):
    """Abstract for stablecoins: USDT, USDC on any supported network.

    Fewer confirmations typically needed vs. native crypto.
    1 USDT ≈ 1 USD — but verify on-chain, do not hardcode the peg.
    """


class ExchangePaymentProvider(PaymentProvider):
    """Abstract for exchange-hosted payment: Binance Pay, Coinbase Commerce.

    The exchange handles custody, conversion, and compliance.
    TRINITY receives a webhook when payment is confirmed.
    Recommended for BTC/crypto MVP before implementing on-chain directly.
    """


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InvalidWebhookSignature(Exception):
    """Raised when a webhook signature fails verification."""


class UnknownWebhookEvent(Exception):
    """Raised when the provider sends an event type we don't handle."""


class PaymentIntentNotFound(Exception):
    """Raised when an intent ID does not exist at the provider."""


class InsufficientFundsError(Exception):
    """Raised when a refund amount exceeds the original payment."""


class ProviderUnavailable(Exception):
    """Raised when the payment provider returns a 5xx or is unreachable.

    Payment Router etapa — this is the AMBIGUOUS-OUTCOME signal: raised
    ONLY for a network-level failure (timeout, connection refused, DNS
    failure) where no confirmed HTTP response was ever received. The
    request MAY have reached the provider and created a real external
    session/charge despite the local side never learning that — see
    PaymentCreationRejected's own docstring for the mutually-exclusive
    counterpart. Every adapter's own transport layer normalizes ALL such
    network-level failures into this ONE class (never into a concrete
    provider-specific error), which is exactly what lets a
    provider-agnostic caller (the Payment Router) treat "ambiguous, do
    not fail over" as a single, reliable `except ProviderUnavailable`
    clause with zero per-provider special-casing.
    """


class PaymentCreationRejected(Exception):
    """Raised by create_intent() (or another provider call) when the
    provider's own API returned a REAL, PARSED response that explicitly,
    unambiguously rejects the request — e.g. an HTTP 400/401/403/422
    with an error body. Never raised for a network-level ambiguity
    (that raises ProviderUnavailable instead, which is NEVER a subclass
    of this).

    Payment Router etapa — this is the SAFE-TO-FAIL-OVER signal: because
    a real response was received and explicitly says "no", the caller
    can be certain no charge/session was created provider-side, so
    trying a different eligible provider for the SAME logical operation
    cannot produce a duplicate external commitment.

    Each concrete adapter's own error class (NOWPaymentsError,
    BinancePayError, MercadoPagoError, StripeError, ...) inherits from
    this — a provider-agnostic caller catches ONLY this base class and
    never needs to import (or even know about) any concrete adapter's
    own exception type, matching this codebase's own "Core must never
    depend on a specific payment SDK" principle applied to error
    handling, not just request/response shapes.
    """
