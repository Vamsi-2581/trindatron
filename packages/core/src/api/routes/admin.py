"""Admin routes — Etapa 5H (Kill Switches + Runtime Settings).

Every route here requires get_current_admin_user() (src/api/deps.py) —
the same JWT/get_current_user mechanism as every other authenticated
route, gated by User.is_admin. There is no client-facing way to reach
these without a token belonging to an is_admin=True user; a regular
user's token gets a 403 (see get_current_admin_user's own docstring).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.api.deps import get_current_admin_user
from src.database import get_db
from src.entitlements import get_plan_entitlement
from src.models.user import User
from src.services.entitlement_service import get_admin_usage_windows
from src.schemas.admin import RuntimeSettingsResponse, UpdateRuntimeSettingRequest
from src.services.runtime_settings import (
    KNOWN_KEYS,
    InvalidSettingValueError,
    UnknownSettingError,
    get_all_settings,
    set_setting,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/settings", response_model=RuntimeSettingsResponse)
def get_settings(
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin_user),
):
    """Effective (fail-safe-resolved) value for every known runtime
    setting — never raises even if a row is missing/corrupted, since
    get_all_settings() already applies the same fail-safe defaults every
    other read path in Core uses."""
    return RuntimeSettingsResponse(settings=get_all_settings(db))


@router.put("/settings/{key}", response_model=RuntimeSettingsResponse)
def update_setting(
    key: str,
    body: UpdateRuntimeSettingRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin_user),
):
    """Validate + persist one setting, then return the full updated
    snapshot (same shape as GET) so the caller doesn't need a second
    request to confirm what actually took effect."""
    try:
        set_setting(db, key, body.value, admin_user_id=admin.id)
    except UnknownSettingError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown setting key: {key!r}. Known keys: {sorted(KNOWN_KEYS)}",
        )
    except InvalidSettingValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )
    return RuntimeSettingsResponse(settings=get_all_settings(db))


# ── Usage windows ────────────────────────────────────────────────────────
#
# Operators need to answer two questions that customer support cannot:
# "why did this user get a 429" and "who shares this team's pool". Both are
# admin-only for the same reason the metrics endpoint is: the figures are
# internal accounting, not product surface.


class TeamAssignment(BaseModel):
    """null clears the assignment, returning the user to an individual pool."""
    team_id: str | None = Field(default=None, max_length=36)


@router.get("/users/{user_id}/usage-windows")
def admin_usage_windows(
    user_id: str,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin_user),
):
    """Session / daily / weekly / team-pool usage for one user."""
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return get_admin_usage_windows(db, user_id=user.id, plan=user.plan,
                                   user_timezone=user.timezone)


@router.put("/users/{user_id}/team")
def admin_set_team(
    user_id: str,
    body: TeamAssignment,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin_user),
):
    """Put a user into (or take them out of) a shared TEAM pool.

    Membership is assigned here rather than through a self-serve team
    feature because the pool only needs to know who shares a budget —
    invites, roles and seat management are a separate product surface that
    does not exist yet, and inventing half of it here would be worse than
    an explicit admin action.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if body.team_id and body.team_id != user.team_id:
        # The pool bounds AI spend however many members share it, but not
        # the accounts the infrastructure serves; TEAM is priced at a headcount.
        cap = get_plan_entitlement("team").team_max_members
        members = db.query(User).filter(User.team_id == body.team_id).count()
        if cap is not None and members >= cap:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Team {body.team_id!r} already has {members} members (limit {cap}).",
            )
    user.team_id = body.team_id or None
    db.commit()
    logger.info("admin: team assignment | user=%s team=%s", user_id, user.team_id)
    return {"user_id": user.id, "team_id": user.team_id}


# ── Economics ────────────────────────────────────────────────────────────


@router.get("/economics")
def admin_economics(
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin_user),
):
    """Where provider money is going, from persisted usage.

    Spend for today / this week / this month by plan, provider and model; the
    month-to-date cost of an average account on each plan; and every spend
    threshold currently crossed (src/services/spend_guard.py). Read-only —
    containment is not decided here.
    """
    from dataclasses import asdict

    from src.config import settings
    from src.services import spend_guard

    thresholds = spend_guard.SpendThresholds()
    return {
        "spend": spend_guard.spend_summary(db),
        "accounts_by_plan": spend_guard.accounts_by_plan(db),
        "month_cost_per_account_usd": spend_guard.month_cost_per_account_usd(db),
        "free_plan_daily_budget_usd": settings.free_plan_global_daily_budget_usd,
        "thresholds_usd": asdict(thresholds),
        "alerts": [asdict(a) for a in spend_guard.evaluate(db, thresholds=thresholds)],
    }


@router.post("/users/{user_id}/revoke-sessions")
def admin_revoke_sessions(
    user_id: str,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin_user),
):
    """Sign a user out of every device. Their refresh tokens stop working at
    once; access tokens already issued expire within access_token_minutes."""
    from src.services.session_token_service import revoke_all_sessions

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    revoked = revoke_all_sessions(db, user.id, reason="admin")
    logger.info("admin: sessions revoked | user=%s tokens=%d", user.id, revoked)
    return {"user_id": user.id, "revoked_tokens": revoked}
