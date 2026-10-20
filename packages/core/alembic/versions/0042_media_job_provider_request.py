"""Media jobs remember the provider job behind them, and can be cancelled.

Additive only:
  * media_jobs.provider_request_id — the request id a queue-based hosted
    provider returned (src/media/higgsfield.py), stored before any polling so
    a restart follows the same job instead of paying for a second one.
  * media_jobs.provider_ref — the rest of that handle (route, model, status
    and cancel URLs, estimate). Internal only.
  * status "cancelled" needs no schema change (media_jobs.status is a
    String(12) without a check constraint).

Revision ID: 0042_media_job_provider_request
Revises: 0041_message_truncated
"""
from alembic import op
import sqlalchemy as sa


revision = "0042_media_job_provider_request"
down_revision = "0041_message_truncated"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("media_jobs", sa.Column("provider_request_id", sa.String(length=100), nullable=True))
    op.add_column("media_jobs", sa.Column("provider_ref", sa.JSON(), nullable=True))
    op.create_index("ix_media_jobs_provider_request_id", "media_jobs", ["provider_request_id"])


def downgrade() -> None:
    op.drop_index("ix_media_jobs_provider_request_id", table_name="media_jobs")
    op.drop_column("media_jobs", "provider_ref")
    op.drop_column("media_jobs", "provider_request_id")
