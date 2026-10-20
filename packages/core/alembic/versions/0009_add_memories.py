"""add memories table for TRINITY MEMORY

Revision ID: 0009_add_memories
Revises: 0008_add_session_retention
Create Date: 2026-08-26

Stores user-scoped persistent memories that survive across chat sessions.
Each memory is a free-text fragment linked to a user and optionally to the
session that produced it.  ON DELETE CASCADE from users; ON DELETE SET NULL
from sessions (memories outlive their source conversation).
"""
from alembic import op
import sqlalchemy as sa

revision = '0009_add_memories'
down_revision = '0008_add_session_retention'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'memories',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('source_session_id', sa.String(36), sa.ForeignKey('sessions.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_memories_user_id', 'memories', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_memories_user_id', table_name='memories')
    op.drop_table('memories')
