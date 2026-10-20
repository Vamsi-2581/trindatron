"""Billing-2: payment_order_refunds table

Revision ID: 0013_payment_order_refunds
Revises: 0012_payment_orders
Create Date: 2026-08-27

Adds payment_order_refunds to support multiple partial refunds per order.

In Billing-1, partial_refund was a terminal state — each order allowed exactly
one refund (full or partial). Billing-2 makes partial_refund non-terminal by
tracking each individual refund event in a separate immutable table.

The service (PaymentOrderService.refund_order) enforces
  SUM(payment_order_refunds.amount) <= payment_orders.credits_amount
under a SELECT FOR UPDATE lock on the PaymentOrder row, preventing concurrent
over-refund in PostgreSQL. SQLite (used in dev/test) silently ignores the lock,
but the UNIQUE(idempotency_key) constraint still prevents accidental duplicate
rows with the same key.

Design constraints
------------------
* Numeric(28, 8) — no Float anywhere.
* amount > 0 enforced at the DB layer via CHECK.
* idempotency_key UNIQUE — duplicate refund calls with the same key are no-ops.
* order_id FK → payment_orders.id ON DELETE RESTRICT — refund records survive
  order deletion for financial audit and dispute resolution.
* user_id FK → users.id ON DELETE RESTRICT — refund records survive user deletion.
  Same policy as payment_orders (intentional divergence from credit_balances CASCADE).
* No updated_at — immutable audit record. Same policy as ledger_entries.

No existing tables are modified in this migration.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_payment_order_refunds"
down_revision = "0012_payment_orders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payment_order_refunds",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column(
            "order_id",
            sa.String(36),
            sa.ForeignKey("payment_orders.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(28, 8), nullable=False),
        sa.Column("currency", sa.String(10), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "amount > 0",
            name="ck_payment_order_refund_amount_positive",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_payment_order_refund_idempotency_key",
        ),
    )

    op.create_index(
        "ix_payment_order_refund_order_id",
        "payment_order_refunds",
        ["order_id"],
    )
    op.create_index(
        "ix_payment_order_refund_user_id",
        "payment_order_refunds",
        ["user_id"],
    )
    op.create_index(
        "ix_payment_order_refund_created_at",
        "payment_order_refunds",
        ["created_at"],
    )
    op.create_index(
        "ix_payment_order_refund_idempotency",
        "payment_order_refunds",
        ["idempotency_key"],
    )


def downgrade() -> None:
    # Drop indexes before the table (required for PostgreSQL).
    op.drop_index("ix_payment_order_refund_idempotency", table_name="payment_order_refunds")
    op.drop_index("ix_payment_order_refund_created_at", table_name="payment_order_refunds")
    op.drop_index("ix_payment_order_refund_user_id", table_name="payment_order_refunds")
    op.drop_index("ix_payment_order_refund_order_id", table_name="payment_order_refunds")
    op.drop_table("payment_order_refunds")
