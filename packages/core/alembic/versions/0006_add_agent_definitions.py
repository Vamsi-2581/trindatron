"""add agent_definitions, project_agents, and session.agent_definition_id

Revision ID: 0006_add_agent_definitions
Revises: 0005_add_skills
Create Date: 2026-08-23 10:01:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '0006_add_agent_definitions'
down_revision = '0005_add_skills'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'agent_definitions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('avatar_emoji', sa.String(length=10), nullable=True),
        sa.Column('system_instructions', sa.Text(), nullable=True),
        sa.Column('skill_ids', sa.JSON(), nullable=False, server_default='[]'),
        sa.Column('model_preference', sa.String(length=20), nullable=False, server_default='balanced'),
        sa.Column('is_archived', sa.Boolean(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_agent_definitions_user_id'), 'agent_definitions', ['user_id'], unique=False)

    op.create_table(
        'project_agents',
        sa.Column('project_id', sa.String(length=36), nullable=False),
        sa.Column('agent_definition_id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['agent_definition_id'], ['agent_definitions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('project_id', 'agent_definition_id'),
    )

    # Add agent_definition_id to sessions (nullable FK with SET NULL on delete)
    op.add_column('sessions', sa.Column('agent_definition_id', sa.String(length=36), nullable=True))
    op.create_index(op.f('ix_sessions_agent_definition_id'), 'sessions', ['agent_definition_id'], unique=False)
    with op.batch_alter_table('sessions') as batch_op:
        batch_op.create_foreign_key(
            'fk_sessions_agent_definition_id',
            'agent_definitions',
            ['agent_definition_id'],
            ['id'],
            ondelete='SET NULL',
        )


def downgrade() -> None:
    op.drop_index(op.f('ix_sessions_agent_definition_id'), table_name='sessions')
    with op.batch_alter_table('sessions') as batch_op:
        batch_op.drop_constraint('fk_sessions_agent_definition_id', type_='foreignkey')
    op.drop_column('sessions', 'agent_definition_id')
    op.drop_table('project_agents')
    op.drop_index(op.f('ix_agent_definitions_user_id'), table_name='agent_definitions')
    op.drop_table('agent_definitions')
