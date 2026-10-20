"""The presentation capability's HTTP surface.

Two endpoints, and the split between them is the point:

  GET  /presentations/capability   can this user make a deck right now, and
                                   how large a one
  POST /presentations             make it

Discovery is separate from creation because the honest answer to "can I do
this" is sometimes no, and the user should learn that before writing a brief
rather than after waiting for a job to fail. A capability whose engine is
asleep, unconfigured or out of quota is not offered as ready — that is
mission-level behaviour, not a nicety: advertising a capability that then
fails is worse than not advertising it.

Everything below speaks in decks. Nothing here mentions Presenton, Gemini,
R2, Docker or TCU, and no response carries a cost figure — the same rule
execution.py already documents.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_user
from src.capability.classifier import Capability
from src.database import get_db
from src.models.user import User
from src.schemas.execution import ExecutionJobResponse
from src.services import execution_service, presentation_service
from src.services.entitlement_service import remaining_budget_tcu
from src.services.runtime_settings import is_capability_disabled

logger = logging.getLogger("trinity.presentation.api")

router = APIRouter(prefix="/presentations", tags=["presentations"])

# A deck below this many slides is not a presentation, and above it is a
# document. Bounds the request before any estimate is computed.
MIN_SLIDES = 3
MAX_SLIDES = 60


class CreatePresentationRequest(BaseModel):
    brief: str = Field(..., min_length=10, max_length=8000)
    n_slides: int = Field(10, ge=MIN_SLIDES, le=MAX_SLIDES)
    language: str = Field("English", max_length=40)
    export_as: str = Field("pptx", pattern="^(pptx|pdf)$")
    web_search: bool = False
    instructions: str | None = Field(None, max_length=2000)
    session_id: str | None = None
    project_id: str | None = None


class CapabilityStatusResponse(BaseModel):
    available: bool
    # Why it is not available, in words a user can act on. Never names
    # infrastructure — "the engine container is unreachable" is an operator's
    # sentence, not a customer's.
    message: str = ""
    max_slides: int = 0


def _user_facing_reason(available: bool, allowed: bool, affordable: int) -> str:
    if not allowed:
        return "Presentations are available on paid plans."
    if not available:
        return "Presentations are temporarily unavailable. Please try again shortly."
    if affordable < MIN_SLIDES:
        return (
            "There is not enough capacity left in your current usage window "
            "to build a presentation."
        )
    return ""


@router.get("/capability", response_model=CapabilityStatusResponse,
            summary="Whether a presentation can be created right now")
async def capability_status(
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CapabilityStatusResponse:
    """Plan, kill switch, engine health, storage and remaining budget — all
    four, because any one of them being wrong makes the answer no."""
    allowed = presentation_service.plan_allows_presentations(current_user.plan)
    if allowed and is_capability_disabled(db, Capability.PRESENTATION.value):
        allowed = False

    available = False
    if allowed:
        available = (await presentation_service.availability()).available

    affordable = 0
    if available:
        remaining = remaining_budget_tcu(
            db, current_user.id, current_user.plan, current_user.timezone
        )
        affordable = (
            MAX_SLIDES
            if remaining is None
            else presentation_service.largest_affordable_deck(
                plan=current_user.plan, remaining_tcu=remaining
            )
        )
        affordable = min(affordable, MAX_SLIDES)

    ready = bool(available and affordable >= MIN_SLIDES)
    return CapabilityStatusResponse(
        available=ready,
        message=_user_facing_reason(available, allowed, affordable),
        max_slides=affordable if ready else 0,
    )


@router.post("", response_model=ExecutionJobResponse,
             status_code=status.HTTP_202_ACCEPTED,
             summary="Create a presentation")
async def create_presentation(
    body: CreatePresentationRequest,
    db: DBSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ExecutionJobResponse:
    """Queue a deck and return immediately.

    202, not 201: generation takes minutes and the response describes work
    that has been accepted, not a presentation that exists. The client polls
    GET /execution/jobs/{id} and collects the file from the artifacts
    endpoint when it completes.

    Every refusal below happens BEFORE the engine is woken. That ordering is
    the whole economic argument for pre-flight: once generation starts, the
    model calls are paid for whether or not the result can be delivered.
    """
    if not presentation_service.plan_allows_presentations(current_user.plan):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Presentations are available on paid plans.",
        )
    if is_capability_disabled(db, Capability.PRESENTATION.value):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="This capability is temporarily disabled. Please try again later.",
        )

    engine = await presentation_service.availability()
    if not engine.available:
        # Logged with the operator's reason, answered without it.
        logger.info("presentation: create refused | reason=%s", engine.reason)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Presentations are temporarily unavailable. Please try again shortly.",
        )

    remaining = remaining_budget_tcu(
        db, current_user.id, current_user.plan, current_user.timezone
    )
    if remaining is not None:
        affordable = presentation_service.largest_affordable_deck(
            plan=current_user.plan, remaining_tcu=remaining
        )
        if affordable < MIN_SLIDES:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "insufficient_capacity",
                    "message": (
                        "There is not enough capacity left in your current usage "
                        "window to build a presentation."
                    ),
                },
            )

    payload = {
        # The step's command IS the brief — the same provider-agnostic reuse
        # of `command` that model_router_provider.py documents, so the
        # worker's step loop needs no presentation-specific branch.
        "steps": [{"command": body.brief}],
        "presentation": {
            "n_slides": body.n_slides,
            "language": body.language,
            "export_as": body.export_as,
            "web_search": body.web_search,
            "instructions": body.instructions,
        },
    }

    try:
        job = execution_service.create_job(
            db,
            user_id=current_user.id,
            plan=current_user.plan,
            user_timezone=current_user.timezone,
            session_id=body.session_id,
            project_id=body.project_id,
            job_type="presentation",
            payload=payload,
            priority="normal",
            # Generation is minutes long and the engine may be cold-starting
            # on top of that. The default 300s would abort a perfectly
            # healthy deck.
            timeout_seconds=1800,
            # One retry, not the default two. A retry regenerates the whole
            # deck at full cost, so the second attempt is worth having for a
            # transient engine failure and a third is just paying twice more
            # for the same bad outcome.
            max_retries=1,
        )
    except execution_service.CapacityRefusedError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "capacity_refused",
                "message": "There is not enough capacity right now. Please try again shortly.",
                "tier": exc.tier,
            },
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    return job
