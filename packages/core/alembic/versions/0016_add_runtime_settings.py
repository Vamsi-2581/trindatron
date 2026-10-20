"""Add runtime_settings + users.is_admin for Etapa 5H (Kill Switches +
Runtime Settings).

Revision ID: 0016_add_runtime_settings
Revises: 0015_add_entitlement_sessions
Create Date: 2026-09-02

Background
----------
Before this migration, EVERY piece of Core's configuration was env-var-only
(src/config.py's pydantic Settings, read once at process start) — there was
no way to disable Agent execution, Continuity Mode, a specific provider/
model, or a specific capability without editing the environment and
restarting/redeploying the process. This migration adds the minimal
server-side mechanism for that: one key-value table
(src/models/runtime_setting.py — one row per known switch, not a single-row
blob) plus one boolean authorization bit on users (is_admin) so the new
admin-only settings endpoints can reuse the EXISTING JWT/get_current_user
mechanism rather than a second auth system.

Absence of a row for any given key is a normal, expected, SAFE state — see
src/services/runtime_settings.py's fail-safe defaults (missing key ->
current/pre-5H behavior; a corrupted-but-present value -> fails toward the
restrictive state for a boolean kill switch, or toward the empty/baseline
state for a disabled-keys list — see that module's own docstring for the
full reasoning). Nothing about this migration itself changes any existing
row or behavior.
"""
from alembic import op
import sqlalchemy as sa


revision = '0016_add_runtime_settings'
down_revision = '0015_add_entitlement_sessions'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users') as batch_op:
        batch_op.add_column(
            sa.Column('is_admin', sa.Boolean(), nullable=False, server_default=sa.false())
        )

    op.create_table(
        'runtime_settings',
        sa.Column('key', sa.String(100), primary_key=True),
        sa.Column('value', sa.JSON(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_by_user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('runtime_settings')
    with op.batch_alter_table('users') as batch_op:
        batch_op.drop_column('is_admin')
