"""Add presentation_llm_calls — measured cost for decks.

Purely additive: one new table, no change to anything existing.

Why it is needed: the presentation engine is a separate service that reports
no token usage of its own, so without this a deck could only ever be billed
against its pre-flight estimate. The gateway the engine calls (see
api/routes/presenton_gateway.py) writes one row here per model call with the
counts Google actually returned, and the job finalizes against the sum.

The index is on created_at because the only query is a range sum over the
window of one generation — see models/presentation_llm_call.py for why
attribution is temporal rather than by correlation id.

Revision ID: 0031_presentation_llm_calls
Revises: 0030_artifact_object_storage
"""
from alembic import op
import sqlalchemy as sa


revision = "0031_presentation_llm_calls"
down_revision = "0030_artifact_object_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "presentation_llm_calls",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_presentation_llm_calls_created_at", "presentation_llm_calls", ["created_at"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_presentation_llm_calls_created_at", table_name="presentation_llm_calls"
    )
    op.drop_table("presentation_llm_calls")
