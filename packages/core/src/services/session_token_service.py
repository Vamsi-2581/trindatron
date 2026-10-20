"""Persistent sessions: short access tokens, rotating refresh tokens.

A TRINDATRON login ends only when the user signs out, when it is revoked, or
when it becomes unrecoverable (expired after long inactivity, or a stolen
token was replayed). Backgrounding the app, locking the screen, reloading or
restarting never signs anyone out: the client exchanges its refresh token for
a new access token whenever it gets a 401.

Rules this module enforces:

* The refresh token is a 64-byte random secret; only its SHA-256 is stored.
* Every exchange rotates it: the old token is marked rotated and a new one is
  returned, with a fresh inactivity expiry (settings.refresh_token_days).
* Concurrency: two exchanges of the same token within
  settings.refresh_reuse_grace_seconds — two tabs waking at once, a network
  retry — both succeed, as long as they come from the same user agent.
* Replay: a rotated token presented after that window was copied. The whole
  family is revoked, so the thief and the victim both have to sign in again
  and the victim finds out.
* Revocation (logout, admin, deactivated account) acts on the family or on
  every family of the user. Access tokens already issued stay valid until
  their short expiry (settings.access_token_minutes).
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session as DBSession

from src.config import settings
from src.models.refresh_token import RefreshToken
from src.models.user import User
from src.services.auth_service import create_access_token

logger = logging.getLogger("trinity.sessions")

CLIENTS = ("web", "desktop", "mobile")


class InvalidRefreshToken(Exception):
    """The refresh token cannot be exchanged. `reason` is for logs only."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class IssuedSession:
    user: User
    client: str
    access_token: str
    access_expires_in: int
    refresh_token: str
    refresh_expires_at: datetime


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def list_active_sessions(db: DBSession, user_id: str, *, current_refresh_token: str | None = None,
                         current_session_id: str | None = None, now: datetime | None = None) -> list[dict]:
    """Every sign-in of this user that can still be refreshed: one per family, newest activity first.

    "current" is the sign-in making the request: the Web is recognised by its refresh cookie, the app — which has
    no cookie — by the "sid" of its access token (auth_service.create_access_token). Without either no row is
    current, which is how the app used to see its own device: unmarked, and offering to sign itself out.

    What a person needs to recognise a device (client kind, browser/app string, when it started and was
    last used) and nothing that could be replayed: no token, no hash.
    """
    now = now or _now()
    current_hash = _hash(current_refresh_token) if current_refresh_token else None
    live = (
        db.query(RefreshToken)
        .filter(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None), RefreshToken.rotated_at.is_(None))
        .all()
    )
    sessions = []
    for row in live:
        if _aware(row.expires_at) <= now:
            continue
        started = (
            db.query(RefreshToken.created_at)
            .filter(RefreshToken.family_id == row.family_id)
            .order_by(RefreshToken.created_at.asc())
            .first()
        )
        sessions.append({
            "id": row.family_id,
            "client": row.client,
            "user_agent": (row.user_agent or "")[:160] or None,
            "started_at": _aware(started[0] if started else row.created_at).isoformat(),
            "last_used_at": _aware(row.last_used_at or row.created_at).isoformat(),
            "expires_at": _aware(row.expires_at).isoformat(),
            "current": (current_hash is not None and row.token_hash == current_hash)
            or (current_session_id is not None and row.family_id == current_session_id),
        })
    sessions.sort(key=lambda item: item["last_used_at"], reverse=True)
    return sessions


def revoke_user_session(db: DBSession, user_id: str, session_id: str, *, reason: str = "user_revoked",
                        now: datetime | None = None) -> bool:
    """Sign out one device of this user. False when the session is not theirs or already over."""
    owned = (
        db.query(RefreshToken.id)
        .filter(RefreshToken.family_id == session_id, RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .first()
    )
    if owned is None:
        return False
    _revoke_family(db, session_id, reason, now or _now())
    db.commit()
    return True


def _aware(value: datetime) -> datetime:
    # Always datetime.timezone.utc itself: the driver may return ZoneInfo("UTC") or an offset, and the
    # refresh cookie's `expires` accepts only timezone.utc — anything else made every login answer 500.
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _access(user_id: str, family_id: str) -> tuple[str, int]:
    minutes = settings.access_token_minutes
    return create_access_token(user_id, minutes=minutes, session_id=family_id), minutes * 60


def _new_refresh(db: DBSession, *, user_id: str, family_id: str, client: str,
                 user_agent: str | None, now: datetime) -> tuple[RefreshToken, str]:
    plain = secrets.token_urlsafe(64)
    row = RefreshToken(
        user_id=user_id, family_id=family_id, token_hash=_hash(plain),
        client=client if client in CLIENTS else "web",
        user_agent=(user_agent or "")[:255] or None,
        created_at=now, expires_at=now + timedelta(days=settings.refresh_token_days),
    )
    db.add(row)
    db.flush()
    return row, plain


def _revoke_family(db: DBSession, family_id: str, reason: str, now: datetime) -> int:
    return (
        db.query(RefreshToken)
        .filter(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .update({RefreshToken.revoked_at: now, RefreshToken.revoked_reason: reason}, synchronize_session=False)
    )


def issue_session(db: DBSession, user: User, *, client: str, user_agent: str | None = None,
                  now: datetime | None = None) -> IssuedSession:
    """Start a new sign-in: a new family, its first refresh token, an access token."""
    now = now or _now()
    row, plain = _new_refresh(db, user_id=user.id, family_id=str(uuid.uuid4()), client=client,
                              user_agent=user_agent, now=now)
    db.commit()
    access, expires_in = _access(user.id, row.family_id)
    return IssuedSession(user, row.client, access, expires_in, plain, _aware(row.expires_at))


def rotate_session(db: DBSession, refresh_token: str | None, *, user_agent: str | None = None,
                   now: datetime | None = None) -> IssuedSession:
    """Exchange a refresh token for a new access token and a new refresh token."""
    now = now or _now()
    if not refresh_token:
        raise InvalidRefreshToken("missing")

    row = (
        db.query(RefreshToken)
        .filter(RefreshToken.token_hash == _hash(refresh_token))
        .with_for_update()
        .first()
    )
    if row is None:
        raise InvalidRefreshToken("unknown")
    if row.revoked_at is not None:
        raise InvalidRefreshToken("revoked")
    if _aware(row.expires_at) <= now:
        raise InvalidRefreshToken("expired")

    user = db.query(User).filter(User.id == row.user_id).first()
    if user is None or not user.is_active:
        _revoke_family(db, row.family_id, "user_inactive", now)
        db.commit()
        raise InvalidRefreshToken("user_inactive")

    if row.rotated_at is not None:
        # Strict: with no grace window, a reuse in the same clock tick is still a replay.
        within_grace = now - _aware(row.rotated_at) < timedelta(seconds=settings.refresh_reuse_grace_seconds)
        same_agent = not row.user_agent or not user_agent or row.user_agent == (user_agent or "")[:255]
        if not (within_grace and same_agent):
            revoked = _revoke_family(db, row.family_id, "reuse_detected", now)
            db.commit()
            logger.warning("sessions: refresh token replayed, family revoked | user=%s family=%s tokens=%d",
                           row.user_id, row.family_id, revoked)
            raise InvalidRefreshToken("reuse_detected")
    else:
        row.rotated_at = now
    row.last_used_at = now

    new_row, plain = _new_refresh(db, user_id=row.user_id, family_id=row.family_id, client=row.client,
                                  user_agent=user_agent or row.user_agent, now=now)
    if row.replaced_by_id is None:
        row.replaced_by_id = new_row.id
    db.commit()
    access, expires_in = _access(user.id, new_row.family_id)
    return IssuedSession(user, new_row.client, access, expires_in, plain, _aware(new_row.expires_at))


def revoke_session(db: DBSession, refresh_token: str | None, *, reason: str = "logout",
                   now: datetime | None = None) -> bool:
    """Sign out the login this refresh token belongs to. True if one was found."""
    if not refresh_token:
        return False
    row = db.query(RefreshToken).filter(RefreshToken.token_hash == _hash(refresh_token)).first()
    if row is None:
        return False
    _revoke_family(db, row.family_id, reason, now or _now())
    db.commit()
    return True


def revoke_all_sessions(db: DBSession, user_id: str, *, reason: str = "admin",
                        now: datetime | None = None) -> int:
    """Sign a user out everywhere."""
    count = (
        db.query(RefreshToken)
        .filter(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .update({RefreshToken.revoked_at: now or _now(), RefreshToken.revoked_reason: reason},
                synchronize_session=False)
    )
    db.commit()
    return count
