from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session as DBSession

from src.models.session import Session
from src.models.usage import UsageRecord

_RETENTION_DAYS = 30


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ForeignResourceReference(Exception):
    """Raised when project_id/agent_definition_id refers to a resource
    that doesn't belong to the requesting user. Security Hardening
    finding BOLA-04: chat_service.py/agent_service.py unconditionally
    follow session.project/session.agent_definition to build the LLM's
    system prompt, so an unvalidated foreign id here would let an
    attacker read another user's private project instructions or
    custom-agent system prompt. Same ownership-check pattern already
    used by agent_instance_service.py's create_agent_instance()."""

    def __init__(self, resource: str, resource_id: str) -> None:
        self.resource = resource
        self.resource_id = resource_id
        super().__init__(f"{resource} {resource_id!r} not found for this user")


def create_session(
    db: DBSession,
    user_id: str,
    title: str = "New conversation",
    model: str | None = None,
    project_id: str | None = None,
    agent_definition_id: str | None = None,
    is_temporary: bool = False,
) -> Session:
    if project_id is not None:
        from src.models.project import Project
        project = db.query(Project).filter(Project.id == project_id, Project.user_id == user_id).first()
        if project is None:
            raise ForeignResourceReference("Project", project_id)

    if agent_definition_id is not None:
        from src.models.agent_definition import AgentDefinition
        definition = (
            db.query(AgentDefinition)
            .filter(AgentDefinition.id == agent_definition_id, AgentDefinition.user_id == user_id)
            .first()
        )
        if definition is None:
            raise ForeignResourceReference("AgentDefinition", agent_definition_id)

    session = Session(
        user_id=user_id,
        title=title,
        model_default=model or "",
        project_id=project_id,
        agent_definition_id=agent_definition_id,
        is_temporary=is_temporary,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def get_sessions(db: DBSession, user_id: str) -> list[Session]:
    return (
        db.query(Session)
        .filter(Session.user_id == user_id, Session.deleted_at.is_(None))
        .order_by(Session.updated_at.desc())
        .all()
    )


def get_session(db: DBSession, session_id: str, user_id: str) -> Session | None:
    return (
        db.query(Session)
        .filter(
            Session.id == session_id,
            Session.user_id == user_id,
            Session.deleted_at.is_(None),
        )
        .first()
    )


def delete_session(db: DBSession, session_id: str, user_id: str) -> bool:
    """Soft-delete: marks deleted_at so the session is hidden from the UI immediately.
    Permanent removal happens via cleanup_expired_sessions() after _RETENTION_DAYS."""
    session = get_session(db, session_id, user_id)
    if not session:
        return False
    session.deleted_at = _utcnow()
    db.commit()
    return True


def cleanup_expired_sessions(db: DBSession) -> int:
    """Hard-delete sessions that have passed their retention window.

    - Regular conversations (is_temporary=False): permanently deleted 30 days after
      the user deleted them (deleted_at + 30 days).
    - Temporary conversations (is_temporary=True): permanently deleted 30 days after
      creation, regardless of deleted_at.

    Usage records survive via ON DELETE SET NULL on usage_records.session_id.
    Returns the number of sessions permanently removed.
    """
    cutoff = _utcnow() - timedelta(days=_RETENTION_DAYS)

    expired = (
        db.query(Session)
        .filter(
            # Soft-deleted regular conversations past their 30-day window
            (Session.deleted_at.isnot(None) & (Session.deleted_at < cutoff))
            # Temporary conversations older than 30 days (deleted_at may or may not be set)
            | (Session.is_temporary.is_(True) & (Session.created_at < cutoff))
        )
        .all()
    )

    count = len(expired)
    for session in expired:
        # Explicitly NULL out session_id on usage records before deleting the session
        # row, so usage data is retained for billing/TCU/audit.  This is necessary
        # because SQLite ignores FK ON DELETE SET NULL without PRAGMA foreign_keys=ON.
        db.query(UsageRecord).filter(UsageRecord.session_id == session.id).update(
            {"session_id": None}, synchronize_session="fetch"
        )
        db.delete(session)
    if count:
        db.commit()
    return count
