"""Persistent Agent Runtime — Tier 2 (LLM DECIDE): add
agent_schedules.pending_decision_job_id and widen the last_decision CHECK
constraint to the new Tier-2 vocabulary.

Revision ID: 0022_agent_decide_tier2
Revises: 0021_agent_instance
Create Date: 2026-09-25

Background
----------
See src/models/agent_schedule.py's "DECIDE (Tier 2 — LLM DECIDE)" section
and src/services/agent_schedule_service.py's module docstring for the
full architecture. This migration is purely additive/widening:

- `pending_decision_job_id` (nullable FK -> execution_jobs.id,
  ondelete='SET NULL') tracks a schedule's outstanding DECIDE
  ExecutionJob. NULL for every existing schedule (Tier 1 and legacy
  schedules never populate it — the column is simply unused for them,
  exactly like `agent_instance_id` was for pre-0021 schedules).
- `ck_agent_schedule_last_decision` is dropped and re-created with four
  new allowed values appended (`decide_pending`, `decide_failed`,
  `decide_invalid`, `decide_skip`) — every value the constraint already
  allowed (`acted`, `skipped`, `capacity_refused`) is UNCHANGED, so no
  existing row can ever violate the new constraint.

No job_type schema change needed
------------------------------------
`ExecutionJob.job_type` has no DB-level CHECK constraint (String(30),
"reserved for future kinds... without a schema change" — see
models/execution.py). The new job_type="agent_decide" needed no
migration of its own.

No execution/billing side effect
-------------------------------------
Purely a column add + a constraint rebuild — never calls
execution_service.create_job() or touches execution_jobs/usage_records,
upgrade or downgrade.
"""
from alembic import op
import sqlalchemy as sa


revision = '0022_agent_decide_tier2'
down_revision = '0021_agent_instance'
branch_labels = None
depends_on = None

_OLD_DECISIONS = ("acted", "skipped", "capacity_refused")
_NEW_DECISIONS = _OLD_DECISIONS + ("decide_pending", "decide_failed", "decide_invalid", "decide_skip")


def _check_sql(decisions: tuple[str, ...]) -> str:
    return "last_decision IS NULL OR last_decision IN (" + ", ".join(repr(d) for d in decisions) + ")"


def upgrade() -> None:
    # batch_alter_table (not plain op.add_column) — SQLite cannot ALTER a
    # table to add a new FK constraint any other way; batch mode's own
    # requirement is that every constraint in the rebuilt table have an
    # explicit name — same technique 0021's own migration already used.
    with op.batch_alter_table('agent_schedules') as batch_op:
        batch_op.add_column(sa.Column('pending_decision_job_id', sa.String(36), nullable=True))
        batch_op.create_foreign_key(
            'fk_agent_schedules_pending_decision_job_id', 'execution_jobs',
            ['pending_decision_job_id'], ['id'], ondelete='SET NULL',
        )
        batch_op.drop_constraint('ck_agent_schedule_last_decision', type_='check')
        batch_op.create_check_constraint('ck_agent_schedule_last_decision', _check_sql(_NEW_DECISIONS))


def downgrade() -> None:
    with op.batch_alter_table('agent_schedules') as batch_op:
        batch_op.drop_constraint('ck_agent_schedule_last_decision', type_='check')
        batch_op.create_check_constraint('ck_agent_schedule_last_decision', _check_sql(_OLD_DECISIONS))
        batch_op.drop_constraint('fk_agent_schedules_pending_decision_job_id', type_='foreignkey')
        batch_op.drop_column('pending_decision_job_id')
