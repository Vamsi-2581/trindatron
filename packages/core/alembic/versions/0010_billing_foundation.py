"""Billing-0 financial foundation: ledger_entries, credit_balances, credit_transactions

Revision ID: 0010_billing_foundation
Revises: 0009_add_memories
Create Date: 2026-08-27

Three new tables for the financial layer:

  ledger_entries      — immutable append-only journal (double-entry accounting)
  credit_balances     — materialized balance per (user, currency) with optimistic lock
  credit_transactions — every balance change with idempotency_key UNIQUE

Design constraints preserved:
  * Payment ≠ Credit ≠ TCU ≠ AI cost — separate concepts
  * Currency-agnostic: USD, EUR, BRL, BTC, USDT, USDC, TCU
  * Numeric(28, 8) everywhere financial — no Float
  * LedgerEntry has NO updated_at (immutable by design)
  * balance CHECK >= 0 and amount CHECK > 0 in the DB
  * idempotency_key UNIQUE in credit_transactions
  * All FK references to users(id) ON DELETE CASCADE

No existing tables are modified in this migration.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_billing_foundation"
down_revision = "0009_add_memories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. ledger_entries — append-only financial journal
    # ------------------------------------------------------------------
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("entry_type", sa.String(30), nullable=False),
        sa.Column("account_from", sa.String(100), nullable=False),
        sa.Column("account_to", sa.String(100), nullable=False),
        sa.Column("amount", sa.Numeric(28, 8), nullable=False),
        sa.Column("currency", sa.String(10), nullable=False),
        sa.Column("reference_id", sa.String(36), nullable=True),
        sa.Column("reference_type", sa.String(50), nullable=True),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("amount > 0", name="ck_ledger_amount_positive"),
        sa.CheckConstraint(
            "entry_type IN ('revenue','fee','ai_cost','credit_purchase',"
            "'credit_consumption','refund','reserve','tax','profit','adjustment')",
            name="ck_ledger_entry_type",
        ),
    )
    op.create_index("ix_ledger_reference", "ledger_entries", ["reference_id", "reference_type"])
    op.create_index("ix_ledger_created_at", "ledger_entries", ["created_at"])
    op.create_index("ix_ledger_account_from", "ledger_entries", ["account_from"])
    op.create_index("ix_ledger_account_to", "ledger_entries", ["account_to"])

    # ------------------------------------------------------------------
    # 2. credit_balances — materialized per-user balance with optimistic lock
    # ------------------------------------------------------------------
    op.create_table(
        "credit_balances",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("currency", sa.String(10), nullable=False),
        sa.Column("balance", sa.Numeric(28, 8), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer, nullable=False, server_default="0"),
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
        sa.UniqueConstraint("user_id", "currency", name="uq_credit_balance_user_currency"),
        sa.CheckConstraint("balance >= 0", name="ck_credit_balance_non_negative"),
    )
    op.create_index("ix_credit_balance_user_id", "credit_balances", ["user_id"])

    # ------------------------------------------------------------------
    # 3. credit_transactions — every debit/credit with idempotency guarantee
    # ------------------------------------------------------------------
    op.create_table(
        "credit_transactions",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "credit_balance_id",
            sa.String(36),
            sa.ForeignKey("credit_balances.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("transaction_type", sa.String(20), nullable=False),
        sa.Column("amount", sa.Numeric(28, 8), nullable=False),
        sa.Column("currency", sa.String(10), nullable=False),
        sa.Column("balance_before", sa.Numeric(28, 8), nullable=False),
        sa.Column("balance_after", sa.Numeric(28, 8), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("reference_id", sa.String(36), nullable=True),
        sa.Column("reference_type", sa.String(50), nullable=True),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_credit_tx_idempotency_key"),
        sa.CheckConstraint("amount > 0", name="ck_credit_tx_amount_positive"),
        sa.CheckConstraint(
            "transaction_type IN ('purchase','consume','refund','expiry','grant','adjustment')",
            name="ck_credit_tx_type",
        ),
        sa.CheckConstraint(
            "balance_after >= 0",
            name="ck_credit_tx_balance_after_non_negative",
        ),
    )
    op.create_index("ix_credit_tx_user_id", "credit_transactions", ["user_id"])
    op.create_index("ix_credit_tx_created_at", "credit_transactions", ["created_at"])
    op.create_index("ix_credit_tx_idempotency", "credit_transactions", ["idempotency_key"])
    op.create_index("ix_credit_tx_reference", "credit_transactions", ["reference_id", "reference_type"])


def downgrade() -> None:
    op.drop_table("credit_transactions")
    op.drop_table("credit_balances")
    op.drop_table("ledger_entries")
