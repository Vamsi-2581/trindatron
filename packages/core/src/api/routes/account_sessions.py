"""Where a person is signed in, and signing out of one device or all of them.

Built on the refresh-token families of src/services/session_token_service.py:
one family per sign-in. Revoking a family ends that device's session at its next
refresh (access tokens already issued expire on their own short clock).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_user
from src.services.auth_service import session_id_from_token
from src.config import settings
from src.database import get_db
from src.models.user import User
from src.services import session_token_service

router = APIRouter(prefix="/auth/sessions", tags=["auth"])


@router.get("")
def list_my_sessions(request: Request, db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    # The web client's own refresh token travels as an httpOnly cookie; it marks "this device".
    current = request.cookies.get(settings.refresh_cookie_name)
    authorization = request.headers.get("authorization") or ""
    bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else None
    return {"sessions": session_token_service.list_active_sessions(
        db, current_user.id, current_refresh_token=current, current_session_id=session_id_from_token(bearer),
    )}


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_my_session(session_id: str, db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    if not session_token_service.revoke_user_session(db, current_user.id, session_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "not_found"})


@router.post("/revoke-all", status_code=status.HTTP_204_NO_CONTENT)
def sign_out_everywhere(db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    session_token_service.revoke_all_sessions(db, current_user.id, reason="user_signout_all")
    done = Response(status_code=status.HTTP_204_NO_CONTENT)
    done.delete_cookie(key=settings.refresh_cookie_name, path="/auth", domain=settings.refresh_cookie_domain or None,
                       secure=settings.refresh_cookie_secure, httponly=True, samesite=settings.refresh_cookie_samesite)
    return done
