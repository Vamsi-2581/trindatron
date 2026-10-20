"""Add users.team_id — the grouping key TEAM's shared usage pool is summed over.

Purely additive and reversible: one nullable column plus its index. No data
is read, rewritten or destroyed, and every existing row keeps team_id NULL,
which is the correct state for an individual plan and for a TEAM account
whose pool is currently just its owner.

Why a column and not a teams table: the shared pool needs exactly one fact —
which users share a budget. Roles, invites and seat management are a separate
product feature, and modelling them here would be schema nobody writes to.

Revision ID: 0029_users_team_id
Revises: 0028_sub_webhook_ordering_stream
"""
from alembic import op
import sqlalchemy as sa


revision = "0029_users_team_id"
down_revision = "0028_sub_webhook_ordering_stream"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("team_id", sa.String(length=36), nullable=True))
    op.create_index("ix_users_team_id", "users", ["team_id"])


def downgrade() -> None:
    op.drop_index("ix_users_team_id", table_name="users")
    op.drop_column("users", "team_id")
