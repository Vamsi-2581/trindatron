"""
Integration tests for Billing-0 financial foundation.

Verifies the full stack: models, CreditService, LedgerService,
and Alembic table schema work correctly together.

All tests use SQLite in-memory (same as other integration tests).
"""
from __future__ import annotations

import uuid
import pytest
from decimal import Decimal
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.database import Base
from src.models import LedgerEntry, CreditBalance, CreditTransaction  # noqa: F401
from src.models.user import User
from src.services.credit_service import CreditService, InsufficientCreditError
from src.services.ledger_service import LedgerService


# ---------------------------------------------------------------------------
# DB fixture (same pattern as other integration tests)
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
    u = User(
        id=str(uuid.uuid4()),
        email="billing-test@trinity.ai",
        hashed_password="not-a-real-hash",
        plan="start",
    )
    db.add(u)
    db.commit()
    return u


def ikey(tag: str = "") -> str:
    return f"integ-{uuid.uuid4()}-{tag}"


# ---------------------------------------------------------------------------
# Schema smoke — tables exist and constraints are in place
# ---------------------------------------------------------------------------


def test_ledger_entries_table_exists(db):
    count = db.execute(text("SELECT COUNT(*) FROM ledger_entries")).scalar()
    assert count == 0


def test_credit_balances_table_exists(db):
    count = db.execute(text("SELECT COUNT(*) FROM credit_balances")).scalar()
    assert count == 0


def test_credit_transactions_table_exists(db):
    count = db.execute(text("SELECT COUNT(*) FROM credit_transactions")).scalar()
    assert count == 0


def test_idempotency_key_unique_constraint(db, user):
    svc = CreditService(db)
    key = ikey("uq")
    svc.credit(user_id=user.id, currency="USD", amount=Decimal("1.00"), idempotency_key=key)
    # Second call with same key must return same tx, not raise
    tx1 = db.query(CreditTransaction).filter(CreditTransaction.idempotency_key == key).first()
    svc.credit(user_id=user.id, currency="USD", amount=Decimal("1.00"), idempotency_key=key)
    tx2 = db.query(CreditTransaction).filter(CreditTransaction.idempotency_key == key).first()
    assert tx1.id == tx2.id
    assert db.query(CreditTransaction).count() == 1


def test_user_currency_balance_unique_per_pair(db, user):
    svc = CreditService(db)
    svc.credit(user_id=user.id, currency="USD", amount=Decimal("5.00"), idempotency_key=ikey("a"))
    svc.credit(user_id=user.id, currency="USD", amount=Decimal("5.00"), idempotency_key=ikey("b"))
    assert db.query(CreditBalance).filter(CreditBalance.user_id == user.id, CreditBalance.currency == "USD").count() == 1


# ---------------------------------------------------------------------------
# End-to-end: full credit purchase flow
# ---------------------------------------------------------------------------


def test_credit_purchase_end_to_end(db, user):
    """Simulate: user pays $10 → credits 10.00 USD → ledger entry created."""
    credit_svc = CreditService(db)
    ledger_svc = LedgerService(db)
    payment_id = str(uuid.uuid4())

    # 1. Credit the user's balance
    tx = credit_svc.credit(
        user_id=user.id,
        currency="USD",
        amount=Decimal("10.00"),
        idempotency_key=f"purchase-{payment_id}",
        transaction_type="purchase",
        reference_id=payment_id,
        reference_type="payment",
    )

    # 2. Record the ledger entry
    ledger_svc.record(
        entry_type="credit_purchase",
        direction="debit",
        account_from=f"user:{user.id}",
        account_to="revenue",
        amount=Decimal("10.00"),
        currency="USD",
        reference_id=payment_id,
        reference_type="payment",
    )

    # Assertions
    assert credit_svc.balance(user.id, "USD") == Decimal("10.00")
    assert tx.balance_before == Decimal("0")
    assert tx.balance_after == Decimal("10.00")

    ledger = db.query(LedgerEntry).filter(LedgerEntry.reference_id == payment_id).first()
    assert ledger is not None
    assert ledger.entry_type == "credit_purchase"
    assert ledger.amount == Decimal("10.00")


def test_ai_consumption_flow(db, user):
    """Simulate: user sends chat → 2.5 TCU consumed → ledger entry → balance updated."""
    credit_svc = CreditService(db)
    ledger_svc = LedgerService(db)
    usage_id = str(uuid.uuid4())

    # Fund the account
    credit_svc.credit(
        user_id=user.id,
        currency="TCU",
        amount=Decimal("100.0000"),
        idempotency_key=ikey("fund"),
    )

    # Consume on AI usage
    credit_svc.debit(
        user_id=user.id,
        currency="TCU",
        amount=Decimal("2.5000"),
        idempotency_key=f"consume-{usage_id}",
        transaction_type="consume",
        reference_id=usage_id,
        reference_type="usage_record",
    )

    # Journal the AI cost
    ledger_svc.record(
        entry_type="ai_cost",
        direction="debit",
        account_from="revenue",
        account_to="ai_cost",
        amount=Decimal("0.00025000"),
        currency="USD",
        reference_id=usage_id,
        reference_type="usage_record",
    )

    assert credit_svc.balance(user.id, "TCU") == Decimal("97.5000")
    assert db.query(LedgerEntry).count() == 1


def test_refund_flow(db, user):
    """Simulate: credit, consume, payment fails → refund → balance restored."""
    credit_svc = CreditService(db)

    credit_svc.credit(user_id=user.id, currency="USD", amount=Decimal("20.00"), idempotency_key=ikey("load"))
    credit_svc.debit(user_id=user.id, currency="USD", amount=Decimal("5.00"), idempotency_key=ikey("use"))
    credit_svc.refund(user_id=user.id, currency="USD", amount=Decimal("5.00"), idempotency_key=ikey("rf"))

    assert credit_svc.balance(user.id, "USD") == Decimal("20.00")
    txs = credit_svc.get_transactions(user.id, "USD")
    types = {tx.transaction_type for tx in txs}
    assert "refund" in types
    assert "consume" in types
    assert "purchase" in types


# ---------------------------------------------------------------------------
# Double-entry accounting via LedgerService
# ---------------------------------------------------------------------------


def test_double_entry_creates_balanced_pair(db):
    ledger_svc = LedgerService(db)
    debit, credit = ledger_svc.record_double_entry(
        entry_type="revenue",
        account_debit="user:alice",
        account_credit="revenue",
        amount=Decimal("25.00"),
        currency="BRL",
    )
    # Canonical: both entries share the same account_from/account_to pair
    assert debit.account_from == "user:alice"
    assert debit.account_to == "revenue"
    assert credit.account_from == "user:alice"
    assert credit.account_to == "revenue"
    assert debit.direction == "debit"
    assert credit.direction == "credit"
    assert debit.amount == credit.amount
    assert db.query(LedgerEntry).count() == 2


# ---------------------------------------------------------------------------
# Insufficient funds — no partial state
# ---------------------------------------------------------------------------


def test_insufficient_funds_leaves_no_partial_state(db, user):
    credit_svc = CreditService(db)

    with pytest.raises(InsufficientCreditError):
        credit_svc.debit(
            user_id=user.id,
            currency="USD",
            amount=Decimal("1.00"),
            idempotency_key=ikey("bad"),
        )

    assert credit_svc.balance(user.id, "USD") == Decimal("0")
    assert db.query(CreditTransaction).count() == 0
    assert db.query(CreditBalance).count() == 0


# ---------------------------------------------------------------------------
# Multi-currency isolation
# ---------------------------------------------------------------------------


def test_multi_currency_balances_isolated(db, user):
    svc = CreditService(db)
    currencies = ["USD", "BRL", "BTC", "USDT", "USDC", "TCU"]
    amounts = {
        "USD": Decimal("100.00"),
        "BRL": Decimal("520.00"),
        "BTC": Decimal("0.00500000"),
        "USDT": Decimal("100.000000"),
        "USDC": Decimal("100.000000"),
        "TCU": Decimal("1000.0000"),
    }
    for cur, amt in amounts.items():
        svc.credit(user_id=user.id, currency=cur, amount=amt, idempotency_key=ikey(cur))

    for cur, expected in amounts.items():
        assert svc.balance(user.id, cur) == expected

    # Debit only USD, others unchanged
    svc.debit(user_id=user.id, currency="USD", amount=Decimal("10.00"), idempotency_key=ikey("d-usd"))
    assert svc.balance(user.id, "USD") == Decimal("90.00")
    assert svc.balance(user.id, "BRL") == Decimal("520.00")
    assert svc.balance(user.id, "TCU") == Decimal("1000.0000")


# ---------------------------------------------------------------------------
# Version field increments on each balance update
# ---------------------------------------------------------------------------


def test_credit_balance_version_increments(db, user):
    svc = CreditService(db)
    svc.credit(user_id=user.id, currency="USD", amount=Decimal("5.00"), idempotency_key=ikey("v1"))
    row = db.query(CreditBalance).filter(CreditBalance.user_id == user.id, CreditBalance.currency == "USD").first()
    v1 = row.version
    svc.credit(user_id=user.id, currency="USD", amount=Decimal("5.00"), idempotency_key=ikey("v2"))
    db.refresh(row)
    assert row.version == v1 + 1


# ---------------------------------------------------------------------------
# User CASCADE delete
# ---------------------------------------------------------------------------


def test_delete_user_cascades_to_credit_tables(db, user):
    svc = CreditService(db)
    svc.credit(user_id=user.id, currency="USD", amount=Decimal("10.00"), idempotency_key=ikey("del"))

    assert db.query(CreditBalance).count() == 1
    assert db.query(CreditTransaction).count() == 1

    db.delete(user)
    db.commit()

    assert db.query(CreditBalance).count() == 0
    assert db.query(CreditTransaction).count() == 0
