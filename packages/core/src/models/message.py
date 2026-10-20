import uuid
from datetime import datetime, timezone
from sqlalchemy import Boolean, String, DateTime, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user | assistant | system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model_used: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # The media job a creation request in this conversation started (assistant replies only).
    media_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # The execution job this reply started (a presentation), followed with GET /execution/jobs/{id}.
    execution_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # The reply ended at the plan's output limit rather than finishing (chat_service._hit_output_cap). NULL = no.
    truncated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)

    session: Mapped["Session"] = relationship("Session", back_populates="messages")
