import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from src.api.deps import get_current_user
from src.config import settings
from src.database import get_db
from src.models.user import User
from src.schemas.auth import LoginRequest, RefreshRequest, RegisterRequest, TokenResponse, UserResponse
from src.services import account_security_service, email_service
from src.services.auth_service import create_access_token, create_user
from src.services.session_token_service import (
    InvalidRefreshToken,
    IssuedSession,
    issue_session,
    revoke_session,
    rotate_session,
)
from src.services.request_control import InMemoryRateLimiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# Security Hardening — credential stuffing / brute force protection.
# Own dedicated buckets (see config.py's auth_login_*_rate_limit docstring),
# module-level singletons matching billing.py's own _order_rate_limiter/
# _webhook_rate_limiter convention.
_login_ip_rate_limiter = InMemoryRateLimiter()
_login_email_rate_limiter = InMemoryRateLimiter()
# FREE anti-abuse: see config.py's auth_register_ip_rate_limit.
_register_ip_rate_limiter = InMemoryRateLimiter()
_refresh_ip_rate_limiter = InMemoryRateLimiter()

#: A cookie-authenticated refresh or logout must carry this header. Browsers
#: never add a custom header to a cross-site form or image request, and adding
#: one from script forces a CORS preflight the allowed origins govern — so a
#: third-party page cannot rotate or end a user's session.
SESSION_HEADER = "x-trindatron-client"


def _set_refresh_cookie(response: Response, session: IssuedSession) -> None:
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=session.refresh_token,
        httponly=True,
        secure=settings.refresh_cookie_secure,
        samesite=settings.refresh_cookie_samesite,
        path="/auth",
        expires=session.refresh_expires_at,
        domain=settings.refresh_cookie_domain or None,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(
        key=settings.refresh_cookie_name,
        path="/auth",
        domain=settings.refresh_cookie_domain or None,
        secure=settings.refresh_cookie_secure,
        httponly=True,
        samesite=settings.refresh_cookie_samesite,
    )


def _session_response(response: Response, session: IssuedSession) -> TokenResponse:
    """Web gets the refresh token as an httpOnly cookie, never in the body;
    desktop and mobile get it in the body for their OS secure storage."""
    web = session.client == "web"
    if web:
        _set_refresh_cookie(response, session)
    return TokenResponse(
        access_token=session.access_token,
        user_id=session.user.id,
        email=session.user.email,
        expires_in=session.access_expires_in,
        refresh_token=None if web else session.refresh_token,
        refresh_expires_at=session.refresh_expires_at,
    )


def _client_ip(request: Request) -> str | None:
    """Same extraction as sessions.py/billing.py's own _client_ip() — see
    sessions.py's docstring for INFRA-01 (HIGH, final gate revision):
    prefers X-Real-IP over X-Forwarded-For's ambiguous hop chain; falls
    back to request.client.host. The per-email bucket below is the real
    backstop against credential stuffing regardless of this header."""
    real_ip = request.headers.get("x-real-ip")
    if real_ip and real_ip.strip():
        return real_ip.strip()
    return request.client.host if request.client else None


async def _check_login_rate_limit(request: Request, email: str) -> None:
    client_ip = _client_ip(request)
    if client_ip is not None:
        ip_decision = await _login_ip_rate_limiter.check(
            f"auth_login_ip:{client_ip}",
            limit=settings.auth_login_ip_rate_limit,
            window_seconds=settings.auth_login_ip_rate_window_s,
        )
        if not ip_decision.allowed:
            logger.warning("auth: login rate limit exceeded (ip) | client_ip=%s", client_ip)
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many login attempts. Please wait and try again.")

    email_decision = await _login_email_rate_limiter.check(
        f"auth_login_email:{email.strip().lower()}",
        limit=settings.auth_login_email_rate_limit,
        window_seconds=settings.auth_login_email_rate_window_s,
    )
    if not email_decision.allowed:
        logger.warning("auth: login rate limit exceeded (email)")
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many login attempts. Please wait and try again.")


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, request: Request, response: Response, background: BackgroundTasks,
                   db: Session = Depends(get_db)):
    client_ip = _client_ip(request)
    if client_ip is not None:
        decision = await _register_ip_rate_limiter.check(
            f"auth_register_ip:{client_ip}",
            limit=settings.auth_register_ip_rate_limit,
            window_seconds=settings.auth_register_ip_rate_window_s,
        )
        if not decision.allowed:
            logger.warning("auth: register rate limit exceeded (ip) | client_ip=%s", client_ip)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many accounts created from this network. Please try again later.",
            )
    try:
        user = create_user(db, email=body.email, password=body.password, full_name=body.full_name)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))

    account_security_service.record(db, user.id, "account_created", method="password",
                                    ip=client_ip, user_agent=request.headers.get("user-agent"))
    confirmation = account_security_service.issue_email_verification(db, user)
    if confirmation is not None:
        background.add_task(email_service.deliver, confirmation)

    if body.client:
        return _session_response(
            response, issue_session(db, user, client=body.client, user_agent=request.headers.get("user-agent")),
        )
    token = create_access_token(user.id)
    return TokenResponse(access_token=token, user_id=user.id, email=user.email)


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, response: Response, background: BackgroundTasks,
                db: Session = Depends(get_db)):
    await _check_login_rate_limit(request, body.email)
    result = account_security_service.password_sign_in(
        db, body.email, body.password, ip=_client_ip(request), user_agent=request.headers.get("user-agent"),
    )
    if result.locked:
        # The same answer the rate limiter gives: a lock is not a way to learn an address has an account.
        # Returned rather than raised so the lock notice still goes out after it.
        if result.locked_notice is not None:
            background.add_task(email_service.deliver, result.locked_notice)
        return JSONResponse(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            content={"detail": "Too many login attempts. Please wait and try again."},
                            background=background)
    user = result.user
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )
    if body.client:
        return _session_response(
            response, issue_session(db, user, client=body.client, user_agent=request.headers.get("user-agent")),
        )
    token = create_access_token(user.id)
    return TokenResponse(access_token=token, user_id=user.id, email=user.email)


def _presented_refresh_token(request: Request, body: RefreshRequest | None) -> tuple[str | None, bool]:
    """(token, from_cookie). A body token wins; otherwise the cookie is used,
    and only when the request proves it came from a TRINDATRON client."""
    if body is not None and body.refresh_token:
        return body.refresh_token, False
    cookie = request.cookies.get(settings.refresh_cookie_name)
    if cookie and not request.headers.get(SESSION_HEADER):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Missing client header")
    return cookie, True


@router.post("/refresh", response_model=TokenResponse)
async def refresh(request: Request, response: Response, body: RefreshRequest | None = None,
                  db: Session = Depends(get_db)):
    """Exchange a refresh token for a new access token and a new refresh token.

    Called by clients when an access token expires (a 401) or on start-up to
    restore the session. Every call rotates the refresh token; see
    session_token_service for the concurrency window and replay protection.
    """
    client_ip = _client_ip(request)
    if client_ip is not None:
        decision = await _refresh_ip_rate_limiter.check(
            f"auth_refresh_ip:{client_ip}",
            limit=settings.auth_refresh_ip_rate_limit,
            window_seconds=settings.auth_refresh_ip_rate_window_s,
        )
        if not decision.allowed:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many requests. Please try again shortly.")

    token, from_cookie = _presented_refresh_token(request, body)
    try:
        session = rotate_session(db, token, user_agent=request.headers.get("user-agent"))
    except InvalidRefreshToken as exc:
        logger.info("auth: refresh refused | reason=%s", exc.reason)
        refused = JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content={"detail": "Session expired. Please sign in again."})
        if from_cookie:
            _clear_refresh_cookie(refused)
        return refused
    return _session_response(response, session)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, body: RefreshRequest | None = None, db: Session = Depends(get_db)):
    """End this login everywhere it is refreshed from. Idempotent: an unknown
    or already-revoked token still clears the cookie and returns 204."""
    token, _ = _presented_refresh_token(request, body)
    revoke_session(db, token, reason="logout")
    done = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_refresh_cookie(done)
    return done


@router.get("/me", response_model=UserResponse)
def me(current_user: User = Depends(get_current_user)):
    return current_user


# Included here rather than in main.py so /auth/google/* ships with /auth.
from src.api.routes.auth_google import router as _google_router  # noqa: E402
from src.api.routes.auth_account import router as _account_router  # noqa: E402

router.include_router(_google_router)
router.include_router(_account_router)
