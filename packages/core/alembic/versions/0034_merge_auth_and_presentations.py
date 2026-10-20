"""Join the persistent-sessions branch and the presentations branch.

0032/0033 (refresh tokens, Google identities) and 0030/0031 (artifact object
storage, presentation LLM calls) were written in parallel on top of 0029.
This revision has no schema change of its own; it only gives Alembic a single
head again so the next migration has one parent.

Revision ID: 0034_merge_auth_presentations
Revises: 0031_presentation_llm_calls, 0033_user_identities
"""

revision = "0034_merge_auth_presentations"
down_revision = ("0031_presentation_llm_calls", "0033_user_identities")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
