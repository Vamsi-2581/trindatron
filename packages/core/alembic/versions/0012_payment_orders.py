"""Billing-1: payment_orders table (PaymentOrder model)

Revision ID: 0012_payment_orders
Revises: 0011_ledger_hardening
Create Date: 2026-08-27

One new table:

  payment_orders  — records a user's intent to purchase credits via an external
                    payment gateway. Created when the user initiates a payment;
                    updated on each provider callback until a terminal status is reached.

Design constraints:
  * Numeric(28, 8) for all financial amounts — no Float.
  * status constrained to the canonical PaymentStatus values (8 values).
  * amount_fiat > 0 and credits_amount > 0 enforced at the DB layer.
  * idempotency_key UNIQUE — duplicate create calls return the existing order.
  * user_id FK uses ON DELETE RESTRICT (not CASCADE): payment records must survive
    user deletion for financial audit and dispute resolution. This is an intentional
    divergence from credit_balances/credit_transactions (which use CASCADE).
  * Four indexes support the primary query patterns:
      - ix_payment_order_user_id          → list orders by owner
      - ix_payment_order_status_expires   → expiry job: pending AND expires_at < NOW()
      - ix_payment_order_external_order_id → webhook receipt: lookup by provider ID
      - ix_payment_order_idempotency_key  → idempotency check before UNIQUE fires

No existing tables are modified in this migration.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_payment_orders"
down_revision = "0011_ledger_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payment_orders",
        # ------------------------------------------------------------------
        # Primary key
        # ------------------------------------------------------------------
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        # ------------------------------------------------------------------
        # Ownership — RESTRICT so orders survive user deletion
        # ------------------------------------------------------------------
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # ------------------------------------------------------------------
        # Provider identity
        # ------------------------------------------------------------------
        # Which payment provider handles this order (e.g. "binance_pay", "stripe").
        sa.Column("provider", sa.String(50), nullable=False),
        # Provider's own order/transaction ID — NULL until assigned after create_intent().
        sa.Column("external_order_id", sa.String(255), nullable=True),
        # ------------------------------------------------------------------
        # Lifecycle state
        # ------------------------------------------------------------------
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="pending",
        ),
        # ------------------------------------------------------------------
        # Financial amounts — Numeric(28, 8), never Float
        # ------------------------------------------------------------------
        # Gross amount the user pays at the provider (fiat or crypto).
        sa.Column("amount_fiat", sa.Numeric(28, 8), nullable=False),
        # Currency of the payment: ISO 4217 fiat or crypto symbol (USD, BRL, BTC, USDT).
        sa.Column("currency_fiat", sa.String(10), nullable=False),
        # Credits to apply when this order is confirmed.
        # Fixed at order creation — NEVER recalculated at confirmation time.
        sa.Column("credits_amount", sa.Numeric(28, 8), nullable=False),
        # Currency of the credits applied to the user's CreditBalance (USD, TCU).
        sa.Column("currency_credits", sa.String(10), nullable=False),
        # ------------------------------------------------------------------
        # Idempotency
        # ------------------------------------------------------------------
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        # ------------------------------------------------------------------
        # Timing
        # ------------------------------------------------------------------
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # Set when status becomes 'confirmed'; NULL in all other states.
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        # ------------------------------------------------------------------
        # Audit
        # ------------------------------------------------------------------
        # Raw provider webhook payload — stored for audit. No secrets.
        sa.Column("webhook_raw", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        # ------------------------------------------------------------------
        # Constraints
        # ------------------------------------------------------------------
        # Status must be one of the eight canonical PaymentStatus values.
        sa.CheckConstraint(
            "status IN ("
            "'pending', 'processing', 'confirmed', 'failed', "
            "'cancelled', 'refunded', 'partial_refund', 'expired'"
            ")",
            name="ck_payment_order_status",
        ),
        # Both monetary amounts must be strictly positive.
        sa.CheckConstraint(
            "amount_fiat > 0",
            name="ck_payment_order_amount_fiat_positive",
        ),
        sa.CheckConstraint(
            "credits_amount > 0",
            name="ck_payment_order_credits_amount_positive",
        ),
        # Idempotency — one key per logical order.
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_payment_order_idempotency_key",
        ),
    )

    # Indexes created separately so they appear in op.create_index() calls
    # and can be dropped cleanly in downgrade().
    op.create_index(
        "ix_payment_order_user_id",
        "payment_orders",
        ["user_id"],
    )
    op.create_index(
        "ix_payment_order_status_expires",
        "payment_orders",
        ["status", "expires_at"],
    )
    op.create_index(
        "ix_payment_order_external_order_id",
        "payment_orders",
        ["external_order_id"],
    )
    op.create_index(
        "ix_payment_order_idempotency_key",
        "payment_orders",
        ["idempotency_key"],
    )


def downgrade() -> None:
    # Drop indexes before the table (required for PostgreSQL).
    op.drop_index("ix_payment_order_idempotency_key", table_name="payment_orders")
    op.drop_index("ix_payment_order_external_order_id", table_name="payment_orders")
    op.drop_index("ix_payment_order_status_expires", table_name="payment_orders")
    op.drop_index("ix_payment_order_user_id", table_name="payment_orders")
    op.drop_table("payment_orders")
