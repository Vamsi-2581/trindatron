"""Stripe Recurring etapa — add webhook-ordering guard columns and
provider_setup_intent_id to subscriptions.

Revision ID: 0027_sub_webhook_ordering
Revises: 0026_subscription_engine
Create Date: 2026-09-08

Revision id shortened (2026-09-09 incident fix)
-------------------------------------------------
Originally "0027_subscription_webhook_ordering" (34 chars). Alembic's
default `alembic_version.version_num` column is VARCHAR(32) — this
revision id was 2 characters too long, so `alembic upgrade head`
crashed production on first real deploy with
`psycopg.errors.StringDataRightTruncation: value too long for type
character varying(32)` while trying to write it. The migration was
never durably applied anywhere (transaction rolled back atomically),
so renaming it here is a clean fix, not a data migration — see
0028's own docstring for the matching down_revision fix.

Background
----------
See src/models/subscription.py's own docstring and
src/services/subscription_service.py's module docstring for the full
rationale of each column:

- `last_webhook_event_id` / `last_webhook_event_at` — a provider's own
  event id + event timestamp (e.g. Stripe Event.id/Event.created) is
  tracked per Subscription so a delayed/out-of-order delivery can never
  overwrite a newer, already-applied state with a stale one. Distinct
  from the EXISTING webhook_nonces table (0024), which guards against a
  DUPLICATE delivery of the SAME event — this guards against a
  genuinely different, OLDER event arriving after a newer one.
- `provider_setup_intent_id` — the setup attempt's own id (e.g. a Stripe
  Checkout Session "cs_..."), set immediately once
  create_subscription() succeeds — mirrors PaymentOrder.
  external_order_id's exact idempotency role, DISTINCT from
  provider_subscription_id (which stays NULL until a webhook confirms
  the mandate is actually established). Closes a real "duas
  subscriptions externas para a mesma intenção" gap found while
  building route_subscription_creation() this etapa.

Purely additive — three nullable columns, NULL for every existing row
(no subscription has ever processed a webhook event or attempted
external creation before these columns existed). No execution/billing
side effect, upgrade or downgrade.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027_sub_webhook_ordering"
down_revision = "0026_subscription_engine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("subscriptions") as batch_op:
        batch_op.add_column(sa.Column("last_webhook_event_id", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("last_webhook_event_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("provider_setup_intent_id", sa.String(255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("subscriptions") as batch_op:
        batch_op.drop_column("provider_setup_intent_id")
        batch_op.drop_column("last_webhook_event_at")
        batch_op.drop_column("last_webhook_event_id")
