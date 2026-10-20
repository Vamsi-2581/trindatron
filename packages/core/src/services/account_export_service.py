"""A person's own data, as one JSON document they can download.

What is exported is what the person created or said: profile, preferences,
memories, custom skills, agents, projects (with file names), conversations and
their messages, creations (prompt and status, not the files) and scheduled
tasks. What is not exported is how TRINDATRON served them — which model
answered, provider costs, internal traces — because that is the product's
operational data, not the person's.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session as DBSession

from src.models.agent_definition import AgentDefinition
from src.models.media import MediaJob
from src.models.memory import Memory
from src.models.message import Message
from src.models.project import Project
from src.models.session import Session
from src.models.skill import Skill
from src.models.user import User


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def export_user_data(db: DBSession, user: User) -> dict:
    from src.services import preferences_service, scheduled_task_service

    sessions = db.query(Session).filter(Session.user_id == user.id).order_by(Session.created_at.asc()).all()
    conversations = []
    for session in sessions:
        messages = (
            db.query(Message).filter(Message.session_id == session.id).order_by(Message.created_at.asc()).all()
        )
        conversations.append({
            "id": session.id, "title": session.title, "project_id": session.project_id,
            "created_at": _iso(session.created_at),
            "messages": [{"role": m.role, "content": m.content, "created_at": _iso(m.created_at)} for m in messages],
        })
    projects = db.query(Project).filter(Project.user_id == user.id).all()
    return {
        "format": "trindatron-export/1",
        "exported_at": _iso(datetime.now(timezone.utc)),
        "profile": {"email": user.email, "full_name": user.full_name, "plan": user.plan,
                    "timezone": user.timezone, "created_at": _iso(user.created_at)},
        "preferences": preferences_service.get_preferences(db, user.id).model_dump(),
        "memories": [{"content": m.content, "created_at": _iso(m.created_at)}
                     for m in db.query(Memory).filter(Memory.user_id == user.id).order_by(Memory.created_at).all()],
        "skills": [{"name": s.name, "description": s.description, "instructions": s.instructions}
                   for s in db.query(Skill).filter(Skill.user_id == user.id).all()],
        "agents": [{"name": a.name, "description": a.description, "instructions": a.system_instructions}
                   for a in db.query(AgentDefinition).filter(AgentDefinition.user_id == user.id,
                                                             AgentDefinition.is_archived.is_(False)).all()],
        "projects": [{"id": p.id, "name": p.name, "description": p.description, "instructions": p.instructions,
                      "files": [f.name for f in (p.files or [])]} for p in projects],
        "conversations": conversations,
        "creations": [{"kind": j.kind, "operation": j.operation, "prompt": j.prompt, "status": j.status,
                       "created_at": _iso(j.created_at)}
                      for j in db.query(MediaJob).filter(MediaJob.user_id == user.id).order_by(MediaJob.created_at).all()],
        "scheduled_tasks": [
            {key: value for key, value in scheduled_task_service.task_view(db, row).items() if key != "last_run"}
            for row in scheduled_task_service.list_tasks(db, user)
        ],
    }
