"""Media jobs and the unified artifact record.

`artifacts` is the one table every client lists and downloads created files
from — images, videos, avatars, documents and agent output — so Mobile, Web
and Desktop share one gallery and one download rule. Access is always decided
by `user_id` here, never by possession of a storage key.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base

ARTIFACT_KINDS = ("image", "video", "avatar", "document", "agent_output")
MEDIA_JOB_KINDS = ("image", "video", "avatar")
MEDIA_OPERATIONS = ("generate", "edit", "image_to_video")
MEDIA_JOB_STATUSES = ("queued", "running", "completed", "failed", "cancelled")
MEDIA_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # Where it came from: "media" (a MediaJob) or "upload" (a user-provided source).
    source: Mapped[str] = mapped_column(String(20), nullable=False, default="media")
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    storage_backend: Mapped[str] = mapped_column(String(20), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class MediaJob(Base):
    __tablename__ = "media_jobs"
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key", name="uq_media_jobs_user_idempotency"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    operation: Mapped[str] = mapped_column(String(20), nullable=False, default="generate")
    status: Mapped[str] = mapped_column(String(12), nullable=False, default="queued", index=True)
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    source_artifact_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("artifacts.id", ondelete="SET NULL"), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reservation_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("usage_records.id", ondelete="SET NULL"), nullable=True)
    # A neutral code a client can translate ("engine_unavailable", "failed").
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    engine_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False, default=Decimal("0"))
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    artifact_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("artifacts.id", ondelete="SET NULL"), nullable=True)
    # The job a queue-based hosted provider accepted (media/engines.py ProviderHandle), persisted the moment it is
    # known so a restart follows it instead of submitting again. Internal only.
    provider_request_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    provider_ref: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)
