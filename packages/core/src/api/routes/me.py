"""Settings a person controls: preferences, plugins/connectors/extensions, their data, their memories.

Everything here is scoped to the authenticated person and returns abstract
names only — never a provider, a model or a cost.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_user
from src.database import get_db
from src.integrations.catalog import PLUGIN_IDS, integrations_view
from src.models.memory import Memory
from src.models.user import User
from src.schemas.preferences import Preferences, PreferencesUpdate
from src.services import preferences_service
from src.services.account_export_service import export_user_data

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me/preferences", response_model=Preferences)
def get_preferences(db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    return preferences_service.get_preferences(db, current_user.id)


@router.patch("/me/preferences", response_model=Preferences)
def update_preferences(body: PreferencesUpdate, db: DBSession = Depends(get_db),
                       current_user: User = Depends(get_current_user)):
    try:
        return preferences_service.update_preferences(db, current_user.id, body)
    except preferences_service.PreferencesRefused as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail={"error": "invalid_request", "message": str(exc)}) from exc


def _integrations(db: DBSession, user: User) -> dict:
    from src.capability import matrix as capability_matrix

    return integrations_view(capability_matrix.snapshot(db, user), preferences_service.disabled_plugins(db, user.id))


@router.get("/me/integrations")
def get_integrations(db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    return _integrations(db, current_user)


class PluginToggle(BaseModel):
    enabled: bool


@router.patch("/me/integrations/plugins/{plugin_id}")
def toggle_plugin(plugin_id: str, body: PluginToggle, db: DBSession = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    if plugin_id not in PLUGIN_IDS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "not_found"})
    disabled = preferences_service.disabled_plugins(db, current_user.id)
    disabled = disabled - {plugin_id} if body.enabled else disabled | {plugin_id}
    preferences_service.update_preferences(db, current_user.id, PreferencesUpdate(disabled_plugins=sorted(disabled)))
    view = _integrations(db, current_user)
    return next(item for item in view["plugins"] if item["id"] == plugin_id)


@router.get("/me/export")
def export_my_data(db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    body = json.dumps(export_user_data(db, current_user), ensure_ascii=False, indent=2)
    return Response(content=body, media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="trindatron-export.json"',
                             "Cache-Control": "no-store"})


@router.get("/me/subscriptions")
def list_my_subscriptions(db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    """The person's own subscriptions: plan, price, status and renewal — never payment-processor identifiers.

    Cancelling uses the existing POST /billing/subscriptions/{id}/cancel.
    """
    from src.models.subscription import Subscription

    rows = (
        db.query(Subscription)
        .filter(Subscription.user_id == current_user.id)
        .order_by(Subscription.created_at.desc())
        .all()
    )

    def iso(value):
        return value.isoformat() if value else None

    return {"subscriptions": [{
        "id": row.id, "plan": row.plan, "status": row.status, "amount": str(row.amount), "currency": row.currency,
        "payment_method": row.payment_method, "current_period_end": iso(row.current_period_end),
        "next_billing_at": iso(row.next_billing_at), "cancelled_at": iso(row.cancelled_at),
    } for row in rows]}


@router.delete("/me/memories")
def clear_my_memories(db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    deleted = db.query(Memory).filter(Memory.user_id == current_user.id).delete(synchronize_session=False)
    db.commit()
    return {"deleted": deleted}
