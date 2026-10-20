"""Link an assistant message to the execution job it started (a presentation, today).

Additive: one nullable column, the execution-job counterpart of 0039's media_job_id. A deck asked for in the
conversation keeps its card — progress, then the file to download — when the conversation is reopened.

Revision ID: 0043_message_execution_job
Revises: 0042_media_job_provider_request
"""
from alembic import op
import sqlalchemy as sa


revision = "0043_message_execution_job"
down_revision = "0042_media_job_provider_request"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("execution_job_id", sa.String(length=36), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "execution_job_id")
