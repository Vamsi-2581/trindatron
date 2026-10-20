"""Add session_attachments — files attached to a conversation.

Purely additive: one new table.

Revision ID: 0036_session_attachments
Revises: 0035_media_jobs_artifacts
"""
from alembic import op
import sqlalchemy as sa


revision = "0036_session_attachments"
down_revision = "0035_media_jobs_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_attachments",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.String(length=36), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("project_file_id", sa.String(length=36), sa.ForeignKey("project_files.id", ondelete="SET NULL"), nullable=True),
        sa.Column("artifact_id", sa.String(length=36), sa.ForeignKey("artifacts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("extraction_status", sa.String(length=20), nullable=False, server_default="unsupported"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_session_attachments_user_id", "session_attachments", ["user_id"])
    op.create_index("ix_session_attachments_session_id", "session_attachments", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_session_attachments_session_id", table_name="session_attachments")
    op.drop_index("ix_session_attachments_user_id", table_name="session_attachments")
    op.drop_table("session_attachments")
