import uuid
from datetime import datetime, timezone
from sqlalchemy import Boolean, String, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True)
    agent_definition_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("agent_definitions.id", ondelete="SET NULL"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="New conversation")
    model_default: Mapped[str] = mapped_column(String(100), nullable=False)
    is_temporary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    user: Mapped["User"] = relationship("User", back_populates="sessions")
    project: Mapped["Project | None"] = relationship("Project", back_populates="sessions")
    agent_definition: Mapped["AgentDefinition | None"] = relationship("AgentDefinition", foreign_keys=[agent_definition_id])
    messages: Mapped[list["Message"]] = relationship(
        "Message", back_populates="session", cascade="all, delete-orphan", order_by="Message.created_at"
    )
    # passive_deletes=True: let the DB (or cleanup_expired_sessions) handle SET NULL
    # rather than SQLAlchemy ORM — required for SQLite which ignores FK actions by default.
    usage_records: Mapped[list["UsageRecord"]] = relationship(
        "UsageRecord", back_populates="session", passive_deletes=True
    )
    memories: Mapped[list["Memory"]] = relationship(
        "Memory", back_populates="session", passive_deletes=True
    )
