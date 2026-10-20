"""
Billing schemas — request and response models for the PaymentOrder API.

Scope
-----
These schemas cover the core PaymentOrder lifecycle and webhook responses:
  CREATE   → CreateOrderRequest
  READ     → PaymentOrderResponse
  LIST     → PaymentOrderListResponse
  REFUND   → RefundOrderRequest
  BALANCE  → CreditBalanceResponse, UserCreditSummaryResponse
  WEBHOOK  → WebhookEventResponse (Billing-2)

Stripe Recurring etapa (2026-09-08 hardening pass) added the minimal
subscription surface:
  CREATE   → CreateSubscriptionRequest / CreateSubscriptionResponse
  READ     → SubscriptionResponse

Explicitly out of scope
-----------------------
- Provider-specific fields (client_secret, wallet address, redirect_url)
- Plan upgrade / pricing schemas
- Subscription update/list endpoints — not built this pass (see
  routes/billing.py's own module docstring for exactly what exists)

Security notes
--------------
* `webhook_raw` is intentionally absent from PaymentOrderResponse.
  It is an audit-only field that may contain provider event data and is
  never exposed via the public API.
* `user_id` is absent from all request schemas.
  It is injected from the authenticated JWT token by the route handler.
* All monetary amounts use Decimal — serialised as strings in JSON by
  Pydantic v2, preserving full financial precision without float rounding.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from src.billing.providers.base import PaymentMethod, PaymentType


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class CreateOrderRequest(BaseModel):
    """Request body for POST /billing/orders.

    Caller is responsible for choosing a stable, unique idempotency_key.
    Duplicate calls with the same key return the existing order without error.
    expires_at must be a future datetime — naive datetimes are interpreted as UTC.

    Financial values are validated as strictly positive. The DB enforces the
    same constraint via CHECK, but schema-level validation gives the caller a
    clear 422 error instead of a 500.
    """

    provider: str = Field(
        ...,
        min_length=1,
        max_length=50,
        description="Payment provider identifier, e.g. 'binance_pay', 'btc_onchain'.",
    )
    amount_fiat: Decimal = Field(
        ...,
        gt=Decimal("0"),
        description="Gross amount the user pays at the provider (strictly positive).",
    )
    currency_fiat: str = Field(
        ...,
        min_length=1,
        max_length=10,
        description="Currency of amount_fiat — ISO 4217 for fiat, symbol for crypto.",
    )
    credits_amount: Decimal = Field(
        ...,
        gt=Decimal("0"),
        description=(
            "Credits to apply when this order is confirmed. "
            "Fixed at order creation — never recalculated at confirmation time."
        ),
    )
    currency_credits: str = Field(
        ...,
        min_length=1,
        max_length=10,
        description="Currency of the credit applied to the user's balance, e.g. 'USD', 'TCU'.",
    )
    idempotency_key: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Caller-provided unique key. Duplicate calls with the same key are idempotent.",
    )
    expires_at: datetime = Field(
        ...,
        description=(
            "UTC datetime when this order expires. "
            "After expiry, confirm_order() transitions it to 'expired'. "
            "Naive datetimes are interpreted as UTC."
        ),
    )

    @field_validator("expires_at", mode="after")
    @classmethod
    def _validate_expires_at(cls, v: datetime) -> datetime:
        """Require timezone-aware datetime and verify the order is not already expired.

        mode="after" guarantees that Pydantic has already coerced the input to a
        datetime object (including ISO-8601 strings from JSON requests), so the
        isinstance check always holds and validation always runs.

        Naive datetimes are rejected with a clear error — callers must supply
        explicit timezone info (UTC strongly recommended).  This avoids silent
        timezone misinterpretations for financial order expiry.

        Example accepted values: '2026-09-01T12:00:00Z', '2026-09-01T12:00:00+00:00'
        """
        if v.tzinfo is None:
            raise ValueError(
                "expires_at must include timezone info (UTC recommended). "
                "Example: '2026-09-01T12:00:00Z' or '2026-09-01T12:00:00+00:00'."
            )
        if v <= datetime.now(timezone.utc):
            raise ValueError("expires_at must be a future datetime (received a past value)")
        return v


class CreateCheckoutRequest(BaseModel):
    """Request body for POST /billing/checkout (Commercial Checkout V1).

    Deliberately NOT amount_fiat/credits_amount — those are looked up
    server-side from src/billing/pricing.py's price catalog, never
    trusted from the client (a client-supplied amount for a plan
    purchase would be a real pricing-integrity hole). This is the ONLY
    difference from the generic CreateOrderRequest above; everything
    else (idempotency, expiry, provider dispatch) reuses the exact same
    PaymentOrderService/create_provider_order() machinery.

    Payments Foundation etapa (2026-09-07): `provider` is now OPTIONAL —
    LEGACY explicit-provider selection, kept only for backward
    compatibility with existing callers/tests. Going forward the caller
    supplies `payment_method`/`payment_type` (never a provider name) and
    the route resolves a registered provider whose declared capabilities
    match (see routes/billing.py's `_resolve_checkout_provider` — a
    plain capability filter, NOT a PaymentRouter: no cost/priority
    logic, no failover, no automatic retry across providers). `currency`
    is optional and defaults to CHECKOUT_CURRENCY (USD); it must have a
    price in src/billing/pricing.py's catalog for `plan` or the request
    404s, same as an unpriced plan does today.
    """

    plan: str = Field(
        ...,
        min_length=1,
        max_length=20,
        description="Target plan, e.g. 'start', 'pro', 'max', 'team'. Must have a price in the catalog.",
    )
    provider: Optional[str] = Field(
        None,
        min_length=1,
        max_length=50,
        description=(
            "LEGACY explicit provider identifier, e.g. 'binance_pay'. Omit "
            "and supply payment_method/payment_type instead so the server "
            "resolves a capable registered provider."
        ),
    )
    payment_method: Optional[PaymentMethod] = Field(
        None,
        description="Desired payment method (e.g. 'pix', 'crypto'). Used to resolve a provider when `provider` is omitted.",
    )
    payment_type: Optional[PaymentType] = Field(
        None,
        description="Desired payment type ('one_time' / 'recurring'). Used to resolve a provider when `provider` is omitted.",
    )
    currency: Optional[str] = Field(
        None,
        min_length=1,
        max_length=10,
        description="ISO 4217 currency for this checkout. Defaults to CHECKOUT_CURRENCY (USD). Must have a catalog price for `plan`.",
    )
    idempotency_key: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description=(
            "Caller-provided unique key for THIS checkout attempt. The frontend "
            "generates this once per attempt and persists it (e.g. sessionStorage) "
            "so a page refresh or a retried click reuses the SAME key — "
            "duplicate calls with the same key return the existing order, never "
            "a second one."
        ),
    )


class CreateSubscriptionRequest(BaseModel):
    """Request body for POST /billing/subscriptions (Stripe Recurring
    etapa). Mirrors CreateCheckoutRequest's own "never trust the client
    for price" discipline: amount is ALWAYS looked up server-side from
    src/billing/pricing.py, keyed by `plan` — never accepted here.
    `provider` is deliberately NOT accepted (unlike the legacy
    CreateCheckoutRequest.provider) — recurring billing is new enough
    that only the Payment Router's own capability-based selection
    (payment_method + payment_type=RECURRING + currency) exists; there
    is no legacy explicit-provider path to preserve for it.
    """

    plan: str = Field(
        ...,
        min_length=1,
        max_length=20,
        description="Target plan, e.g. 'start', 'pro', 'max', 'team'. Must have a price in the catalog.",
    )
    payment_method: PaymentMethod = Field(
        ...,
        description="Desired payment method (e.g. 'credit_card'). Used to resolve a RECURRING-capable registered provider.",
    )
    currency: Optional[str] = Field(
        None,
        min_length=1,
        max_length=10,
        description="ISO 4217 currency for this subscription. Defaults to CHECKOUT_CURRENCY (USD). Must have a catalog price for `plan`.",
    )
    idempotency_key: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description=(
            "Caller-provided unique key for THIS subscription-creation attempt. "
            "Duplicate calls with the same key return the existing Subscription "
            "and never create a second one, nor call the provider a second time "
            "once the external mandate has been set up."
        ),
    )


class RefundOrderRequest(BaseModel):
    """Request body for POST /billing/orders/{order_id}/refund.

    amount must be ≤ the available credits (credits_amount minus already-refunded).
    If the cumulative refunds equal credits_amount the order transitions to 'refunded'.
    Any smaller positive amount gives 'partial_refund' (non-terminal in Billing-2).

    The guard `amount > available` raises HTTP 422 via
    RefundAmountExceedsEligible from PaymentOrderService — validated
    against live DB data (cumulative sum), not here.
    """

    amount: Decimal = Field(
        ...,
        gt=Decimal("0"),
        description="Positive amount to refund (in order.currency_credits). Must be ≤ available credits.",
    )
    note: Optional[str] = Field(
        None,
        max_length=500,
        description="Optional audit note stored on the CreditTransaction.",
    )
    idempotency_key: Optional[str] = Field(
        None,
        min_length=1,
        max_length=128,
        description=(
            "Caller-provided idempotency key. Duplicate calls with the same key for the "
            "same order return the existing order without creating a new refund record. "
            "If omitted, a fresh UUID is generated — no idempotent protection."
        ),
    )


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class PaymentOrderResponse(BaseModel):
    """Response representation of a single PaymentOrder.

    Fields intentionally omitted
    ----------------------------
    * webhook_raw — audit-only field, may contain provider event data.
      Never exposed via the public API.
    """

    id: str
    user_id: str
    provider: str
    external_order_id: Optional[str] = None
    status: str
    amount_fiat: Decimal
    currency_fiat: str
    credits_amount: Decimal
    currency_credits: str
    idempotency_key: str
    expires_at: datetime
    confirmed_at: Optional[datetime] = None
    # Commercial Checkout V1 — the plan this order activates on
    # confirmation, or None for a plain credit-purchase order.
    plan: Optional[str] = None
    # Payments Foundation — None for orders predating this column or
    # created without one supplied (fully valid).
    payment_method: Optional[str] = None
    payment_type: Optional[str] = None
    subscription_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PaymentOrderListResponse(BaseModel):
    """Paginated list of PaymentOrders for the authenticated user."""

    orders: list[PaymentOrderResponse]
    total: int


class PlanPriceResponse(BaseModel):
    """One entry in GET /billing/plans' price catalog."""

    plan: str
    amount: Decimal
    currency: str


class CheckoutResponse(BaseModel):
    """Response for POST /billing/checkout — the order plus, when a real
    provider was just contacted for the FIRST time, whatever it needs the
    user to actually go pay (a hosted-checkout redirect_url, or a widget
    client_secret). Both are None when no provider is registered (local-
    only order — see docs/CHECKOUT.md) OR when this call was itself an
    idempotent retry of an already-processing order (the ORIGINAL
    PaymentIntent from the provider's create-order call is a one-time
    value, never persisted or re-fetchable — see
    payment_order_service.create_provider_order()'s own docstring)."""

    order: PaymentOrderResponse
    redirect_url: Optional[str] = None
    client_secret: Optional[str] = None


class SubscriptionResponse(BaseModel):
    """Response representation of a single Subscription (Stripe
    Recurring etapa). Fields intentionally omitted: none — unlike
    PaymentOrder there is no webhook_raw-equivalent audit-only field on
    this model."""

    id: str
    user_id: str
    plan: str
    provider: str
    payment_method: str
    payment_type: str
    currency: str
    amount: Decimal
    status: str
    provider_subscription_id: Optional[str] = None
    provider_setup_intent_id: Optional[str] = None
    current_period_start: Optional[datetime] = None
    current_period_end: Optional[datetime] = None
    next_billing_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    consecutive_failed_cycles: int
    grace_period_started_at: Optional[datetime] = None
    grace_period_ends_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CreateSubscriptionResponse(BaseModel):
    """Response for POST /billing/subscriptions — the Subscription row
    plus the hosted Checkout Session URL the user must visit to
    complete setup (mode="subscription" — see StripeAdapter.
    create_subscription()'s own docstring). redirect_url is None only
    if this call was an idempotent retry of an already-processing
    creation (see route_subscription_creation()'s own docstring for
    when a SubscriptionIntent carries no redirect_url)."""

    subscription: SubscriptionResponse
    redirect_url: Optional[str] = None


class PaymentProvidersResponse(BaseModel):
    """GET /billing/providers — which payment providers are ACTUALLY
    usable right now (registered with real, non-empty config — see
    ProviderRegistry). An empty list is the honest, expected answer in
    any environment without real payment credentials configured; the
    frontend uses this to decide whether to show a real checkout button
    or fall back to the existing manual/email upgrade flow — see
    docs/CHECKOUT.md."""

    providers: list[str]


class CreditBalanceResponse(BaseModel):
    """Current credit balance for a single (user, currency) pair.

    balance is always non-negative (enforced at the DB and service layers).
    """

    user_id: str
    currency: str
    balance: Decimal


class UserCreditSummaryResponse(BaseModel):
    """All credit balances for the authenticated user, one entry per currency.

    Returns an empty list if the user has no credit balances yet.
    """

    user_id: str
    balances: list[CreditBalanceResponse]


# ---------------------------------------------------------------------------
# Webhook schemas (Billing-2)
# ---------------------------------------------------------------------------


class WebhookEventResponse(BaseModel):
    """Response returned to the webhook caller after processing.

    HTTP 200 is always returned to prevent provider retries on business-logic
    failures. This body provides machine-readable processing status for logging.

    Fields intentionally absent:
    * raw_payload — audit-only; never returned to the provider.
    * signing_secret / signature — never returned.
    """

    received: bool = True
    provider: str
    event_type: str
    order_id: Optional[str] = None
    # Stripe Recurring etapa — set instead of order_id when this
    # delivery was a subscription-lifecycle event (SubscriptionEventKind),
    # never both; a one-time payment event always leaves this None.
    subscription_id: Optional[str] = None
    new_status: Optional[str] = None
