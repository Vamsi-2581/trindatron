"""Sign in with Google — the same account from "Entrar" and "Criar conta".

Web (GET /auth/google/start → Google → GET /auth/google/callback):
    The API runs the whole OpenID Connect code flow. `state`, `nonce` and the
    PKCE verifier live in a signed, httpOnly, 10-minute cookie scoped to
    /auth/google, so the callback only completes in the browser that started
    it (CSRF and login-CSRF protection). On success the refresh cookie is set
    and the browser goes to <web>/auth/google, which restores the session
    through /auth/refresh like any reload. Every failure, including the user
    cancelling at Google, goes back to <web>/auth?error=<code> with no detail.

Desktop (GET /auth/google/desktop-config, POST /auth/google/token):
    Google does not allow its sign-in page inside an embedded browser, and
    supports only loopback redirects for desktop apps. The app opens the
    system browser with its own PKCE pair and nonce, receives the code on
    http://127.0.0.1:<port>, and posts it here; the API exchanges it and
    verifies the ID token with the same rules as web.
"""
from __future__ import annotations

import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.config import settings
from src.database import get_db
from src.schemas.auth import TokenResponse
from src.services import account_security_service
from src.services import google_auth_service as google
from src.services.request_control import InMemoryRateLimiter
from src.services.session_token_service import issue_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/google", tags=["auth"])

_google_ip_rate_limiter = InMemoryRateLimiter()

FLOW_COOKIE = "trindatron_google_flow"
FLOW_COOKIE_PATH = "/auth/google"
FLOW_MINUTES = 10
_LOOPBACK = re.compile(r"^http://(?:127\.0\.0\.1|\[::1\]|localhost):\d{1,5}(?:/[A-Za-z0-9._~\-/]*)?$")

UNAVAILABLE = "Google sign-in is not available right now."
FAILED = "Google sign-in failed. Please try again."


def _web(path: str, **query: str) -> str:
    base = google.web_app_url().rstrip("/")
    return f"{base}{path}" + (f"?{urlencode(query)}" if query else "")


def _callback_uri(request: Request) -> str:
    return settings.google_redirect_uri or str(request.url_for("google_callback"))


async def _rate_limited(request: Request) -> bool:
    from src.api.routes.auth import _client_ip

    ip = _client_ip(request)
    if ip is None:
        return False
    decision = await _google_ip_rate_limiter.check(
        f"auth_google_ip:{ip}",
        limit=settings.auth_login_ip_rate_limit,
        window_seconds=settings.auth_login_ip_rate_window_s,
    )
    return not decision.allowed


def _error_code(exc: google.GoogleAuthError) -> str:
    return "google_account_disabled" if exc.reason == "account_disabled" else "google_failed"


@router.get("/start")
async def google_start(request: Request):
    if not google.web_configured():
        return RedirectResponse(_web("/auth", error="google_unavailable"), status_code=status.HTTP_303_SEE_OTHER)
    if await _rate_limited(request):
        return RedirectResponse(_web("/auth", error="google_rate_limited"), status_code=status.HTTP_303_SEE_OTHER)

    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    verifier, challenge = google.pkce_pair()
    flow = jwt.encode(
        {"typ": "google_flow", "state": state, "nonce": nonce, "verifier": verifier,
         "exp": datetime.now(timezone.utc) + timedelta(minutes=FLOW_MINUTES)},
        settings.jwt_secret_key, algorithm=settings.jwt_algorithm,
    )
    resp = RedirectResponse(
        google.authorization_url(client_id=settings.google_client_id, redirect_uri=_callback_uri(request),
                                 state=state, nonce=nonce, code_challenge=challenge),
        status_code=status.HTTP_303_SEE_OTHER,
    )
    resp.set_cookie(FLOW_COOKIE, flow, max_age=FLOW_MINUTES * 60, httponly=True,
                    secure=settings.refresh_cookie_secure, samesite="lax", path=FLOW_COOKIE_PATH)
    return resp


@router.get("/callback", name="google_callback")
async def google_callback(request: Request, code: str | None = None, state: str | None = None,
                          error: str | None = None, db: Session = Depends(get_db)):
    from src.api.routes.auth import _set_refresh_cookie

    def finish(url: str) -> RedirectResponse:
        resp = RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
        resp.delete_cookie(FLOW_COOKIE, path=FLOW_COOKIE_PATH, secure=settings.refresh_cookie_secure,
                           httponly=True, samesite="lax")
        return resp

    if error:
        return finish(_web("/auth", error="google_cancelled" if error == "access_denied" else "google_failed"))

    try:
        flow = jwt.decode(request.cookies.get(FLOW_COOKIE) or "", settings.jwt_secret_key,
                          algorithms=[settings.jwt_algorithm])
    except JWTError:
        logger.info("auth: google callback without a valid flow cookie")
        return finish(_web("/auth", error="google_failed"))
    if (flow.get("typ") != "google_flow" or not code or not state
            or not hmac.compare_digest(str(flow.get("state", "")).encode("utf-8"), state.encode("utf-8"))):
        logger.info("auth: google callback state mismatch")
        return finish(_web("/auth", error="google_failed"))

    try:
        tokens = await google.exchange_code(
            code=code, client_id=settings.google_client_id, client_secret=settings.google_client_secret,
            redirect_uri=_callback_uri(request), code_verifier=flow["verifier"],
        )
        identity = await google.verify_id_token(tokens["id_token"], audience=settings.google_client_id,
                                                nonce=flow["nonce"], access_token=tokens.get("access_token"))
        user = google.sign_in(db, identity)
    except google.GoogleAuthError as exc:
        logger.info("auth: google sign-in refused | reason=%s", exc.reason)
        return finish(_web("/auth", error=_error_code(exc)))

    _record_sign_in(db, user, request)
    session = issue_session(db, user, client="web", user_agent=request.headers.get("user-agent"))
    resp = finish(_web("/auth/google"))
    _set_refresh_cookie(resp, session)
    return resp


def _record_sign_in(db: Session, user, request: Request) -> None:
    from src.api.routes.auth import _client_ip

    account_security_service.record(db, user.id, "login_success", method="google",
                                    ip=_client_ip(request), user_agent=request.headers.get("user-agent"))


class GoogleDesktopConfig(BaseModel):
    client_id: str
    authorization_endpoint: str
    scope: str


@router.get("/desktop-config", response_model=GoogleDesktopConfig)
def google_desktop_config():
    """The public half of the desktop client registration."""
    if not google.desktop_configured():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE)
    return GoogleDesktopConfig(client_id=settings.google_desktop_client_id,
                               authorization_endpoint=google.GOOGLE_AUTH_URL, scope=google.SCOPE)


class GoogleTokenRequest(BaseModel):
    code: str = Field(min_length=1, max_length=2048)
    code_verifier: str = Field(min_length=43, max_length=128)
    redirect_uri: str = Field(min_length=1, max_length=200)
    nonce: str = Field(min_length=16, max_length=128)
    client: Literal["desktop"] = "desktop"


@router.post("/token", response_model=TokenResponse)
async def google_token(body: GoogleTokenRequest, request: Request, response: Response,
                       db: Session = Depends(get_db)):
    from src.api.routes.auth import _session_response

    if not google.desktop_configured():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE)
    if await _rate_limited(request):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="Too many requests. Please try again shortly.")
    if not _LOOPBACK.match(body.redirect_uri):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=FAILED)

    try:
        tokens = await google.exchange_code(
            code=body.code, client_id=settings.google_desktop_client_id,
            client_secret=settings.google_desktop_client_secret,
            redirect_uri=body.redirect_uri, code_verifier=body.code_verifier,
        )
        identity = await google.verify_id_token(tokens["id_token"], audience=settings.google_desktop_client_id,
                                                nonce=body.nonce, access_token=tokens.get("access_token"))
        user = google.sign_in(db, identity)
    except google.GoogleAuthError as exc:
        logger.info("auth: google desktop sign-in refused | reason=%s", exc.reason)
        if exc.reason == "account_disabled":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account is disabled.") from exc
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=FAILED) from exc

    _record_sign_in(db, user, request)
    session = issue_session(db, user, client=body.client, user_agent=request.headers.get("user-agent"))
    return _session_response(response, session)


# ── Mobile (native Android/iOS and the Expo web build) ────────────────────────
#
# The app runs Google's own sign-in (system browser / Google Sign-In SDK) with
# a nonce it generated, receives an ID token issued to one of ITS client IDs,
# and posts the token and the raw nonce here. The API verifies the token with
# the same rules as web and desktop — RS256 against Google's keys, iss, exp,
# nonce, verified e-mail — and requires the audience to be one of the
# configured mobile client IDs, so a token minted for any other app is refused.


class GoogleMobileConfig(BaseModel):
    android_client_id: str | None
    ios_client_id: str | None
    web_client_id: str | None
    scope: str


@router.get("/mobile-config", response_model=GoogleMobileConfig)
def google_mobile_config():
    from src.services.google_mobile_settings import google_mobile_settings

    s = google_mobile_settings()
    if not s.audiences():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE)
    return GoogleMobileConfig(android_client_id=s.google_android_client_id or None,
                              ios_client_id=s.google_ios_client_id or None,
                              web_client_id=s.google_mobile_web_client_id or None, scope=google.SCOPE)


class GoogleIdTokenRequest(BaseModel):
    id_token: str = Field(min_length=20, max_length=8192)
    nonce: str = Field(min_length=16, max_length=128)
    client: Literal["mobile"] = "mobile"


@router.post("/id-token", response_model=TokenResponse)
async def google_id_token(body: GoogleIdTokenRequest, request: Request, response: Response,
                          db: Session = Depends(get_db)):
    from src.api.routes.auth import _session_response
    from src.services.google_mobile_settings import google_mobile_settings

    audiences = google_mobile_settings().audiences()
    if not audiences:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE)
    if await _rate_limited(request):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="Too many requests. Please try again shortly.")

    try:
        claimed = jwt.get_unverified_claims(body.id_token).get("aud")
    except JWTError:
        claimed = None
    claimed_list = [claimed] if isinstance(claimed, str) else list(claimed or [])
    audience = next((aud for aud in audiences if aud in claimed_list), None)

    try:
        if audience is None:
            raise google.GoogleAuthError("wrong_audience")
        identity = await google.verify_id_token(body.id_token, audience=audience, nonce=body.nonce)
        user = google.sign_in(db, identity)
    except google.GoogleAuthError as exc:
        logger.info("auth: google mobile sign-in refused | reason=%s", exc.reason)
        if exc.reason == "account_disabled":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account is disabled.") from exc
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=FAILED) from exc

    _record_sign_in(db, user, request)
    session = issue_session(db, user, client=body.client, user_agent=request.headers.get("user-agent"))
    return _session_response(response, session)
