"""Add direction and idempotency_key to ledger_entries (Billing-0.5 hardening).

direction:       required field for canonical double-entry accounting.
                 'debit'  — the debit side of a financial event.
                 'credit' — the credit side of a financial event.
                 Invariant: for every record_double_entry() call, exactly one
                 debit entry and one credit entry are created with the same amount.

idempotency_key: nullable UNIQUE field.
                 NULL  → no idempotency requirement (platform-internal entries).
                 Non-NULL → idempotent write; duplicate key returns existing entry.

Migration strategy: safe for production; no existing data is modified.
server_default='debit' handles any rows that existed before the migration
(from migration 0010) without requiring a backfill.
"""

revision = "0011_ledger_hardening"
down_revision = "0010_billing_foundation"
branch_labels = None
depends_on = None

import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    # batch_alter_table works on both SQLite (table rebuild) and PostgreSQL (ALTER TABLE).
    with op.batch_alter_table("ledger_entries", schema=None) as batch_op:
        # direction: NOT NULL with a safe default for pre-existing rows.
        batch_op.add_column(
            sa.Column(
                "direction",
                sa.String(6),
                nullable=False,
                server_default="debit",
            )
        )
        batch_op.create_check_constraint(
            "ck_ledger_direction",
            "direction IN ('debit', 'credit')",
        )
        # idempotency_key: nullable; UNIQUE allows multiple NULLs in both PG and SQLite.
        batch_op.add_column(
            sa.Column("idempotency_key", sa.String(128), nullable=True)
        )
        batch_op.create_unique_constraint(
            "uq_ledger_idempotency_key", ["idempotency_key"]
        )

    op.create_index(
        "ix_ledger_idempotency_key", "ledger_entries", ["idempotency_key"]
    )


def downgrade() -> None:
    op.drop_index("ix_ledger_idempotency_key", table_name="ledger_entries")
    with op.batch_alter_table("ledger_entries", schema=None) as batch_op:
        batch_op.drop_constraint("uq_ledger_idempotency_key", type_="unique")
        batch_op.drop_column("idempotency_key")
        batch_op.drop_constraint("ck_ledger_direction", type_="check")
        batch_op.drop_column("direction")
