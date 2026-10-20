"""Add object-storage columns to execution_artifacts.

Purely additive and reversible: three nullable columns plus one index. No
data is read, rewritten or destroyed. Every existing row keeps
storage_backend NULL, which is exactly right — those artifacts DO live on
the workspace filesystem, and NULL is what the model reads as "filesystem,
addressed by relative_path" (see models/execution.py's ExecutionArtifact
docstring). So the migration needs no backfill: the default value is the
truth for every row that already exists.

The index is on expires_at because the retention sweep's only query is
"which artifacts are past their expiry" — a scan of a table that grows one
row per generated file is the one access pattern worth an index here.

Revision ID: 0030_artifact_object_storage
Revises: 0029_users_team_id
"""
from alembic import op
import sqlalchemy as sa


revision = "0030_artifact_object_storage"
down_revision = "0029_users_team_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "execution_artifacts",
        sa.Column("storage_backend", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "execution_artifacts",
        sa.Column("storage_key", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "execution_artifacts",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_execution_artifacts_expires_at", "execution_artifacts", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_execution_artifacts_expires_at", table_name="execution_artifacts")
    op.drop_column("execution_artifacts", "expires_at")
    op.drop_column("execution_artifacts", "storage_key")
    op.drop_column("execution_artifacts", "storage_backend")
