"""Refresh tokens — what keeps a TRINDATRON login alive.

Access tokens are short-lived JWTs. A refresh token is a random secret the
client exchanges for a new access token (and a new refresh token) whenever
the old access token expires, so a session survives background, lock screen,
reload and restart without the user signing in again.

Only a SHA-256 hash is stored. Tokens of one login share a family_id; every
rotation adds a row to the family and marks the previous one rotated. A
rotated token presented again after the concurrency grace window means it
was copied, and the whole family is revoked (replay protection).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    #: Every token issued for one sign-in shares this id; revocation acts on it.
    family_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    #: SHA-256 hex of the token. The token itself is never stored.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: "web" (httpOnly cookie) | "desktop" | "mobile" (client secure storage).
    client: Mapped[str] = mapped_column(String(20), nullable=False, default="web")
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Set the first time this token is exchanged.
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: "logout" | "reuse_detected" | "user_inactive" | "admin".
    revoked_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
