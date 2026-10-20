"""Subscription Engine — add 'grace_period' to subscriptions.status and
dunning/grace-period tracking columns.

Revision ID: 0026_subscription_engine
Revises: 0025_payment_domain_foundation
Create Date: 2026-09-08

Background
----------
See src/models/subscription.py's own docstring (SubscriptionStatus,
Subscription) and src/services/subscription_service.py's module
docstring for the full lifecycle/transition design this etapa builds.
Purely additive/widening — no existing column, constraint, or row is
altered in a breaking way:

- `subscriptions.status`'s CHECK constraint is dropped and re-created
  with ONE new allowed value ('grace_period') appended — every value it
  already allowed is UNCHANGED, so no existing row can ever violate the
  new constraint.
- `subscriptions.consecutive_failed_cycles` (integer, NOT NULL,
  server_default 0) — every existing row backfills to 0 (no failures
  recorded before this column existed, which is the correct, honest
  default).
- `subscriptions.grace_period_started_at` / `grace_period_ends_at`
  (nullable timestamps) — NULL for every existing row (no subscription
  has ever entered a grace period before this column existed).
- `subscriptions.idempotency_key` (nullable, UNIQUE) — final-review
  hardening: prevents a double-click/retried "create subscription"
  request from ever producing two rows for the same logical intent,
  mirroring PaymentOrder.idempotency_key. NULL for every existing row
  (NULLs are distinct in a UNIQUE constraint, so they never collide).

No execution/billing side effect — no data is created, credited, or
charged by this migration, upgrade or downgrade. The Subscription table
itself remains otherwise inert (see its own module docstring) — this
migration only prepares the schema.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026_subscription_engine"
down_revision = "0025_payment_domain_foundation"
branch_labels = None
depends_on = None

_OLD_STATUSES = ("active", "past_due", "cancelled", "expired")
_NEW_STATUSES = ("active", "past_due", "grace_period", "cancelled", "expired")


def _check_sql(statuses: tuple[str, ...]) -> str:
    return "status IN (" + ", ".join(f"'{s}'" for s in statuses) + ")"


def upgrade() -> None:
    with op.batch_alter_table("subscriptions") as batch_op:
        batch_op.add_column(
            sa.Column("consecutive_failed_cycles", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("grace_period_started_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("grace_period_ends_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("idempotency_key", sa.String(128), nullable=True))
        batch_op.drop_constraint("ck_subscription_status", type_="check")
        batch_op.create_check_constraint("ck_subscription_status", _check_sql(_NEW_STATUSES))
        batch_op.create_unique_constraint("uq_subscription_idempotency_key", ["idempotency_key"])

    op.create_index("ix_subscription_idempotency_key", "subscriptions", ["idempotency_key"])


def downgrade() -> None:
    op.drop_index("ix_subscription_idempotency_key", table_name="subscriptions")

    with op.batch_alter_table("subscriptions") as batch_op:
        batch_op.drop_constraint("uq_subscription_idempotency_key", type_="unique")
        batch_op.drop_constraint("ck_subscription_status", type_="check")
        batch_op.create_check_constraint("ck_subscription_status", _check_sql(_OLD_STATUSES))
        batch_op.drop_column("idempotency_key")
        batch_op.drop_column("grace_period_ends_at")
        batch_op.drop_column("grace_period_started_at")
        batch_op.drop_column("consecutive_failed_cycles")
