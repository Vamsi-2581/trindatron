"""Add entitlement_sessions for the 5-hour rolling session window.

Revision ID: 0015_add_entitlement_sessions
Revises: 0014_add_user_timezone
Create Date: 2026-08-31

Background
----------
New product rule: a "session" for entitlement purposes is a 5-hour rolling
window that starts on the first operation that actually reserves TCU after
no session is active — never at login/app-open, never extended by later
activity. See src/services/session_window_service.py for the full rule.

This is a SEPARATE table (not columns on users, not tied to billing/credits
or to the chat conversation Session model) — one row per user, holding
current state, overwritten in place when a new 5-hour window starts.

UNIQUE(user_id) is the concurrency safety net for dialects without the
PostgreSQL advisory lock check_and_reserve() already uses (SQLite): two
racing inserts for the same user can't both succeed.
"""
from alembic import op
import sqlalchemy as sa


revision = '0015_add_entitlement_sessions'
down_revision = '0014_add_user_timezone'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'entitlement_sessions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('session_started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('session_expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('user_id', name='uq_entitlement_sessions_user_id'),
    )
    op.create_index('ix_entitlement_sessions_user_id', 'entitlement_sessions', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_entitlement_sessions_user_id', table_name='entitlement_sessions')
    op.drop_table('entitlement_sessions')
