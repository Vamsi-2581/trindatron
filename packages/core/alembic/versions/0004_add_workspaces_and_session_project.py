"""add workspaces and session project_id

Revision ID: 0004_workspaces_session
Revises: 0003_add_projects
Create Date: 2026-08-23 00:01:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '0004_workspaces_session'
down_revision = '0003_add_projects'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add project_id to sessions (nullable FK)
    op.add_column('sessions', sa.Column('project_id', sa.String(length=36), nullable=True))
    op.create_index(op.f('ix_sessions_project_id'), 'sessions', ['project_id'], unique=False)
    with op.batch_alter_table('sessions') as batch_op:
        batch_op.create_foreign_key(
            'fk_sessions_project_id',
            'projects',
            ['project_id'],
            ['id'],
            ondelete='SET NULL',
        )

    # Create code_workspaces (stub — no execution yet)
    op.create_table(
        'code_workspaces',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('project_id', sa.String(length=36), nullable=False),
        sa.Column('root_path', sa.String(length=500), nullable=True),
        sa.Column('language_hint', sa.String(length=50), nullable=True),
        sa.Column('run_command', sa.String(length=500), nullable=True),
        sa.Column('build_command', sa.String(length=500), nullable=True),
        sa.Column('test_command', sa.String(length=500), nullable=True),
        sa.Column('git_enabled', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('sandbox_image', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_id'),
    )
    op.create_index(op.f('ix_code_workspaces_project_id'), 'code_workspaces', ['project_id'], unique=True)

    # Create work_workspaces (stub — no execution yet)
    op.create_table(
        'work_workspaces',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('project_id', sa.String(length=36), nullable=False),
        sa.Column('requires_approval', sa.Boolean(), nullable=False, server_default='1'),
        sa.Column('approval_policy', sa.String(length=20), nullable=False, server_default='smart'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('project_id'),
    )
    op.create_index(op.f('ix_work_workspaces_project_id'), 'work_workspaces', ['project_id'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_work_workspaces_project_id'), table_name='work_workspaces')
    op.drop_table('work_workspaces')
    op.drop_index(op.f('ix_code_workspaces_project_id'), table_name='code_workspaces')
    op.drop_table('code_workspaces')
    op.drop_index(op.f('ix_sessions_project_id'), table_name='sessions')
    with op.batch_alter_table('sessions') as batch_op:
        batch_op.drop_constraint('fk_sessions_project_id', type_='foreignkey')
    op.drop_column('sessions', 'project_id')
