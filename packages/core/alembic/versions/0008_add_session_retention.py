"""add session retention fields and decouple usage records from session hard-delete

Revision ID: 0008_add_session_retention
Revises: 0007_add_usage_status
Create Date: 2026-08-24

Policy implemented:
- Regular conversations: soft-deleted (deleted_at set), UI removes immediately,
  permanent backend deletion within 30 days.
- Temporary conversations: is_temporary=True, auto-deleted 30 days after creation,
  not shown in permanent history.
- Usage records: retained independently of session deletion (session_id → SET NULL,
  nullable) for billing / TCU accounting / fraud prevention / auditing.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0008_add_session_retention'
down_revision = '0007_add_usage_status'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── sessions: add is_temporary and deleted_at ──────────────────────────
    with op.batch_alter_table('sessions') as batch_op:
        batch_op.add_column(
            sa.Column('is_temporary', sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_index('ix_sessions_deleted_at', ['deleted_at'])

    # ── usage_records: make session_id nullable so records survive session ──
    # deletion. ON DELETE SET NULL replaces the previous ON DELETE CASCADE.
    with op.batch_alter_table('usage_records') as batch_op:
        batch_op.alter_column(
            'session_id',
            existing_type=sa.String(36),
            nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table('usage_records') as batch_op:
        batch_op.alter_column(
            'session_id',
            existing_type=sa.String(36),
            nullable=False,
        )

    with op.batch_alter_table('sessions') as batch_op:
        batch_op.drop_index('ix_sessions_deleted_at')
        batch_op.drop_column('deleted_at')
        batch_op.drop_column('is_temporary')
