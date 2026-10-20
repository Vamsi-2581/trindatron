"""
Subscription — a recurring billing agreement for a paid plan.

Payments Foundation etapa (2026-09-07): the canonical domain object that
will let a future Fiat adapter (Mercado Pago, then Stripe/Adyen) drive
recurring charges without inventing a new concept per provider. This
model is deliberately inert today — nothing creates, renews, or cancels
a Subscription automatically yet (no adapter, no PaymentRouter, no
scheduler touches this table). It exists so the schema is ready for the
first Fiat adapter without another migration.

Relationship to PaymentOrder
-----------------------------
A Subscription is the recurring AGREEMENT; each individual charge in
that agreement is still a PaymentOrder (one row per billing cycle),
optionally linked back via PaymentOrder.subscription_id. This keeps
PaymentOrder's existing invariants (one order = one transaction,
confirm_order()'s atomic credit/ledger/plan-activation logic) completely
unchanged — a Subscription never replaces a PaymentOrder, it only groups
a sequence of them.

PaymentOrder has ZERO required dependency on Subscription:
subscription_id is nullable, and every existing one-time or crypto
purchase flow keeps creating PaymentOrder rows with
subscription_id=NULL, exactly as before this phase.

States
------
Deliberately limited to four (no TRIALING/PAUSED yet — out of scope for
this phase, add only with a real product decision):
  active     — currently entitled; provider expected to bill on schedule
  past_due   — a scheduled charge failed; grace period (no automatic
               entitlement change happens here — that policy is a future
               decision, not implemented by this model)
  cancelled  — user or provider cancelled; will not renew
  expired    — the current period ended without renewal
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.billing.providers.base import PaymentMethod, PaymentType
from src.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SubscriptionStatus(str, Enum):
    """Canonical Subscription lifecycle states — see module docstring.

    Subscription Engine etapa (2026-09-08) added GRACE_PERIOD as a fifth,
    distinct state from PAST_DUE: PAST_DUE is the FIRST failure signal
    for a billing cycle (still eligible for an ordinary retry, no
    automatic entitlement change); GRACE_PERIOD is a subsequent,
    last-chance window entered once the configurable past-due threshold
    is exceeded (see settings.subscription_max_past_due_cycles), after
    which EXPIRED follows if the cycle is still unpaid when
    grace_period_ends_at passes. See SubscriptionService's own module
    docstring for the full transition table and every trigger.
    """

    ACTIVE = "active"
    PAST_DUE = "past_due"
    GRACE_PERIOD = "grace_period"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


_STATUS_VALUES: tuple[str, ...] = tuple(s.value for s in SubscriptionStatus)
_STATUS_SQL: str = ", ".join(f"'{v}'" for v in _STATUS_VALUES)

_PAYMENT_METHOD_VALUES: tuple[str, ...] = tuple(m.value for m in PaymentMethod)
_PAYMENT_METHOD_SQL: str = ", ".join(f"'{v}'" for v in _PAYMENT_METHOD_VALUES)

_PAYMENT_TYPE_VALUES: tuple[str, ...] = tuple(t.value for t in PaymentType)
_PAYMENT_TYPE_SQL: str = ", ".join(f"'{v}'" for v in _PAYMENT_TYPE_VALUES)


class Subscription(Base):
    """A recurring billing agreement for one user's paid plan.

    One row per active-or-formerly-active subscription agreement — NOT
    one row per charge (see module docstring for the PaymentOrder split).
    """

    __tablename__ = "subscriptions"

    VALID_STATUSES: frozenset[str] = frozenset(_STATUS_VALUES)

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # RESTRICT — matches PaymentOrder's own choice: a financial agreement
    # record must survive user deletion for audit/dispute resolution.
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Not a FK — validated against src/billing/pricing.py in Python, same
    # convention as PaymentOrder.plan.
    plan: Mapped[str] = mapped_column(String(20), nullable=False)

    # Payment provider identifier, e.g. "mercado_pago" (no adapter
    # implements this yet — see module docstring).
    provider: Mapped[str] = mapped_column(String(50), nullable=False)

    payment_method: Mapped[str] = mapped_column(String(20), nullable=False)

    # Always "recurring" in practice (a Subscription IS the recurring
    # schedule) — kept as an explicit column rather than a hardcoded
    # constant so a future one-off "recurring via manual re-invoice"
    # rail (e.g. today's NOWPayments reality) can still be modeled
    # honestly if ever attached to a Subscription.
    payment_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PaymentType.RECURRING.value
    )

    currency: Mapped[str] = mapped_column(String(10), nullable=False)

    # Amount charged per billing cycle. Numeric(28, 8) — same precision
    # convention as every other monetary column in this codebase.
    amount: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=SubscriptionStatus.ACTIVE.value
    )

    # Provider's own subscription/mandate identifier — e.g. Stripe's
    # real "sub_..." object. NULL until a webhook CONFIRMS the mandate
    # was actually established (see SubscriptionService.
    # confirm_external_subscription()) — never set merely because a
    # setup attempt was made.
    provider_subscription_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )

    # Stripe Recurring etapa — the SETUP intent's own id (e.g. a Checkout
    # Session "cs_..."), set IMMEDIATELY once create_subscription()
    # succeeds — mirrors PaymentOrder.external_order_id's exact role:
    # a retry of the SAME creation intent must see this already set and
    # skip calling the provider again, even though
    # provider_subscription_id itself is still NULL (the mandate isn't
    # confirmed yet — only attempted). Without this field, a retry before
    # the webhook confirms completion would call create_subscription()
    # a SECOND time — the exact "duas subscriptions externas para a
    # mesma intenção" bug this field exists to prevent.
    provider_setup_intent_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )

    # Current billing-cycle window and next scheduled charge. All NULL
    # until a real adapter populates them — no scheduler reads these yet.
    current_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_billing_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ------------------------------------------------------------------
    # Subscription Engine etapa — dunning/grace-period tracking
    # ------------------------------------------------------------------

    # How many CONSECUTIVE renewal cycles have ended in explicit
    # rejection (never incremented for an ambiguous/ProviderUnavailable
    # outcome — see SubscriptionService's own docstring for why an
    # unresolved outcome must never be treated as a failure). Reset to 0
    # on every successful renewal confirmation.
    consecutive_failed_cycles: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )

    # Set when status transitions PAST_DUE -> GRACE_PERIOD; NULL otherwise.
    grace_period_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Computed once, at grace-period-entry time, from the configurable
    # settings.subscription_grace_period_days — stored (not recomputed)
    # so a later change to that setting never retroactively shifts an
    # already-running grace period's own deadline.
    grace_period_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Final-review hardening: an optional, caller-provided idempotency
    # key for SUBSCRIPTION CREATION itself (distinct from a renewal
    # cycle's own deterministic key — see SubscriptionService's own
    # module docstring). NULL for a subscription created without one
    # (every test/caller before this addition) — NULLs are distinct in
    # the UNIQUE constraint below, same convention as
    # provider_subscription_id. Prevents a double-click/retried "create
    # subscription" request from ever producing two rows for the same
    # logical intent, mirroring PaymentOrder.idempotency_key exactly.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Stripe Recurring etapa — webhook ORDERING guard, PER STREAM
    # (distinct from webhook_nonces' own DUPLICATE-delivery guard).
    # A provider's own event id + event timestamp (e.g. Stripe
    # Event.id/Event.created) — an incoming event whose occurred_at is
    # OLDER than the LAST APPLIED EVENT OF THE SAME STREAM is rejected/
    # absorbed, so a delayed/out-of-order delivery can never overwrite a
    # newer state with a stale one. NULL until the first event of that
    # stream is ever applied.
    #
    # Post-commit hardening (2026-09-08, functional audit finding):
    # originally a SINGLE (last_webhook_event_id, last_webhook_event_at)
    # pair covered every event kind. That is too coarse — Stripe
    # delivers events for two INDEPENDENT object streams per
    # subscription (the Subscription object itself, and the Invoice
    # object), with no guaranteed relative ordering of timestamps
    # BETWEEN the two streams. A single shared timestamp let a
    # more-recently-processed SUBSCRIPTION_UPDATED silently absorb a
    # genuine, never-before-seen RENEWAL_SUCCEEDED merely because the
    # invoice event's OWN timestamp happened to be earlier — a real
    # money-tracking bug (reproduced during the audit; see migration
    # 0028's own docstring). Splitting into two independent pairs, one
    # per stream, fixes the granularity without removing the guard:
    # each stream is still protected against ITS OWN stale/duplicate
    # deliveries, but the two streams can never block each other.
    #
    # last_subscription_event_id/_at — guards SUBSCRIPTION_CREATED,
    # SUBSCRIPTION_UPDATED, SUBSCRIPTION_CANCELED (the Subscription
    # object's own event stream).
    last_subscription_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_subscription_event_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # last_invoice_event_id/_at — guards RENEWAL_SUCCEEDED,
    # RENEWAL_FAILED (the Invoice object's own event stream).
    last_invoice_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_invoice_event_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    user: Mapped["User"] = relationship("User", back_populates="subscriptions")
    payment_orders: Mapped[list["PaymentOrder"]] = relationship(
        "PaymentOrder", back_populates="subscription"
    )

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_subscription_amount_positive"),
        CheckConstraint(
            f"status IN ({_STATUS_SQL})",
            name="ck_subscription_status",
        ),
        CheckConstraint(
            f"payment_method IN ({_PAYMENT_METHOD_SQL})",
            name="ck_subscription_payment_method",
        ),
        CheckConstraint(
            f"payment_type IN ({_PAYMENT_TYPE_SQL})",
            name="ck_subscription_payment_type",
        ),
        # NULLs are distinct in Postgres/SQLite UNIQUE constraints, so
        # multiple subscriptions with no provider_subscription_id yet
        # never collide with each other.
        UniqueConstraint(
            "provider", "provider_subscription_id",
            name="uq_subscription_provider_ref",
        ),
        UniqueConstraint("idempotency_key", name="uq_subscription_idempotency_key"),
        Index("ix_subscription_user_id", "user_id"),
        Index("ix_subscription_status", "status"),
        Index("ix_subscription_provider_subscription_id", "provider_subscription_id"),
        Index("ix_subscription_idempotency_key", "idempotency_key"),
    )
