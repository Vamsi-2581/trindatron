"""TEAM by seat — owners buy seats, members hold them, payment makes them real.

Rules this module enforces (docs/TEAM_SEATS.md):

* Prices and seat plans come only from src/billing/team_pricing.py. The client
  sends seat counts; the amount is always computed here.
* 2–150 seats per team, Standard and Premium mixed freely.
* A seat is usable only while the team is `active`, and a team becomes active
  only when a payment for exactly the seats in its pending checkout is
  confirmed (PaymentOrderService.confirm_order → activate_from_order). An
  unpaid team grants nobody anything.
* Holding a seat sets the member's plan to the seat's plan and users.team_id to
  the team; leaving, being removed or the team lapsing puts the member back on
  FREE. No pool, no overage, no invented quota.
* An invitation is accepted only by the account whose e-mail it was sent to,
  so a forwarded link cannot hand a paid seat to someone else.
* A member who already pays for an individual plan must end it first — joining
  never silently overwrites a plan someone is paying for.
* Fewer seats take effect at the next activation, never below the seats in use.
  More seats are a new checkout for the new monthly total; the new period
  starts when it is paid (no proration in this version).
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session as DBSession

from src.billing.team_pricing import (
    SEAT_PLAN,
    SEAT_PRICES_USD,
    SEAT_TYPES,
    InvalidSeatCount,
    team_monthly_price,
    validate_seat_counts,
)
from src.models.payment_order import PaymentOrder
from src.models.team import Team, TeamMember
from src.models.user import User

logger = logging.getLogger("trinity.teams")

BILLING_PERIOD = timedelta(days=30)
INVITE_TTL = timedelta(days=7)
SEAT_HOLDING_STATUSES = ("invited", "pending", "active")


class TeamRefused(Exception):
    STATUS = {
        "not_found": 404, "forbidden": 403, "invalid_seats": 422, "invalid": 422, "already_in_team": 409,
        "team_not_active": 409, "no_seat_available": 409, "individual_plan_active": 409,
        "seats_in_use": 409, "invite_expired": 410, "owner_cannot_leave": 409,
        "unsupported_currency": 409,
    }
    MESSAGES = {
        "not_found": "Not found.",
        "forbidden": "Only the team owner or an admin can do this.",
        "invalid_seats": "A team has between 2 and 150 seats.",
        "invalid": "Check the request and try again.",
        "already_in_team": "This account already belongs to a team.",
        "team_not_active": "The team's seats are not active yet.",
        "no_seat_available": "There is no free seat of this type.",
        "individual_plan_active": "End your current plan before joining a team.",
        "seats_in_use": "More seats of this type are in use than requested.",
        "invite_expired": "This invitation has expired.",
        "owner_cannot_leave": "The owner's seat cannot be removed.",
        "unsupported_currency": "Seats are not priced in the requested currency.",
    }

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = self.STATUS[code]
        self.message = self.MESSAGES[code]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


# ── Reading ──────────────────────────────────────────────────────────────────

def membership(db: DBSession, user: User) -> TeamMember | None:
    return (
        db.query(TeamMember)
        .filter(TeamMember.user_id == user.id, TeamMember.status.in_(("pending", "active")))
        .first()
    )


def team_for(db: DBSession, user: User) -> tuple[Team, TeamMember]:
    member = membership(db, user)
    team = db.query(Team).filter(Team.id == member.team_id).first() if member else None
    if member is None or team is None:
        raise TeamRefused("not_found")
    return team, member


def _manageable(db: DBSession, user: User, team_id: str, *, owner_only: bool = False) -> tuple[Team, TeamMember]:
    team, member = team_for(db, user)
    if team.id != team_id:
        raise TeamRefused("not_found")
    allowed = ("owner",) if owner_only else ("owner", "admin")
    if member.role not in allowed:
        raise TeamRefused("forbidden")
    return team, member


def seats_used(db: DBSession, team: Team, seat_type: str) -> int:
    return (
        db.query(TeamMember)
        .filter(TeamMember.team_id == team.id, TeamMember.seat_type == seat_type,
                TeamMember.status.in_(SEAT_HOLDING_STATUSES))
        .count()
    )


def _purchased(team: Team, seat_type: str) -> int:
    return team.standard_seats if seat_type == "standard" else team.premium_seats


def team_view(db: DBSession, team: Team, viewer: TeamMember) -> dict:
    view = {
        "id": team.id,
        "name": team.name,
        "status": team.status,
        "my_role": viewer.role,
        "my_seat_type": viewer.seat_type,
        "current_period_end": _aware(team.current_period_end).isoformat() if team.current_period_end else None,
        "seats": {
            seat: {"purchased": _purchased(team, seat), "used": seats_used(db, team, seat),
                   "price_usd": str(SEAT_PRICES_USD[seat])}
            for seat in SEAT_TYPES
        },
        "pending_seats": (
            {"standard": team.pending_standard_seats, "premium": team.pending_premium_seats}
            if team.pending_standard_seats is not None else None
        ),
        "monthly_total_usd": str(
            SEAT_PRICES_USD["standard"] * team.standard_seats + SEAT_PRICES_USD["premium"] * team.premium_seats
        ),
    }
    if viewer.role in ("owner", "admin"):
        rows = db.query(TeamMember).filter(TeamMember.team_id == team.id).order_by(TeamMember.created_at.asc()).all()
        view["members"] = [
            {"id": row.id, "email": row.email, "role": row.role, "seat_type": row.seat_type, "status": row.status,
             "joined_at": _aware(row.joined_at).isoformat() if row.joined_at else None}
            for row in rows
        ]
    return view


# ── Membership ↔ plan ────────────────────────────────────────────────────────

def _grant(user: User, team: Team, seat_type: str) -> None:
    user.plan = SEAT_PLAN[seat_type]
    user.team_id = team.id


def _revoke(user: User | None, team: Team) -> None:
    if user is not None and user.team_id == team.id:
        user.plan = "free"
        user.team_id = None


# ── Creating and paying ──────────────────────────────────────────────────────

def create_team(db: DBSession, owner: User, *, name: str, owner_seat_type: str) -> Team:
    name = " ".join((name or "").split())
    if not name or len(name) > 120 or owner_seat_type not in SEAT_TYPES:
        raise TeamRefused("invalid")
    if membership(db, owner) is not None:
        raise TeamRefused("already_in_team")
    if owner.plan != "free":
        raise TeamRefused("individual_plan_active")
    team = Team(owner_id=owner.id, name=name, status="pending_payment")
    db.add(team)
    db.flush()
    db.add(TeamMember(team_id=team.id, user_id=owner.id, email=_normalize_email(owner.email), role="owner",
                      seat_type=owner_seat_type, status="pending", joined_at=_now()))
    db.commit()
    db.refresh(team)
    return team


@dataclass(frozen=True)
class SeatCheckout:
    team: Team
    standard: int
    premium: int
    amount: Decimal
    currency: str


def prepare_checkout(
    db: DBSession, owner: User, team_id: str, *, standard: int, premium: int, currency: str = "USD",
) -> SeatCheckout:
    """Validate a seat purchase and compute its price in `currency`. Nothing is
    written yet. `currency` defaults to USD so every pre-existing single-
    keyword caller is unaffected."""
    team, _ = _manageable(db, owner, team_id, owner_only=True)
    try:
        amount = team_monthly_price(standard, premium, currency)
    except InvalidSeatCount as exc:
        raise TeamRefused("invalid_seats") from exc
    if amount is None:
        raise TeamRefused("unsupported_currency")
    for seat, requested in (("standard", standard), ("premium", premium)):
        if requested < seats_used(db, team, seat):
            raise TeamRefused("seats_in_use")
    return SeatCheckout(team, standard, premium, amount, currency.upper())


def attach_checkout(db: DBSession, checkout: SeatCheckout, order: PaymentOrder) -> None:
    """Record the order whose confirmation will activate exactly these seats."""
    order.team_id = checkout.team.id
    checkout.team.checkout_order_id = order.id
    checkout.team.checkout_standard_seats = checkout.standard
    checkout.team.checkout_premium_seats = checkout.premium
    db.commit()


def request_fewer_seats(db: DBSession, owner: User, team_id: str, *, standard: int, premium: int) -> Team:
    """A reduction applied at the next activation; never below the seats in use."""
    team, _ = _manageable(db, owner, team_id, owner_only=True)
    try:
        validate_seat_counts(standard, premium)
    except InvalidSeatCount as exc:
        raise TeamRefused("invalid_seats") from exc
    if standard > team.standard_seats or premium > team.premium_seats:
        raise TeamRefused("invalid_seats")
    if standard < seats_used(db, team, "standard") or premium < seats_used(db, team, "premium"):
        raise TeamRefused("seats_in_use")
    team.pending_standard_seats, team.pending_premium_seats = standard, premium
    db.commit()
    return team


def activate_from_order(db: DBSession, order: PaymentOrder, *, now: datetime | None = None) -> Team:
    """Called inside PaymentOrderService.confirm_order's transaction (flush only).

    Refuses — raising, so the whole confirmation rolls back — any order that is
    not the team's own pending checkout: a seat can only be activated by the
    payment that was created for it.
    """
    now = now or _now()
    team = db.query(Team).filter(Team.id == order.team_id).first() if order.team_id else None
    if team is None or team.owner_id != order.user_id or team.checkout_order_id != order.id:
        raise ValueError("seat order does not match the team's pending checkout")
    standard, premium = team.checkout_standard_seats or 0, team.checkout_premium_seats or 0
    expected = team_monthly_price(standard, premium, order.currency_fiat)
    if expected is None or Decimal(str(order.amount_fiat)) != expected:
        raise ValueError("seat order amount does not match the seat prices")

    team.standard_seats, team.premium_seats = standard, premium
    if team.pending_standard_seats is not None:
        team.standard_seats = min(team.standard_seats, team.pending_standard_seats)
        team.premium_seats = min(team.premium_seats, team.pending_premium_seats or 0)
        team.pending_standard_seats = team.pending_premium_seats = None
    team.status = "active"
    team.current_period_end = now + BILLING_PERIOD
    team.checkout_order_id = team.checkout_standard_seats = team.checkout_premium_seats = None

    # Members who accepted while the team was unpaid take their seats now, in
    # the order they joined, up to what was paid for.
    granted = {seat: 0 for seat in SEAT_TYPES}
    members = (
        db.query(TeamMember)
        .filter(TeamMember.team_id == team.id, TeamMember.status.in_(("pending", "active")))
        .order_by(TeamMember.role.desc(), TeamMember.joined_at.asc())
        .all()
    )
    for member in sorted(members, key=lambda m: (m.role != "owner", _aware(m.joined_at) or now)):
        user = db.query(User).filter(User.id == member.user_id).first() if member.user_id else None
        if user is not None and granted[member.seat_type] < _purchased(team, member.seat_type):
            granted[member.seat_type] += 1
            member.status = "active"
            _grant(user, team, member.seat_type)
        else:
            member.status = "pending"
            _revoke(user, team)
    db.flush()
    logger.info("teams: seats activated | team=%s standard=%d premium=%d", team.id, team.standard_seats, team.premium_seats)
    return team


def lapse(db: DBSession, team: Team, *, status: str = "past_due") -> None:
    """Payment stopped: every seat stops granting its plan, immediately."""
    team.status = status
    for member in db.query(TeamMember).filter(TeamMember.team_id == team.id, TeamMember.status == "active").all():
        member.status = "pending"
        user = db.query(User).filter(User.id == member.user_id).first() if member.user_id else None
        _revoke(user, team)
    db.commit()


# ── Invitations and seats ────────────────────────────────────────────────────

def invite(db: DBSession, actor: User, team_id: str, *, email: str, seat_type: str, role: str = "member") -> tuple[TeamMember, str]:
    team, actor_member = _manageable(db, actor, team_id)
    email = _normalize_email(email)
    if "@" not in email or seat_type not in SEAT_TYPES or role not in ("admin", "member"):
        raise TeamRefused("invalid")
    if role == "admin" and actor_member.role != "owner":
        raise TeamRefused("forbidden")
    if team.status != "active":
        raise TeamRefused("team_not_active")
    if seats_used(db, team, seat_type) >= _purchased(team, seat_type):
        raise TeamRefused("no_seat_available")
    existing = db.query(TeamMember).filter(TeamMember.team_id == team.id, TeamMember.email == email).first()
    if existing is not None:
        raise TeamRefused("already_in_team")
    token = secrets.token_urlsafe(32)
    member = TeamMember(team_id=team.id, email=email, role=role, seat_type=seat_type, status="invited",
                        invite_token_hash=_hash(token), invite_expires_at=_now() + INVITE_TTL)
    db.add(member)
    db.commit()
    db.refresh(member)
    return member, token


def accept(db: DBSession, user: User, *, token: str) -> tuple[Team, TeamMember]:
    member = db.query(TeamMember).filter(TeamMember.invite_token_hash == _hash(token or "")).first()
    # Wrong token and someone else's invitation look the same.
    if member is None or member.status != "invited" or member.email != _normalize_email(user.email):
        raise TeamRefused("not_found")
    if _aware(member.invite_expires_at) is not None and _aware(member.invite_expires_at) <= _now():
        raise TeamRefused("invite_expired")
    if membership(db, user) is not None:
        raise TeamRefused("already_in_team")
    if user.plan != "free":
        raise TeamRefused("individual_plan_active")
    team = db.query(Team).filter(Team.id == member.team_id).first()
    if team is None:
        raise TeamRefused("not_found")
    member.user_id, member.joined_at, member.invite_token_hash = user.id, _now(), None
    if team.status == "active":
        member.status = "active"
        _grant(user, team, member.seat_type)
    else:
        member.status = "pending"
    db.commit()
    return team, member


def remove_member(db: DBSession, actor: User, team_id: str, member_id: str) -> None:
    team, actor_member = _manageable(db, actor, team_id)
    member = db.query(TeamMember).filter(TeamMember.id == member_id, TeamMember.team_id == team.id).first()
    if member is None:
        raise TeamRefused("not_found")
    if member.role == "owner":
        raise TeamRefused("owner_cannot_leave")
    if member.role == "admin" and actor_member.role != "owner":
        raise TeamRefused("forbidden")
    user = db.query(User).filter(User.id == member.user_id).first() if member.user_id else None
    _revoke(user, team)
    db.delete(member)
    db.commit()


def change_member(db: DBSession, actor: User, team_id: str, member_id: str, *, seat_type: str | None = None,
                  role: str | None = None) -> TeamMember:
    team, actor_member = _manageable(db, actor, team_id)
    member = db.query(TeamMember).filter(TeamMember.id == member_id, TeamMember.team_id == team.id).first()
    if member is None:
        raise TeamRefused("not_found")
    if role is not None:
        if actor_member.role != "owner" or member.role == "owner" or role not in ("admin", "member"):
            raise TeamRefused("forbidden")
        member.role = role
    if seat_type is not None and seat_type != member.seat_type:
        if seat_type not in SEAT_TYPES:
            raise TeamRefused("invalid")
        if seats_used(db, team, seat_type) >= _purchased(team, seat_type):
            raise TeamRefused("no_seat_available")
        member.seat_type = seat_type
        if member.status == "active" and member.user_id:
            user = db.query(User).filter(User.id == member.user_id).first()
            if user is not None:
                _grant(user, team, seat_type)
    db.commit()
    db.refresh(member)
    return member


def seat_backs_plan(db: DBSession, user: User) -> bool:
    """Evidence for plan reconciliation: an active seat in an active team grants this plan."""
    member = (
        db.query(TeamMember)
        .join(Team, TeamMember.team_id == Team.id)
        .filter(TeamMember.user_id == user.id, TeamMember.status == "active", Team.status == "active")
        .first()
    )
    return member is not None and SEAT_PLAN.get(member.seat_type) == user.plan
