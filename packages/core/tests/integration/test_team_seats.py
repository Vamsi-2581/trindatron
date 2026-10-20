"""TEAM by seat end to end: buy, pay, invite, hold, change, remove, lapse — and abuse.

Payment confirmation is driven through PaymentOrderService.confirm_order with a
provider payload, exactly as a verified webhook does. The client can never
confirm or price anything.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.api.deps import get_provider_registry
from src.billing.providers.base import PaymentIntent, PaymentProvider, PaymentStatus
from src.billing.providers.registry import ProviderRegistry
from src.main import app
from src.models.payment_order import PaymentOrder
from src.models.team import Team, TeamMember
from src.models.user import User
from src.services import team_service
from src.services.payment_order_service import PaymentOrderService
from src.services.plan_reconciliation import find_unbacked_paid_accounts


@pytest.fixture(autouse=True)
def no_payment_providers():
    app.dependency_overrides[get_provider_registry] = lambda: ProviderRegistry()
    yield
    app.dependency_overrides.pop(get_provider_registry, None)


class _FakeMercadoPagoLikeAdapter(PaymentProvider):
    """BRL-only, like the real MercadoPagoAdapter (see mercadopago_adapter.
    py's supported_currencies — no USD) — enough to prove B2 resolves TEAM
    seat checkouts to BRL when this is the only registered provider."""

    @property
    def provider_name(self) -> str:
        return "mercado_pago"

    @property
    def supported_payment_methods(self):
        return frozenset()

    @property
    def supported_payment_types(self):
        return frozenset()

    @property
    def supported_currencies(self):
        return frozenset({"BRL"})

    async def create_intent(self, *, amount, currency, user_id, idempotency_key, metadata=None) -> PaymentIntent:
        return PaymentIntent(
            id=f"ext-{idempotency_key}", provider="mercado_pago", amount=amount, currency=currency.upper(),
            status=PaymentStatus.PENDING, idempotency_key=idempotency_key,
            redirect_url="https://mercadopago.example/checkout",
        )

    async def get_intent(self, intent_id: str):
        raise NotImplementedError

    async def verify_webhook(self, payload: bytes, headers):
        raise NotImplementedError

    async def refund(self, payment_id: str, **kwargs):
        raise NotImplementedError


def _register(client, email: str) -> dict:
    response = client.post("/auth/register", json={"email": email, "password": "securepass123", "full_name": email.split("@")[0]})
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['access_token']}", "user_id": response.json()["user_id"], "email": email}


def _h(account: dict) -> dict:
    return {"Authorization": account["Authorization"]}


def _plan(db, account) -> str:
    db.expire_all()
    return db.query(User).filter(User.id == account["user_id"]).one().plan


def _create(client, owner, seat="standard"):
    response = client.post("/teams", headers=_h(owner), json={"name": "Agência Norte", "owner_seat_type": seat})
    assert response.status_code == 201
    return response.json()


def _checkout(client, owner, team_id, standard, premium, key="checkout-0001"):
    return client.post(f"/teams/{team_id}/checkout", headers=_h(owner),
                       json={"standard_seats": standard, "premium_seats": premium, "idempotency_key": key})


def _pay(db, order_id: str, owner) -> None:
    """A verified payment: the provider's payload is recorded with the confirmation."""
    PaymentOrderService(db).confirm_order(order_id, owner["user_id"], webhook_raw={"provider": "test", "paid": True})


def _active_team(client, db, standard=2, premium=1, seat="standard"):
    owner = _register(client, "owner@agencia.com")
    team = _create(client, owner, seat)
    order = _checkout(client, owner, team["id"], standard, premium).json()["order"]
    _pay(db, order["id"], owner)
    return owner, team


def _invite(client, actor, team_id, email, seat="standard", role="member"):
    return client.post(f"/teams/{team_id}/invites", headers=_h(actor), json={"email": email, "seat_type": seat, "role": role})


# ── Buying ───────────────────────────────────────────────────────────────────

def test_seat_prices_are_published_by_the_server(client):
    body = client.get("/teams/pricing").json()
    assert body["seats"] == {"standard": {"price_usd": "24.90", "price": "24.90", "entitlement": "pro"},
                             "premium": {"price_usd": "99.90", "price": "99.90", "entitlement": "max"}}
    assert (body["min_seats"], body["max_seats"]) == (2, 150)
    assert body["currency"] == "USD"


def test_the_amount_is_computed_from_the_seats_never_taken_from_the_client(client, db):
    owner = _register(client, "owner@agencia.com")
    team = _create(client, owner)
    response = client.post(f"/teams/{team['id']}/checkout", headers=_h(owner),
                           json={"standard_seats": 3, "premium_seats": 1, "idempotency_key": "k-00000001",
                                 "amount_fiat": "0.01"})
    assert response.status_code == 201
    assert response.json()["order"]["amount_fiat"] == "174.60000000"
    order = db.query(PaymentOrder).one()
    assert order.plan == "team_seats" and order.team_id == team["id"] and order.amount_fiat == Decimal("174.60")


@pytest.mark.parametrize("standard,premium", [(1, 0), (151, 0), (100, 51)])
def test_teams_are_between_2_and_150_seats(client, standard, premium):
    owner = _register(client, "owner@agencia.com")
    team = _create(client, owner)
    response = _checkout(client, owner, team["id"], standard, premium)
    assert response.status_code == 422


def test_a_retried_checkout_returns_the_same_order(client, db):
    owner = _register(client, "owner@agencia.com")
    team = _create(client, owner)
    first = _checkout(client, owner, team["id"], 2, 0).json()["order"]["id"]
    second = _checkout(client, owner, team["id"], 2, 0).json()["order"]["id"]
    assert first == second and db.query(PaymentOrder).count() == 1


def test_an_unpaid_team_grants_nothing_and_cannot_invite(client, db):
    owner = _register(client, "owner@agencia.com")
    team = _create(client, owner)
    _checkout(client, owner, team["id"], 2, 0)
    assert _plan(db, owner) == "free"
    refused = _invite(client, owner, team["id"], "ana@agencia.com")
    assert refused.status_code == 409 and refused.json()["detail"]["error"] == "team_not_active"


def test_a_verified_payment_activates_the_seats_and_the_owners_plan(client, db):
    owner, team = _active_team(client, db, standard=2, premium=1, seat="premium")
    view = client.get("/teams/me", headers=_h(owner)).json()
    assert view["status"] == "active"
    assert view["seats"]["standard"] == {"purchased": 2, "used": 0, "price_usd": "24.90"}
    assert view["seats"]["premium"] == {"purchased": 1, "used": 1, "price_usd": "99.90"}
    assert view["monthly_total_usd"] == "149.70"
    assert _plan(db, owner) == "max"
    assert client.get("/users/me/entitlement", headers=_h(owner)).json()["plan"] == "max"


def test_a_payment_for_other_seats_cannot_activate_a_team(client, db):
    owner = _register(client, "owner@agencia.com")
    team = _create(client, owner)
    order = _checkout(client, owner, team["id"], 2, 0).json()["order"]
    # A forged or stale order: amount no longer matches the pending checkout.
    db.query(PaymentOrder).filter(PaymentOrder.id == order["id"]).update({"amount_fiat": Decimal("1.00")})
    db.commit()
    with pytest.raises(Exception):
        _pay(db, order["id"], owner)
    db.rollback()
    db.expire_all()
    assert db.query(Team).one().status == "pending_payment"
    assert _plan(db, owner) == "free"


def test_the_client_cannot_confirm_a_seat_order(client, db, monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "billing_client_confirm_enabled", False)
    owner = _register(client, "owner@agencia.com")
    team = _create(client, owner)
    order = _checkout(client, owner, team["id"], 2, 0).json()["order"]
    response = client.post(f"/billing/orders/{order['id']}/confirm", headers=_h(owner))
    assert response.status_code in (403, 409, 422)
    assert _plan(db, owner) == "free"


# ── Holding seats ────────────────────────────────────────────────────────────

def test_an_invited_member_gets_the_seats_plan_when_they_accept(client, db):
    owner, team = _active_team(client, db, standard=2, premium=1)
    ana = _register(client, "ana@agencia.com")
    invite = _invite(client, owner, team["id"], "Ana@Agencia.com").json()
    assert "invite_token" in invite

    joined = client.post("/teams/invites/accept", headers=_h(ana), json={"token": invite["invite_token"]})
    assert joined.status_code == 200
    assert _plan(db, ana) == "pro"
    assert client.get("/users/me/entitlement", headers=_h(ana)).json()["plan"] == "pro"
    # A token works once.
    assert client.post("/teams/invites/accept", headers=_h(ana), json={"token": invite["invite_token"]}).status_code == 404


def test_a_forwarded_invitation_cannot_be_used_by_another_account(client, db):
    owner, team = _active_team(client, db)
    token = _invite(client, owner, team["id"], "ana@agencia.com").json()["invite_token"]
    intruder = _register(client, "intruder@example.com")
    assert client.post("/teams/invites/accept", headers=_h(intruder), json={"token": token}).status_code == 404
    assert _plan(db, intruder) == "free"


def test_seats_cannot_be_used_beyond_what_was_paid(client, db):
    owner, team = _active_team(client, db, standard=2, premium=1)  # owner holds a standard seat
    assert _invite(client, owner, team["id"], "a@agencia.com").status_code == 201
    over = _invite(client, owner, team["id"], "b@agencia.com")
    assert over.status_code == 409 and over.json()["detail"]["error"] == "no_seat_available"
    assert _invite(client, owner, team["id"], "c@agencia.com", seat="premium").status_code == 201
    assert _invite(client, owner, team["id"], "d@agencia.com", seat="premium").status_code == 409


def test_someone_paying_for_an_individual_plan_must_end_it_before_joining(client, db):
    owner, team = _active_team(client, db)
    paying = _register(client, "paying@agencia.com")
    db.query(User).filter(User.id == paying["user_id"]).update({"plan": "max"})
    db.commit()
    token = _invite(client, owner, team["id"], "paying@agencia.com").json()["invite_token"]
    refused = client.post("/teams/invites/accept", headers=_h(paying), json={"token": token})
    assert refused.status_code == 409 and refused.json()["detail"]["error"] == "individual_plan_active"
    assert _plan(db, paying) == "max"


def test_moving_a_member_to_premium_changes_their_plan(client, db):
    owner, team = _active_team(client, db, standard=2, premium=1, seat="standard")
    ana = _register(client, "ana@agencia.com")
    token = _invite(client, owner, team["id"], "ana@agencia.com").json()["invite_token"]
    client.post("/teams/invites/accept", headers=_h(ana), json={"token": token})
    member_id = db.query(TeamMember).filter(TeamMember.email == "ana@agencia.com").one().id

    moved = client.patch(f"/teams/{team['id']}/members/{member_id}", headers=_h(owner), json={"seat_type": "premium"})
    assert moved.status_code == 200 and _plan(db, ana) == "max"


def test_removing_a_member_returns_them_to_free_and_frees_the_seat(client, db):
    owner, team = _active_team(client, db, standard=2, premium=0)
    ana = _register(client, "ana@agencia.com")
    token = _invite(client, owner, team["id"], "ana@agencia.com").json()["invite_token"]
    client.post("/teams/invites/accept", headers=_h(ana), json={"token": token})
    member_id = db.query(TeamMember).filter(TeamMember.email == "ana@agencia.com").one().id

    assert client.delete(f"/teams/{team['id']}/members/{member_id}", headers=_h(owner)).status_code == 204
    db.expire_all()
    removed = db.query(User).filter(User.id == ana["user_id"]).one()
    assert (removed.plan, removed.team_id) == ("free", None)
    assert _invite(client, owner, team["id"], "bruno@agencia.com").status_code == 201


def test_members_cannot_manage_and_outsiders_cannot_see_the_team(client, db):
    owner, team = _active_team(client, db, standard=3, premium=0)
    ana = _register(client, "ana@agencia.com")
    token = _invite(client, owner, team["id"], "ana@agencia.com").json()["invite_token"]
    client.post("/teams/invites/accept", headers=_h(ana), json={"token": token})

    assert _invite(client, ana, team["id"], "x@agencia.com").status_code == 403
    assert "members" not in client.get("/teams/me", headers=_h(ana)).json()
    owner_member = db.query(TeamMember).filter(TeamMember.role == "owner").one().id
    assert client.delete(f"/teams/{team['id']}/members/{owner_member}", headers=_h(ana)).status_code == 403

    outsider = _register(client, "outsider@example.com")
    assert client.get("/teams/me", headers=_h(outsider)).status_code == 404
    assert _invite(client, outsider, team["id"], "y@example.com").status_code == 404
    assert _checkout(client, outsider, team["id"], 5, 0, key="outsider-key-1").status_code == 404


def test_fewer_seats_never_drop_below_the_seats_in_use_and_apply_on_the_next_payment(client, db):
    owner, team = _active_team(client, db, standard=4, premium=1)  # owner holds a standard seat
    _invite(client, owner, team["id"], "a@agencia.com")
    _invite(client, owner, team["id"], "b@agencia.com")
    too_few = client.post(f"/teams/{team['id']}/seats/reduce", headers=_h(owner), json={"standard_seats": 2, "premium_seats": 1})
    assert too_few.status_code == 409 and too_few.json()["detail"]["error"] == "seats_in_use"

    reduced = client.post(f"/teams/{team['id']}/seats/reduce", headers=_h(owner), json={"standard_seats": 3, "premium_seats": 1})
    assert reduced.status_code == 200 and reduced.json()["pending_seats"] == {"standard": 3, "premium": 1}
    assert reduced.json()["seats"]["standard"]["purchased"] == 4

    renewal = _checkout(client, owner, team["id"], 3, 1, key="renewal-key-01").json()["order"]
    _pay(db, renewal["id"], owner)
    assert client.get("/teams/me", headers=_h(owner)).json()["seats"]["standard"]["purchased"] == 3


def test_when_payment_stops_every_seat_stops_granting_its_plan(client, db):
    owner, team = _active_team(client, db, standard=2, premium=0)
    ana = _register(client, "ana@agencia.com")
    token = _invite(client, owner, team["id"], "ana@agencia.com").json()["invite_token"]
    client.post("/teams/invites/accept", headers=_h(ana), json={"token": token})
    assert _plan(db, ana) == "pro"

    team_service.lapse(db, db.query(Team).one())
    assert _plan(db, ana) == "free" and _plan(db, owner) == "free"


def test_plan_reconciliation_recognises_a_paid_seat(client, db):
    owner, team = _active_team(client, db, standard=2, premium=0)
    ana = _register(client, "ana@agencia.com")
    token = _invite(client, owner, team["id"], "ana@agencia.com").json()["invite_token"]
    client.post("/teams/invites/accept", headers=_h(ana), json={"token": token})
    flagged = {finding.user_id for finding in find_unbacked_paid_accounts(db)}
    assert ana["user_id"] not in flagged and owner["user_id"] not in flagged


# ── BRL added 2026-09-24 (go-live checkout-currency fix, B2) ────────────────


def test_pricing_resolves_to_brl_when_only_a_brl_only_provider_is_registered(client):
    reg = ProviderRegistry()
    reg.register(_FakeMercadoPagoLikeAdapter())
    app.dependency_overrides[get_provider_registry] = lambda: reg

    body = client.get("/teams/pricing").json()
    assert body["currency"] == "BRL"
    assert body["seats"]["standard"] == {"price_usd": "24.90", "price": "124.50", "entitlement": "pro"}
    assert body["seats"]["premium"] == {"price_usd": "99.90", "price": "499.50", "entitlement": "max"}


def test_checkout_resolves_to_brl_and_a_matching_payment_activates_the_seats(client, db):
    reg = ProviderRegistry()
    reg.register(_FakeMercadoPagoLikeAdapter())
    app.dependency_overrides[get_provider_registry] = lambda: reg

    owner = _register(client, "owner@agencia.com")
    team = _create(client, owner)
    checkout = _checkout(client, owner, team["id"], 2, 1)
    assert checkout.status_code == 201, checkout.text
    order = checkout.json()["order"]
    assert order["currency_fiat"] == "BRL"
    # 2 standard (R$124.50) + 1 premium (R$499.50) = R$748.50
    assert order["amount_fiat"] == "748.50000000"
    assert checkout.json()["redirect_url"] == "https://mercadopago.example/checkout"

    _pay(db, order["id"], owner)
    view = client.get("/teams/me", headers=_h(owner)).json()
    assert view["status"] == "active"
    assert view["seats"]["standard"]["purchased"] == 2 and view["seats"]["premium"]["purchased"] == 1
