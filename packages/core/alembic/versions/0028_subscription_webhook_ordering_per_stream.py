"""Stripe Recurring etapa hardening — split the webhook ordering guard
into per-stream columns (subscription lifecycle vs invoice/payment
lifecycle) instead of one global timestamp/id pair.

Revision ID: 0028_sub_webhook_ordering_stream
Revises: 0027_sub_webhook_ordering
Create Date: 2026-09-08

Revision id shortened (2026-09-09 incident fix)
-------------------------------------------------
Originally "0028_subscription_webhook_ordering_per_stream" (45 chars)
with down_revision "0027_subscription_webhook_ordering" (34 chars) —
both exceeded Alembic's default `alembic_version.version_num` VARCHAR(32)
column, the second one production would have hit right after 0027's own
id was fixed. Renamed to "0028_sub_webhook_ordering_stream" (32 chars,
exactly at the limit) with down_revision updated to match 0027's new id.
Never durably applied anywhere (see 0027's own docstring for the
incident), so this is a clean rename, not a data migration.

Background
----------
0027 introduced a SINGLE (last_webhook_event_id, last_webhook_event_at)
pair per Subscription row to guard against a stale/duplicate webhook
overwriting newer state. A post-commit functional audit found this too
coarse: Stripe delivers events for two semantically INDEPENDENT object
streams per subscription — the Subscription object itself
(SUBSCRIPTION_CREATED/UPDATED/CANCELED) and the Invoice object
(RENEWAL_SUCCEEDED/RENEWAL_FAILED) — with no guaranteed relative
ordering between the two streams' timestamps. A single shared
timestamp could silently absorb a genuine, never-before-seen
RENEWAL_SUCCEEDED event just because an unrelated, more-recently-
processed SUBSCRIPTION_UPDATED had a LATER timestamp — a real
money-tracking bug, reproduced with a throwaway script during the
audit (a real Stripe invoice payment would never be reflected in the
ledger/entitlement, with no error surfaced anywhere).

This migration replaces the single pair with two independent pairs,
one per stream, so ordering/duplicate protection is enforced WITHIN
each stream but can never block a legitimate event in the OTHER
stream. See src/models/subscription.py's own updated docstring and
src/services/subscription_service.py's _EVENT_STREAM mapping for the
consuming logic.

No production database has ever used the 0027 columns (this etapa was
never wired to a live webhook route before this same pass — see
subscription_service.py's own module docstring), so a straight
drop+add (not a rename/data-migration) is safe and simplest.

Purely additive/structural — four new nullable columns, two dropped.
No data migration needed (no row has ever had the old columns
populated outside test databases).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0028_sub_webhook_ordering_stream"
down_revision = "0027_sub_webhook_ordering"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("subscriptions") as batch_op:
        batch_op.drop_column("last_webhook_event_id")
        batch_op.drop_column("last_webhook_event_at")
        batch_op.add_column(sa.Column("last_subscription_event_id", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("last_subscription_event_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("last_invoice_event_id", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("last_invoice_event_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("subscriptions") as batch_op:
        batch_op.drop_column("last_invoice_event_at")
        batch_op.drop_column("last_invoice_event_id")
        batch_op.drop_column("last_subscription_event_at")
        batch_op.drop_column("last_subscription_event_id")
        batch_op.add_column(sa.Column("last_webhook_event_id", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("last_webhook_event_at", sa.DateTime(timezone=True), nullable=True))
