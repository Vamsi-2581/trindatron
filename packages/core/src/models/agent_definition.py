import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Text, Boolean, DateTime, ForeignKey, Integer, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AgentDefinition(Base):
    __tablename__ = "agent_definitions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    avatar_emoji: Mapped[str | None] = mapped_column(String(10), nullable=True)
    system_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    skill_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    model_preference: Mapped[str] = mapped_column(String(20), nullable=False, default="balanced")
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Execution Runtime V1 (mission section 12 "AGENT BUDGET") — internal
    # only, never surfaced to the end user as a number (see
    # adaptive_budget_service.py). Governs two things: the default
    # ExecutionJob.priority for jobs created without an explicit override,
    # and admission under AdaptiveBudgetService's capacity tiers.
    priority: Mapped[str] = mapped_column(String(10), nullable=False, default="normal")
    execution_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    user: Mapped["User"] = relationship("User", back_populates="agent_definitions")
    project_agents: Mapped[list["ProjectAgent"]] = relationship("ProjectAgent", cascade="all, delete-orphan")
