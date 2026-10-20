"""Payments Foundation — payment_method/payment_type/subscription_id on
payment_orders, and the new subscriptions table.

Revision ID: 0025_payment_domain_foundation
Revises: 0024_webhook_nonce
Create Date: 2026-09-07

Background
----------
See src/billing/providers/base.py (PaymentMethod/PaymentType enums,
PaymentProvider.supported_payment_methods/supported_payment_types) and
src/models/subscription.py for the full domain design. This migration
builds the canonical foundation for multi-method/multi-gateway global
payments (Stripe/Mercado Pago/etc., not yet implemented) WITHOUT
implementing any adapter, router, or automatic recurring charging.

Purely additive — no existing column, constraint, or row is altered:
- payment_orders gains THREE new nullable columns
  (payment_method, payment_type, subscription_id) plus two NULL-or-valid
  CHECK constraints and one new index. Every pre-existing row reads back
  unchanged (all three new columns NULL).
- subscriptions is a brand-new table; payment_orders.subscription_id is
  a nullable FK into it (ON DELETE SET NULL — a PaymentOrder must never
  depend on a Subscription's existence).

No execution/billing side effect — no data is created, credited, or
charged by this migration, upgrade or downgrade.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_payment_domain_foundation"
down_revision = "0024_webhook_nonce"
branch_labels = None
depends_on = None

_PAYMENT_METHODS = ("pix", "credit_card", "debit_card", "boleto", "bank_transfer", "wallet", "crypto")
_PAYMENT_TYPES = ("one_time", "recurring")
_SUBSCRIPTION_STATUSES = ("active", "past_due", "cancelled", "expired")


def _in_sql(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # subscriptions — new table (created BEFORE payment_orders gets its
    # FK to it, so the FK target exists when the batch ALTER runs).
    # ------------------------------------------------------------------
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("plan", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("payment_method", sa.String(20), nullable=False),
        sa.Column("payment_type", sa.String(20), nullable=False, server_default="recurring"),
        sa.Column("currency", sa.String(10), nullable=False),
        sa.Column("amount", sa.Numeric(28, 8), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("provider_subscription_id", sa.String(255), nullable=True),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_billing_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("amount > 0", name="ck_subscription_amount_positive"),
        sa.CheckConstraint(_in_sql("status", _SUBSCRIPTION_STATUSES), name="ck_subscription_status"),
        sa.CheckConstraint(_in_sql("payment_method", _PAYMENT_METHODS), name="ck_subscription_payment_method"),
        sa.CheckConstraint(_in_sql("payment_type", _PAYMENT_TYPES), name="ck_subscription_payment_type"),
        sa.UniqueConstraint("provider", "provider_subscription_id", name="uq_subscription_provider_ref"),
    )
    op.create_index("ix_subscription_user_id", "subscriptions", ["user_id"])
    op.create_index("ix_subscription_status", "subscriptions", ["status"])
    op.create_index(
        "ix_subscription_provider_subscription_id", "subscriptions", ["provider_subscription_id"]
    )

    # ------------------------------------------------------------------
    # payment_orders — additive columns + CHECK constraints + index
    #
    # batch_alter_table (not a plain op.add_column) — SQLite's dialect
    # cannot ALTER a table to add a new FK constraint any other way.
    # Batch mode requires every constraint in the rebuilt table to have
    # an explicit name — hence the named ForeignKeyConstraint below
    # instead of an inline sa.ForeignKey(...) on the column (same
    # convention as 0021_agent_instance.py).
    # ------------------------------------------------------------------
    with op.batch_alter_table("payment_orders") as batch_op:
        batch_op.add_column(sa.Column("payment_method", sa.String(20), nullable=True))
        batch_op.add_column(sa.Column("payment_type", sa.String(20), nullable=True))
        batch_op.add_column(sa.Column("subscription_id", sa.String(36), nullable=True))
        batch_op.create_foreign_key(
            "fk_payment_orders_subscription_id", "subscriptions",
            ["subscription_id"], ["id"], ondelete="SET NULL",
        )
        batch_op.create_check_constraint(
            "ck_payment_order_payment_method",
            f"payment_method IS NULL OR {_in_sql('payment_method', _PAYMENT_METHODS)}",
        )
        batch_op.create_check_constraint(
            "ck_payment_order_payment_type",
            f"payment_type IS NULL OR {_in_sql('payment_type', _PAYMENT_TYPES)}",
        )

    op.create_index("ix_payment_order_subscription_id", "payment_orders", ["subscription_id"])


def downgrade() -> None:
    op.drop_index("ix_payment_order_subscription_id", table_name="payment_orders")

    with op.batch_alter_table("payment_orders") as batch_op:
        batch_op.drop_constraint("ck_payment_order_payment_type", type_="check")
        batch_op.drop_constraint("ck_payment_order_payment_method", type_="check")
        batch_op.drop_constraint("fk_payment_orders_subscription_id", type_="foreignkey")
        batch_op.drop_column("subscription_id")
        batch_op.drop_column("payment_type")
        batch_op.drop_column("payment_method")

    op.drop_index("ix_subscription_provider_subscription_id", table_name="subscriptions")
    op.drop_index("ix_subscription_status", table_name="subscriptions")
    op.drop_index("ix_subscription_user_id", table_name="subscriptions")
    op.drop_table("subscriptions")
