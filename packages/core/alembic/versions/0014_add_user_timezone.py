"""Add users.timezone for the timezone-aware weekly reset rule.

Revision ID: 0014_add_user_timezone
Revises: 0013_payment_order_refunds
Create Date: 2026-08-31

Background
----------
The weekly TCU quota used to reset Monday 00:00 UTC for everyone. The new
global rule resets every SUNDAY AT 12:00 in the user's LOCAL IANA timezone
(see src/timezones.py). That requires knowing each user's IANA timezone.

Column
------
users.timezone VARCHAR(64) NULLABLE, no default.

Backfill strategy for existing rows
------------------------------------
Existing rows get NULL, not 'UTC'. This is deliberate: UTC is a
computational fallback (src/timezones.FALLBACK_TIMEZONE) used only inside
week_window() when a timezone is genuinely unknown — it must never be
written into the database as if it were a real, detected timezone, or a
future audit could not distinguish "we detected UTC" from "we never asked".
NULL is the honest signal for "not yet detected". The frontend detects and
persists the real value automatically on the next authenticated page load
(Intl.DateTimeFormat().resolvedOptions().timeZone -> PATCH
/users/me/timezone), so this is self-healing for active users without a
data migration script.
"""
from alembic import op
import sqlalchemy as sa


revision = '0014_add_user_timezone'
down_revision = '0013_payment_order_refunds'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users') as batch_op:
        batch_op.add_column(
            sa.Column('timezone', sa.String(64), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_column('timezone')
