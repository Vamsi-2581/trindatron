"""
Execution Runtime V1 — HTTP surface for ExecutionJob (async, long-running)
and AgentSchedule (persistent-agent trigger).

Security invariants (same pattern as billing.py)
--------------------------------------------------
* user_id is ALWAYS sourced from the authenticated JWT via get_current_user
  — never accepted from the request body.
* Every job/schedule-specific endpoint enforces ownership: 404 (not 403)
  for another user's job/schedule — no existence leak, same rule
  billing.py's routes already establish.
* No TCU/cost is ever returned here — ExecutionJobResponse carries no such
  field (see schemas/execution.py); capacity/budget is entirely the
  concern of GET /users/me/capacity (users.py).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_user
from src.database import get_db
from src.models.user import User
from src.schemas.execution import (
    AgentScheduleResponse,
    CreateAgentScheduleRequest,
    CreateExecutionJobRequest,
    ExecutionJobListResponse,
    ExecutionJobResponse,
)
from src.services import agent_schedule_service, artifact_service, execution_service

router = APIRouter(prefix="/execution", tags=["execution"])


def _404_job() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution job not found")


@router.post("/jobs", response_model=ExecutionJobResponse, status_code=status.HTTP_201_CREATED, summary="Create and dispatch an execution job")
async def create_job(
    body: CreateExecutionJobRequest,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ExecutionJobResponse:
    """Returns as soon as the job is queued and dispatched — the actual
    work continues asynchronously (see execution_service.py's module
    docstring) whether or not this HTTP connection stays open. Poll
    GET /execution/jobs/{id} for status.

    Must be `async def` (not sync): execution_service.create_job() calls
    asyncio.create_task() internally, which requires a running event loop —
    FastAPI runs sync route handlers in a worker thread with no event loop
    of its own, so this would otherwise fail with "no running event loop"
    on every call. Nothing in this function body needs to `await` anything
    itself; the `async def` is solely to keep execution on the event loop.
    """
    if body.job_type == "agent_decide":
        # Persistent Agent Runtime Tier 2 — "agent_decide" is an INTERNAL
        # job_type, created ONLY by agent_schedule_service.py's own WAKE
        # cycle (which calls execution_service.create_job() directly, not
        # through this route). A user-submitted "agent_decide" job would
        # have no owning AgentSchedule to ever consume its result — it
        # would just bill the user for a decision nobody reads. Rejected
        # here, not in execution_service.create_job() itself (which the
        # scheduler calls directly and must keep accepting it).
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="job_type 'agent_decide' cannot be created directly")
    try:
        job = execution_service.create_job(
            db, user_id=current_user.id, plan=current_user.plan, user_timezone=current_user.timezone,
            agent_id=body.agent_id, session_id=body.session_id, project_id=body.project_id,
            job_type=body.job_type, payload=body.payload, priority=body.priority,
            timeout_seconds=body.timeout_seconds, max_retries=body.max_retries,
            sentinel_criteria=body.sentinel_criteria,
        )
    except execution_service.CapacityRefusedError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "capacity_refused",
                "message": "Capacidade insuficiente para esta prioridade no momento.",
                "tier": exc.tier,
            },
        )
    except execution_service.CapabilityDisabledError:
        # Same status/semantics as sessions.py's _reject_if_capability_disabled.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="This capability is temporarily disabled. Please try again later.",
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return job


@router.get("/jobs", response_model=ExecutionJobListResponse, summary="List the authenticated user's execution jobs")
def list_jobs(
    status_filter: str | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ExecutionJobListResponse:
    jobs = execution_service.list_jobs(db, current_user.id, status=status_filter, limit=limit)
    return ExecutionJobListResponse(jobs=jobs, total=len(jobs))


@router.get("/jobs/{job_id}", response_model=ExecutionJobResponse, summary="Get one execution job")
def get_job(
    job_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ExecutionJobResponse:
    job = execution_service.get_job(db, job_id, current_user.id)
    if job is None:
        raise _404_job()
    return job


@router.post("/jobs/{job_id}/cancel", response_model=ExecutionJobResponse, summary="Cancel an execution job")
def cancel_job(
    job_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ExecutionJobResponse:
    try:
        job = execution_service.cancel_job(db, job_id, current_user.id)
    except execution_service.JobNotFound:
        raise _404_job()
    return job


# ---------------------------------------------------------------------------
# Artifacts — the files a job produced (decks, exports)
# ---------------------------------------------------------------------------


@router.get("/jobs/{job_id}/artifacts", summary="List the files a job produced")
def list_job_artifacts(
    job_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """Metadata only — never file content. The bytes live in object storage
    and are reached through the download endpoint below, which is where
    authorization and URL expiry are enforced."""
    job = execution_service.get_job(db, job_id, current_user.id)
    if job is None:
        raise _404_job()
    artifacts = artifact_service.list_job_artifacts(db, job_id, current_user.id)
    return {
        "artifacts": [
            {
                "id": a.id,
                "filename": a.filename,
                "content_type": a.content_type,
                "size_bytes": a.size_bytes,
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "expires_at": a.expires_at.isoformat() if a.expires_at else None,
            }
            for a in artifacts
        ],
    }


@router.get("/artifacts/{artifact_id}/link", summary="A short-lived link to a file a job produced")
async def artifact_link(
    artifact_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> dict:
    """The same authorization as /download below, answered as JSON: a browser link cannot carry the session's
    bearer token, so the chat's file card asks here for a signed URL it can open or save directly. `url` is null
    where the store cannot sign (local development); the card then reads /download with the session instead."""
    try:
        target = await artifact_service.download_target(db, artifact_id, current_user.id)
    except artifact_service.ArtifactNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
    except artifact_service.ArtifactExpired:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="This file is no longer available.")
    return {"url": target.url, "filename": target.filename, "content_type": target.media_type}


@router.get("/artifacts/{artifact_id}/download", summary="Download a file a job produced")
async def download_artifact(
    artifact_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Authorize against the owning job, then hand back the file.

    Two shapes, decided by the storage backend rather than by the caller: a
    307 to a short-lived signed URL when the store can mint one (the bytes
    then never pass through this process), or the bytes themselves when it
    cannot. See services/artifact_service.py.

    404 covers "no such artifact" and "someone else's artifact" alike — the
    same no-existence-leak rule the job routes follow.
    """
    try:
        target = await artifact_service.download_target(db, artifact_id, current_user.id)
    except artifact_service.ArtifactNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
    except artifact_service.ArtifactExpired:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="This file is no longer available.",
        )

    if target.url is not None:
        # 307 rather than 302: preserves the method and, more importantly,
        # signals to every cache in the path that this URL is not a permanent
        # location for the resource — it stops working within minutes.
        return RedirectResponse(url=target.url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)

    return Response(
        content=target.data,
        media_type=target.media_type,
        headers={"Content-Disposition": f'attachment; filename="{target.filename}"'},
    )


# ---------------------------------------------------------------------------
# Persistent agents (AgentSchedule) — mission section 13
# ---------------------------------------------------------------------------


@router.post("/schedules", response_model=AgentScheduleResponse, status_code=status.HTTP_201_CREATED, summary="Create a persistent-agent schedule")
def create_schedule(
    body: CreateAgentScheduleRequest,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AgentScheduleResponse:
    try:
        schedule = agent_schedule_service.create_schedule(
            db, user_id=current_user.id, agent_id=body.agent_id,
            interval_seconds=body.interval_seconds, job_payload=body.job_payload,
            priority=body.priority,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return schedule


@router.get("/schedules", response_model=list[AgentScheduleResponse], summary="List the authenticated user's persistent-agent schedules")
def list_schedules(
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[AgentScheduleResponse]:
    return agent_schedule_service.list_schedules(db, current_user.id)


@router.post("/schedules/{schedule_id}/deactivate", response_model=AgentScheduleResponse, summary="Deactivate a persistent-agent schedule")
def deactivate_schedule(
    schedule_id: str,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AgentScheduleResponse:
    schedule = agent_schedule_service.deactivate_schedule(db, schedule_id, current_user.id)
    if schedule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")
    return schedule
