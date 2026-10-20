"""Commercial Checkout V1 — add payment_orders.plan and widen the ledger
entry_type CHECK constraint to accept 'plan_purchase'.

Revision ID: 0023_payment_order_plan
Revises: 0022_agent_decide_tier2
Create Date: 2026-09-30

Background
----------
See src/billing/pricing.py and src/services/payment_order_service.py's
"Plan-purchase orders" section for the full architecture. This migration
is purely additive/widening:

- `payment_orders.plan` (nullable String(20)) — the plan this order
  activates on confirmation (e.g. "start", "pro"), or NULL for every
  existing/legacy order (a plain credit-purchase order, unaffected).
  Not a foreign key — plan values are validated against
  src/entitlements.py/src/billing/pricing.py in Python, the same
  convention AgentSchedule.priority already uses for a small controlled
  string vocabulary that doesn't warrant its own lookup table.
- `ck_ledger_entry_type` is dropped and re-created with one new allowed
  value (`plan_purchase`) appended — every value the constraint already
  allowed is UNCHANGED, so no existing row can ever violate the new
  constraint.

No execution/billing side effect
-------------------------------------
Purely a column add + a constraint rebuild — never calls
PaymentOrderService.confirm_order() or touches any ledger/credit row,
upgrade or downgrade.
"""
from alembic import op
import sqlalchemy as sa


revision = '0023_payment_order_plan'
down_revision = '0022_agent_decide_tier2'
branch_labels = None
depends_on = None

_OLD_LEDGER_ENTRY_TYPES = (
    "revenue", "fee", "ai_cost", "credit_purchase", "credit_consumption",
    "refund", "reserve", "tax", "profit", "adjustment",
)
_NEW_LEDGER_ENTRY_TYPES = _OLD_LEDGER_ENTRY_TYPES + ("plan_purchase",)


def _check_sql(entry_types: tuple[str, ...]) -> str:
    return "entry_type IN (" + ", ".join(f"'{t}'" for t in entry_types) + ")"


def upgrade() -> None:
    with op.batch_alter_table('payment_orders') as batch_op:
        batch_op.add_column(sa.Column('plan', sa.String(20), nullable=True))

    with op.batch_alter_table('ledger_entries') as batch_op:
        batch_op.drop_constraint('ck_ledger_entry_type', type_='check')
        batch_op.create_check_constraint('ck_ledger_entry_type', _check_sql(_NEW_LEDGER_ENTRY_TYPES))


def downgrade() -> None:
    with op.batch_alter_table('ledger_entries') as batch_op:
        batch_op.drop_constraint('ck_ledger_entry_type', type_='check')
        batch_op.create_check_constraint('ck_ledger_entry_type', _check_sql(_OLD_LEDGER_ENTRY_TYPES))

    with op.batch_alter_table('payment_orders') as batch_op:
        batch_op.drop_column('plan')
