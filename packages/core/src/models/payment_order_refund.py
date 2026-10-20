"""
PaymentOrderRefund — immutable record of each refund applied to a PaymentOrder.

Design
------
Each row represents one discrete refund event. Multiple rows can exist per order,
enabling multiple partial refunds (Billing-2). The cumulative SUM(amount) for an
order must never exceed PaymentOrder.credits_amount — enforced at the service layer
under SELECT FOR UPDATE.

Immutability
------------
No updated_at — this is an append-only audit record. Once written, the row is
never modified. Corrections are new compensating entries, not updates.
Same policy as LedgerEntry.

Financial audit
---------------
Both FKs (order_id, user_id) use ON DELETE RESTRICT so refund records survive
even if the referenced PaymentOrder or User is deleted. This matches the audit
policy established in PaymentOrder (also RESTRICT on user_id).
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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PaymentOrderRefund(Base):
    """Immutable record of a single refund applied to a PaymentOrder.

    One PaymentOrder can have multiple PaymentOrderRefund rows (Billing-2).
    The service enforces SUM(amount) <= PaymentOrder.credits_amount under a
    SELECT FOR UPDATE lock to prevent concurrent over-refund.
    """

    __tablename__ = "payment_order_refunds"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # RESTRICT — refund records must survive PaymentOrder deletion (financial audit).
    order_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("payment_orders.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # RESTRICT — refund records must survive user deletion (financial audit).
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Credits removed from the user's balance in this refund event.
    amount: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)

    # Currency of the credits — always equals PaymentOrder.currency_credits.
    currency: Mapped[str] = mapped_column(String(10), nullable=False)

    # Caller-provided idempotency key — UNIQUE.
    # Duplicate refund attempts with the same key return the existing order.
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)

    # Optional audit note — stored on the CreditTransaction as well.
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Immutable creation timestamp — no updated_at by design.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    # Read-only relationships — no cascade; audit records are permanent.
    order: Mapped["PaymentOrder"] = relationship("PaymentOrder")  # type: ignore[name-defined]
    user: Mapped["User"] = relationship("User")  # type: ignore[name-defined]

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payment_order_refund_amount_positive"),
        UniqueConstraint("idempotency_key", name="uq_payment_order_refund_idempotency_key"),
        Index("ix_payment_order_refund_order_id", "order_id"),
        Index("ix_payment_order_refund_user_id", "user_id"),
        Index("ix_payment_order_refund_created_at", "created_at"),
        Index("ix_payment_order_refund_idempotency", "idempotency_key"),
    )
