"""Add status column and period-query indexes to usage_records.

Revision ID: 0007_add_usage_status
Revises: 0006_add_agent_definitions
Create Date: 2026-08-24 00:00:00.000000

Changes
-------
usage_records.status VARCHAR(20) NOT NULL DEFAULT 'completed'
    'pending'   — reservation created, provider not yet called
    'completed' — finalized with real TCU from provider
    'failed'    — cancelled (provider error, stream interrupt, client cancel)
    Existing rows receive server_default='completed' — zero data loss.

ix_usage_user_created  (user_id, created_at)
    Covers the period-window SUM query in entitlement_service._sum_tcu():
    WHERE user_id = ? AND status != 'failed' AND created_at >= ? AND created_at < ?

ix_usage_user_status   (user_id, status)
    Secondary index for filtering by status within a user's records.

Note: the existing single-column ix_usage_records_user_id and
ix_usage_records_created_at are preserved. The composite ix_usage_user_created
supersedes them for quota-window queries.
"""
from alembic import op
import sqlalchemy as sa


revision = '0007_add_usage_status'
down_revision = '0006_add_agent_definitions'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('usage_records') as batch_op:
        batch_op.add_column(
            sa.Column(
                'status',
                sa.String(20),
                nullable=False,
                server_default='completed',
            )
        )
        batch_op.create_index('ix_usage_user_created', ['user_id', 'created_at'])
        batch_op.create_index('ix_usage_user_status',  ['user_id', 'status'])


def downgrade() -> None:
    with op.batch_alter_table('usage_records') as batch_op:
        batch_op.drop_index('ix_usage_user_status')
        batch_op.drop_index('ix_usage_user_created')
        batch_op.drop_column('status')
