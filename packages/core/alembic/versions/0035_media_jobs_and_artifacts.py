"""Add artifacts and media_jobs — image, video and avatar creation.

Purely additive: two new tables, no existing row read or changed.

Revision ID: 0035_media_jobs_artifacts
Revises: 0034_merge_auth_presentations
"""
from alembic import op
import sqlalchemy as sa


revision = "0035_media_jobs_artifacts"
down_revision = "0034_merge_auth_presentations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="media"),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("storage_backend", sa.String(length=20), nullable=False),
        sa.Column("storage_key", sa.String(length=500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_artifacts_user_id", "artifacts", ["user_id"])
    op.create_index("ix_artifacts_project_id", "artifacts", ["project_id"])
    op.create_index("ix_artifacts_kind", "artifacts", ["kind"])
    op.create_index("ix_artifacts_created_at", "artifacts", ["created_at"])
    op.create_index("ix_artifacts_expires_at", "artifacts", ["expires_at"])

    op.create_table(
        "media_jobs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", sa.String(length=36), sa.ForeignKey("projects.id", ondelete="SET NULL"), nullable=True),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("operation", sa.String(length=20), nullable=False, server_default="generate"),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="queued"),
        sa.Column("progress", sa.Float(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("source_artifact_id", sa.String(length=36), sa.ForeignKey("artifacts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column("reservation_id", sa.String(length=36), sa.ForeignKey("usage_records.id", ondelete="SET NULL"), nullable=True),
        sa.Column("error_code", sa.String(length=40), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("engine_seconds", sa.Float(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Numeric(18, 8), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric(18, 8), nullable=True),
        sa.Column("artifact_id", sa.String(length=36), sa.ForeignKey("artifacts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_media_jobs_user_idempotency"),
    )
    op.create_index("ix_media_jobs_user_id", "media_jobs", ["user_id"])
    op.create_index("ix_media_jobs_project_id", "media_jobs", ["project_id"])
    op.create_index("ix_media_jobs_status", "media_jobs", ["status"])
    op.create_index("ix_media_jobs_created_at", "media_jobs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_media_jobs_created_at", table_name="media_jobs")
    op.drop_index("ix_media_jobs_status", table_name="media_jobs")
    op.drop_index("ix_media_jobs_project_id", table_name="media_jobs")
    op.drop_index("ix_media_jobs_user_id", table_name="media_jobs")
    op.drop_table("media_jobs")
    op.drop_index("ix_artifacts_expires_at", table_name="artifacts")
    op.drop_index("ix_artifacts_created_at", table_name="artifacts")
    op.drop_index("ix_artifacts_kind", table_name="artifacts")
    op.drop_index("ix_artifacts_project_id", table_name="artifacts")
    op.drop_index("ix_artifacts_user_id", table_name="artifacts")
    op.drop_table("artifacts")
