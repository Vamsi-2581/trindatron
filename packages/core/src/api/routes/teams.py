"""TEAM by seat: pricing, team, checkout, invitations, members.

Payment follows the same path as every plan: an order is created server-side
with a server-computed amount, and only its verified confirmation activates the
seats (see services/team_service.activate_from_order).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DBSession

from src.api.deps import get_current_user, get_provider_registry
from src.billing.payment_router import AllProvidersRejected, NoEligibleProvider, resolve_default_currency, route_checkout
from src.billing.providers.registry import ProviderRegistry
from src.billing.team_pricing import (
    MAX_SEATS,
    MIN_SEATS,
    SEAT_PLAN,
    SEAT_PRICES,
    SEAT_PRICES_USD,
    TEAM_SEATS_PLAN,
    seat_currency_candidates,
)
from src.config import settings
from src.database import get_db
from src.models.user import User
from src.services import team_service
from src.services.payment_order_service import PaymentOrderService
from src.services.team_service import TeamRefused

router = APIRouter(prefix="/teams", tags=["teams"])


def _http(exc: TeamRefused) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"error": exc.code, "message": exc.message})


class CreateTeamBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    owner_seat_type: str = "standard"


class SeatsBody(BaseModel):
    standard_seats: int = Field(ge=0, le=MAX_SEATS)
    premium_seats: int = Field(ge=0, le=MAX_SEATS)


class CheckoutBody(SeatsBody):
    idempotency_key: str = Field(min_length=8, max_length=128)


class InviteBody(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    seat_type: str = "standard"
    role: str = "member"


class AcceptBody(BaseModel):
    token: str = Field(min_length=16, max_length=128)


class ChangeMemberBody(BaseModel):
    seat_type: str | None = None
    role: str | None = None


@router.get("/pricing")
def pricing(registry: ProviderRegistry = Depends(get_provider_registry)):
    """The only seat prices any client shows.

    `currency` is the SAME B2 default POST /{team_id}/checkout would
    resolve for a checkout with no explicit provider (Mercado Pago
    registered and no USD-capable provider -> BRL; falls back to USD
    when no registered provider is eligible for any priced currency —
    see resolve_default_currency()'s own docstring). `price_usd` is kept
    on every seat, unchanged, for existing/mobile clients that read it
    as a fixed USD reference regardless of what is actually charged;
    `price` is the amount in the resolved `currency`."""
    currency = resolve_default_currency(registry, catalog_currencies=seat_currency_candidates()) or "USD"
    return {
        "seats": {
            seat: {
                "price_usd": str(SEAT_PRICES_USD[seat]),
                "price": str(SEAT_PRICES[seat].get(currency, SEAT_PRICES_USD[seat])),
                "entitlement": SEAT_PLAN[seat],
            }
            for seat in SEAT_PRICES_USD
        },
        "min_seats": MIN_SEATS,
        "max_seats": MAX_SEATS,
        "currency": currency,
        "billing_period": "month",
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_team(body: CreateTeamBody, db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        team = team_service.create_team(db, current_user, name=body.name, owner_seat_type=body.owner_seat_type)
        _, member = team_service.team_for(db, current_user)
    except TeamRefused as exc:
        raise _http(exc)
    return team_service.team_view(db, team, member)


@router.get("/me")
def my_team(db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        team, member = team_service.team_for(db, current_user)
    except TeamRefused as exc:
        raise _http(exc)
    return team_service.team_view(db, team, member)


@router.post("/{team_id}/checkout", status_code=status.HTTP_201_CREATED)
async def checkout(team_id: str, body: CheckoutBody, db: DBSession = Depends(get_db),
                   current_user: User = Depends(get_current_user),
                   registry: ProviderRegistry = Depends(get_provider_registry)):
    from src.api.routes.billing import _check_order_rate_limit

    await _check_order_rate_limit(current_user)

    # B2: resolve which of the seat catalog's priced currencies an
    # eligible registered provider can actually accept — Mercado Pago
    # -> BRL, a USD-capable provider -> USD unchanged. No eligible
    # provider for any priced currency falls back to USD, the exact
    # pre-existing default (and what the NoEligibleProvider/"manual"
    # fallback path below still ends up recording).
    currency = resolve_default_currency(registry, catalog_currencies=seat_currency_candidates()) or "USD"
    try:
        plan = team_service.prepare_checkout(db, current_user, team_id, standard=body.standard_seats,
                                             premium=body.premium_seats, currency=currency)
    except TeamRefused as exc:
        raise _http(exc)

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=settings.checkout_order_ttl_seconds)
    intent = None
    try:
        order, intent = await route_checkout(
            db, registry, user_id=current_user.id, plan=TEAM_SEATS_PLAN, amount_fiat=plan.amount,
            currency_fiat=plan.currency, credits_amount=plan.amount, currency_credits=plan.currency,
            idempotency_key=body.idempotency_key, expires_at=expires_at, payment_method=None, payment_type=None,
        )
    except NoEligibleProvider:
        # No payment provider in this deployment: a pending order is recorded
        # and can only be confirmed by a verified payment later — never by the client.
        order = PaymentOrderService(db).create_order(
            user_id=current_user.id, provider="manual", amount_fiat=plan.amount, currency_fiat=plan.currency,
            credits_amount=plan.amount, currency_credits=plan.currency, idempotency_key=body.idempotency_key,
            expires_at=expires_at, plan=TEAM_SEATS_PLAN,
        )
    except AllProvidersRejected:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Payment is temporarily unavailable. Please try again shortly.")

    if order.plan != TEAM_SEATS_PLAN or order.team_id not in (None, plan.team.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This checkout key was already used.")
    team_service.attach_checkout(db, plan, order)
    return {
        "order": {"id": order.id, "status": order.status, "amount_fiat": str(order.amount_fiat),
                  "currency_fiat": order.currency_fiat, "expires_at": order.expires_at.isoformat()},
        "seats": {"standard": plan.standard, "premium": plan.premium},
        "redirect_url": intent.redirect_url if intent else None,
        "client_secret": intent.client_secret if intent else None,
    }


@router.post("/{team_id}/seats/reduce")
def reduce_seats(team_id: str, body: SeatsBody, db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        team = team_service.request_fewer_seats(db, current_user, team_id, standard=body.standard_seats,
                                                premium=body.premium_seats)
        _, member = team_service.team_for(db, current_user)
    except TeamRefused as exc:
        raise _http(exc)
    return team_service.team_view(db, team, member)


@router.post("/{team_id}/invites", status_code=status.HTTP_201_CREATED)
def invite(team_id: str, body: InviteBody, db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        member, token = team_service.invite(db, current_user, team_id, email=body.email, seat_type=body.seat_type,
                                            role=body.role)
    except TeamRefused as exc:
        raise _http(exc)
    # TRINDATRON does not send e-mail yet: the owner shares this token (as a link) themself.
    return {"member_id": member.id, "email": member.email, "seat_type": member.seat_type, "role": member.role,
            "invite_token": token, "expires_at": member.invite_expires_at.isoformat()}


@router.post("/invites/accept")
def accept(body: AcceptBody, db: DBSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    try:
        team, member = team_service.accept(db, current_user, token=body.token)
    except TeamRefused as exc:
        raise _http(exc)
    return team_service.team_view(db, team, member)


@router.patch("/{team_id}/members/{member_id}")
def change_member(team_id: str, member_id: str, body: ChangeMemberBody, db: DBSession = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    try:
        team_service.change_member(db, current_user, team_id, member_id, seat_type=body.seat_type, role=body.role)
        team, member = team_service.team_for(db, current_user)
    except TeamRefused as exc:
        raise _http(exc)
    return team_service.team_view(db, team, member)


@router.delete("/{team_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(team_id: str, member_id: str, db: DBSession = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    try:
        team_service.remove_member(db, current_user, team_id, member_id)
    except TeamRefused as exc:
        raise _http(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
