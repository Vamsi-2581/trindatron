"""
PaymentOrder — a user's intent to purchase credits via an external payment provider.

Lifecycle
---------
  pending       → order created; awaiting user action at the provider
  processing    → provider processing (e.g. awaiting blockchain confirmations)
  confirmed     → payment received and verified; credits applied to the user's balance
  failed        → payment attempt failed at the provider
  cancelled     → cancelled before completion
  expired       → order timed out without payment
  refunded      → fully refunded; credits reversed via BillingOrchestrator.refund_and_record()
  partial_refund → partially refunded

Key invariants
--------------
1. credits_amount is fixed at order creation — it is NEVER recalculated at confirmation
   to avoid fx-rate races between order creation and payment confirmation.

2. Credits are applied ONLY when status transitions to 'confirmed', atomically via
   BillingOrchestrator.credit_and_record(). No credits exist without a LedgerEntry pair.

3. idempotency_key is caller-provided and UNIQUE per order. A duplicate create call with
   the same key returns the existing order without creating a new one.

4. confirmed_at is set ONLY when status = 'confirmed'; NULL in all other states.

5. external_order_id is NULL until the provider assigns its own ID (on create_intent()).
   The index on external_order_id enables O(log n) webhook lookup by provider order ID.

6. webhook_raw stores the parsed provider payload for audit. It MUST NOT contain
   decrypted secrets, API keys, or HMAC signing keys — only the normalized event data.

7. user_id FK uses RESTRICT (not CASCADE) by design: payment records must survive
   even if the user account is deleted, for financial audit and dispute resolution.
   This is an intentional divergence from the CreditBalance/CreditTransaction pattern
   (which use CASCADE) because those are operational records; PaymentOrder is a
   financial receipt.

Commercial Checkout V1 — plan-purchase orders
--------------------------------------------------
`plan` (nullable) marks this order as a PLAN purchase (e.g. "start",
"pro") rather than a plain credit top-up. When set, confirm_order()
does NOT credit CreditBalance for this order — see
PaymentOrderService.confirm_order()'s own docstring for why (crediting a
spendable balance for a plan purchase would be exactly the "saldo
paralelo que pode divergir do entitlement" this architecture avoids).
Instead, confirmation records the revenue directly in the ledger
(entry_type="plan_purchase") and sets `user.plan = order.plan`
atomically, in the same commit. A plan-purchase order cannot be refunded
through refund_order() (see that method's guard) — refunding a plan
activation would require deciding what plan to revert to, a product
decision explicitly out of scope for this phase; see
docs/CHECKOUT.md.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.billing.providers.base import PaymentMethod, PaymentStatus, PaymentType
from src.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Internal constants — derived from PaymentStatus (single source of truth)
# ---------------------------------------------------------------------------

# All valid status values as strings — used for CHECK constraint and validation.
_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in PaymentStatus)
_STATUS_SQL: str = ", ".join(f"'{v}'" for v in _STATUS_VALUES)

_PAYMENT_METHOD_VALUES: tuple[str, ...] = tuple(m.value for m in PaymentMethod)
_PAYMENT_METHOD_SQL: str = ", ".join(f"'{v}'" for v in _PAYMENT_METHOD_VALUES)

_PAYMENT_TYPE_VALUES: tuple[str, ...] = tuple(t.value for t in PaymentType)
_PAYMENT_TYPE_SQL: str = ", ".join(f"'{v}'" for v in _PAYMENT_TYPE_VALUES)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class PaymentOrder(Base):
    """Represents a user's intent to purchase credits via an external payment provider.

    One PaymentOrder corresponds to one provider-side payment intent.
    Credits are applied atomically (with LedgerEntry pair) only on confirmation.
    """

    __tablename__ = "payment_orders"

    # Expose valid status values for service-layer validation — avoids importing
    # PaymentStatus (an ABC concern) directly into every service that validates state.
    VALID_STATUSES: frozenset[str] = frozenset(_STATUS_VALUES)

    # ------------------------------------------------------------------
    # Primary key
    # ------------------------------------------------------------------

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # ------------------------------------------------------------------
    # Ownership
    # ------------------------------------------------------------------

    # RESTRICT — payment records survive user deletion (financial audit safety).
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # ------------------------------------------------------------------
    # Provider identity
    # ------------------------------------------------------------------

    # Identifies which payment provider handles this order.
    # Examples: "binance_pay", "stripe", "btc_onchain", "usdt_trc20"
    provider: Mapped[str] = mapped_column(String(50), nullable=False)

    # Provider's own order/transaction identifier — NULL until the provider assigns it.
    # Used to look up the order on webhook receipt.
    external_order_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ------------------------------------------------------------------
    # Lifecycle state
    # ------------------------------------------------------------------

    # Current order state — constrained to PaymentStatus values via CHECK.
    # Default 'pending' — set on creation before the provider is contacted.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PaymentStatus.PENDING.value
    )

    # ------------------------------------------------------------------
    # Financial amounts — Numeric(28, 8), never float
    # ------------------------------------------------------------------

    # Gross amount the user pays at the provider.
    # Examples: 10.00 USD, 0.00050000 BTC, 10.000000 USDT
    amount_fiat: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)

    # Currency of the payment (ISO 4217 for fiat, symbol for crypto).
    # Examples: "USD", "BRL", "BTC", "USDT", "USDC"
    currency_fiat: Mapped[str] = mapped_column(String(10), nullable=False)

    # Credits to apply when this order is confirmed.
    # Fixed at order creation — NEVER recalculated at confirmation time.
    credits_amount: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)

    # Currency of the credit applied to the user's CreditBalance.
    # Examples: "USD", "TCU"
    currency_credits: Mapped[str] = mapped_column(String(10), nullable=False)

    # ------------------------------------------------------------------
    # Commercial Checkout V1 — plan-purchase orders (see module docstring)
    # ------------------------------------------------------------------

    # NULL = plain credit-purchase order (every order before this phase,
    # and any future non-plan order) — unaffected. Non-NULL = the plan
    # this order activates on confirmation, e.g. "start"/"pro"/"max"/"team".
    # Not a foreign key — validated against src/billing/pricing.py in
    # Python (same convention as AgentSchedule.priority's small
    # controlled string vocabulary).
    plan: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # TEAM by seat: the team whose seats this order pays for (plan =
    # "team_seats"). Not a foreign key, like `plan`; the team service checks
    # it against the team's own pending checkout before activating anything.
    team_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)

    # ------------------------------------------------------------------
    # Payment domain foundation — method / type / subscription linkage
    # ------------------------------------------------------------------

    # NULL = order predates this column, or the caller didn't supply one
    # (both fully valid — every pre-existing order and every generic
    # POST /billing/orders call is unaffected). Non-NULL = a
    # PaymentMethod value (e.g. "pix", "crypto") — independent of
    # `provider`; see PaymentMethod's own docstring for why the two must
    # never be conflated.
    payment_method: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # NULL = unspecified (pre-existing orders). Non-NULL = a PaymentType
    # value ("one_time" / "recurring"). A plan-purchase order (`plan`
    # set) is normally "one_time" even though it activates a recurring
    # plan — the recurring CHARGE SCHEDULE lives on `Subscription`, not
    # on any single PaymentOrder.
    payment_type: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # NULL = this order is not tied to a Subscription's billing cycle —
    # true for every order today (no adapter creates Subscriptions yet)
    # and for every one-time/crypto purchase going forward. Deliberately
    # nullable and SET NULL on delete: PaymentOrder must keep working
    # (and keep existing) with zero dependency on Subscription — see
    # Subscription's own module docstring.
    subscription_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("subscriptions.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ------------------------------------------------------------------
    # Idempotency
    # ------------------------------------------------------------------

    # Caller-provided idempotency key — UNIQUE.
    # Duplicate create calls with the same key return the existing order.
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)

    # ------------------------------------------------------------------
    # Timing
    # ------------------------------------------------------------------

    # When this order expires. After this point, status transitions to 'expired'
    # and the order can no longer be confirmed.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Set when status becomes 'confirmed'; NULL in all other states.
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    # Raw webhook/callback payload from the provider — stored for audit.
    # MUST NOT contain decrypted secrets, signing keys, or API credentials.
    webhook_raw: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Immutable creation timestamp.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    # Updated on every status transition.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    # ------------------------------------------------------------------
    # Relationships
    # ------------------------------------------------------------------

    # back_populates will be added to User in the step that modifies user.py.
    user: Mapped["User"] = relationship("User")

    subscription: Mapped["Subscription | None"] = relationship(
        "Subscription", back_populates="payment_orders"
    )

    # ------------------------------------------------------------------
    # Constraints and indexes
    # ------------------------------------------------------------------

    __table_args__ = (
        # Status must be one of the canonical PaymentStatus values.
        CheckConstraint(
            f"status IN ({_STATUS_SQL})",
            name="ck_payment_order_status",
        ),
        # Both monetary amounts must be strictly positive.
        CheckConstraint(
            "amount_fiat > 0",
            name="ck_payment_order_amount_fiat_positive",
        ),
        CheckConstraint(
            "credits_amount > 0",
            name="ck_payment_order_credits_amount_positive",
        ),
        # NULL-or-valid — both new columns are optional (backward compat).
        CheckConstraint(
            f"payment_method IS NULL OR payment_method IN ({_PAYMENT_METHOD_SQL})",
            name="ck_payment_order_payment_method",
        ),
        CheckConstraint(
            f"payment_type IS NULL OR payment_type IN ({_PAYMENT_TYPE_SQL})",
            name="ck_payment_order_payment_type",
        ),
        # Idempotency — one key per logical order.
        UniqueConstraint("idempotency_key", name="uq_payment_order_idempotency_key"),
        # Lookup by owner — most common query pattern.
        Index("ix_payment_order_user_id", "user_id"),
        # Expiry job: find orders WHERE status='pending' AND expires_at < NOW().
        Index("ix_payment_order_status_expires", "status", "expires_at"),
        # Webhook receipt: find order by provider's own ID.
        Index("ix_payment_order_external_order_id", "external_order_id"),
        # Supports idempotency lookup before the UNIQUE constraint fires.
        Index("ix_payment_order_idempotency_key", "idempotency_key"),
        # Subscription-driven renewal lookups (e.g. "orders for this subscription").
        Index("ix_payment_order_subscription_id", "subscription_id"),
    )
