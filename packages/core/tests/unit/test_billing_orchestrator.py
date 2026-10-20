"""Unit tests for BillingOrchestrator — atomicity and idempotency.

Test matrix (Billing-0.5b requirements):

A) Success: credit_flush + ledger_flush + commit → balance changed, TX + 2 LedgerEntries.
B) Ledger failure: credit_flush OK → ledger raises → rollback → nothing persisted.
C) Exception after balance flush, before commit → rollback complete.
D) Debit insufficient → nothing in ledger, balance unchanged.
E) Idempotency: same operation twice → no duplicates.
F) Concurrency (retry preserved in credit_svc commit variants — not in flush variants).
"""
from __future__ import annotations

import pytest
import uuid
from decimal import Decimal
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database import Base
from src.models import LedgerEntry, CreditBalance, CreditTransaction  # noqa: F401
from src.models.user import User
from src.services.billing_orchestrator import BillingOrchestrator, BillingResult
from src.services.credit_service import (
    CreditService,
    InsufficientCreditError,
    ConcurrentModificationError,
)
from src.services.ledger_service import LedgerService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def user(db):
    u = User(email=f"orch-{uuid.uuid4()}@test.com", hashed_password="x", plan="start")
    db.add(u)
    db.commit()
    return u


@pytest.fixture
def orch(db):
    return BillingOrchestrator(db)


@pytest.fixture(autouse=True)
def no_retry_sleep():
    original = CreditService._RETRY_BASE_DELAY_MS
    CreditService._RETRY_BASE_DELAY_MS = 0.0
    yield
    CreditService._RETRY_BASE_DELAY_MS = original


def _ckey(suffix: str = "") -> str:
    return f"credit-{uuid.uuid4()}-{suffix}"


def _lkey(suffix: str = "") -> str:
    return f"ledger-{uuid.uuid4()}-{suffix}"


# ---------------------------------------------------------------------------
# A) Success path — all four writes committed atomically
# ---------------------------------------------------------------------------


def test_A_success_credit_creates_tx_and_ledger_pair(orch, db, user):
    """A: credit_flush + ledger_flush + commit → balance changed, CreditTX + 2 LedgerEntries."""
    result = orch.credit_and_record(
        user_id=user.id,
        currency="USD",
        amount=Decimal("10.00"),
        transaction_type="purchase",
        ledger_entry_type="credit_purchase",
        account_debit=f"user:{user.id}",
        account_credit="revenue",
        credit_idempotency_key=_ckey("A"),
        ledger_idempotency_key=_lkey("A"),
    )

    assert isinstance(result, BillingResult)
    assert result.transaction.amount == Decimal("10.00")
    assert result.ledger_debit.direction == "debit"
    assert result.ledger_credit.direction == "credit"
    assert result.ledger_debit.account_from == f"user:{user.id}"
    assert result.ledger_credit.account_from == f"user:{user.id}"

    # Verify DB state — all four writes committed
    assert db.query(CreditTransaction).count() == 1
    assert db.query(LedgerEntry).count() == 2
    assert CreditService(db).balance(user.id, "USD") == Decimal("10.00")


def test_A_success_debit_creates_tx_and_ledger_pair(orch, db, user):
    CreditService(db).credit(
        user_id=user.id, currency="USD",
        amount=Decimal("20.00"), idempotency_key=_ckey("fund"),
    )

    result = orch.debit_and_record(
        user_id=user.id,
        currency="USD",
        amount=Decimal("5.00"),
        transaction_type="consume",
        ledger_entry_type="credit_consumption",
        account_debit=f"user:{user.id}",
        account_credit="ai_cost",
        credit_idempotency_key=_ckey("A-debit"),
        ledger_idempotency_key=_lkey("A-debit"),
    )

    assert result.transaction.amount == Decimal("5.00")
    assert db.query(LedgerEntry).count() == 2
    assert CreditService(db).balance(user.id, "USD") == Decimal("15.00")


# ---------------------------------------------------------------------------
# B) Ledger failure → complete rollback
# ---------------------------------------------------------------------------


def test_B_ledger_failure_rolls_back_credit(orch, db, user):
    """B: credit_flush succeeds → ledger raises → rollback → NOTHING persisted."""
    ck = _ckey("B")
    lk = _lkey("B")

    with patch.object(
        orch._ledger_svc, "record_double_entry_flush",
        side_effect=RuntimeError("ledger write failed"),
    ):
        with pytest.raises(RuntimeError, match="ledger write failed"):
            orch.credit_and_record(
                user_id=user.id,
                currency="USD",
                amount=Decimal("10.00"),
                transaction_type="purchase",
                ledger_entry_type="credit_purchase",
                account_debit=f"user:{user.id}",
                account_credit="revenue",
                credit_idempotency_key=ck,
                ledger_idempotency_key=lk,
            )

    # Rollback must leave zero writes
    assert db.query(CreditTransaction).count() == 0
    assert db.query(LedgerEntry).count() == 0
    assert CreditService(db).balance(user.id, "USD") == Decimal("0")


def test_B_ledger_failure_debit_rolls_back(orch, db, user):
    """B (debit variant): debit_flush succeeds → ledger raises → rollback → nothing persisted."""
    CreditService(db).credit(
        user_id=user.id, currency="USD",
        amount=Decimal("20.00"), idempotency_key=_ckey("B-fund"),
    )

    with patch.object(
        orch._ledger_svc, "record_double_entry_flush",
        side_effect=RuntimeError("ledger write failed"),
    ):
        with pytest.raises(RuntimeError, match="ledger write failed"):
            orch.debit_and_record(
                user_id=user.id,
                currency="USD",
                amount=Decimal("5.00"),
                transaction_type="consume",
                ledger_entry_type="credit_consumption",
                account_debit=f"user:{user.id}",
                account_credit="ai_cost",
                credit_idempotency_key=_ckey("B-debit"),
                ledger_idempotency_key=_lkey("B-debit"),
            )

    # Only the funded balance (committed by CreditService.credit before) survives
    assert db.query(CreditTransaction).count() == 1  # the funding tx
    assert db.query(LedgerEntry).count() == 0
    assert CreditService(db).balance(user.id, "USD") == Decimal("20.00")  # unchanged


# ---------------------------------------------------------------------------
# C) Exception after balance flush, before commit → complete rollback
# ---------------------------------------------------------------------------


def test_C_exception_before_commit_rolls_back_everything(orch, db, user):
    """C: both flush() calls succeed; exception raised before commit → full rollback."""
    ck = _ckey("C")
    lk = _lkey("C")

    original_commit = db.commit
    call_count = [0]

    def fail_on_first_commit():
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("commit failed")
        return original_commit()

    with patch.object(db, "commit", side_effect=fail_on_first_commit):
        with pytest.raises(RuntimeError, match="commit failed"):
            orch.credit_and_record(
                user_id=user.id,
                currency="USD",
                amount=Decimal("15.00"),
                transaction_type="purchase",
                ledger_entry_type="credit_purchase",
                account_debit=f"user:{user.id}",
                account_credit="revenue",
                credit_idempotency_key=ck,
                ledger_idempotency_key=lk,
            )

    assert db.query(CreditTransaction).count() == 0
    assert db.query(LedgerEntry).count() == 0
    assert CreditService(db).balance(user.id, "USD") == Decimal("0")


# ---------------------------------------------------------------------------
# D) Debit insufficient → nothing persisted
# ---------------------------------------------------------------------------


def test_D_debit_insufficient_leaves_no_ledger(orch, db, user):
    """D: debit_flush fails (insufficient) → no LedgerEntry, no CreditTransaction."""
    with pytest.raises(InsufficientCreditError):
        orch.debit_and_record(
            user_id=user.id,
            currency="USD",
            amount=Decimal("999.00"),
            transaction_type="consume",
            ledger_entry_type="credit_consumption",
            account_debit=f"user:{user.id}",
            account_credit="ai_cost",
            credit_idempotency_key=_ckey("D"),
            ledger_idempotency_key=_lkey("D"),
        )

    assert db.query(LedgerEntry).count() == 0
    assert db.query(CreditTransaction).count() == 0


# ---------------------------------------------------------------------------
# E) Idempotency — same operation twice does not duplicate
# ---------------------------------------------------------------------------


def test_E_idempotency_credit_does_not_duplicate(orch, db, user):
    """E: calling credit_and_record twice with same keys returns existing records."""
    ck = _ckey("E")
    lk = _lkey("E")
    kwargs = dict(
        user_id=user.id,
        currency="USD",
        amount=Decimal("10.00"),
        transaction_type="purchase",
        ledger_entry_type="credit_purchase",
        account_debit=f"user:{user.id}",
        account_credit="revenue",
        credit_idempotency_key=ck,
        ledger_idempotency_key=lk,
    )

    r1 = orch.credit_and_record(**kwargs)
    r2 = orch.credit_and_record(**kwargs)

    # Same records returned
    assert r1.transaction.id == r2.transaction.id
    assert r1.ledger_debit.id == r2.ledger_debit.id
    assert r1.ledger_credit.id == r2.ledger_credit.id

    # No duplicates in DB
    assert db.query(CreditTransaction).count() == 1
    assert db.query(LedgerEntry).count() == 2

    # Balance credited exactly once
    assert CreditService(db).balance(user.id, "USD") == Decimal("10.00")


def test_E_idempotency_debit_does_not_duplicate(orch, db, user):
    """E (debit): same debit_and_record twice → balance debited once, no ledger duplicate."""
    CreditService(db).credit(
        user_id=user.id, currency="USD",
        amount=Decimal("20.00"), idempotency_key=_ckey("E-fund"),
    )
    ck = _ckey("E-debit")
    lk = _lkey("E-debit")
    kwargs = dict(
        user_id=user.id,
        currency="USD",
        amount=Decimal("5.00"),
        transaction_type="consume",
        ledger_entry_type="credit_consumption",
        account_debit=f"user:{user.id}",
        account_credit="ai_cost",
        credit_idempotency_key=ck,
        ledger_idempotency_key=lk,
    )
    r1 = orch.debit_and_record(**kwargs)
    r2 = orch.debit_and_record(**kwargs)

    assert r1.transaction.id == r2.transaction.id
    assert r1.ledger_debit.id == r2.ledger_debit.id
    assert db.query(LedgerEntry).count() == 2  # exactly one pair
    assert CreditService(db).balance(user.id, "USD") == Decimal("15.00")


# ---------------------------------------------------------------------------
# F) Concurrency: retry preserved in committing credit() variants
# ---------------------------------------------------------------------------


def test_F_concurrent_modification_retry_preserved_in_credit(db, user):
    """F: credit() still retries ConcurrentModificationError up to _MAX_RETRIES."""
    svc = CreditService(db)
    original_update = svc._update_balance_optimistic
    call_count = [0]

    def patched_update(balance_row, new_balance):
        call_count[0] += 1
        if call_count[0] == 1:
            db.rollback()
            raise ConcurrentModificationError(balance_row.id)
        return original_update(balance_row, new_balance)

    svc._update_balance_optimistic = patched_update

    tx = svc.credit(
        user_id=user.id, currency="USD",
        amount=Decimal("5.00"), idempotency_key=_ckey("F"),
    )

    assert tx is not None
    assert svc.balance(user.id, "USD") == Decimal("5.00")
    assert call_count[0] == 2  # first failed, second succeeded


def test_F_concurrent_modification_propagates_from_flush(db, user):
    """F (flush mode): ConcurrentModificationError propagates from credit_flush."""
    svc = CreditService(db)
    original_update = svc._update_balance_no_rollback

    def always_fail(balance_row, new_balance):
        raise ConcurrentModificationError(balance_row.id)

    svc._update_balance_no_rollback = always_fail

    # Must fund first so there's a balance row to update
    CreditService(db).credit(
        user_id=user.id, currency="USD",
        amount=Decimal("20.00"), idempotency_key=_ckey("F-fund"),
    )

    with pytest.raises(ConcurrentModificationError):
        svc.credit_flush(
            user_id=user.id, currency="USD",
            amount=Decimal("5.00"), idempotency_key=_ckey("F-flush"),
        )


# ---------------------------------------------------------------------------
# Σ-invariant across multiple operations
# ---------------------------------------------------------------------------


def test_double_entry_sum_invariant_across_multiple_orchestrated_ops(orch, db, user):
    """After multiple credit_and_record() calls, Σdebits == Σcredits."""
    for i in range(3):
        orch.credit_and_record(
            user_id=user.id,
            currency="USD",
            amount=Decimal(f"{(i + 1) * 5}.00"),
            transaction_type="purchase",
            ledger_entry_type="credit_purchase",
            account_debit=f"user:{user.id}",
            account_credit="revenue",
            credit_idempotency_key=_ckey(f"sum-{i}"),
            ledger_idempotency_key=_lkey(f"sum-{i}"),
        )

    entries = db.query(LedgerEntry).all()
    total_debit = sum(e.amount for e in entries if e.direction == "debit")
    total_credit = sum(e.amount for e in entries if e.direction == "credit")
    assert total_debit == total_credit == Decimal("30.00")
    assert db.query(CreditTransaction).count() == 3
