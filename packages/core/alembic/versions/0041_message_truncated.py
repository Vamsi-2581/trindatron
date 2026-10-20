"""An answer that stopped at the plan's output limit says so.

Additive only:
  * messages.truncated — true when the reply ended because it reached the
    plan's maximum output length, not because it was finished
    (src/services/chat_service.py). NULL means "not truncated", so every
    existing row keeps its meaning.

Revision ID: 0041_message_truncated
Revises: 0040_user_preferences
"""
from alembic import op
import sqlalchemy as sa


revision = "0041_message_truncated"
down_revision = "0040_user_preferences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("truncated", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "truncated")
