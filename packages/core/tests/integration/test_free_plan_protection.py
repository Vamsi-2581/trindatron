"""FREE is an acquisition cost, and its worst case has to be a chosen number.

Found in the 2026-09-13 economic audit:

1. A new account was not on FREE at all. create_user() never set a plan and
   the column default was "start", so every signup received START's agent
   mode, TRINITY CODE, the standard quality band and 110 TCU a week, unpaid.
2. Nothing limited how many accounts one script could create, and each came
   with its own weekly allowance — so FREE's total exposure grew with the
   number of accounts rather than with anything the business decided.

What is pinned here:
  * a new account is on "free";
  * signup is rate-limited per network address;
  * every free account together draws from ONE daily provider budget that
    holds under concurrency (own lock, clamped reservations) and resets at
    00:00 UTC — and paid plans never touch it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from src.config import settings
from src.entitlements import PLAN_ENTITLEMENTS
from src.models.usage import UsageRecord
from src.models.user import User
from src.services.entitlement_service import check_and_reserve


def _signup(client, email: str):
    return client.post("/auth/register", json={
        "email": email, "password": "TestPass123!", "full_name": "Free Tester",
    })


def _spend(db, user, cost_usd: float, *, status: str = "completed", created_at: datetime | None = None):
    record = UsageRecord(
        user_id=user.id, model="groq/openai/gpt-oss-20b", cost_usd=cost_usd,
        tcu=cost_usd * 100.0, status=status,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(record)
    db.commit()
    return record


# ── Signup ───────────────────────────────────────────────────────────────


def test_a_new_account_starts_on_free(client, db):
    resp = _signup(client, "brand-new@test.com")
    assert resp.status_code == 201
    user = db.query(User).filter(User.id == resp.json()["user_id"]).one()
    assert user.plan == "free", "a signup must never receive a paid plan it did not buy"


def test_the_model_default_is_free_too(db):
    user = User(email="orm-default@test.com", hashed_password="x", full_name="x")
    db.add(user)
    db.commit()
    assert user.plan == "free"


def test_signup_is_rate_limited_per_network_address(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_register_ip_rate_limit", 2)
    assert _signup(client, "script-1@test.com").status_code == 201
    assert _signup(client, "script-2@test.com").status_code == 201
    third = _signup(client, "script-3@test.com")
    assert third.status_code == 429


# ── The plan-wide daily budget ───────────────────────────────────────────


@pytest.fixture
def tight_free_budget(monkeypatch):
    monkeypatch.setattr(settings, "free_plan_global_daily_budget_usd", 0.05)
    return 0.05


def test_free_accounts_share_one_daily_budget(db, db2_user_factory, tight_free_budget):
    spender = db2_user_factory("spender@test.com", plan="free")
    newcomer = db2_user_factory("newcomer@test.com", plan="free")
    _spend(db, spender, 0.05)

    with pytest.raises(HTTPException) as exc:
        check_and_reserve(db, user_id=newcomer.id, session_id=None, plan="free")

    assert exc.value.status_code == 429
    assert exc.value.detail["error"] == "free_capacity_exhausted"
    assert "resets_at" in exc.value.detail


def test_the_last_cents_are_usable_but_never_overshot(db, db2_user_factory, tight_free_budget):
    spender = db2_user_factory("spender@test.com", plan="free")
    newcomer = db2_user_factory("newcomer@test.com", plan="free")
    _spend(db, spender, 0.04)   # $0.01 left = 1 TCU

    reservation_id = check_and_reserve(db, user_id=newcomer.id, session_id=None, plan="free")
    reserved = db.query(UsageRecord).filter(UsageRecord.id == reservation_id).one()
    assert float(reserved.tcu) == pytest.approx(1.0)
    assert float(reserved.tcu) < PLAN_ENTITLEMENTS["free"].max_tcu_per_request


def test_failed_calls_and_yesterday_do_not_count(db, db2_user_factory, tight_free_budget):
    spender = db2_user_factory("spender@test.com", plan="free")
    newcomer = db2_user_factory("newcomer@test.com", plan="free")
    _spend(db, spender, 0.05, status="failed")
    _spend(db, spender, 0.05, created_at=datetime.now(timezone.utc) - timedelta(days=1, hours=1))

    assert check_and_reserve(db, user_id=newcomer.id, session_id=None, plan="free")


def test_paid_plans_never_draw_on_the_free_budget(db, db2_user_factory, tight_free_budget):
    free_user = db2_user_factory("free@test.com", plan="free")
    paying = db2_user_factory("paying@test.com", plan="pro")
    _spend(db, free_user, 0.05)

    assert check_and_reserve(db, user_id=paying.id, session_id=None, plan="pro")


def test_paid_spend_does_not_consume_the_free_budget(db, db2_user_factory, tight_free_budget):
    paying = db2_user_factory("paying@test.com", plan="pro")
    newcomer = db2_user_factory("newcomer@test.com", plan="free")
    _spend(db, paying, 5.00)

    assert check_and_reserve(db, user_id=newcomer.id, session_id=None, plan="free")


def test_the_budget_is_on_by_default():
    """A FREE plan with no plan-wide ceiling has an exposure proportional to
    the number of accounts anyone can create."""
    from src.config import Settings

    default = Settings.model_fields["free_plan_global_daily_budget_usd"].default
    assert default is not None and 0 < default <= 10.0
