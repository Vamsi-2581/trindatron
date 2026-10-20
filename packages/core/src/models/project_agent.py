from datetime import datetime, timezone
from sqlalchemy import String, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProjectAgent(Base):
    """Association between a Project and an AgentDefinition."""
    __tablename__ = "project_agents"

    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True)
    agent_definition_id: Mapped[str] = mapped_column(String(36), ForeignKey("agent_definitions.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    project: Mapped["Project"] = relationship("Project", back_populates="project_agents")
    agent_definition: Mapped["AgentDefinition"] = relationship("AgentDefinition", back_populates="project_agents")
