"""Per-person preferences, and what a provider really billed.

Additive only:
  * user_preferences — one row per person, a JSON document of validated
    settings (src/services/preferences_service.py). A new setting is a new key,
    not a migration.
  * usage_records.provider_cost_usd — what the provider actually charged for a
    call, when that differs from the list price that sizes TCU (a free provider
    tier bills nothing). NULL means "the same as cost_usd", so every existing
    row keeps its meaning.

Revision ID: 0040_user_preferences
Revises: 0039_message_media_job
"""
from alembic import op
import sqlalchemy as sa


revision = "0040_user_preferences"
down_revision = "0039_message_media_job"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_preferences",
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("usage_records", sa.Column("provider_cost_usd", sa.Numeric(18, 8), nullable=True))


def downgrade() -> None:
    op.drop_column("usage_records", "provider_cost_usd")
    op.drop_table("user_preferences")
