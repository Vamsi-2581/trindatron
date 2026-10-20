import uuid
from datetime import datetime, timezone
from sqlalchemy import String, Text, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CodeWorkspace(Base):
    __tablename__ = "code_workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, unique=True, index=True)
    root_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    language_hint: Mapped[str | None] = mapped_column(String(50), nullable=True)
    run_command: Mapped[str | None] = mapped_column(String(500), nullable=True)
    build_command: Mapped[str | None] = mapped_column(String(500), nullable=True)
    test_command: Mapped[str | None] = mapped_column(String(500), nullable=True)
    git_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sandbox_image: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    project: Mapped["Project"] = relationship("Project", back_populates="code_workspace")
