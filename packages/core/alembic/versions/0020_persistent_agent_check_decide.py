"""Persistent Agent Runtime V1 (Tier 1) — add CHECK/DECIDE columns to
agent_schedules and make agent_schedules.priority nullable (an explicit
"no override" state, distinct from an explicit low-priority choice).

Revision ID: 0020_agent_check_decide
Revises: 0019_durable_execution
Create Date: 2026-09-11

Background
----------
AgentSchedule previously only implemented WAKE and ACT (a static
job_payload fired unconditionally on a fixed interval) despite its own
docstring claiming the full WAKE -> CHECK -> DECIDE -> ACT -> SLEEP cycle.
This migration adds the three columns the newly-wired CHECK/DECIDE step
needs to persist its outcome:
  last_check_signature — the CHECK step's observed-state fingerprint
    (see src/services/agent_check_source.py), compared against on the
    NEXT wake to detect change.
  last_decision — one of 'acted' | 'skipped' | 'capacity_refused',
    enforced by a CHECK constraint (NULL allowed — a schedule that has
    never fired yet has no decision).
  last_decision_at — when that decision was recorded.
All three are nullable with no data migration needed; every pre-existing
row gets NULL, which is exactly the correct "never evaluated yet" state.

`priority` changes from NOT NULL (server_default 'low') to nullable, no
default. NULL now means "no override — inherit AgentDefinition.priority
at ACT time" (see agent_schedule_service.py). Existing rows keep whatever
concrete value they already have — an existing schedule's already-explicit
'low' (or any other) priority is entirely unaffected by this migration;
only rows created AFTER this migration, by a caller that omits priority,
newly get NULL instead of a silently-defaulted 'low'.
"""
from alembic import op
import sqlalchemy as sa


# Shortened from '0020_persistent_agent_check_decide' (34 chars) — exceeded
# alembic_version.version_num's default VARCHAR(32) and would crash
# `alembic upgrade head` on a real PostgreSQL database (SQLite doesn't
# enforce VARCHAR length, so this was invisible until tested against
# real Postgres). Same fix already applied once before for 0004.
revision = '0020_agent_check_decide'
down_revision = '0019_durable_execution'
branch_labels = None
depends_on = None

_DECISIONS = ("acted", "skipped", "capacity_refused")


def upgrade() -> None:
    with op.batch_alter_table('agent_schedules') as batch_op:
        batch_op.add_column(sa.Column('last_check_signature', sa.String(64), nullable=True))
        batch_op.add_column(sa.Column('last_decision', sa.String(20), nullable=True))
        batch_op.add_column(sa.Column('last_decision_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.create_check_constraint(
            'ck_agent_schedule_last_decision',
            "last_decision IS NULL OR last_decision IN (" + ", ".join(repr(d) for d in _DECISIONS) + ")",
        )
        batch_op.alter_column(
            'priority', existing_type=sa.String(10), nullable=True, server_default=None,
        )


def downgrade() -> None:
    # Existing NULL priorities become 'low' on downgrade, since the older
    # schema cannot represent "no override" — the same default the
    # original 0018 migration used.
    op.execute("UPDATE agent_schedules SET priority = 'low' WHERE priority IS NULL")
    with op.batch_alter_table('agent_schedules') as batch_op:
        batch_op.alter_column(
            'priority', existing_type=sa.String(10), nullable=False, server_default='low',
        )
        batch_op.drop_constraint('ck_agent_schedule_last_decision', type_='check')
        batch_op.drop_column('last_decision_at')
        batch_op.drop_column('last_decision')
        batch_op.drop_column('last_check_signature')
