"""Unit tests for CreditService."""
from __future__ import annotations

import uuid
import pytest
from decimal import Decimal
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database import Base
from src.models import LedgerEntry, CreditBalance, CreditTransaction  # noqa: F401 — tables
from src.models.user import User
from src.services.credit_service import (
    CreditService,
    InsufficientCreditError,
    ConcurrentModificationError,
    DuplicateIdempotencyKey,
)


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
    u = User(
        id=str(uuid.uuid4()),
        email="alice@example.com",
        hashed_password="not-a-real-hash",
        plan="start",
    )
    db.add(u)
    db.commit()
    return u


@pytest.fixture
def svc(db):
    return CreditService(db)


def ikey(suffix: str = "") -> str:
    """Generate a unique idempotency key for each test call."""
    return f"test-{uuid.uuid4()}-{suffix}"


# ---------------------------------------------------------------------------
# balance() — zero for new user
# ---------------------------------------------------------------------------


def test_balance_returns_zero_for_new_user(svc, user):
    b = svc.balance(user.id, "USD")
    assert b == Decimal("0")


def test_balance_returns_decimal_not_float(svc, user):
    result = svc.balance(user.id, "USD")
    assert isinstance(result, Decimal)


# ---------------------------------------------------------------------------
# credit() — add credits
# ---------------------------------------------------------------------------


def test_credit_increases_balance(svc, user):
    svc.credit(
        user_id=user.id,
        currency="USD",
        amount=Decimal("10.00"),
        idempotency_key=ikey("c1"),
    )
    assert svc.balance(user.id, "USD") == Decimal("10.00")


def test_credit_returns_transaction_with_correct_snapshots(svc, user):
    tx = svc.credit(
        user_id=user.id,
        currency="USD",
        amount=Decimal("5.00"),
        idempotency_key=ikey("snap"),
    )
    assert tx.balance_before == Decimal("0")
    assert tx.balance_after == Decimal("5.00")
    assert tx.amount == Decimal("5.00")
    assert tx.transaction_type == "purchase"


def test_credit_accumulates_across_calls(svc, user):
    svc.credit(user_id=user.id, currency="USD", amount=Decimal("3.00"), idempotency_key=ikey("a"))
    svc.credit(user_id=user.id, currency="USD", amount=Decimal("7.00"), idempotency_key=ikey("b"))
    assert svc.balance(user.id, "USD") == Decimal("10.00")


def test_credit_idempotency_no_duplicate(svc, user):
    key = ikey("idem")
    tx1 = svc.credit(user_id=user.id, currency="USD", amount=Decimal("5.00"), idempotency_key=key)
    tx2 = svc.credit(user_id=user.id, currency="USD", amount=Decimal("5.00"), idempotency_key=key)
    assert tx1.id == tx2.id
    assert svc.balance(user.id, "USD") == Decimal("5.00")


def test_credit_different_currencies_independent(svc, user):
    svc.credit(user_id=user.id, currency="USD", amount=Decimal("10.00"), idempotency_key=ikey("usd"))
    svc.credit(user_id=user.id, currency="TCU", amount=Decimal("500.0000"), idempotency_key=ikey("tcu"))
    assert svc.balance(user.id, "USD") == Decimal("10.00")
    assert svc.balance(user.id, "TCU") == Decimal("500.0000")
    assert svc.balance(user.id, "BTC") == Decimal("0")


def test_credit_rejects_non_decimal_amount(svc, user):
    with pytest.raises(TypeError):
        svc.credit(user_id=user.id, currency="USD", amount=5.0, idempotency_key=ikey())


def test_credit_rejects_zero_amount(svc, user):
    with pytest.raises(ValueError, match="positive"):
        svc.credit(user_id=user.id, currency="USD", amount=Decimal("0"), idempotency_key=ikey())


def test_credit_rejects_negative_amount(svc, user):
    with pytest.raises(ValueError, match="positive"):
        svc.credit(user_id=user.id, currency="USD", amount=Decimal("-1"), idempotency_key=ikey())


def test_credit_rejects_invalid_transaction_type(svc, user):
    with pytest.raises(ValueError, match="transaction_type"):
        svc.credit(
            user_id=user.id,
            currency="USD",
            amount=Decimal("1.00"),
            idempotency_key=ikey(),
            transaction_type="not_a_type",
        )


def test_credit_stores_reference_metadata(svc, user):
    tx = svc.credit(
        user_id=user.id,
        currency="USD",
        amount=Decimal("1.00"),
        idempotency_key=ikey(),
        reference_id="pay-123",
        reference_type="payment",
        note="Stripe confirmed",
    )
    assert tx.reference_id == "pay-123"
    assert tx.reference_type == "payment"
    assert tx.note == "Stripe confirmed"


# ---------------------------------------------------------------------------
# debit() — subtract credits
# ---------------------------------------------------------------------------


def test_debit_decreases_balance(svc, user):
    svc.credit(user_id=user.id, currency="USD", amount=Decimal("10.00"), idempotency_key=ikey("seed"))
    svc.debit(user_id=user.id, currency="USD", amount=Decimal("3.00"), idempotency_key=ikey("d"))
    assert svc.balance(user.id, "USD") == Decimal("7.00")


def test_debit_creates_consume_transaction_by_default(svc, user):
    svc.credit(user_id=user.id, currency="USD", amount=Decimal("10.00"), idempotency_key=ikey("seed"))
    tx = svc.debit(user_id=user.id, currency="USD", amount=Decimal("2.00"), idempotency_key=ikey("d"))
    assert tx.transaction_type == "consume"


def test_debit_records_correct_snapshots(svc, user):
    svc.credit(user_id=user.id, currency="USD", amount=Decimal("10.00"), idempotency_key=ikey("seed"))
    tx = svc.debit(user_id=user.id, currency="USD", amount=Decimal("4.00"), idempotency_key=ikey("d"))
    assert tx.balance_before == Decimal("10.00")
    assert tx.balance_after == Decimal("6.00")


def test_debit_raises_on_insufficient_funds(svc, user):
    with pytest.raises(InsufficientCreditError) as exc_info:
        svc.debit(user_id=user.id, currency="USD", amount=Decimal("1.00"), idempotency_key=ikey())
    err = exc_info.value
    assert err.balance == Decimal("0")
    assert err.requested == Decimal("1.00")


def test_debit_insufficient_funds_leaves_balance_unchanged(svc, user):
    svc.credit(user_id=user.id, currency="USD", amount=Decimal("5.00"), idempotency_key=ikey("seed"))
    with pytest.raises(InsufficientCreditError):
        svc.debit(user_id=user.id, currency="USD", amount=Decimal("10.00"), idempotency_key=ikey())
    assert svc.balance(user.id, "USD") == Decimal("5.00")


def test_debit_idempotency_no_duplicate(svc, user):
    svc.credit(user_id=user.id, currency="USD", amount=Decimal("10.00"), idempotency_key=ikey("seed"))
    key = ikey("idem")
    tx1 = svc.debit(user_id=user.id, currency="USD", amount=Decimal("3.00"), idempotency_key=key)
    tx2 = svc.debit(user_id=user.id, currency="USD", amount=Decimal("3.00"), idempotency_key=key)
    assert tx1.id == tx2.id
    assert svc.balance(user.id, "USD") == Decimal("7.00")


def test_debit_exact_balance_to_zero(svc, user):
    svc.credit(user_id=user.id, currency="TCU", amount=Decimal("100.0000"), idempotency_key=ikey("seed"))
    svc.debit(user_id=user.id, currency="TCU", amount=Decimal("100.0000"), idempotency_key=ikey("d"))
    assert svc.balance(user.id, "TCU") == Decimal("0")


# ---------------------------------------------------------------------------
# refund() — return credits
# ---------------------------------------------------------------------------


def test_refund_increases_balance(svc, user):
    svc.credit(user_id=user.id, currency="USD", amount=Decimal("10.00"), idempotency_key=ikey("seed"))
    svc.debit(user_id=user.id, currency="USD", amount=Decimal("5.00"), idempotency_key=ikey("d"))
    svc.refund(user_id=user.id, currency="USD", amount=Decimal("5.00"), idempotency_key=ikey("r"))
    assert svc.balance(user.id, "USD") == Decimal("10.00")


def test_refund_creates_refund_transaction_type(svc, user):
    tx = svc.refund(
        user_id=user.id,
        currency="USD",
        amount=Decimal("1.00"),
        idempotency_key=ikey("r"),
    )
    assert tx.transaction_type == "refund"


# ---------------------------------------------------------------------------
# get_transactions()
# ---------------------------------------------------------------------------


def test_get_transactions_returns_history(svc, user):
    svc.credit(user_id=user.id, currency="USD", amount=Decimal("5.00"), idempotency_key=ikey("t1"))
    svc.credit(user_id=user.id, currency="USD", amount=Decimal("3.00"), idempotency_key=ikey("t2"))
    txs = svc.get_transactions(user.id)
    assert len(txs) == 2


def test_get_transactions_filter_by_currency(svc, user):
    svc.credit(user_id=user.id, currency="USD", amount=Decimal("5.00"), idempotency_key=ikey("u"))
    svc.credit(user_id=user.id, currency="TCU", amount=Decimal("50.0000"), idempotency_key=ikey("t"))
    usd_txs = svc.get_transactions(user.id, "USD")
    tcu_txs = svc.get_transactions(user.id, "TCU")
    assert len(usd_txs) == 1
    assert len(tcu_txs) == 1


# ---------------------------------------------------------------------------
# Crypto / multi-currency
# ---------------------------------------------------------------------------


def test_credit_btc_high_precision(svc, user):
    svc.credit(
        user_id=user.id,
        currency="BTC",
        amount=Decimal("0.00100000"),
        idempotency_key=ikey("btc"),
    )
    assert svc.balance(user.id, "BTC") == Decimal("0.00100000")


def test_credit_usdt_amount(svc, user):
    svc.credit(
        user_id=user.id,
        currency="USDT",
        amount=Decimal("50.000000"),
        idempotency_key=ikey("usdt"),
    )
    assert svc.balance(user.id, "USDT") == Decimal("50.000000")


# ---------------------------------------------------------------------------
# Fixture: disable retry backoff so tests don't sleep
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_retry_sleep():
    """Set _RETRY_BASE_DELAY_MS=0 so retry tests run without actual sleeps."""
    original = CreditService._RETRY_BASE_DELAY_MS
    CreditService._RETRY_BASE_DELAY_MS = 0.0
    yield
    CreditService._RETRY_BASE_DELAY_MS = original


# ---------------------------------------------------------------------------
# DuplicateIdempotencyKey — conflicting params on reuse
# ---------------------------------------------------------------------------


def test_credit_duplicate_key_different_amount_raises(svc, user):
    """Reusing an idempotency_key with a different amount must raise, never silently credit."""
    key = ikey("conflict-amt")
    svc.credit(user_id=user.id, currency="USD", amount=Decimal("5.00"), idempotency_key=key)

    with pytest.raises(DuplicateIdempotencyKey, match="amount"):
        svc.credit(user_id=user.id, currency="USD", amount=Decimal("9.99"), idempotency_key=key)

    # Balance must be exactly the first credit — no double-credit
    assert svc.balance(user.id, "USD") == Decimal("5.00")


def test_credit_duplicate_key_different_type_raises(svc, user):
    """Reusing a key with a different transaction_type must raise."""
    key = ikey("conflict-type")
    svc.credit(
        user_id=user.id, currency="USD", amount=Decimal("5.00"),
        idempotency_key=key, transaction_type="purchase",
    )

    with pytest.raises(DuplicateIdempotencyKey, match="transaction_type"):
        svc.credit(
            user_id=user.id, currency="USD", amount=Decimal("5.00"),
            idempotency_key=key, transaction_type="grant",
        )


def test_debit_duplicate_key_different_amount_raises(svc, user):
    """Reusing a debit key with a different amount must raise."""
    svc.credit(user_id=user.id, currency="USD", amount=Decimal("20.00"), idempotency_key=ikey("fund"))
    key = ikey("conflict-debit")
    svc.debit(user_id=user.id, currency="USD", amount=Decimal("3.00"), idempotency_key=key)

    with pytest.raises(DuplicateIdempotencyKey, match="amount"):
        svc.debit(user_id=user.id, currency="USD", amount=Decimal("5.00"), idempotency_key=key)

    # Balance unchanged from the original debit
    assert svc.balance(user.id, "USD") == Decimal("17.00")


# ---------------------------------------------------------------------------
# Idempotency race — IntegrityError path (concurrent duplicate key)
# ---------------------------------------------------------------------------


def test_credit_idempotency_race_returns_existing(db, user, svc):
    """Simulate concurrent idempotency race: first _find_existing_tx returns None
    (race window), then commit hits UNIQUE IntegrityError; service rolls back and
    returns the pre-committed transaction without double-crediting."""
    from src.models.ledger import CreditBalance, CreditTransaction

    key = ikey("race")
    amount = Decimal("5.00")

    # Simulate Thread A: pre-commit a balance + transaction with this key.
    balance = CreditBalance(user_id=user.id, currency="USD", balance=amount, version=1)
    db.add(balance)
    db.flush()
    pre_tx = CreditTransaction(
        user_id=user.id,
        credit_balance_id=balance.id,
        transaction_type="purchase",
        amount=amount,
        currency="USD",
        balance_before=Decimal("0"),
        balance_after=amount,
        idempotency_key=key,
    )
    db.add(pre_tx)
    db.commit()  # Thread A's successful commit

    # Simulate Thread B: _find_existing_tx returns None on first call (race window).
    call_count = [0]
    original_find = svc._find_existing_tx

    def patched_find(k):
        call_count[0] += 1
        if call_count[0] == 1:
            return None  # Thread B is in the race window — key not yet visible to it
        return original_find(k)

    svc._find_existing_tx = patched_find

    # credit() will: try to insert duplicate key → IntegrityError → rollback →
    # re-fetch pre_tx → return it.
    result = svc.credit(user_id=user.id, currency="USD", amount=amount, idempotency_key=key)

    assert result.id == pre_tx.id, "Must return Thread A's transaction, not create a new one"

    # Balance must remain at 5.00 — rollback undid Thread B's balance UPDATE attempt.
    db.expire_all()
    bal_row = db.query(CreditBalance).filter_by(user_id=user.id, currency="USD").first()
    assert bal_row.balance == amount, "No double-credit: balance must stay at the original amount"
    assert db.query(CreditTransaction).count() == 1, "Exactly one transaction exists"


# ---------------------------------------------------------------------------
# Optimistic locking retry — ConcurrentModificationError
# ---------------------------------------------------------------------------


def test_credit_retries_on_concurrent_modification(db, user, svc):
    """credit() retries internally when _update_balance_optimistic raises
    ConcurrentModificationError on the first attempt."""
    key = ikey("retry-credit")
    original_update = svc._update_balance_optimistic
    call_count = [0]

    def patched_update(balance_row, new_balance):
        call_count[0] += 1
        if call_count[0] == 1:
            # Simulate a concurrent write that bumped the version
            db.rollback()
            raise ConcurrentModificationError(balance_row.id if balance_row else "test-id")
        return original_update(balance_row, new_balance)

    svc._update_balance_optimistic = patched_update

    tx = svc.credit(user_id=user.id, currency="USD", amount=Decimal("5.00"), idempotency_key=key)

    assert tx is not None
    assert tx.amount == Decimal("5.00")
    assert svc.balance(user.id, "USD") == Decimal("5.00")
    assert call_count[0] == 2, "First attempt failed, second succeeded"


def test_debit_retries_on_concurrent_modification(db, user, svc):
    """debit() retries internally when _update_balance_optimistic fails once."""
    svc.credit(user_id=user.id, currency="USD", amount=Decimal("10.00"), idempotency_key=ikey("fund"))

    original_update = svc._update_balance_optimistic
    call_count = [0]

    def patched_update(balance_row, new_balance):
        call_count[0] += 1
        if call_count[0] == 1:
            db.rollback()
            raise ConcurrentModificationError(balance_row.id)
        return original_update(balance_row, new_balance)

    svc._update_balance_optimistic = patched_update

    key = ikey("retry-debit")
    tx = svc.debit(user_id=user.id, currency="USD", amount=Decimal("3.00"), idempotency_key=key)

    assert tx is not None
    assert svc.balance(user.id, "USD") == Decimal("7.00")
    assert call_count[0] == 2


def test_credit_raises_after_max_retries(db, user, svc):
    """After _MAX_RETRIES consecutive ConcurrentModificationError, the service
    propagates the exception instead of silently failing."""
    def always_fail(balance_row, new_balance):
        db.rollback()
        raise ConcurrentModificationError(balance_row.id if balance_row else "test-id")

    svc._update_balance_optimistic = always_fail

    with pytest.raises(ConcurrentModificationError):
        svc.credit(
            user_id=user.id, currency="USD",
            amount=Decimal("5.00"), idempotency_key=ikey("exhaust"),
        )

    # No credits were applied
    assert svc.balance(user.id, "USD") == Decimal("0")
