"""Unit tests for LedgerService."""
from __future__ import annotations

import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database import Base
from src.models import LedgerEntry, CreditBalance, CreditTransaction  # noqa: F401 — ensure tables created
from src.models.user import User
from src.services.ledger_service import DuplicateLedgerKey, LedgerService


# ---------------------------------------------------------------------------
# In-memory SQLite fixture
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
def svc(db):
    return LedgerService(db)


# ---------------------------------------------------------------------------
# record() — single entry
# ---------------------------------------------------------------------------


def test_record_creates_immutable_entry(svc, db):
    entry = svc.record(
        entry_type="revenue",
        direction="debit",
        account_from="user:abc",
        account_to="revenue",
        amount=Decimal("10.00"),
        currency="USD",
    )
    assert entry.id is not None
    assert entry.entry_type == "revenue"
    assert entry.direction == "debit"
    assert entry.amount == Decimal("10.00")
    assert entry.currency == "USD"
    # No updated_at column on LedgerEntry
    assert not hasattr(entry, "updated_at")


def test_record_requires_positive_amount(svc):
    with pytest.raises(ValueError, match="positive"):
        svc.record(
            entry_type="revenue",
            direction="debit",
            account_from="user:abc",
            account_to="revenue",
            amount=Decimal("0"),
            currency="USD",
        )


def test_record_rejects_negative_amount(svc):
    with pytest.raises(ValueError, match="positive"):
        svc.record(
            entry_type="revenue",
            direction="debit",
            account_from="user:abc",
            account_to="revenue",
            amount=Decimal("-5.00"),
            currency="USD",
        )


def test_record_rejects_invalid_entry_type(svc):
    with pytest.raises(ValueError, match="entry_type"):
        svc.record(
            entry_type="not_a_real_type",
            direction="debit",
            account_from="user:abc",
            account_to="revenue",
            amount=Decimal("1.00"),
            currency="USD",
        )


def test_record_rejects_invalid_direction(svc):
    with pytest.raises(ValueError, match="direction"):
        svc.record(
            entry_type="revenue",
            direction="sideways",
            account_from="user:abc",
            account_to="revenue",
            amount=Decimal("1.00"),
            currency="USD",
        )


def test_record_requires_decimal_not_float(svc):
    with pytest.raises(TypeError, match="Decimal"):
        svc.record(
            entry_type="revenue",
            direction="debit",
            account_from="user:abc",
            account_to="revenue",
            amount=10.0,  # float, not Decimal
            currency="USD",
        )


def test_record_stores_reference_and_note(svc, db):
    entry = svc.record(
        entry_type="ai_cost",
        direction="debit",
        account_from="revenue",
        account_to="ai_cost",
        amount=Decimal("0.00025000"),
        currency="USD",
        reference_id="usage-record-123",
        reference_type="usage_record",
        note="Claude Sonnet 4.6 inference cost",
    )
    assert entry.reference_id == "usage-record-123"
    assert entry.reference_type == "usage_record"
    assert entry.note == "Claude Sonnet 4.6 inference cost"


def test_record_persists_to_database(svc, db):
    svc.record(
        entry_type="fee",
        direction="debit",
        account_from="revenue",
        account_to="fee:stripe",
        amount=Decimal("0.50"),
        currency="USD",
    )
    count = db.query(LedgerEntry).count()
    assert count == 1


# ---------------------------------------------------------------------------
# record() — idempotency
# ---------------------------------------------------------------------------


def test_record_idempotency_returns_existing(svc, db):
    """Same key + same params → returns existing entry, no duplicate."""
    key = "pay-001:single"
    first = svc.record(
        entry_type="revenue",
        direction="credit",
        account_from="user:alice",
        account_to="revenue",
        amount=Decimal("5.00"),
        currency="USD",
        idempotency_key=key,
    )
    db.commit()

    second = svc.record(
        entry_type="revenue",
        direction="credit",
        account_from="user:alice",
        account_to="revenue",
        amount=Decimal("5.00"),
        currency="USD",
        idempotency_key=key,
    )
    assert second.id == first.id
    assert db.query(LedgerEntry).count() == 1


def test_record_idempotency_raises_on_conflict(svc, db):
    """Same key + different amount → DuplicateLedgerKey."""
    key = "pay-002:single"
    svc.record(
        entry_type="revenue",
        direction="debit",
        account_from="user:alice",
        account_to="revenue",
        amount=Decimal("5.00"),
        currency="USD",
        idempotency_key=key,
    )
    db.commit()

    with pytest.raises(DuplicateLedgerKey) as exc_info:
        svc.record(
            entry_type="revenue",
            direction="debit",
            account_from="user:alice",
            account_to="revenue",
            amount=Decimal("9.99"),  # different!
            currency="USD",
            idempotency_key=key,
        )
    assert exc_info.value.field == "amount"


# ---------------------------------------------------------------------------
# record_double_entry() — canonical double-entry pair
# ---------------------------------------------------------------------------


def test_double_entry_creates_two_rows(svc, db):
    debit, credit = svc.record_double_entry(
        entry_type="credit_purchase",
        account_debit="user:alice",
        account_credit="revenue",
        amount=Decimal("5.00"),
        currency="USD",
    )
    assert db.query(LedgerEntry).count() == 2
    # Canonical: both entries share the same account_from/account_to
    assert debit.account_from == "user:alice"
    assert debit.account_to == "revenue"
    assert credit.account_from == "user:alice"
    assert credit.account_to == "revenue"
    # Direction distinguishes them
    assert debit.direction == "debit"
    assert credit.direction == "credit"
    assert debit.amount == credit.amount == Decimal("5.00")


def test_double_entry_sum_invariant(svc, db):
    """Σdebits == Σcredits — the fundamental ledger invariant."""
    svc.record_double_entry(
        entry_type="credit_purchase",
        account_debit="user:alice",
        account_credit="revenue",
        amount=Decimal("7.50"),
        currency="USD",
    )
    entries = db.query(LedgerEntry).all()
    total_debit = sum(e.amount for e in entries if e.direction == "debit")
    total_credit = sum(e.amount for e in entries if e.direction == "credit")
    assert total_debit == total_credit


def test_double_entry_atomic_on_validation_failure(svc, db):
    with pytest.raises(ValueError):
        svc.record_double_entry(
            entry_type="INVALID",
            account_debit="user:alice",
            account_credit="revenue",
            amount=Decimal("5.00"),
            currency="USD",
        )
    assert db.query(LedgerEntry).count() == 0


def test_double_entry_idempotency_returns_existing_pair(svc, db):
    """Same key → returns the same (debit, credit) pair."""
    key = "purchase-001"
    d1, c1 = svc.record_double_entry(
        entry_type="credit_purchase",
        account_debit="user:bob",
        account_credit="revenue",
        amount=Decimal("3.00"),
        currency="USD",
        idempotency_key=key,
    )
    d2, c2 = svc.record_double_entry(
        entry_type="credit_purchase",
        account_debit="user:bob",
        account_credit="revenue",
        amount=Decimal("3.00"),
        currency="USD",
        idempotency_key=key,
    )
    assert d1.id == d2.id
    assert c1.id == c2.id
    assert db.query(LedgerEntry).count() == 2


# ---------------------------------------------------------------------------
# record() atomicity — caller owns the transaction
# ---------------------------------------------------------------------------


def test_record_does_not_commit(svc, db):
    """record() flushes but does NOT commit — rollback removes the entry."""
    svc.record(
        entry_type="revenue",
        direction="debit",
        account_from="user:abc",
        account_to="revenue",
        amount=Decimal("1.00"),
        currency="USD",
    )
    # Flushed row is visible within the same session
    assert db.query(LedgerEntry).count() == 1

    # Roll back — must remove the unflushed entry since it was never committed
    db.rollback()
    assert db.query(LedgerEntry).count() == 0


def test_record_caller_commits(svc, db):
    """Caller that commits after record() persists the entry across rollback."""
    svc.record(
        entry_type="revenue",
        direction="credit",
        account_from="user:abc",
        account_to="revenue",
        amount=Decimal("1.00"),
        currency="USD",
    )
    db.commit()  # caller is responsible for commit

    # Post-commit rollback starts a new transaction — committed data survives
    db.rollback()
    assert db.query(LedgerEntry).count() == 1


def test_record_rollback_on_second_failure_leaves_no_partial_entry(svc, db):
    """If two record() calls are in the same tx and the tx fails, both are absent."""
    svc.record(
        entry_type="revenue",
        direction="debit",
        account_from="user:abc",
        account_to="revenue",
        amount=Decimal("1.00"),
        currency="USD",
    )
    svc.record(
        entry_type="ai_cost",
        direction="credit",
        account_from="revenue",
        account_to="ai_cost",
        amount=Decimal("0.50"),
        currency="USD",
    )
    # Both flushed — visible in session
    assert db.query(LedgerEntry).count() == 2

    # Caller decides to roll back the whole operation
    db.rollback()
    assert db.query(LedgerEntry).count() == 0


def test_double_entry_rollback_removes_both_entries(svc, db):
    """Simulated commit failure leaves no partial entries in the DB."""
    from unittest.mock import patch

    with patch.object(db, "commit", side_effect=RuntimeError("simulated DB failure")):
        with pytest.raises(RuntimeError, match="simulated DB failure"):
            svc.record_double_entry(
                entry_type="revenue",
                account_debit="user:abc",
                account_credit="revenue",
                amount=Decimal("5.00"),
                currency="USD",
            )

    # Clean up the failed transaction state
    db.rollback()
    assert db.query(LedgerEntry).count() == 0


# ---------------------------------------------------------------------------
# get_entries() — query
# ---------------------------------------------------------------------------


def test_get_entries_filter_by_reference(svc):
    svc.record(
        entry_type="revenue",
        direction="debit",
        account_from="user:alice",
        account_to="revenue",
        amount=Decimal("1.00"),
        currency="USD",
        reference_id="pay-001",
        reference_type="payment",
    )
    svc.record(
        entry_type="revenue",
        direction="debit",
        account_from="user:bob",
        account_to="revenue",
        amount=Decimal("2.00"),
        currency="USD",
        reference_id="pay-002",
        reference_type="payment",
    )
    results = svc.get_entries(reference_id="pay-001", reference_type="payment")
    assert len(results) == 1
    assert results[0].reference_id == "pay-001"


def test_get_entries_filter_by_account(svc):
    svc.record(
        entry_type="revenue",
        direction="debit",
        account_from="user:alice",
        account_to="revenue",
        amount=Decimal("1.00"),
        currency="USD",
    )
    svc.record(
        entry_type="ai_cost",
        direction="debit",
        account_from="revenue",
        account_to="ai_cost",
        amount=Decimal("0.50"),
        currency="USD",
    )
    results = svc.get_entries(account="revenue")
    assert len(results) == 2  # revenue appears in both


def test_get_entries_supports_crypto_amounts(svc):
    entry = svc.record(
        entry_type="credit_purchase",
        direction="debit",
        account_from="user:alice",
        account_to="revenue",
        amount=Decimal("0.00015000"),  # 0.00015 BTC
        currency="BTC",
    )
    assert entry.currency == "BTC"
    assert entry.amount == Decimal("0.00015000")


def test_all_valid_entry_types_accepted(svc):
    valid_types = list(LedgerEntry.ENTRY_TYPES)
    for entry_type in valid_types:
        svc.record(
            entry_type=entry_type,
            direction="debit",
            account_from="user:x",
            account_to="revenue",
            amount=Decimal("1.00"),
            currency="USD",
        )
