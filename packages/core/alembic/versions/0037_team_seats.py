"""Add teams and team_members (TEAM by seat) and payment_orders.team_id.

Additive: two new tables and one nullable column. No existing row changes.

Revision ID: 0037_team_seats
Revises: 0036_session_attachments
"""
from alembic import op
import sqlalchemy as sa


revision = "0037_team_seats"
down_revision = "0036_session_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "teams",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("owner_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending_payment"),
        sa.Column("standard_seats", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("premium_seats", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pending_standard_seats", sa.Integer(), nullable=True),
        sa.Column("pending_premium_seats", sa.Integer(), nullable=True),
        sa.Column("checkout_order_id", sa.String(length=36), nullable=True),
        sa.Column("checkout_standard_seats", sa.Integer(), nullable=True),
        sa.Column("checkout_premium_seats", sa.Integer(), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_teams_owner_id", "teams", ["owner_id"])

    op.create_table(
        "team_members",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("team_id", sa.String(length=36), sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=10), nullable=False, server_default="member"),
        sa.Column("seat_type", sa.String(length=10), nullable=False, server_default="standard"),
        sa.Column("status", sa.String(length=10), nullable=False, server_default="invited"),
        sa.Column("invite_token_hash", sa.String(length=64), nullable=True, unique=True),
        sa.Column("invite_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("team_id", "email", name="uq_team_members_team_email"),
    )
    op.create_index("ix_team_members_team_id", "team_members", ["team_id"])
    op.create_index("ix_team_members_user_id", "team_members", ["user_id"])

    with op.batch_alter_table("payment_orders") as batch:
        batch.add_column(sa.Column("team_id", sa.String(length=36), nullable=True))
        batch.create_index("ix_payment_orders_team_id", ["team_id"])


def downgrade() -> None:
    with op.batch_alter_table("payment_orders") as batch:
        batch.drop_index("ix_payment_orders_team_id")
        batch.drop_column("team_id")
    op.drop_index("ix_team_members_user_id", table_name="team_members")
    op.drop_index("ix_team_members_team_id", table_name="team_members")
    op.drop_table("team_members")
    op.drop_index("ix_teams_owner_id", table_name="teams")
    op.drop_table("teams")
