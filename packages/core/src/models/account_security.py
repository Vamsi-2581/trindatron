"""Account security records: single-use email links, and the account's security log.

An AccountToken is the secret behind a "reset your password" or "confirm your
email" link. Like refresh tokens, only a SHA-256 hash is stored, so a database
leak does not hand out working links. A token is good once, until expires_at.

A SecurityEvent is one line of the account's security history — sign-ins, wrong
passwords, lockouts, password and email changes — with the address and device it
came from. The owner can read it (GET /auth/security-events); it never holds a
password, a token or a link.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AccountToken(Base):
    __tablename__ = "account_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    #: "password_reset" | "email_verify".
    purpose: Mapped[str] = mapped_column(String(20), nullable=False)
    #: SHA-256 hex of the token. The token itself is only ever in the email.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: The address the link was issued for; a link stops working if the account's email changes.
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Set when the link is used, or when a newer link of the same purpose replaces it.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SecurityEvent(Base):
    __tablename__ = "security_events"
    __table_args__ = (Index("ix_security_events_user_created", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    #: See account_security_service.EVENT_KINDS.
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    #: "password" | "google" | None when the event is not a sign-in.
    method: Mapped[str | None] = mapped_column(String(20), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
