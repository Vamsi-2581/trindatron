"""Etapa 5F (second round) — Continuity entitlement/budget/financial tests.

Covers the section-15 required list. Uses the real `db`/SQLite fixture for
everything except the genuine-concurrency proof (item 8/19), which is
Postgres-gated (mirrors test_entitlement.py's
test_concurrent_reservation_only_one_succeeds exactly — same helper, same
pattern, applied to Continuity's own advisory lock key).
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from src.capability import Capability
from src.entitlements import PlanEntitlement, get_plan_entitlement
from src.models.ledger import CreditTransaction, LedgerEntry
from src.services.continuity_entitlement_service import (
    ContinuityBudgetExceededError,
    ContinuityNotEnabledError,
    check_and_debit_continuity,
    continuity_used_today,
    true_up_continuity,
)
from src.services.continuity_mode import (
    ContinuityActivation,
    ContinuityNotApplicable,
    ContinuityRejected,
    activate_continuity_or_raise,
    is_premium_quota_exhausted,
)
from src.services.credit_service import CreditService, InsufficientCreditError
from src.services.entitlement_service import check_and_reserve
from tests.postgres_helpers import get_test_postgres_url, postgres_available


def _new_user_id() -> str:
    return str(uuid.uuid4())


def _grant_credits(db, user_id: str, amount: str) -> None:
    CreditService(db).credit(
        user_id=user_id, currency="TCU", amount=Decimal(amount),
        idempotency_key=f"test-grant-{uuid.uuid4()}", transaction_type="grant", note="test setup",
    )


def _continuity_entitlement(plan: str, *, enabled: bool, daily_limit: float) -> PlanEntitlement:
    base = get_plan_entitlement(plan)
    from dataclasses import replace
    return replace(base, continuity_enabled=enabled, continuity_daily_tcu_limit=daily_limit)


# ── 1. Free sem Continuity ───────────────────────────────────────────────────

def test_free_plan_has_continuity_disabled_in_production_config():
    e = get_plan_entitlement("free")
    assert e.continuity_enabled is False


def test_free_plan_raises_not_enabled(db):
    user_id = _new_user_id()
    _grant_credits(db, user_id, "1000")
    with pytest.raises(ContinuityNotEnabledError):
        check_and_debit_continuity(
            db, user_id=user_id, plan="free", estimated_tcu=1.0, execution_id=str(uuid.uuid4()),
        )
    # No financial trace of a rejected-before-any-ledger-write attempt.
    assert db.query(CreditTransaction).filter(CreditTransaction.user_id == user_id, CreditTransaction.transaction_type == "consume").count() == 0


# ── 2. Plano pago com Continuity ─────────────────────────────────────────────

@pytest.mark.parametrize("plan", ["start", "pro", "max", "team"])
def test_paid_plans_have_continuity_enabled_in_production_config(plan):
    e = get_plan_entitlement(plan)
    assert e.continuity_enabled is True


# ── 3/4. Quota premium disponível -> NORMAL; esgotada -> CONTINUITY ────────

def test_normal_quota_available_detail_is_not_premium_quota_exhausted():
    """A successful check_and_reserve() never produces this shape at all —
    this test documents the negative space: nothing resembling a normal
    "OK" outcome could ever be mistaken for the trigger condition."""
    assert is_premium_quota_exhausted(None) is False


def test_quota_exhausted_and_continuity_enabled_and_budget_available_activates(db, monkeypatch):
    user_id = _new_user_id()
    _grant_credits(db, user_id, "1000")
    monkeypatch.setattr(
        "src.services.continuity_entitlement_service.get_plan_entitlement",
        lambda plan: _continuity_entitlement(plan, enabled=True, daily_limit=50.0),
    )

    activation = activate_continuity_or_raise(
        db, user_id=user_id, plan="start", capability=Capability.CHAT,
        quota_rejection_detail={"error": "daily_limit_exceeded"},
        estimated_tcu=5.0, execution_id=str(uuid.uuid4()),
    )
    assert isinstance(activation, ContinuityActivation)
    assert activation.model.modality == "text"
    from trinity_router.providers import PROVIDER_CATALOG
    assert PROVIDER_CATALOG[activation.provider_key] is activation.model
    assert activation.estimated_tcu == 5.0

    balance = CreditService(db).balance(user_id, "TCU")
    assert balance == Decimal("995")  # 1000 - 5 debited


# ── 5/7/18/19. Continuity budget esgotado -> rejeição controlada ───────────

def test_continuity_budget_exhausted_rejects_even_with_ample_balance(db, monkeypatch):
    """7: real financial balance existing does NOT bypass an exhausted
    Continuity budget — the two controls are independently enforced."""
    user_id = _new_user_id()
    _grant_credits(db, user_id, "1000")  # plenty of money
    monkeypatch.setattr(
        "src.services.continuity_entitlement_service.get_plan_entitlement",
        lambda plan: _continuity_entitlement(plan, enabled=True, daily_limit=10.0),
    )

    # First call consumes the whole daily budget (10.0).
    check_and_debit_continuity(db, user_id=user_id, plan="start", estimated_tcu=10.0, execution_id=str(uuid.uuid4()))

    with pytest.raises(ContinuityBudgetExceededError) as exc_info:
        check_and_debit_continuity(db, user_id=user_id, plan="start", estimated_tcu=0.01, execution_id=str(uuid.uuid4()))
    assert exc_info.value.used == pytest.approx(10.0)
    assert exc_info.value.limit == pytest.approx(10.0)

    # No second debit occurred — balance only reflects the first, authorized charge.
    balance = CreditService(db).balance(user_id, "TCU")
    assert balance == Decimal("990")


def test_free_plan_production_budget_is_zero(db):
    """free.continuity_daily_tcu_limit stays 0.0 in production — moot in
    practice since continuity_enabled=False rejects free plan requests
    before the budget is ever consulted (see test_free_plan_raises_not_enabled),
    but this documents the value is deliberately never a positive number."""
    assert get_plan_entitlement("free").continuity_daily_tcu_limit == 0.0


# ── 2/3/4/5. Official per-plan production budget values ────────────────────

# Rescaled 2026-09-11 alongside each plan's own windows: a Continuity
# budget that is a large fraction of the daily allowance would let scheduled
# work crowd out the person who is actually sitting there. The derivation of
# the windows these hang off is in tests/unit/test_plan_economics.py.
@pytest.mark.parametrize("plan,expected_limit", [
    ("start", 4.0),
    ("pro", 8.0),
    ("max", 30.0),
    ("team", 30.0),
])
def test_paid_plan_production_budget_matches_official_value(plan, expected_limit):
    e = get_plan_entitlement(plan)
    assert e.continuity_enabled is True
    assert e.continuity_daily_tcu_limit == expected_limit


@pytest.mark.parametrize("plan,daily_limit", [
    ("start", 4.0), ("pro", 8.0), ("max", 30.0), ("team", 30.0),
])
def test_production_budget_admits_a_request_within_the_official_limit(db, plan, daily_limit):
    """Uses the REAL production PlanEntitlement (no monkeypatch) — proves
    the provisioned values actually authorize a Continuity execution, not
    just that the numbers are stored correctly."""
    user_id = _new_user_id()
    _grant_credits(db, user_id, "1000")
    result = check_and_debit_continuity(
        db, user_id=user_id, plan=plan, estimated_tcu=daily_limit - 0.01, execution_id=str(uuid.uuid4()),
    )
    assert result.transaction.amount == pytest.approx(Decimal(str(daily_limit - 0.01)))


@pytest.mark.parametrize("plan,daily_limit", [
    ("start", 4.0), ("pro", 8.0), ("max", 30.0), ("team", 30.0),
])
def test_production_budget_rejects_a_request_beyond_the_official_limit(db, plan, daily_limit):
    user_id = _new_user_id()
    _grant_credits(db, user_id, "1000")
    with pytest.raises(ContinuityBudgetExceededError):
        check_and_debit_continuity(
            db, user_id=user_id, plan=plan, estimated_tcu=daily_limit + 0.01, execution_id=str(uuid.uuid4()),
        )


# ── 6/20. Saldo financeiro insuficiente -> rejeição sem saldo negativo ─────

def test_insufficient_balance_rejects_without_negative_balance(db, monkeypatch):
    user_id = _new_user_id()
    _grant_credits(db, user_id, "3")  # less than what we'll try to spend
    monkeypatch.setattr(
        "src.services.continuity_entitlement_service.get_plan_entitlement",
        lambda plan: _continuity_entitlement(plan, enabled=True, daily_limit=1000.0),  # budget is not the constraint
    )

    with pytest.raises(InsufficientCreditError):
        check_and_debit_continuity(db, user_id=user_id, plan="start", estimated_tcu=5.0, execution_id=str(uuid.uuid4()))

    balance = CreditService(db).balance(user_id, "TCU")
    assert balance == Decimal("3")  # unchanged — never went negative, never partially debited
    assert db.query(CreditTransaction).filter(CreditTransaction.user_id == user_id, CreditTransaction.transaction_type == "consume").count() == 0


def test_user_with_zero_balance_is_rejected_not_granted_free_access(db, monkeypatch):
    user_id = _new_user_id()  # no credit grant at all
    monkeypatch.setattr(
        "src.services.continuity_entitlement_service.get_plan_entitlement",
        lambda plan: _continuity_entitlement(plan, enabled=True, daily_limit=1000.0),
    )
    with pytest.raises(InsufficientCreditError):
        check_and_debit_continuity(db, user_id=user_id, plan="start", estimated_tcu=1.0, execution_id=str(uuid.uuid4()))


# ── 9/10. Idempotência / nenhuma duplicação financeira ──────────────────────

def test_same_execution_id_retried_does_not_double_charge(db, monkeypatch):
    user_id = _new_user_id()
    _grant_credits(db, user_id, "100")
    monkeypatch.setattr(
        "src.services.continuity_entitlement_service.get_plan_entitlement",
        lambda plan: _continuity_entitlement(plan, enabled=True, daily_limit=50.0),
    )
    execution_id = str(uuid.uuid4())

    result1 = check_and_debit_continuity(db, user_id=user_id, plan="start", estimated_tcu=5.0, execution_id=execution_id)
    result2 = check_and_debit_continuity(db, user_id=user_id, plan="start", estimated_tcu=5.0, execution_id=execution_id)

    assert result1.transaction.id == result2.transaction.id
    balance = CreditService(db).balance(user_id, "TCU")
    assert balance == Decimal("95")  # debited exactly once, not twice

    consume_count = db.query(CreditTransaction).filter(
        CreditTransaction.user_id == user_id, CreditTransaction.transaction_type == "consume",
    ).count()
    assert consume_count == 1


# ── 11. Nenhuma alteração indevida no ledger ────────────────────────────────

def test_successful_debit_creates_exactly_one_ledger_entry_pair(db, monkeypatch):
    user_id = _new_user_id()
    _grant_credits(db, user_id, "100")
    monkeypatch.setattr(
        "src.services.continuity_entitlement_service.get_plan_entitlement",
        lambda plan: _continuity_entitlement(plan, enabled=True, daily_limit=50.0),
    )
    execution_id = str(uuid.uuid4())
    check_and_debit_continuity(db, user_id=user_id, plan="start", estimated_tcu=5.0, execution_id=execution_id)

    entries = db.query(LedgerEntry).filter(LedgerEntry.reference_id == execution_id).all()
    assert len(entries) == 2  # one debit, one credit — canonical double-entry pair
    directions = {e.direction for e in entries}
    assert directions == {"debit", "credit"}
    accounts = {(e.account_from, e.account_to) for e in entries}
    assert accounts == {(f"user:{user_id}", "ai_cost")}
    assert all(e.entry_type == "ai_cost" for e in entries)


def test_rejected_attempt_creates_zero_ledger_entries(db):
    user_id = _new_user_id()
    execution_id = str(uuid.uuid4())
    # 10.01 exceeds start's real, provisioned 10.0 daily budget — a
    # deliberately real (not monkeypatched) budget rejection.
    with pytest.raises(ContinuityBudgetExceededError):
        check_and_debit_continuity(db, user_id=user_id, plan="start", estimated_tcu=10.01, execution_id=execution_id)
    assert db.query(LedgerEntry).filter(LedgerEntry.reference_id == execution_id).count() == 0


# ── 12. Nenhuma alteração em PaymentOrder ───────────────────────────────────

def test_continuity_never_touches_payment_order(db, monkeypatch):
    from src.models.payment_order import PaymentOrder

    user_id = _new_user_id()
    _grant_credits(db, user_id, "100")
    monkeypatch.setattr(
        "src.services.continuity_entitlement_service.get_plan_entitlement",
        lambda plan: _continuity_entitlement(plan, enabled=True, daily_limit=50.0),
    )
    before = db.query(PaymentOrder).count()
    check_and_debit_continuity(db, user_id=user_id, plan="start", estimated_tcu=5.0, execution_id=str(uuid.uuid4()))
    after = db.query(PaymentOrder).count()
    assert before == after == 0


# ── 13. NORMAL permanece inalterado ─────────────────────────────────────────

def test_premium_check_and_reserve_is_never_called_by_continuity(db, monkeypatch):
    called = {"n": 0}
    original = check_and_reserve

    def _spy(*args, **kwargs):
        called["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr("src.services.entitlement_service.check_and_reserve", _spy)
    monkeypatch.setattr(
        "src.services.continuity_entitlement_service.get_plan_entitlement",
        lambda plan: _continuity_entitlement(plan, enabled=True, daily_limit=50.0),
    )
    user_id = _new_user_id()
    _grant_credits(db, user_id, "100")
    check_and_debit_continuity(db, user_id=user_id, plan="start", estimated_tcu=5.0, execution_id=str(uuid.uuid4()))
    assert called["n"] == 0, "Continuity must never call the premium check_and_reserve()"


# ── 14. Provider failure não ativa Continuity ───────────────────────────────

@pytest.mark.parametrize("detail", [
    {"error": "circuit_open"},
    {"error": "budget_exceeded"},
    {"error": "provider_rate_limited"},
    None,
])
def test_non_quota_rejection_never_activates_continuity(db, detail):
    with pytest.raises(ContinuityNotApplicable):
        activate_continuity_or_raise(
            db, user_id=_new_user_id(), plan="start", capability=Capability.CHAT,
            quota_rejection_detail=detail, estimated_tcu=1.0, execution_id=str(uuid.uuid4()),
        )


# ── 15/16. Multimodalidade não consome orçamento / não chama provider ──────

@pytest.mark.parametrize("capability", [Capability.IMAGE, Capability.VIDEO, Capability.AUDIO])
def test_multimodal_capability_never_reaches_the_ledger(db, monkeypatch, capability):
    user_id = _new_user_id()
    _grant_credits(db, user_id, "100")
    monkeypatch.setattr(
        "src.services.continuity_entitlement_service.get_plan_entitlement",
        lambda plan: _continuity_entitlement(plan, enabled=True, daily_limit=50.0),
    )
    with pytest.raises(ContinuityRejected):
        activate_continuity_or_raise(
            db, user_id=user_id, plan="start", capability=capability,
            quota_rejection_detail={"error": "daily_limit_exceeded"},
            estimated_tcu=5.0, execution_id=str(uuid.uuid4()),
        )
    # Zero financial trace — the TEXT-ONLY gate ran before any ledger code.
    assert db.query(CreditTransaction).filter(CreditTransaction.user_id == user_id, CreditTransaction.transaction_type == "consume").count() == 0
    balance = CreditService(db).balance(user_id, "TCU")
    assert balance == Decimal("100")  # untouched


# ── True-up ───────────────────────────────────────────────────────────────

def test_true_up_refunds_overestimate(db, monkeypatch):
    user_id = _new_user_id()
    _grant_credits(db, user_id, "100")
    monkeypatch.setattr(
        "src.services.continuity_entitlement_service.get_plan_entitlement",
        lambda plan: _continuity_entitlement(plan, enabled=True, daily_limit=50.0),
    )
    execution_id = str(uuid.uuid4())
    check_and_debit_continuity(db, user_id=user_id, plan="start", estimated_tcu=10.0, execution_id=execution_id)
    assert CreditService(db).balance(user_id, "TCU") == Decimal("90")

    true_up_continuity(db, user_id=user_id, estimated_tcu=10.0, actual_tcu=4.0, execution_id=execution_id)
    assert CreditService(db).balance(user_id, "TCU") == Decimal("96")  # refunded the 6.0 difference


def test_true_up_no_op_when_estimate_matches_actual(db, monkeypatch):
    user_id = _new_user_id()
    _grant_credits(db, user_id, "100")
    monkeypatch.setattr(
        "src.services.continuity_entitlement_service.get_plan_entitlement",
        lambda plan: _continuity_entitlement(plan, enabled=True, daily_limit=50.0),
    )
    execution_id = str(uuid.uuid4())
    check_and_debit_continuity(db, user_id=user_id, plan="start", estimated_tcu=10.0, execution_id=execution_id)
    true_up_continuity(db, user_id=user_id, estimated_tcu=10.0, actual_tcu=10.0, execution_id=execution_id)
    assert CreditService(db).balance(user_id, "TCU") == Decimal("90")


def test_true_up_is_idempotent(db, monkeypatch):
    user_id = _new_user_id()
    _grant_credits(db, user_id, "100")
    monkeypatch.setattr(
        "src.services.continuity_entitlement_service.get_plan_entitlement",
        lambda plan: _continuity_entitlement(plan, enabled=True, daily_limit=50.0),
    )
    execution_id = str(uuid.uuid4())
    check_and_debit_continuity(db, user_id=user_id, plan="start", estimated_tcu=10.0, execution_id=execution_id)
    true_up_continuity(db, user_id=user_id, estimated_tcu=10.0, actual_tcu=4.0, execution_id=execution_id)
    true_up_continuity(db, user_id=user_id, estimated_tcu=10.0, actual_tcu=4.0, execution_id=execution_id)  # retried
    assert CreditService(db).balance(user_id, "TCU") == Decimal("96")  # refunded once, not twice


# ── continuity_used_today() ──────────────────────────────────────────────────

def test_continuity_used_today_accumulates_across_calls(db, monkeypatch):
    user_id = _new_user_id()
    _grant_credits(db, user_id, "100")
    monkeypatch.setattr(
        "src.services.continuity_entitlement_service.get_plan_entitlement",
        lambda plan: _continuity_entitlement(plan, enabled=True, daily_limit=50.0),
    )
    assert continuity_used_today(db, user_id) == 0.0
    check_and_debit_continuity(db, user_id=user_id, plan="start", estimated_tcu=3.0, execution_id=str(uuid.uuid4()))
    check_and_debit_continuity(db, user_id=user_id, plan="start", estimated_tcu=4.0, execution_id=str(uuid.uuid4()))
    assert continuity_used_today(db, user_id) == pytest.approx(7.0)


def test_continuity_used_today_is_isolated_per_user(db, monkeypatch):
    user_a, user_b = _new_user_id(), _new_user_id()
    _grant_credits(db, user_a, "100")
    monkeypatch.setattr(
        "src.services.continuity_entitlement_service.get_plan_entitlement",
        lambda plan: _continuity_entitlement(plan, enabled=True, daily_limit=50.0),
    )
    check_and_debit_continuity(db, user_id=user_a, plan="start", estimated_tcu=10.0, execution_id=str(uuid.uuid4()))
    assert continuity_used_today(db, user_a) == pytest.approx(10.0)
    assert continuity_used_today(db, user_b) == 0.0


# ── 8/19. Real concurrency — Postgres-gated, mirrors test_entitlement.py ───

@pytest.mark.skipif(not postgres_available(), reason="TEST_POSTGRES_URL not set or PostgreSQL unreachable — see tests/postgres_helpers.py")
def test_concurrent_continuity_debits_never_exceed_the_budget():
    """Scenario: continuity_daily_tcu_limit=10, two concurrent threads each
    attempt to debit 6 (6+6=12 > 10 — only one may succeed). Mirrors
    test_entitlement.py's test_concurrent_reservation_only_one_succeeds
    exactly, applied to Continuity's own advisory lock key."""
    import threading

    from sqlalchemy import create_engine, func
    from sqlalchemy.orm import sessionmaker

    import src.models  # noqa: F401 — registers all models on Base.metadata
    from src.database import Base
    from src.models.user import User

    url = get_test_postgres_url()
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)

    setup_db = Session()
    try:
        user = User(
            id=str(uuid.uuid4()), email=f"pgcontinuity_{uuid.uuid4().hex[:8]}@test.com",
            hashed_password="x", plan="start",
        )
        setup_db.add(user)
        setup_db.commit()
        user_id = user.id
        CreditService(setup_db).credit(
            user_id=user_id, currency="TCU", amount=Decimal("1000"),
            idempotency_key=f"pg-grant-{uuid.uuid4()}", transaction_type="grant", note="test setup",
        )
    finally:
        setup_db.close()

    fake_entitlement = _continuity_entitlement("start", enabled=True, daily_limit=10.0)
    results: dict[str, tuple] = {}
    barrier = threading.Barrier(2)

    def attempt(label: str) -> None:
        session = Session()
        try:
            barrier.wait(timeout=10)
            check_and_debit_continuity(
                session, user_id=user_id, plan="start", estimated_tcu=6.0, execution_id=str(uuid.uuid4()),
            )
            results[label] = ("ok",)
        except ContinuityBudgetExceededError:
            results[label] = ("blocked",)
        except Exception as exc:
            results[label] = ("error", repr(exc))
        finally:
            session.close()

    # Patched ONCE, here, around both threads — never inside them. mock.patch is not thread-safe:
    # two threads each patching the same attribute can save each other's mock as "the original",
    # and whichever exits last then restores a MOCK, which stayed installed for the rest of the
    # suite (seen in CI: FREE debiting 0 TCU, MAX/TEAM refused by a leaked 10-TCU budget).
    import unittest.mock as mock
    import src.services.continuity_entitlement_service as continuity_entitlement_service

    real_get_plan_entitlement = continuity_entitlement_service.get_plan_entitlement
    with mock.patch.object(continuity_entitlement_service, "get_plan_entitlement", return_value=fake_entitlement):
        t_a = threading.Thread(target=attempt, args=("A",))
        t_b = threading.Thread(target=attempt, args=("B",))
        t_a.start(); t_b.start()
        t_a.join(timeout=20); t_b.join(timeout=20)
    assert continuity_entitlement_service.get_plan_entitlement is real_get_plan_entitlement

    try:
        outcomes = [results.get("A"), results.get("B")]
        assert None not in outcomes, f"a thread did not finish in time: {outcomes}"
        ok_count = sum(1 for o in outcomes if o[0] == "ok")
        blocked_count = sum(1 for o in outcomes if o[0] == "blocked")
        assert ok_count == 1, f"expected exactly one successful debit, got: {outcomes}"
        assert blocked_count == 1, f"expected exactly one budget-blocked attempt, got: {outcomes}"

        verify_db = Session()
        try:
            total = verify_db.query(func.coalesce(func.sum(CreditTransaction.amount), 0)).filter(
                CreditTransaction.user_id == user_id,
                CreditTransaction.reference_type == "continuity",
                CreditTransaction.transaction_type == "consume",
            ).scalar()
            assert float(total) <= 10.0, f"total continuity debits {total} exceeded the daily budget of 10.0"
        finally:
            verify_db.close()
    finally:
        engine.dispose()
