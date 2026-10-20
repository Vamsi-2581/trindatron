"""Account security routes: password recovery, password change, email
confirmation and the security log. Mounted under /auth by auth.py.

The rules live in src/services/account_security_service.py; this module adds
rate limits and turns its results into HTTP. Email is always sent after the
response (a background task), so the time an answer takes never says whether
an address has an account.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from src.api.deps import get_current_user
from src.api.routes.auth import _client_ip
from src.config import settings
from src.database import get_db
from src.models.user import User
from src.schemas.auth import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    ResetPasswordRequest,
    SecurityEventResponse,
    VerifyEmailRequest,
)
from src.services import account_security_service as security
from src.services import email_service
from src.services.request_control import InMemoryRateLimiter

logger = logging.getLogger(__name__)

router = APIRouter()

_email_ip_rate_limiter = InMemoryRateLimiter()
_email_address_rate_limiter = InMemoryRateLimiter()
_change_password_rate_limiter = InMemoryRateLimiter()

#: What "forgot password" always answers, account or not.
FORGOT_ACCEPTED = {"detail": "If this address has an account, a reset link is on its way."}
INVALID_LINK = "This link is invalid or has expired. Please request a new one."


async def _limit_network(request: Request) -> None:
    client_ip = _client_ip(request)
    if client_ip is None:
        return
    decision = await _email_ip_rate_limiter.check(
        f"auth_email_ip:{client_ip}",
        limit=settings.auth_email_ip_rate_limit,
        window_seconds=settings.auth_email_ip_rate_window_s,
    )
    if not decision.allowed:
        logger.warning("auth: email rate limit exceeded (ip) | client_ip=%s", client_ip)
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many requests. Please try again later.")


async def _address_allowed(address: str) -> bool:
    decision = await _email_address_rate_limiter.check(
        f"auth_email_address:{address.strip().lower()}",
        limit=settings.auth_email_address_rate_limit,
        window_seconds=settings.auth_email_address_rate_window_s,
    )
    return decision.allowed


@router.post("/password/forgot", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(body: ForgotPasswordRequest, request: Request, background: BackgroundTasks,
                          db: Session = Depends(get_db)) -> dict:
    await _limit_network(request)
    if not await _address_allowed(body.email):
        logger.info("auth: reset link not sent, address limit reached")
        return FORGOT_ACCEPTED
    message = security.request_password_reset(
        db, body.email, ip=_client_ip(request), user_agent=request.headers.get("user-agent"),
    )
    if message is not None:
        background.add_task(email_service.deliver, message)
    return FORGOT_ACCEPTED


@router.post("/password/reset")
async def reset_password(body: ResetPasswordRequest, request: Request, background: BackgroundTasks,
                         db: Session = Depends(get_db)) -> dict:
    await _limit_network(request)
    try:
        notice = security.reset_password(
            db, body.token, body.password, ip=_client_ip(request), user_agent=request.headers.get("user-agent"),
        )
    except security.InvalidLink:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=INVALID_LINK)
    background.add_task(email_service.deliver, notice)
    return {"detail": "Password updated. Please sign in with your new password."}


@router.post("/password/change", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(body: ChangePasswordRequest, request: Request, background: BackgroundTasks,
                          db: Session = Depends(get_db), current_user: User = Depends(get_current_user)) -> Response:
    """Ends every session, this one included: the client signs in again with the new password."""
    decision = await _change_password_rate_limiter.check(
        f"auth_change_password:{current_user.id}",
        limit=settings.auth_login_email_rate_limit,
        window_seconds=settings.auth_login_email_rate_window_s,
    )
    if not decision.allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many attempts. Please wait and try again.")
    user = db.query(User).filter(User.id == current_user.id).one()
    notice = security.change_password(
        db, user, body.current_password, body.new_password,
        ip=_client_ip(request), user_agent=request.headers.get("user-agent"),
    )
    if notice is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")
    background.add_task(email_service.deliver, notice)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/email/verify")
async def verify_email(body: VerifyEmailRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    await _limit_network(request)
    try:
        security.verify_email(db, body.token, ip=_client_ip(request), user_agent=request.headers.get("user-agent"))
    except security.InvalidLink:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=INVALID_LINK)
    return {"detail": "Email confirmed."}


@router.post("/email/resend", status_code=status.HTTP_202_ACCEPTED)
async def resend_verification(request: Request, background: BackgroundTasks, db: Session = Depends(get_db),
                              current_user: User = Depends(get_current_user)) -> dict:
    await _limit_network(request)
    user = db.query(User).filter(User.id == current_user.id).one()
    if user.email_verified_at is not None:
        return {"detail": "Email already confirmed.", "email_verified": True}
    if not await _address_allowed(user.email):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many requests. Please try again later.")
    message = security.issue_email_verification(db, user)
    if message is not None:
        background.add_task(email_service.deliver, message)
    return {"detail": "Confirmation email sent.", "email_verified": False}


@router.get("/security-events", response_model=list[SecurityEventResponse])
def security_events(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return [
        SecurityEventResponse(
            kind=event.kind, label=security.EVENT_KINDS.get(event.kind, event.kind), method=event.method,
            ip=event.ip, user_agent=event.user_agent, created_at=event.created_at,
        )
        for event in security.list_events(db, current_user.id)
    ]
