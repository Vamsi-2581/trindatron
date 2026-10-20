from __future__ import annotations

from sqlalchemy.orm import Session as DBSession

from src.models.agent_definition import AgentDefinition
from src.models.project_agent import ProjectAgent
from src.services.skill_service import validate_skill_ids_for_user


def _get_agent_or_none(db: DBSession, agent_id: str, user_id: str) -> AgentDefinition | None:
    return (
        db.query(AgentDefinition)
        .filter(AgentDefinition.id == agent_id, AgentDefinition.user_id == user_id)
        .first()
    )


def list_agent_definitions(db: DBSession, user_id: str) -> list[AgentDefinition]:
    return (
        db.query(AgentDefinition)
        .filter(AgentDefinition.user_id == user_id, AgentDefinition.is_archived == False)
        .order_by(AgentDefinition.created_at.desc())
        .all()
    )


def create_agent_definition(
    db: DBSession,
    user_id: str,
    name: str,
    skill_ids: list[str] | None = None,
    **kwargs,
) -> AgentDefinition:
    resolved_skill_ids = skill_ids or []
    validate_skill_ids_for_user(db, resolved_skill_ids, user_id)
    agent = AgentDefinition(
        user_id=user_id,
        name=name,
        skill_ids=resolved_skill_ids,
        description=kwargs.get("description"),
        avatar_emoji=kwargs.get("avatar_emoji"),
        system_instructions=kwargs.get("system_instructions"),
        model_preference=kwargs.get("model_preference", "balanced"),
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent


def get_agent_definition(db: DBSession, agent_id: str, user_id: str) -> AgentDefinition | None:
    return _get_agent_or_none(db, agent_id, user_id)


def update_agent_definition(
    db: DBSession, agent_id: str, user_id: str, **updates
) -> AgentDefinition | None:
    agent = _get_agent_or_none(db, agent_id, user_id)
    if not agent:
        return None
    if "skill_ids" in updates and updates["skill_ids"] is not None:
        validate_skill_ids_for_user(db, updates["skill_ids"], user_id)
    for key, value in updates.items():
        if value is not None or key in ("skill_ids", "system_instructions", "description", "avatar_emoji"):
            setattr(agent, key, value)
    db.commit()
    db.refresh(agent)
    return agent


def archive_agent_definition(db: DBSession, agent_id: str, user_id: str) -> bool:
    agent = _get_agent_or_none(db, agent_id, user_id)
    if not agent:
        return False
    agent.is_archived = True
    db.commit()
    return True


# ── Project ↔ Agent association ───────────────────────────────────────────────

def add_agent_to_project(
    db: DBSession,
    project_id: str,
    agent_definition_id: str,
    project_owner_id: str,
) -> ProjectAgent | None:
    """
    Associate an agent with a project.
    Validates that:
    - The agent exists and belongs to project_owner_id
    - No duplicate association exists
    """
    agent = _get_agent_or_none(db, agent_definition_id, project_owner_id)
    if not agent:
        return None

    existing = (
        db.query(ProjectAgent)
        .filter(
            ProjectAgent.project_id == project_id,
            ProjectAgent.agent_definition_id == agent_definition_id,
        )
        .first()
    )
    if existing:
        return existing

    pa = ProjectAgent(project_id=project_id, agent_definition_id=agent_definition_id)
    db.add(pa)
    db.commit()
    db.refresh(pa)
    return pa


def remove_agent_from_project(
    db: DBSession,
    project_id: str,
    agent_definition_id: str,
    project_owner_id: str,
) -> bool:
    """Remove agent association from project (validates agent ownership)."""
    agent = _get_agent_or_none(db, agent_definition_id, project_owner_id)
    if not agent:
        return False
    pa = (
        db.query(ProjectAgent)
        .filter(
            ProjectAgent.project_id == project_id,
            ProjectAgent.agent_definition_id == agent_definition_id,
        )
        .first()
    )
    if not pa:
        return False
    db.delete(pa)
    db.commit()
    return True


def list_project_agents(
    db: DBSession,
    project_id: str,
) -> list[AgentDefinition]:
    """Return AgentDefinitions associated with a project (non-archived)."""
    rows = (
        db.query(ProjectAgent)
        .filter(ProjectAgent.project_id == project_id)
        .all()
    )
    agent_ids = [r.agent_definition_id for r in rows]
    if not agent_ids:
        return []
    return (
        db.query(AgentDefinition)
        .filter(AgentDefinition.id.in_(agent_ids), AgentDefinition.is_archived == False)
        .all()
    )
