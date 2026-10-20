import asyncio
from datetime import timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from src.database import get_db
from src.models.user import User
from src.services.auth_service import decode_token, token_issued_at_ms

_security = HTTPBearer()

# ---------------------------------------------------------------------------
# Provider registry DI (Billing-2)
# ---------------------------------------------------------------------------

_provider_registry = None  # ProviderRegistry | None — set once at startup


def set_provider_registry(registry) -> None:
    """Initialize the provider registry. Called once from main.py lifespan."""
    global _provider_registry
    _provider_registry = registry


def get_provider_registry():
    """FastAPI dependency — injects the ProviderRegistry into route handlers.

    Raises RuntimeError if called before set_provider_registry().
    In tests, override with app.dependency_overrides[get_provider_registry].
    """
    if _provider_registry is None:
        raise RuntimeError(
            "ProviderRegistry not initialized — call set_provider_registry() in lifespan"
        )
    return _provider_registry


# ---------------------------------------------------------------------------
# Voice provider registry DI
# ---------------------------------------------------------------------------

_voice_registry = None  # VoiceProviderRegistry | None — set once at startup


def set_voice_registry(registry) -> None:
    """Initialize the voice provider registry. Called once from main.py lifespan."""
    global _voice_registry
    _voice_registry = registry


def get_voice_registry():
    """FastAPI dependency — injects the VoiceProviderRegistry into route handlers.

    Raises RuntimeError if called before set_voice_registry().
    In tests, override with app.dependency_overrides[get_voice_registry].
    """
    if _voice_registry is None:
        raise RuntimeError(
            "VoiceProviderRegistry not initialized — call set_voice_registry() in lifespan"
        )
    return _voice_registry


def _get_active_user_by_id(db: Session, user_id: str) -> User | None:
    return db.query(User).filter(User.id == user_id, User.is_active.is_(True)).first()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_security),
    db: Session = Depends(get_db),
) -> User:
    try:
        user_id = decode_token(credentials.credentials)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Connection-retention follow-up (see 9378494's own analysis): this was
    # a synchronous SQLAlchemy call running directly on the event loop —
    # the ONLY un-threaded DB touch in the whole /chat path. Under pool
    # contention, that blocks the entire event loop (not just one of the
    # executor's worker threads) waiting for a connection, which can freeze
    # the whole process. asyncio.to_thread() moves it off the loop, exactly
    # like every other DB call in this codebase already does — same query,
    # same filters, same behavior, no semantic change.
    user = await asyncio.to_thread(_get_active_user_by_id, db, user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or account inactive",
        )
    if _issued_before_password_change(credentials.credentials, user):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def _issued_before_password_change(token: str, user: User) -> bool:
    """A new password ends every token issued under the old one. A token with
    no issue time in milliseconds predates this check, so it ends too."""
    changed = user.password_changed_at
    if changed is None:
        return False
    if changed.tzinfo is None:
        changed = changed.replace(tzinfo=timezone.utc)
    issued = token_issued_at_ms(token)
    return issued is None or issued < int(changed.timestamp() * 1000)


async def get_current_admin_user(current_user: User = Depends(get_current_user)) -> User:
    """Etapa 5H — the ONLY admin-authorization gate in this codebase.

    Deliberately layered ON TOP of get_current_user() (same JWT, same
    session, same is_active check) rather than a second auth mechanism —
    an admin is just a User with is_admin=True, authenticated exactly like
    any other request. 403 (not 404) is intentional: unlike a resource
    lookup, there is no ambiguity to hide here — the caller already proved
    who they are via a valid token, this only decides whether that
    identity is authorized for admin routes.
    """
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required.",
        )
    return current_user
