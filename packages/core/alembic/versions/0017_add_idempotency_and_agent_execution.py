"""Add idempotency_keys + agent_executions for Etapa 5I (Request
Idempotency & Execution Recovery).

Revision ID: 0017_idempotency_agent_exec
Revises: 0016_add_runtime_settings
Create Date: 2026-09-02

Note: the revision id is shortened to fit alembic_version.version_num's
varchar(32) column (same fix already applied to migration 0004 — see its
own file for the identical precedent; a revision id over 32 chars fails
"alembic upgrade head" on PostgreSQL with StringDataRightTruncation).

Background
----------
Neither table replaces or duplicates any existing financial/entitlement
table. `idempotency_keys` protects POST /chat, /agent, /message against
duplicate effects from a retried HTTP request (opt-in via the
Idempotency-Key header — a request with no header is completely
unaffected, byte for byte, by this migration). `agent_executions` is an
unconditional, non-financial lifecycle record for every /agent execution
(admitted -> running -> completed | failed | interrupted), so a crashed or
restarted process leaves a diagnosable trail instead of a silently
abandoned pending UsageRecord. See src/services/idempotency_service.py and
src/services/agent_execution_service.py for the full contract.
"""
from alembic import op
import sqlalchemy as sa


revision = '0017_idempotency_agent_exec'
down_revision = '0016_add_runtime_settings'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'idempotency_keys',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('key', sa.String(128), nullable=False),
        sa.Column('endpoint', sa.String(20), nullable=False),
        sa.Column('fingerprint', sa.String(64), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='in_progress'),
        sa.Column('reservation_id', sa.String(36), sa.ForeignKey('usage_records.id', ondelete='SET NULL'), nullable=True),
        sa.Column('response_session_id', sa.String(36), nullable=True),
        sa.Column('response_message_id', sa.String(36), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('user_id', 'key', name='uq_idempotency_user_key'),
    )
    op.create_index('ix_idempotency_keys_user_id', 'idempotency_keys', ['user_id'])

    op.create_table(
        'agent_executions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('session_id', sa.String(36), sa.ForeignKey('sessions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('reservation_id', sa.String(36), sa.ForeignKey('usage_records.id', ondelete='SET NULL'), nullable=True),
        sa.Column('continuity_execution_id', sa.String(36), nullable=True),
        sa.Column('idempotency_key_id', sa.String(36), sa.ForeignKey('idempotency_keys.id', ondelete='SET NULL'), nullable=True),
        sa.Column('status', sa.String(20), nullable=False, server_default='admitted'),
        sa.Column('message_id', sa.String(36), nullable=True),
        sa.Column('iterations_done', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('note', sa.String(255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_agent_executions_user_id', 'agent_executions', ['user_id'])
    op.create_index('ix_agent_executions_session_id', 'agent_executions', ['session_id'])


def downgrade() -> None:
    op.drop_index('ix_agent_executions_session_id', table_name='agent_executions')
    op.drop_index('ix_agent_executions_user_id', table_name='agent_executions')
    op.drop_table('agent_executions')
    op.drop_index('ix_idempotency_keys_user_id', table_name='idempotency_keys')
    op.drop_table('idempotency_keys')
