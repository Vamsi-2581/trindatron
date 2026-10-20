"""Link an assistant message to the media job it started.

Additive: one nullable column. A creation asked for in the conversation keeps
its result card when the conversation is reopened.

Revision ID: 0039_message_media_job
Revises: 0038_support
"""
from alembic import op
import sqlalchemy as sa


revision = "0039_message_media_job"
down_revision = "0038_support"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("media_job_id", sa.String(length=36), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "media_job_id")
