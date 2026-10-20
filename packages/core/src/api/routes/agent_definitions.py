from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from src.api.deps import get_current_user
from src.api.routes.projects import _get_project_or_404
from src.database import get_db
from src.models.user import User
from src.schemas.agent_definition import (
    AgentDefinitionListResponse,
    AgentDefinitionResponse,
    CreateAgentDefinitionRequest,
    ProjectAgentResponse,
    UpdateAgentDefinitionRequest,
)
from src.services.agent_definition_service import (
    add_agent_to_project,
    archive_agent_definition,
    create_agent_definition,
    get_agent_definition,
    list_agent_definitions,
    list_project_agents,
    remove_agent_from_project,
    update_agent_definition,
)

router = APIRouter(tags=["agents"])


# ── Agent Definitions CRUD ────────────────────────────────────────────────────

@router.get("/agents", response_model=AgentDefinitionListResponse)
def list_agents(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agents = list_agent_definitions(db, current_user.id)
    return AgentDefinitionListResponse(agents=agents, total=len(agents))


@router.post("/agents", response_model=AgentDefinitionResponse, status_code=status.HTTP_201_CREATED)
def create_agent(
    body: CreateAgentDefinitionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        agent = create_agent_definition(
            db,
            user_id=current_user.id,
            name=body.name,
            skill_ids=body.skill_ids,
            description=body.description,
            avatar_emoji=body.avatar_emoji,
            system_instructions=body.system_instructions,
            model_preference=body.model_preference,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return agent


@router.get("/agents/{agent_id}", response_model=AgentDefinitionResponse)
def get_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    agent = get_agent_definition(db, agent_id, current_user.id)
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return agent


@router.patch("/agents/{agent_id}", response_model=AgentDefinitionResponse)
def update_agent(
    agent_id: str,
    body: UpdateAgentDefinitionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        updated = update_agent_definition(
            db,
            agent_id=agent_id,
            user_id=current_user.id,
            **body.model_dump(exclude_unset=True),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")
    return updated


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    archived = archive_agent_definition(db, agent_id, current_user.id)
    if not archived:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")


# ── Project ↔ Agent associations ──────────────────────────────────────────────

@router.get("/projects/{project_id}/agents", response_model=list[AgentDefinitionResponse])
def list_agents_in_project(
    project_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project_or_404(db, project_id, current_user.id)
    agents = list_project_agents(db, project_id)
    return agents


@router.post(
    "/projects/{project_id}/agents/{agent_id}",
    response_model=ProjectAgentResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_agent_to_project_route(
    project_id: str,
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project_or_404(db, project_id, current_user.id)
    pa = add_agent_to_project(db, project_id, agent_id, current_user.id)
    if not pa:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Agent not found or does not belong to you",
        )
    return pa


@router.delete("/projects/{project_id}/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_agent_from_project_route(
    project_id: str,
    agent_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_project_or_404(db, project_id, current_user.id)
    removed = remove_agent_from_project(db, project_id, agent_id, current_user.id)
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Association not found")
