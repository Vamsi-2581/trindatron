from datetime import datetime, timedelta, timezone

import bcrypt as _bcrypt
from jose import JWTError, jwt
from sqlalchemy.orm import Session as DBSession

from src.config import settings
from src.models.user import User


def get_password_hash(password: str) -> str:
    return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return _bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def create_access_token(user_id: str, minutes: int | None = None, session_id: str | None = None) -> str:
    """`minutes` defaults to the legacy long lifetime, kept for clients that
    have not adopted refresh tokens yet; session_token_service passes the
    short settings.access_token_minutes.

    `session_id` is the sign-in (refresh-token family) the token belongs to,
    as the "sid" claim. It is the same id GET /auth/sessions lists, not a
    secret, and it is how a client with no refresh cookie — the mobile app
    keeps its refresh token in secure storage — is recognised as "this device".
    """
    lifetime = settings.jwt_expiration_minutes if minutes is None else minutes
    issued = datetime.now(timezone.utc)
    expire = issued + timedelta(minutes=lifetime)
    # "iat_ms" lets a password change end every token issued before it
    # (deps.get_current_user). Milliseconds, because the standard "iat" is whole
    # seconds and a token issued in the second of a reset must not survive it.
    payload = {"sub": user_id, "exp": expire, "iat": int(issued.timestamp()),
               "iat_ms": int(issued.timestamp() * 1000)}
    if session_id:
        payload["sid"] = session_id
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def session_id_from_token(token: str | None) -> str | None:
    """The "sid" of a valid access token, or None (no token, an invalid one, or a token issued before sid)."""
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except Exception:
        return None
    sid = payload.get("sid")
    return sid if isinstance(sid, str) and sid else None


def token_issued_at_ms(token: str) -> int | None:
    """When a valid access token was issued, in Unix milliseconds, or None for a token from before "iat_ms"."""
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except Exception:
        return None
    issued = payload.get("iat_ms")
    return issued if isinstance(issued, int) else None


def decode_token(token: str) -> str:
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        user_id: str | None = payload.get("sub")
        if not user_id:
            raise ValueError("Missing subject in token")
        return user_id
    except JWTError as exc:
        raise ValueError("Invalid or expired token") from exc


def create_user(db: DBSession, email: str, password: str, full_name: str = "") -> User:
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise ValueError("Email already registered")
    user = User(
        email=email,
        hashed_password=get_password_hash(password),
        full_name=full_name,
        # Explicit, not left to the column default: a new account has paid
        # for nothing. Until 2026-09-13 this line did not exist and the model
        # default was "start", so every signup silently received START's agent
        # mode, TRINITY CODE, the standard quality band and 110 TCU a week.
        plan="free",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(db: DBSession, email: str, password: str) -> User | None:
    user = db.query(User).filter(User.email == email, User.is_active.is_(True)).first()
    if not user or not verify_password(password, user.hashed_password):
        return None
    return user
