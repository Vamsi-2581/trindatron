"""No account keeps a paid plan that nothing paid for.

The reconciliation must find exactly the accounts without evidence — never a
paying customer, an admin, a deliberate comp or a TEAM member — and applying
it must be idempotent, auditable and reversible.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.models.subscription import Subscription
from src.models.usage import UsageRecord
from src.models.user import User
from src.services import plan_reconciliation as pr
from src.services.payment_order_service import PaymentOrderService


def _order(db, user, *, plan: str, webhook: bool):
    order = PaymentOrderService(db).create_order(
        user_id=user.id, provider="stripe", amount_fiat=Decimal("9.90"), currency_fiat="USD",
        credits_amount=Decimal("9.90"), currency_credits="USD", idempotency_key=str(uuid.uuid4()),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1), plan=plan,
    )
    order.status = "confirmed"
    order.confirmed_at = datetime.now(timezone.utc)
    if webhook:
        order.webhook_raw = '{"event": "payment.confirmed"}'
    db.commit()
    return order


def _subscription(db, user, *, plan: str, status: str):
    db.add(Subscription(user_id=user.id, plan=plan, provider="stripe", payment_method="credit_card",
                        currency="USD", amount=Decimal("19.90"), status=status))
    db.commit()


def _ids(findings):
    return {f.user_id for f in findings}


def test_a_signup_default_start_account_is_found_and_a_dry_run_changes_nothing(db, db2_user_factory):
    user = db2_user_factory("legacy@test.com", plan="start")
    db.add(UsageRecord(user_id=user.id, model="gpt-4o-mini", cost_usd=0.02, tcu=2.0, status="completed"))
    db.commit()

    findings = pr.find_unbacked_paid_accounts(db)

    assert _ids(findings) == {user.id}
    assert findings[0].usage_records == 1 and findings[0].spend_usd == pytest.approx(0.02)
    assert pr.summarize(findings)["to_change"] == 1
    db.refresh(user)
    assert user.plan == "start"


def test_a_webhook_confirmed_order_for_the_current_plan_is_evidence(db, db2_user_factory):
    paid = db2_user_factory("paid@test.com", plan="pro")
    _order(db, paid, plan="pro", webhook=True)
    assert pr.find_unbacked_paid_accounts(db) == []


def test_a_self_confirmed_order_is_not_evidence(db, db2_user_factory):
    cheat = db2_user_factory("cheat@test.com", plan="max")
    _order(db, cheat, plan="max", webhook=False)
    [finding] = pr.find_unbacked_paid_accounts(db)
    assert finding.user_id == cheat.id and finding.self_confirmed_orders == 1


def test_evidence_must_be_for_the_plan_the_account_holds(db, db2_user_factory):
    upgraded = db2_user_factory("upgraded@test.com", plan="max")
    _order(db, upgraded, plan="start", webhook=True)
    assert _ids(pr.find_unbacked_paid_accounts(db)) == {upgraded.id}


@pytest.mark.parametrize("status,backed", [
    ("active", True), ("past_due", True), ("grace_period", True), ("cancelled", False), ("expired", False),
])
def test_only_a_live_subscription_is_evidence(db, db2_user_factory, status, backed):
    user = db2_user_factory(f"sub-{status}@test.com", plan="pro")
    _subscription(db, user, plan="pro", status=status)
    assert (pr.find_unbacked_paid_accounts(db) == []) is backed


def test_admins_keep_list_and_free_accounts_are_never_touched(db, db2_user_factory):
    admin = db2_user_factory("admin@test.com", plan="max")
    admin.is_admin = True
    comp = db2_user_factory("comp@test.com", plan="pro")
    db2_user_factory("free@test.com", plan="free")
    db.commit()
    assert pr.find_unbacked_paid_accounts(db, keep_user_ids=frozenset({comp.id})) == []


def test_team_members_are_reported_for_review_not_changed(db, db2_user_factory):
    member = db2_user_factory("member@test.com", plan="team")
    member.team_id = "team-1"
    db.commit()

    findings = pr.find_unbacked_paid_accounts(db)
    report = pr.apply(db, findings)

    assert findings[0].review_only
    assert report["changes"] == [] and len(report["review_only"]) == 1
    db.refresh(member)
    assert member.plan == "team"


def test_apply_is_idempotent_and_writes_an_audit_trail(db, db2_user_factory):
    user = db2_user_factory("legacy@test.com", plan="start")

    report = pr.apply(db, pr.find_unbacked_paid_accounts(db))

    assert report["changes"] == [{"user_id": user.id, "from": "start", "to": "free"}]
    db.refresh(user)
    assert user.plan == "free"
    assert pr.find_unbacked_paid_accounts(db) == []
    assert pr.apply(db, pr.find_unbacked_paid_accounts(db))["changes"] == []


def test_apply_skips_an_account_that_paid_after_the_report_was_built(db, db2_user_factory):
    user = db2_user_factory("late-buyer@test.com", plan="pro")
    findings = pr.find_unbacked_paid_accounts(db)
    _order(db, user, plan="pro", webhook=True)

    assert pr.apply(db, findings)["changes"] == []
    db.refresh(user)
    assert user.plan == "pro"


def test_restore_undoes_the_change_but_never_overwrites_a_later_purchase(db, db2_user_factory):
    reverted = db2_user_factory("reverted@test.com", plan="start")
    bought = db2_user_factory("bought@test.com", plan="max")
    report = pr.apply(db, pr.find_unbacked_paid_accounts(db))
    db.query(User).filter(User.id == bought.id).one().plan = "pro"   # a real purchase afterwards
    db.commit()

    assert pr.restore(db, report) == 1
    db.refresh(reverted)
    db.refresh(bought)
    assert reverted.plan == "start"
    assert bought.plan == "pro"


def test_the_cli_refuses_to_apply_without_a_report():
    with pytest.raises(SystemExit):
        pr.main(["--apply"])
