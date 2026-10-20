"""Account security: password sign-in with lockout, password recovery, email
confirmation, and the account's security log.

Rules this module keeps:

* No answer reveals whether an address has an account. "Forgot password" says
  the same thing for every address, a locked account answers a wrong guess the
  same way the rate limiter does, and a sign-in with an unknown address still
  spends a bcrypt check so timing does not tell either.
* Links are single-use, short-lived and stored only as SHA-256 hashes. A newer
  link of the same purpose retires the older ones.
* Setting a new password ends every session: refresh tokens are revoked and
  access tokens issued before the change stop authenticating (src/api/deps.py).
* Lockout stops a guesser, never the owner: password reset and Google sign-in
  still work while password sign-in is locked.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session as DBSession

from src.config import settings
from src.models.account_security import AccountToken, SecurityEvent
from src.models.user import User
from src.services import email_service
from src.services.auth_service import get_password_hash, verify_password
from src.services.session_token_service import revoke_all_sessions

logger = logging.getLogger(__name__)

PASSWORD_RESET = "password_reset"
EMAIL_VERIFY = "email_verify"

#: Every kind of SecurityEvent, and what the owner sees for it.
EVENT_KINDS = {
    "login_success": "Entrada na conta",
    "login_failed": "Senha incorreta",
    "login_blocked": "Tentativa com login bloqueado",
    "account_locked": "Login com senha bloqueado por tentativas",
    "account_created": "Conta criada",
    "password_reset_requested": "Pedido de redefinição de senha",
    "password_reset": "Senha redefinida por e-mail",
    "password_changed": "Senha alterada",
    "email_verified": "E-mail confirmado",
}

#: A bcrypt hash of nothing anyone knows, checked when the address has no
#: account so an unknown address costs the same time as a wrong password.
_DUMMY_HASH = get_password_hash(secrets.token_urlsafe(24))


class InvalidLink(Exception):
    """The link is unknown, used, replaced, expired, or no longer matches the account."""


@dataclass(frozen=True)
class SignInResult:
    user: User | None
    #: True when password sign-in is locked for this account right now.
    locked: bool = False
    #: Set when this attempt is the one that locked it: the notice to send.
    locked_notice: email_service.Email | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _web(path: str) -> str:
    from src.services.google_auth_service import web_app_url
    return f"{web_app_url().rstrip('/')}{path}"


def forgot_link() -> str:
    return _web("/auth/forgot")


def record(db: DBSession, user_id: str, kind: str, *, method: str | None = None,
           ip: str | None = None, user_agent: str | None = None, commit: bool = True) -> None:
    db.add(SecurityEvent(user_id=user_id, kind=kind, method=method, ip=(ip or None) and ip[:64],
                         user_agent=(user_agent or None) and user_agent[:255]))
    if commit:
        db.commit()


def list_events(db: DBSession, user_id: str, *, limit: int = 50) -> list[SecurityEvent]:
    return (
        db.query(SecurityEvent)
        .filter(SecurityEvent.user_id == user_id)
        .order_by(SecurityEvent.created_at.desc())
        .limit(limit)
        .all()
    )


def find_by_email(db: DBSession, email: str) -> User | None:
    return (
        db.query(User)
        .filter(func.lower(User.email) == email.strip().lower(), User.is_active.is_(True))
        .order_by(User.created_at)
        .first()
    )


# ── Password sign-in with lockout ─────────────────────────────────────────────


def password_sign_in(db: DBSession, email: str, password: str, *,
                     ip: str | None = None, user_agent: str | None = None) -> SignInResult:
    user = db.query(User).filter(User.email == email, User.is_active.is_(True)).first()
    if user is None:
        verify_password(password, _DUMMY_HASH)
        return SignInResult(user=None)

    now = _now()
    locked_until = _aware(user.locked_until)
    if locked_until is not None and locked_until > now:
        # The password is not even checked: a guesser learns nothing while locked.
        record(db, user.id, "login_blocked", method="password", ip=ip, user_agent=user_agent)
        return SignInResult(user=None, locked=True)

    if verify_password(password, user.hashed_password):
        user.failed_login_count, user.last_failed_login_at, user.locked_until = 0, None, None
        record(db, user.id, "login_success", method="password", ip=ip, user_agent=user_agent)
        return SignInResult(user=user)

    last = _aware(user.last_failed_login_at)
    window = timedelta(minutes=settings.auth_lockout_window_minutes)
    streak = user.failed_login_count if last is not None and now - last <= window else 0
    user.failed_login_count, user.last_failed_login_at = streak + 1, now
    record(db, user.id, "login_failed", method="password", ip=ip, user_agent=user_agent, commit=False)
    if user.failed_login_count >= settings.auth_lockout_threshold:
        user.locked_until = now + timedelta(minutes=settings.auth_lockout_minutes)
        user.failed_login_count = 0
        record(db, user.id, "account_locked", method="password", ip=ip, user_agent=user_agent, commit=False)
        db.commit()
        logger.warning("auth: password sign-in locked | user=%s minutes=%d", user.id, settings.auth_lockout_minutes)
        return SignInResult(user=None, locked=True, locked_notice=email_service.account_locked_email(
            user.email, settings.auth_lockout_minutes, forgot_link()))
    db.commit()
    return SignInResult(user=None)


# ── Single-use links ──────────────────────────────────────────────────────────


def _issue(db: DBSession, user: User, purpose: str, lifetime: timedelta) -> str:
    now = _now()
    (
        db.query(AccountToken)
        .filter(AccountToken.user_id == user.id, AccountToken.purpose == purpose, AccountToken.used_at.is_(None))
        .update({AccountToken.used_at: now}, synchronize_session=False)
    )
    token = secrets.token_urlsafe(32)
    db.add(AccountToken(user_id=user.id, purpose=purpose, token_hash=_hash(token), email=user.email,
                        created_at=now, expires_at=now + lifetime))
    return token


def _redeem(db: DBSession, token: str, purpose: str) -> tuple[AccountToken, User]:
    """The live link and its account, marked used. Raises InvalidLink otherwise."""
    if not token or len(token) > 200:
        raise InvalidLink()
    row = db.query(AccountToken).filter(AccountToken.token_hash == _hash(token)).first()
    if row is None or row.purpose != purpose or row.used_at is not None:
        raise InvalidLink()
    now = _now()
    if _aware(row.expires_at) <= now:
        raise InvalidLink()
    user = db.query(User).filter(User.id == row.user_id, User.is_active.is_(True)).first()
    if user is None or user.email.lower() != row.email.lower():
        raise InvalidLink()
    row.used_at = now
    return row, user


def request_password_reset(db: DBSession, email: str, *, ip: str | None = None,
                           user_agent: str | None = None) -> email_service.Email | None:
    """The reset email to send, or None when the address has no active account."""
    user = find_by_email(db, email)
    if user is None:
        return None
    token = _issue(db, user, PASSWORD_RESET, timedelta(minutes=settings.password_reset_minutes))
    record(db, user.id, "password_reset_requested", ip=ip, user_agent=user_agent, commit=False)
    db.commit()
    return email_service.password_reset_email(
        user.email, _web(f"/auth/reset#token={token}"), settings.password_reset_minutes)


def _set_password(db: DBSession, user: User, password: str, *, kind: str,
                  ip: str | None, user_agent: str | None) -> email_service.Email:
    now = _now()
    user.hashed_password = get_password_hash(password)
    user.password_changed_at = now
    user.failed_login_count, user.last_failed_login_at, user.locked_until = 0, None, None
    (
        db.query(AccountToken)
        .filter(AccountToken.user_id == user.id, AccountToken.purpose == PASSWORD_RESET, AccountToken.used_at.is_(None))
        .update({AccountToken.used_at: now}, synchronize_session=False)
    )
    record(db, user.id, kind, ip=ip, user_agent=user_agent, commit=False)
    db.commit()
    revoked = revoke_all_sessions(db, user.id, reason=kind)
    logger.info("auth: password set | user=%s kind=%s sessions_revoked=%d", user.id, kind, revoked)
    return email_service.password_changed_email(user.email, forgot_link())


def reset_password(db: DBSession, token: str, password: str, *, ip: str | None = None,
                   user_agent: str | None = None) -> email_service.Email:
    """Set the password from a reset link. Returns the "your password changed" notice to send."""
    _, user = _redeem(db, token, PASSWORD_RESET)
    if user.email_verified_at is None:
        # Opening the link proved the address, the same as a confirmation link.
        user.email_verified_at = _now()
    return _set_password(db, user, password, kind="password_reset", ip=ip, user_agent=user_agent)


def change_password(db: DBSession, user: User, current: str, new: str, *, ip: str | None = None,
                    user_agent: str | None = None) -> email_service.Email | None:
    """None when `current` is wrong; otherwise the notice to send."""
    if not verify_password(current, user.hashed_password):
        return None
    return _set_password(db, user, new, kind="password_changed", ip=ip, user_agent=user_agent)


def issue_email_verification(db: DBSession, user: User) -> email_service.Email | None:
    """The confirmation email to send, or None when the address is already confirmed."""
    if user.email_verified_at is not None:
        return None
    token = _issue(db, user, EMAIL_VERIFY, timedelta(hours=settings.email_verify_hours))
    db.commit()
    return email_service.email_verify_email(
        user.email, _web(f"/auth/verify#token={token}"), settings.email_verify_hours)


def verify_email(db: DBSession, token: str, *, ip: str | None = None, user_agent: str | None = None) -> User:
    _, user = _redeem(db, token, EMAIL_VERIFY)
    if user.email_verified_at is None:
        user.email_verified_at = _now()
        record(db, user.id, "email_verified", ip=ip, user_agent=user_agent, commit=False)
    db.commit()
    return user
