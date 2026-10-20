"""Durable Execution — add queue/lease/retry columns to execution_jobs,
add 'retrying' to the status check constraint.

Revision ID: 0019_durable_execution
Revises: 0018_execution_runtime_v1
Create Date: 2026-09-08

Background
----------
Execution Runtime V1 dispatched jobs via an in-process asyncio.Task
(execution_service.create_job() spawned it directly) — correct only for a
single-process deployment, and jobs did not survive a process restart
mid-execution. This migration adds the columns
execution_worker.py's DB-backed claim/lease/recovery model needs:
worker_id + lease_expires_at (claimed-by / claim-deadline, set together
atomically on claim, cleared together on release), heartbeat_at (per-step
liveness marker), next_run_at (durable retry-backoff scheduling, replacing
the old in-process asyncio.sleep()-based retry loop). No existing column
is removed or repurposed; no data is deleted. Existing rows get NULL for
all four new columns (nullable=True, no server_default needed — a NULL
worker_id/lease_expires_at/next_run_at is exactly the correct "not
currently claimed / eligible whenever its status allows" state for any
pre-existing row).

The status check constraint is recreated to also accept 'retrying' — see
models/execution.py's module docstring for why it's a distinct status
from 'queued' (observability only; the worker's claim query treats both
identically).
"""
from alembic import op
import sqlalchemy as sa


revision = '0019_durable_execution'
down_revision = '0018_execution_runtime_v1'
branch_labels = None
depends_on = None

_OLD_STATUSES = (
    'created', 'queued', 'starting', 'running', 'waiting',
    'completed', 'failed', 'cancelled', 'expired', 'cleanup', 'destroyed',
)
_NEW_STATUSES = (
    'created', 'queued', 'starting', 'running', 'retrying', 'waiting',
    'completed', 'failed', 'cancelled', 'expired', 'cleanup', 'destroyed',
)


def upgrade() -> None:
    with op.batch_alter_table('execution_jobs') as batch_op:
        batch_op.add_column(sa.Column('worker_id', sa.String(64), nullable=True))
        batch_op.add_column(sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.drop_constraint('ck_execution_job_status', type_='check')
        batch_op.create_check_constraint(
            'ck_execution_job_status',
            "status IN (" + ", ".join(repr(s) for s in _NEW_STATUSES) + ")",
        )

    op.create_index('ix_execution_jobs_worker_id', 'execution_jobs', ['worker_id'])
    op.create_index('ix_execution_jobs_lease_expires_at', 'execution_jobs', ['lease_expires_at'])
    op.create_index('ix_execution_jobs_next_run_at', 'execution_jobs', ['next_run_at'])


def downgrade() -> None:
    op.drop_index('ix_execution_jobs_next_run_at', table_name='execution_jobs')
    op.drop_index('ix_execution_jobs_lease_expires_at', table_name='execution_jobs')
    op.drop_index('ix_execution_jobs_worker_id', table_name='execution_jobs')

    with op.batch_alter_table('execution_jobs') as batch_op:
        batch_op.drop_constraint('ck_execution_job_status', type_='check')
        batch_op.create_check_constraint(
            'ck_execution_job_status',
            "status IN (" + ", ".join(repr(s) for s in _OLD_STATUSES) + ")",
        )
        batch_op.drop_column('next_run_at')
        batch_op.drop_column('heartbeat_at')
        batch_op.drop_column('lease_expires_at')
        batch_op.drop_column('worker_id')
