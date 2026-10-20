"""Add execution_jobs, execution_artifacts, agent_schedules for Execution
Runtime V1; add priority/execution_timeout_seconds to agent_definitions.

Revision ID: 0018_execution_runtime_v1
Revises: 0017_idempotency_agent_exec
Create Date: 2026-09-03

Background
----------
None of these tables replace or duplicate an existing one. execution_jobs
carries NO cost/TCU column of its own (reservation_id is a reference-only
pointer into the pre-existing usage_records table, same pattern
agent_executions.reservation_id already established in 0017) — see
src/models/execution.py's module docstring. agent_schedules is a trigger
source that calls the same execution_service.create_job() an HTTP request
would; it does not duplicate agent_scheduler.py (in-process concurrency
admission, unrelated and untouched by this migration).
"""
from alembic import op
import sqlalchemy as sa


revision = '0018_execution_runtime_v1'
down_revision = '0017_idempotency_agent_exec'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'execution_jobs',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('agent_id', sa.String(36), sa.ForeignKey('agent_definitions.id', ondelete='SET NULL'), nullable=True),
        sa.Column('session_id', sa.String(36), sa.ForeignKey('sessions.id', ondelete='SET NULL'), nullable=True),
        sa.Column('project_id', sa.String(36), sa.ForeignKey('projects.id', ondelete='SET NULL'), nullable=True),
        sa.Column('job_type', sa.String(30), nullable=False, server_default='shell'),
        sa.Column('provider_name', sa.String(30), nullable=False, server_default='docker'),
        sa.Column('priority', sa.String(10), nullable=False, server_default='normal'),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='created'),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('checkpoint', sa.JSON(), nullable=False),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('logs', sa.Text(), nullable=True),
        sa.Column('max_retries', sa.Integer(), nullable=False, server_default='2'),
        sa.Column('attempt_number', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('retry_reason', sa.String(255), nullable=True),
        sa.Column('sentinel_criteria', sa.JSON(), nullable=True),
        sa.Column('sentinel_status', sa.String(10), nullable=True),
        sa.Column('sentinel_report', sa.JSON(), nullable=True),
        sa.Column('reservation_id', sa.String(36), sa.ForeignKey('usage_records.id', ondelete='SET NULL'), nullable=True),
        sa.Column('cancel_requested', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('timeout_seconds', sa.Integer(), nullable=False, server_default='300'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('created','queued','starting','running','waiting','completed','failed','cancelled','expired','cleanup','destroyed')",
            name='ck_execution_job_status',
        ),
        sa.CheckConstraint("priority IN ('critical','high','normal','low')", name='ck_execution_job_priority'),
    )
    op.create_index('ix_execution_jobs_user_id', 'execution_jobs', ['user_id'])
    op.create_index('ix_execution_jobs_agent_id', 'execution_jobs', ['agent_id'])
    op.create_index('ix_execution_jobs_session_id', 'execution_jobs', ['session_id'])
    op.create_index('ix_execution_jobs_project_id', 'execution_jobs', ['project_id'])

    op.create_table(
        'execution_artifacts',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('job_id', sa.String(36), sa.ForeignKey('execution_jobs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('filename', sa.String(255), nullable=False),
        sa.Column('relative_path', sa.String(500), nullable=False),
        sa.Column('content_type', sa.String(100), nullable=True),
        sa.Column('size_bytes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_execution_artifacts_job_id', 'execution_artifacts', ['job_id'])

    op.create_table(
        'agent_schedules',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('agent_id', sa.String(36), sa.ForeignKey('agent_definitions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('interval_seconds', sa.Integer(), nullable=False),
        sa.Column('job_payload', sa.JSON(), nullable=False),
        sa.Column('priority', sa.String(10), nullable=False, server_default='low'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_run_job_id', sa.String(36), sa.ForeignKey('execution_jobs.id', ondelete='SET NULL'), nullable=True),
        sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_agent_schedules_user_id', 'agent_schedules', ['user_id'])
    op.create_index('ix_agent_schedules_agent_id', 'agent_schedules', ['agent_id'])

    op.add_column('agent_definitions', sa.Column('priority', sa.String(10), nullable=False, server_default='normal'))
    op.add_column('agent_definitions', sa.Column('execution_timeout_seconds', sa.Integer(), nullable=False, server_default='300'))


def downgrade() -> None:
    op.drop_column('agent_definitions', 'execution_timeout_seconds')
    op.drop_column('agent_definitions', 'priority')

    op.drop_index('ix_agent_schedules_agent_id', table_name='agent_schedules')
    op.drop_index('ix_agent_schedules_user_id', table_name='agent_schedules')
    op.drop_table('agent_schedules')

    op.drop_index('ix_execution_artifacts_job_id', table_name='execution_artifacts')
    op.drop_table('execution_artifacts')

    op.drop_index('ix_execution_jobs_project_id', table_name='execution_jobs')
    op.drop_index('ix_execution_jobs_session_id', table_name='execution_jobs')
    op.drop_index('ix_execution_jobs_agent_id', table_name='execution_jobs')
    op.drop_index('ix_execution_jobs_user_id', table_name='execution_jobs')
    op.drop_table('execution_jobs')
