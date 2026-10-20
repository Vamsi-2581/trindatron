"""TEAM by seat: a team, its paid seats and the people holding them.

A seat becomes usable only when the team is `active`, and a team becomes active
only when a payment for its seats is confirmed (PaymentOrderService.
confirm_order → team_service.activate_from_order). While a member holds an
active seat their effective plan is the seat's plan (src/billing/team_pricing.py)
and `users.team_id` points here.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from src.database import Base

TEAM_STATUSES = ("pending_payment", "active", "past_due", "cancelled")
MEMBER_ROLES = ("owner", "admin", "member")
MEMBER_STATUSES = ("invited", "pending", "active")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    owner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending_payment")
    # Paid, usable seats.
    standard_seats: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    premium_seats: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # A reduction the owner asked for, applied when the next payment activates.
    pending_standard_seats: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pending_premium_seats: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The checkout in progress: what its confirmation will activate.
    checkout_order_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    checkout_standard_seats: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checkout_premium_seats: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class TeamMember(Base):
    __tablename__ = "team_members"
    __table_args__ = (UniqueConstraint("team_id", "email", name="uq_team_members_team_email"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    team_id: Mapped[str] = mapped_column(String(36), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(10), nullable=False, default="member")
    seat_type: Mapped[str] = mapped_column(String(10), nullable=False, default="standard")
    # invited: link sent, not accepted · pending: accepted, team not active yet · active: seat in use
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="invited")
    invite_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    invite_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
