"""Add refresh_tokens — persistent sessions (short access tokens + rotation).

Purely additive and reversible: one new table and its indexes. No existing
row is read or changed; clients that do not ask for a persistent session
keep working exactly as before.

Chain note: this revision follows 0029_users_team_id, the last committed
revision. The Presenton work in progress carries 0030/0031 on the same parent
and is not committed yet; when it lands, re-parent this chain onto it
(down_revision = "0031_presentation_llm_calls") so there is a single head.

Revision ID: 0032_auth_refresh_tokens
Revises: 0029_users_team_id
"""
from alembic import op
import sqlalchemy as sa


revision = "0032_auth_refresh_tokens"
down_revision = "0029_users_team_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("family_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("client", sa.String(length=20), nullable=False),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", sa.String(length=36), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.String(length=40), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])


def downgrade() -> None:
    op.drop_index("ix_refresh_tokens_family_id", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
