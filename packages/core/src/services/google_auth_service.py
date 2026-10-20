"""Sign in with Google (OpenID Connect).

Endpoints, issuers and claims follow Google's own discovery document
(https://accounts.google.com/.well-known/openid-configuration) and its
OpenID Connect guide. What this module guarantees:

* The authorization request carries `state` (bound to the browser by the
  route's flow cookie), a `nonce`, and a PKCE S256 challenge.
* The ID token is only accepted if its RS256 signature verifies against
  Google's published keys and `iss`, `aud`, `exp`, `nonce` and (when an access
  token came with it) `at_hash` all check out, and Google says the email is
  verified. python-jose skips a missing `aud`, so it is required explicitly.
* An account is found by Google's `sub`, never by email alone.
* Linking to an existing password account: signup here has never verified
  email ownership, so whoever registered the address first may not own it.
  When a Google identity with a verified email claims such an account, the
  password is disabled and every refresh session revoked — whoever set that
  password cannot keep a way in. The owner signs in with Google from then on.
* A Google account belongs to one TRINDATRON account: an account already
  linked to a different Google identity is not linked again.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from jose import JWTError, jwt
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from src.config import settings
from src.models.user import User
from src.models.user_identity import UserIdentity
from src.services.auth_service import get_password_hash
from src.services.session_token_service import revoke_all_sessions

logger = logging.getLogger("trinity.auth.google")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
SCOPE = "openid email profile"
PROVIDER = "google"

_JWKS_TTL_S = 3600.0
#: A token naming an unknown key refetches the key set at most this often,
#: so forged tokens cannot turn the API into a request amplifier.
_JWKS_MIN_REFETCH_S = 60.0
_jwks_cache: dict = {"document": None, "fetched_at": 0.0}


class GoogleAuthError(Exception):
    """Google sign-in refused. `reason` is for logs only, never for users."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class GoogleIdentity:
    subject: str
    email: str
    name: str


def web_configured() -> bool:
    return bool(settings.google_client_id and settings.google_client_secret)


def desktop_configured() -> bool:
    return bool(settings.google_desktop_client_id and settings.google_desktop_client_secret)


def web_app_url() -> str:
    if settings.web_app_url:
        return settings.web_app_url
    return settings.allowed_origins[0] if settings.allowed_origins else ""


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge) — RFC 7636."""
    verifier = secrets.token_urlsafe(72)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorization_url(*, client_id: str, redirect_uri: str, state: str, nonce: str, code_challenge: str) -> str:
    return GOOGLE_AUTH_URL + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    })


# ── Network (patched in tests) ────────────────────────────────────────────


async def _fetch_jwks_document() -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(GOOGLE_JWKS_URL)
        resp.raise_for_status()
        return resp.json()


async def _post_token_request(data: dict) -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(GOOGLE_TOKEN_URL, data=data)
    if resp.status_code != 200:
        raise GoogleAuthError("code_exchange_rejected")
    return resp.json()


async def _jwks(*, force: bool = False) -> dict:
    document = _jwks_cache["document"]
    age = time.monotonic() - _jwks_cache["fetched_at"]
    stale = document is None or age > _JWKS_TTL_S
    if stale or (force and age > _JWKS_MIN_REFETCH_S):
        try:
            document = await _fetch_jwks_document()
        except (httpx.HTTPError, ValueError) as exc:
            if document is None:
                raise GoogleAuthError("jwks_unavailable") from exc
            logger.warning("google: key set refresh failed, keeping cached keys")
        else:
            _jwks_cache.update(document=document, fetched_at=time.monotonic())
    return document


# ── Protocol ──────────────────────────────────────────────────────────────


async def exchange_code(*, code: str, client_id: str, client_secret: str, redirect_uri: str,
                        code_verifier: str) -> dict:
    """Trade an authorization code for tokens. Returns Google's JSON, which
    always carries an id_token for the openid scope."""
    try:
        payload = await _post_token_request({
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
        })
    except (httpx.HTTPError, ValueError) as exc:
        raise GoogleAuthError("code_exchange_failed") from exc
    if not isinstance(payload, dict) or not payload.get("id_token"):
        raise GoogleAuthError("no_id_token")
    return payload


async def verify_id_token(id_token: str, *, audience: str, nonce: str,
                          access_token: str | None = None) -> GoogleIdentity:
    try:
        header = jwt.get_unverified_header(id_token)
    except JWTError as exc:
        raise GoogleAuthError("malformed_token") from exc
    if header.get("alg") != "RS256":
        raise GoogleAuthError("unexpected_algorithm")

    document = await _jwks()
    if not any(k.get("kid") == header.get("kid") for k in document.get("keys", [])):
        document = await _jwks(force=True)

    try:
        claims = jwt.decode(
            id_token, document, algorithms=["RS256"], audience=audience, issuer=GOOGLE_ISSUERS,
            access_token=access_token,
            options={"leeway": 30, "require_aud": True, "require_iss": True, "require_exp": True,
                     "require_iat": True, "require_sub": True},
        )
    except JWTError as exc:
        raise GoogleAuthError("invalid_token") from exc

    aud = claims.get("aud")
    audiences = [aud] if isinstance(aud, str) else list(aud or [])
    if audience not in audiences:
        raise GoogleAuthError("wrong_audience")
    if len(audiences) > 1 and claims.get("azp") != audience:
        raise GoogleAuthError("wrong_authorized_party")
    if not nonce or not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise GoogleAuthError("nonce_mismatch")
    subject, email = claims.get("sub"), claims.get("email")
    if not subject or not email:
        raise GoogleAuthError("missing_claims")
    if claims.get("email_verified") not in (True, "true"):
        raise GoogleAuthError("email_unverified")
    return GoogleIdentity(subject=str(subject), email=str(email).strip().lower(),
                          name=str(claims.get("name") or "")[:255])


# ── Accounts ──────────────────────────────────────────────────────────────


def _unusable_password() -> str:
    """A real bcrypt hash of a secret nobody holds: password sign-in fails
    cleanly instead of erroring on a malformed hash."""
    return get_password_hash(secrets.token_urlsafe(48))


def sign_in(db: DBSession, identity: GoogleIdentity) -> User:
    """The TRINDATRON account for this Google identity, created or linked if needed."""
    now = datetime.now(timezone.utc)

    link = db.query(UserIdentity).filter(
        UserIdentity.provider == PROVIDER, UserIdentity.subject == identity.subject,
    ).first()
    if link is not None:
        user = db.query(User).filter(User.id == link.user_id).first()
        if user is None or not user.is_active:
            raise GoogleAuthError("account_disabled")
        link.email, link.last_login_at = identity.email, now
        if user.email_verified_at is None and user.email.lower() == identity.email:
            user.email_verified_at = now
        db.commit()
        return user

    user = db.query(User).filter(func.lower(User.email) == identity.email).first()
    if user is not None:
        if not user.is_active:
            raise GoogleAuthError("account_disabled")
        other = db.query(UserIdentity).filter(
            UserIdentity.provider == PROVIDER, UserIdentity.user_id == user.id,
        ).first()
        if other is not None:
            raise GoogleAuthError("account_linked_to_another_google_identity")
        db.add(UserIdentity(user_id=user.id, provider=PROVIDER, subject=identity.subject,
                            email=identity.email, last_login_at=now))
        user.hashed_password = _unusable_password()
        user.email_verified_at = user.email_verified_at or now
        db.commit()
        revoked = revoke_all_sessions(db, user.id, reason="identity_linked")
        logger.warning("google: identity linked to an existing account; password disabled, "
                       "sessions revoked | user=%s tokens=%d", user.id, revoked)
        return user

    user = User(email=identity.email, hashed_password=_unusable_password(),
                full_name=identity.name, plan="free", email_verified_at=now)
    db.add(user)
    try:
        db.flush()
        db.add(UserIdentity(user_id=user.id, provider=PROVIDER, subject=identity.subject,
                            email=identity.email, last_login_at=now))
        db.commit()
    except IntegrityError as exc:
        # Two first sign-ins raced; the other one created the account.
        db.rollback()
        raise GoogleAuthError("concurrent_signup") from exc
    db.refresh(user)
    logger.info("google: account created | user=%s", user.id)
    return user
