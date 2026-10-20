"""Files attached to a conversation.

An attachment points at the bytes rather than copying them: a project file is
referenced by id, an upload is stored once as an Artifact. What the model
reads is `extracted_text`, taken once at attach time and bounded, so a turn
never re-parses or re-sends the whole file.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base

# "vision": an image whose content was read and described by the vision engine.
EXTRACTION_STATUSES = ("text", "vision", "unsupported", "empty")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SessionAttachment(Base):
    __tablename__ = "session_attachments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # "document" | "image"
    project_file_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("project_files.id", ondelete="SET NULL"), nullable=True)
    artifact_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("artifacts.id", ondelete="SET NULL"), nullable=True)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unsupported")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
