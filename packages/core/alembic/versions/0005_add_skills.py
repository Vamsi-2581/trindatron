"""add skills table

Revision ID: 0005_add_skills
Revises: 0004_workspaces_session
Create Date: 2026-08-23 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '0005_add_skills'
down_revision = '0004_workspaces_session'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'skills',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('instructions', sa.Text(), nullable=True),
        sa.Column('tool_ids', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('icon_emoji', sa.String(length=10), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_skills_user_id'), 'skills', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_skills_user_id'), table_name='skills')
    op.drop_table('skills')
