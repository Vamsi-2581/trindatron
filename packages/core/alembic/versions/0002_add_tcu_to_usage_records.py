"""add tcu to usage_records

Revision ID: 0002
Revises: 00fd14ed089d
Create Date: 2026-08-19 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '00fd14ed089d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'usage_records',
        sa.Column('tcu', sa.Float(), nullable=False, server_default='0.0'),
    )


def downgrade() -> None:
    op.drop_column('usage_records', 'tcu')
