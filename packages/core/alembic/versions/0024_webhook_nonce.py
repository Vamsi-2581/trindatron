"""Production Payment Activation hardening — add webhook_nonces table for
DB-backed, multi-process-safe webhook replay protection.

Revision ID: 0024_webhook_nonce
Revises: 0023_payment_order_plan
Create Date: 2026-10-07

Background
----------
See src/models/webhook_nonce.py's own docstring for the full design and
crash-safety analysis. Closes the documented gap in
BinancePayAdapter.verify_webhook() ("does NOT track nonce reuse").

Purely additive: one new table, no existing table touched.
"""
from alembic import op
import sqlalchemy as sa


revision = '0024_webhook_nonce'
down_revision = '0023_payment_order_plan'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'webhook_nonces',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('provider', sa.String(50), nullable=False),
        sa.Column('nonce', sa.String(128), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('provider', 'nonce', name='uq_webhook_nonce_provider_nonce'),
    )
    op.create_index('ix_webhook_nonce_created_at', 'webhook_nonces', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_webhook_nonce_created_at', table_name='webhook_nonces')
    op.drop_table('webhook_nonces')
