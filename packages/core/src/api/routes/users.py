from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from src.api.deps import get_current_user
from src.database import get_db
from src.entitlements import entitlement_to_dict, get_plan_entitlement
from src.models.user import User
from src.schemas.execution import CapacityResponse
from src.schemas.user import TimezoneResponse, UpdateTimezoneRequest
from src.services import adaptive_budget_service, agent_instance_service
from src.services.entitlement_service import get_user_period_usage, get_user_week_usage
from src.services.session_window_service import get_session_status

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me/usage")
def user_usage(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Return daily, weekly, and monthly TCU usage with limits.

    Response shape:
    {
      "plan": "start",
      "daily":   {"used": 45.2, "limit": 100.0,  "remaining": 54.8},
      "weekly":  {"used": 120.0, "limit": 500.0, "remaining": 380.0},
      "monthly": {"used": 312.7, "limit": 1000.0, "remaining": 687.3}
    }

    For unlimited tiers (max, team): limit and remaining are null.
    remaining is never negative.
    """
    return get_user_period_usage(db, user_id=current_user.id, plan=current_user.plan, user_timezone=current_user.timezone)


@router.get("/me/usage/week")
def user_usage_week(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Return TCU usage for the current weekly window (Sunday 12:00 in the
    user's local IANA timezone — see src/timezones.py)."""
    return get_user_week_usage(db, user_id=current_user.id, plan=current_user.plan, user_timezone=current_user.timezone)


@router.patch("/me/timezone", response_model=TimezoneResponse)
def update_my_timezone(
    body: UpdateTimezoneRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Persist the IANA timezone detected by the browser
    (Intl.DateTimeFormat().resolvedOptions().timeZone).

    Drives the weekly reset rule (Sunday 12:00 local) — never the server's
    timezone. Idempotent: a no-op write is skipped if the value is already
    current, so the frontend calling this on every authenticated page load
    doesn't generate redundant writes.
    """
    if current_user.timezone != body.timezone:
        current_user.timezone = body.timezone
        db.commit()
        db.refresh(current_user)
    return TimezoneResponse(timezone=current_user.timezone)


@router.get("/me/session")
def user_session_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Read-only status of the user's 5-hour rolling entitlement session.

    Response shape:
    {
      "active": true,
      "session_started_at": "2026-08-31T14:37:22+00:00",
      "session_expires_at": "2026-08-31T19:37:22+00:00"
    }

    NEVER starts or extends a session — that only happens as a side effect
    of check_and_reserve() on an actual TCU-reserving operation (see
    src/services/session_window_service.py). If the user has never
    performed one, active is false and both timestamps are null.

    The backend remains the authority: the frontend computes a countdown
    from session_expires_at for display only, never to authorize anything.
    """
    now = datetime.now(timezone.utc)
    return get_session_status(db, user_id=current_user.id, now=now)


@router.get("/me/entitlement")
def user_entitlement(current_user: User = Depends(get_current_user)):
    """
    Return the full entitlement for the authenticated user's plan.

    Response shape:
    {
      "plan": "start",
      "quota": {
        "daily_tcu_limit": 100.0,
        "weekly_tcu_limit": 500.0,
        "monthly_tcu_limit": 1000.0,
        "max_tcu_per_request": 10.0
      },
      "features": {
        "supports_agent_mode": true,
        "supports_trinity_code": true,
        "supports_trinity_work": false,
        "supports_image_generation": false
      },
      "limits": {
        "max_projects": 10,
        "max_agents": 5
      }
    }

    null values in quota/limits mean unlimited (max/team plans).
    """
    entitlement = get_plan_entitlement(current_user.plan)
    return entitlement_to_dict(entitlement)


@router.get("/me/capacity", response_model=CapacityResponse)
def user_capacity(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """User-facing usage summary — Execution Runtime V1 (mission section 11).

    Deliberately NEVER exposes TCU, tokens, cost, or any internal unit —
    only percentages, a renewal instant, and counts. See
    src/services/adaptive_budget_service.py for exactly how each figure is
    derived from the existing TCU/entitlement engine (GET /users/me/usage
    above remains the TCU-denominated endpoint, unchanged, for any
    internal/admin surface that still needs it).

    Response shape:
    {
      "tier": "normal",
      "capacity_remaining_pct": 62.0,
      "session": {"active": true, "pct_used": 38.0, "resets_at": "2026-09-03T19:37:22+00:00"},
      "weekly": {"pct_used": 38.0, "resets_at": "2026-09-06T13:00:00+00:00"},
      "active_agents": 2,
      "optimizing": false
    }

    active_agents (Persistent Agent Instance V1 — semantics corrected):
    count of the user's AgentInstance rows with status == "active" — NOT
    a count of currently-running ExecutionJobs, which is what this used
    to report (an active instance idling between wakes still counts; a
    raw ExecutionJob with no owning AgentInstance never counted and still
    doesn't). See agent_instance_service.count_active_instances().
    """
    snapshot = adaptive_budget_service.compute_capacity(
        db, user_id=current_user.id, plan=current_user.plan, user_timezone=current_user.timezone,
    )
    active_agents = agent_instance_service.count_active_instances(db, current_user.id)
    return CapacityResponse(
        tier=snapshot.tier,
        capacity_remaining_pct=snapshot.capacity_remaining_pct,
        session={
            "active": snapshot.session_active,
            "pct_used": snapshot.session_pct_used,
            "resets_at": snapshot.session_resets_at,
        },
        weekly={
            "pct_used": snapshot.weekly_pct_used,
            "resets_at": snapshot.weekly_resets_at,
        },
        active_agents=active_agents,
        optimizing=snapshot.tier != adaptive_budget_service.TIER_NORMAL,
    )
