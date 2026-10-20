"""Account security: email confirmation, password recovery, lockout and a security log.

Additive. Five nullable/defaulted columns on users, and two new tables:
account_tokens (hashed single-use links for password reset and email
confirmation) and security_events (the account's security history).

Accounts that already sign in with Google have a Google-verified address, so
they start confirmed. Everyone else starts unconfirmed; nothing is blocked on it.

Revision ID: 0044_account_security
Revises: 0043_message_execution_job
"""
from alembic import op
import sqlalchemy as sa


revision = "0044_account_security"
down_revision = "0043_message_execution_job"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column("last_failed_login_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))

    op.execute(
        "UPDATE users SET email_verified_at = created_at "
        "WHERE id IN (SELECT user_id FROM user_identities WHERE provider = 'google')"
    )

    op.create_table(
        "account_tokens",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("purpose", sa.String(length=20), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_account_tokens_token_hash"),
    )
    op.create_index("ix_account_tokens_user_id", "account_tokens", ["user_id"])

    op.create_table(
        "security_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("method", sa.String(length=20), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_security_events_user_created", "security_events", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_security_events_user_created", table_name="security_events")
    op.drop_table("security_events")
    op.drop_index("ix_account_tokens_user_id", table_name="account_tokens")
    op.drop_table("account_tokens")
    op.drop_column("users", "locked_until")
    op.drop_column("users", "last_failed_login_at")
    op.drop_column("users", "failed_login_count")
    op.drop_column("users", "password_changed_at")
    op.drop_column("users", "email_verified_at")
