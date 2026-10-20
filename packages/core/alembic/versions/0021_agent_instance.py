"""Persistent Agent Instance V1 — add agent_instances table and
agent_schedules.agent_instance_id, with a safe backfill for existing
schedules.

Revision ID: 0021_agent_instance
Revises: 0020_agent_check_decide
Create Date: 2026-09-18

Background
----------
AgentDefinition (template/config), AgentSchedule (trigger), and
ExecutionJob (one execution) already existed. The missing entity is
AgentInstance — the persistent agent the user actually owns and looks
at. See src/models/agent_instance.py's module docstring for the full
rationale and the fields deliberately NOT added.

`agent_schedules.agent_id` (-> agent_definitions) is left COMPLETELY
UNCHANGED — this migration only ADDS `agent_instance_id` (-> a new
agent_instances row), nullable, alongside it. No existing column is
renamed, repurposed, or has its meaning changed; every existing
reader of AgentSchedule.agent_id keeps working exactly as before.

Backfill strategy (safe, no duplicate executions)
------------------------------------------------------
Purely additive: creates one AgentInstance per DISTINCT (user_id,
agent_id) pair that has at least one existing AgentSchedule row,
reusing ONE instance across multiple schedules for the same
(user, agent_definition) pair rather than creating a duplicate per
schedule (avoids fragmenting a user's monitoring of the same agent
definition into unrelated instances). Each backfilled AgentInstance:
  - status='active' (every existing schedule that predates this
    migration was already running as if unconditionally active — no
    schedule was ever "paused" by an AgentInstance concept before now,
    since the concept didn't exist)
  - name = the AgentDefinition's own name at backfill time (a
    reasonable default a user can rename later via the API)
  - last_run_at / last_execution_job_id backfilled from whichever of
    that pair's schedules has the most recent last_run_at, so the new
    instance's own "last run" reflects real history, not a blank slate

This creates ROWS (new agent_instances + a column update on existing
agent_schedules) but triggers NO execution of any kind — it never calls
execution_service.create_job() or touches execution_jobs/usage_records —
so no duplicate/new execution is possible as a side effect of running
this migration, upgrade or downgrade.

downgrade() drops the column and the table — the backfilled
AgentInstance rows are only ever a derived convenience, not a required
source of truth for anything upgrade-side code introduced elsewhere
(AgentSchedule.agent_id keeps working with no AgentInstance at all).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import table, column
from sqlalchemy.sql import select, func


revision = '0021_agent_instance'
down_revision = '0020_agent_check_decide'
branch_labels = None
depends_on = None

_STATUSES = ("active", "paused", "disabled", "error")


def upgrade() -> None:
    op.create_table(
        'agent_instances',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('agent_definition_id', sa.String(36), sa.ForeignKey('agent_definitions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('project_id', sa.String(36), sa.ForeignKey('projects.id', ondelete='SET NULL'), nullable=True),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('status', sa.String(10), nullable=False, server_default='active'),
        sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_execution_job_id', sa.String(36), sa.ForeignKey('execution_jobs.id', ondelete='SET NULL'), nullable=True),
        sa.Column('failure_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN (" + ", ".join(repr(s) for s in _STATUSES) + ")",
            name='ck_agent_instance_status',
        ),
    )
    op.create_index('ix_agent_instances_user_id', 'agent_instances', ['user_id'])
    op.create_index('ix_agent_instances_agent_definition_id', 'agent_instances', ['agent_definition_id'])
    op.create_index('ix_agent_instances_project_id', 'agent_instances', ['project_id'])
    op.create_index('ix_agent_instances_status', 'agent_instances', ['status'])

    # batch_alter_table (not a plain op.add_column) — SQLite's dialect
    # cannot ALTER a table to add a new FK constraint any other way
    # (confirmed: plain op.add_column raises "No support for ALTER of
    # constraints in SQLite dialect"). Batch mode's own requirement is
    # that every constraint in the rebuilt table have an explicit name —
    # hence the named ForeignKeyConstraint below instead of an inline
    # sa.ForeignKey(...) on the column.
    with op.batch_alter_table('agent_schedules') as batch_op:
        batch_op.add_column(sa.Column('agent_instance_id', sa.String(36), nullable=True))
        batch_op.create_foreign_key(
            'fk_agent_schedules_agent_instance_id', 'agent_instances',
            ['agent_instance_id'], ['id'], ondelete='CASCADE',
        )
    op.create_index('ix_agent_schedules_agent_instance_id', 'agent_schedules', ['agent_instance_id'])

    _backfill()


def _backfill() -> None:
    """See module docstring's "Backfill strategy" section. Pure Python
    loop over a small, bounded result set (distinct (user_id, agent_id)
    pairs that have a schedule) — no execution, no job creation."""
    conn = op.get_bind()

    agent_schedules = table(
        'agent_schedules',
        column('id', sa.String), column('user_id', sa.String), column('agent_id', sa.String),
        column('agent_instance_id', sa.String), column('last_run_job_id', sa.String),
        column('last_run_at', sa.DateTime),
    )
    agent_definitions = table('agent_definitions', column('id', sa.String), column('name', sa.String))
    agent_instances = table(
        'agent_instances',
        column('id', sa.String), column('user_id', sa.String), column('agent_definition_id', sa.String),
        column('name', sa.String), column('status', sa.String),
        column('last_run_at', sa.DateTime), column('last_execution_job_id', sa.String),
        column('failure_count', sa.Integer), column('created_at', sa.DateTime), column('updated_at', sa.DateTime),
    )

    pairs = conn.execute(
        select(agent_schedules.c.user_id, agent_schedules.c.agent_id).distinct()
    ).fetchall()

    import uuid
    from datetime import datetime, timezone

    for user_id, agent_id in pairs:
        definition_row = conn.execute(
            select(agent_definitions.c.name).where(agent_definitions.c.id == agent_id)
        ).first()
        definition_name = definition_row[0] if definition_row else "Agent"

        # Most-recently-run schedule for this (user, definition) pair —
        # its last_run_job_id/last_run_at seed the backfilled instance's
        # own last-run fields with real history instead of a blank slate.
        latest = conn.execute(
            select(agent_schedules.c.last_run_job_id, agent_schedules.c.last_run_at)
            .where(agent_schedules.c.user_id == user_id, agent_schedules.c.agent_id == agent_id)
            .order_by(agent_schedules.c.last_run_at.desc().nulls_last())
            .limit(1)
        ).first()
        last_job_id, last_run_at = (latest[0], latest[1]) if latest else (None, None)

        instance_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        conn.execute(
            agent_instances.insert().values(
                id=instance_id, user_id=user_id, agent_definition_id=agent_id,
                name=definition_name, status="active",
                last_run_at=last_run_at, last_execution_job_id=last_job_id,
                failure_count=0, created_at=now, updated_at=now,
            )
        )
        conn.execute(
            agent_schedules.update()
            .where(agent_schedules.c.user_id == user_id, agent_schedules.c.agent_id == agent_id)
            .values(agent_instance_id=instance_id)
        )


def downgrade() -> None:
    op.drop_index('ix_agent_schedules_agent_instance_id', table_name='agent_schedules')
    with op.batch_alter_table('agent_schedules') as batch_op:
        batch_op.drop_constraint('fk_agent_schedules_agent_instance_id', type_='foreignkey')
        batch_op.drop_column('agent_instance_id')

    op.drop_index('ix_agent_instances_status', table_name='agent_instances')
    op.drop_index('ix_agent_instances_project_id', table_name='agent_instances')
    op.drop_index('ix_agent_instances_agent_definition_id', table_name='agent_instances')
    op.drop_index('ix_agent_instances_user_id', table_name='agent_instances')
    op.drop_table('agent_instances')
