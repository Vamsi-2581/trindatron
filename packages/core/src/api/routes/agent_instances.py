"""Persistent Agent Instance V1 — HTTP surface.

Deliberately NOT mounted at `/agents` — that path already serves
AgentDefinition CRUD (src/api/routes/agent_definitions.py, and the
existing packages/web/src/app/(app)/agents page), a materially different
resource (a reusable template, not a live, running agent). Colliding with
it would either break the existing AgentDefinition API or require
renaming it — both are exactly the kind of "reabrir uma auditoria já
concluída" this phase was told not to do. `/agent-instances` is a new,
non-colliding path for the new resource; see docs/PERSISTENT_AGENTS.md
for the full rationale.

Same security conventions as billing.py/execution.py: user_id always
from the JWT (never the body), 404 (not 403) for another user's
instance — no existence leak.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_user
from src.database import get_db
from src.models.agent_instance import AgentInstance
from src.models.agent_schedule import AgentSchedule
from src.models.execution import ExecutionJob
from src.models.user import User
from src.schemas.agent_instance import (
    AgentInstanceListResponse,
    AgentInstanceResponse,
    CreateAgentInstanceRequest,
    UpdateAgentInstanceRequest,
)
from src.schemas.execution import ExecutionJobResponse
from src.services import agent_instance_service, execution_service

router = APIRouter(prefix="/agent-instances", tags=["agent-instances"])


def _to_response(db: DBSession, instance: AgentInstance) -> AgentInstanceResponse:
    """Adds the derived, non-column fields (health, next_run_at, current
    job status) — none of these are stored redundantly on AgentInstance
    itself (see models/agent_instance.py's docstring on why)."""
    next_run_at = (
        db.query(AgentSchedule.next_run_at)
        .filter(AgentSchedule.agent_instance_id == instance.id, AgentSchedule.is_active.is_(True))
        .order_by(AgentSchedule.next_run_at.asc())
        .limit(1)
        .scalar()
    )
    current_job_status = None
    if instance.last_execution_job_id:
        job = db.query(ExecutionJob).filter(ExecutionJob.id == instance.last_execution_job_id).first()
        if job is not None:
            current_job_status = job.status
    # Tier 2 observability (mission item 18) — the most-recently-decided
    # active schedule's own last_decision, e.g. "acted"/"decide_pending"/
    # "decide_skip". An instance can have >1 schedule; picking the most
    # recent is a reasonable single summary value without inventing a new
    # per-instance column (see agent_instance.py's docstring on avoiding
    # duplicated derivable state).
    last_decision = (
        db.query(AgentSchedule.last_decision)
        .filter(AgentSchedule.agent_instance_id == instance.id, AgentSchedule.last_decision.isnot(None))
        .order_by(AgentSchedule.last_decision_at.desc())
        .limit(1)
        .scalar()
    )
    return AgentInstanceResponse(
        id=instance.id, user_id=instance.user_id, agent_definition_id=instance.agent_definition_id,
        project_id=instance.project_id, name=instance.name, status=instance.status,
        health=agent_instance_service.derive_health(instance),
        failure_count=instance.failure_count, last_error=instance.last_error,
        last_run_at=instance.last_run_at, next_run_at=next_run_at,
        last_execution_job_id=instance.last_execution_job_id, current_execution_job_status=current_job_status,
        last_decision=last_decision,
        created_at=instance.created_at, updated_at=instance.updated_at,
    )


def _404() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent instance not found")


@router.post("", response_model=AgentInstanceResponse, status_code=status.HTTP_201_CREATED)
def create_instance(
    body: CreateAgentInstanceRequest,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        instance = agent_instance_service.create_agent_instance(
            db, user_id=current_user.id, agent_definition_id=body.agent_definition_id,
            name=body.name, project_id=body.project_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return _to_response(db, instance)


@router.get("", response_model=AgentInstanceListResponse)
def list_instances(
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    instances = agent_instance_service.list_agent_instances(db, current_user.id)
    return AgentInstanceListResponse(instances=[_to_response(db, i) for i in instances], total=len(instances))


@router.get("/{instance_id}", response_model=AgentInstanceResponse)
def get_instance(
    instance_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    instance = agent_instance_service.get_agent_instance(db, instance_id, current_user.id)
    if instance is None:
        raise _404()
    return _to_response(db, instance)


@router.patch("/{instance_id}", response_model=AgentInstanceResponse)
def update_instance(
    instance_id: str,
    body: UpdateAgentInstanceRequest,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    fields_set = body.model_fields_set
    try:
        instance = agent_instance_service.update_agent_instance(
            db, instance_id, current_user.id,
            name=body.name,
            project_id=body.project_id if "project_id" in fields_set else ...,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    if instance is None:
        raise _404()
    return _to_response(db, instance)


@router.post("/{instance_id}/pause", response_model=AgentInstanceResponse)
def pause_instance(
    instance_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    instance = agent_instance_service.pause_agent_instance(db, instance_id, current_user.id)
    if instance is None:
        raise _404()
    return _to_response(db, instance)


@router.post("/{instance_id}/resume", response_model=AgentInstanceResponse)
def resume_instance(
    instance_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    instance = agent_instance_service.resume_agent_instance(db, instance_id, current_user.id)
    if instance is None:
        raise _404()
    return _to_response(db, instance)


@router.post("/{instance_id}/run", response_model=ExecutionJobResponse, status_code=status.HTTP_201_CREATED)
def run_instance(
    instance_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        job = agent_instance_service.manual_run(db, instance_id=instance_id, user_id=current_user.id)
    except agent_instance_service.AgentInstanceNotFound:
        raise _404()
    except agent_instance_service.NoScheduleForInstance:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="This agent has no active schedule to run")
    except agent_instance_service.InstanceNotRunnable as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Agent is {exc.status!r}, not runnable")
    except agent_instance_service.InstanceJobAlreadyActive:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This agent already has an active execution job")
    except execution_service.CapacityRefusedError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "capacity_refused", "message": "Capacidade insuficiente para esta prioridade no momento.", "tier": exc.tier},
        )
    return job
