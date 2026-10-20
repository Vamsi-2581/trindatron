"""Unit tests for BillingOrchestrator.refund_and_record() — Billing-1 / Passo 4.

Semantics
---------
refund_and_record() reverses a PaymentOrder purchase:
  * User returns credits → gets money back from the gateway
  * TRINITY DEBITS the user's credit balance (removes credits)
  * Records a canonical double-entry pair (entry_type='refund') in the ledger
  * All four writes are atomic — committed or rolled back together

Contrast with CreditService.refund() which ADDS credits (service compensation).
refund_and_record() REMOVES credits — it is the inverse of credit_and_record().

Test matrix
-----------
R1)  Valid refund — balance debited, CreditTransaction(type='refund') + 2 LedgerEntries.
R2)  Idempotent refund — same keys twice → same records, balance debited once.
R3)  Invalid amount (zero / negative / non-Decimal) — raises before any DB access.
R4)  Exceeds eligible ceiling — RefundAmountExceedsEligible before any DB access.
R4b) Exactly at ceiling — succeeds.
R5)  Insufficient balance — InsufficientCreditError, no LedgerEntry written.
R6)  Ledger failure rolls back — debit flushed OK → ledger raises → nothing committed.
R7)  Σ double-entry invariant across a purchase + refund cycle.
R8)  No ceiling (max_refundable_amount=None) — any positive amount ≤ balance accepted.
R9)  Reference fields propagated to CreditTransaction and both LedgerEntries.
R10) transaction_type is always 'refund' regardless of other params.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.database import Base
from src.models import LedgerEntry, CreditBalance, CreditTransaction  # noqa: F401 — tables
from src.models.user import User
from src.services.billing_orchestrator import BillingOrchestrator, BillingResult, RefundAmountExceedsEligible
from src.services.credit_service import CreditService, InsufficientCreditError


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
    u = User(email=f"refund-{uuid.uuid4()}@test.com", hashed_password="x", plan="start")
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
    return f"refund-credit-{uuid.uuid4()}-{suffix}"


def _lkey(suffix: str = "") -> str:
    return f"refund-ledger-{uuid.uuid4()}-{suffix}"


def _fund(db, user_id: str, amount: str = "20.00") -> None:
    """Credit the user's balance so refund tests start with a funded account."""
    CreditService(db).credit(
        user_id=user_id,
        currency="USD",
        amount=Decimal(amount),
        idempotency_key=f"fund-{uuid.uuid4()}",
        transaction_type="purchase",
    )


def _refund_kwargs(user_id: str, amount: str = "10.00", **overrides):
    """Build refund_and_record() kwargs with canonical account direction."""
    defaults = dict(
        user_id=user_id,
        currency="USD",
        amount=Decimal(amount),
        ledger_entry_type="refund",
        # Canonical account direction for a PaymentOrder refund:
        #   account_debit  = user's account (credits are being removed)
        #   account_credit = revenue (the purchase revenue is being reversed)
        account_debit=f"user:{user_id}",
        account_credit="revenue",
        credit_idempotency_key=_ckey(),
        ledger_idempotency_key=_lkey(),
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# R1 — Valid refund: balance debited + CreditTX(refund) + 2 LedgerEntries
# ---------------------------------------------------------------------------


def test_R1_valid_refund_debits_balance_and_creates_ledger_pair(orch, db, user):
    """R1: a valid refund removes credits from the user and writes the journal pair."""
    _fund(db, user.id, "20.00")

    result = orch.refund_and_record(**_refund_kwargs(user.id, amount="10.00"))

    assert isinstance(result, BillingResult)
    assert result.transaction.transaction_type == "refund"
    assert result.transaction.amount == Decimal("10.00")
    assert result.ledger_debit.direction == "debit"
    assert result.ledger_credit.direction == "credit"
    assert result.ledger_debit.entry_type == "refund"
    assert result.ledger_credit.entry_type == "refund"

    # Both entries have the canonical account direction for a refund
    assert result.ledger_debit.account_from == f"user:{user.id}"
    assert result.ledger_debit.account_to == "revenue"
    assert result.ledger_credit.account_from == f"user:{user.id}"
    assert result.ledger_credit.account_to == "revenue"

    # DB state: fund TX + refund TX = 2 total; 2 ledger entries from refund only
    # (funding was done via CreditService.credit, which has no ledger entry here)
    assert db.query(CreditTransaction).filter_by(transaction_type="refund").count() == 1
    assert db.query(LedgerEntry).count() == 2

    # Balance: 20 − 10 = 10
    assert CreditService(db).balance(user.id, "USD") == Decimal("10.00")


# ---------------------------------------------------------------------------
# R2 — Idempotency: same keys twice → no duplicates, balance debited once
# ---------------------------------------------------------------------------


def test_R2_idempotent_refund_does_not_duplicate(orch, db, user):
    """R2: calling refund_and_record twice with the same keys returns existing records."""
    _fund(db, user.id, "20.00")
    ck = _ckey("R2")
    lk = _lkey("R2")
    kwargs = _refund_kwargs(
        user.id,
        amount="10.00",
        credit_idempotency_key=ck,
        ledger_idempotency_key=lk,
    )

    r1 = orch.refund_and_record(**kwargs)
    r2 = orch.refund_and_record(**kwargs)

    # Same records returned
    assert r1.transaction.id == r2.transaction.id
    assert r1.ledger_debit.id == r2.ledger_debit.id
    assert r1.ledger_credit.id == r2.ledger_credit.id

    # No duplicate CreditTransactions of type 'refund'
    assert db.query(CreditTransaction).filter_by(transaction_type="refund").count() == 1
    assert db.query(LedgerEntry).count() == 2

    # Balance debited exactly once: 20 − 10 = 10
    assert CreditService(db).balance(user.id, "USD") == Decimal("10.00")


# ---------------------------------------------------------------------------
# R3 — Invalid amount: zero, negative, non-Decimal
# ---------------------------------------------------------------------------


def test_R3_refund_amount_zero_raises(orch, db, user):
    """R3a: amount=0 raises ValueError before any DB write."""
    _fund(db, user.id)
    tx_count_before = db.query(CreditTransaction).count()

    with pytest.raises(ValueError, match="positive"):
        orch.refund_and_record(**_refund_kwargs(user.id, amount="0"))

    assert db.query(CreditTransaction).count() == tx_count_before


def test_R3_refund_amount_negative_raises(orch, db, user):
    """R3b: amount<0 raises ValueError before any DB write."""
    _fund(db, user.id)
    tx_count_before = db.query(CreditTransaction).count()

    with pytest.raises(ValueError, match="positive"):
        orch.refund_and_record(**_refund_kwargs(user.id, amount="-5.00"))

    assert db.query(CreditTransaction).count() == tx_count_before


def test_R3_refund_amount_non_decimal_raises(orch, db, user):
    """R3c: amount not Decimal raises TypeError before any DB write."""
    _fund(db, user.id)
    kwargs = _refund_kwargs(user.id)
    kwargs["amount"] = 10.0  # float — must not be accepted
    tx_count_before = db.query(CreditTransaction).count()

    with pytest.raises(TypeError, match="Decimal"):
        orch.refund_and_record(**kwargs)

    assert db.query(CreditTransaction).count() == tx_count_before


# ---------------------------------------------------------------------------
# R4 — Exceeds eligible ceiling: RefundAmountExceedsEligible raised pre-DB
# ---------------------------------------------------------------------------


def test_R4_refund_exceeds_ceiling_raises_before_db(orch, db, user):
    """R4: amount > max_refundable_amount raises RefundAmountExceedsEligible before DB."""
    _fund(db, user.id, "20.00")
    tx_count_before = db.query(CreditTransaction).count()

    with pytest.raises(RefundAmountExceedsEligible) as exc_info:
        orch.refund_and_record(
            **_refund_kwargs(user.id, amount="15.00"),
            max_refundable_amount=Decimal("10.00"),
        )

    assert exc_info.value.amount == Decimal("15.00")
    assert exc_info.value.max_refundable == Decimal("10.00")
    # Guard fires before the try-block → no new transactions
    assert db.query(CreditTransaction).count() == tx_count_before
    assert db.query(LedgerEntry).count() == 0


def test_R4_refund_exactly_at_ceiling_succeeds(orch, db, user):
    """R4b: amount == max_refundable_amount is within the limit and must succeed."""
    _fund(db, user.id, "20.00")

    result = orch.refund_and_record(
        **_refund_kwargs(user.id, amount="10.00"),
        max_refundable_amount=Decimal("10.00"),
    )
    assert result.transaction.amount == Decimal("10.00")
    assert CreditService(db).balance(user.id, "USD") == Decimal("10.00")


# ---------------------------------------------------------------------------
# R5 — Insufficient balance: InsufficientCreditError, no LedgerEntry written
# ---------------------------------------------------------------------------


def test_R5_insufficient_balance_raises_and_rolls_back(orch, db, user):
    """R5: user balance is lower than refund amount → InsufficientCreditError, nothing persisted."""
    # Fund with only 5.00, try to refund 10.00
    _fund(db, user.id, "5.00")
    tx_count_before = db.query(CreditTransaction).count()

    with pytest.raises(InsufficientCreditError):
        orch.refund_and_record(**_refund_kwargs(user.id, amount="10.00"))

    # No new refund transaction, no ledger entries
    assert db.query(CreditTransaction).filter_by(transaction_type="refund").count() == 0
    assert db.query(LedgerEntry).count() == 0
    # Balance unchanged
    assert CreditService(db).balance(user.id, "USD") == Decimal("5.00")


def test_R5_zero_balance_raises_before_ledger(orch, db, user):
    """R5b: user has no balance → InsufficientCreditError before any ledger write."""
    with pytest.raises(InsufficientCreditError):
        orch.refund_and_record(**_refund_kwargs(user.id, amount="1.00"))

    assert db.query(LedgerEntry).count() == 0


# ---------------------------------------------------------------------------
# R6 — Ledger failure rolls back: debit flushed → ledger raises → rollback
# ---------------------------------------------------------------------------


def test_R6_ledger_failure_rolls_back_refund(orch, db, user):
    """R6: debit_flush succeeds → ledger raises → rollback → NOTHING committed."""
    _fund(db, user.id, "20.00")

    with patch.object(
        orch._ledger_svc, "record_double_entry_flush",
        side_effect=RuntimeError("ledger failure"),
    ):
        with pytest.raises(RuntimeError, match="ledger failure"):
            orch.refund_and_record(**_refund_kwargs(user.id, amount="10.00"))

    # Rollback: no refund transaction, no ledger entries
    assert db.query(CreditTransaction).filter_by(transaction_type="refund").count() == 0
    assert db.query(LedgerEntry).count() == 0
    # Balance unchanged from funding: debit was rolled back
    assert CreditService(db).balance(user.id, "USD") == Decimal("20.00")


# ---------------------------------------------------------------------------
# R7 — Σ double-entry invariant across a full purchase + refund cycle
# ---------------------------------------------------------------------------


def test_R7_double_entry_sum_invariant_after_purchase_and_refund(orch, db, user):
    """R7: purchase then partial refund — Σdebits == Σcredits holds."""
    # Purchase: +20 credits, ledger: user → revenue
    orch.credit_and_record(
        user_id=user.id,
        currency="USD",
        amount=Decimal("20.00"),
        transaction_type="purchase",
        ledger_entry_type="credit_purchase",
        account_debit=f"user:{user.id}",
        account_credit="revenue",
        credit_idempotency_key=_ckey("buy"),
        ledger_idempotency_key=_lkey("buy"),
    )

    # Partial refund: −8 credits (user sends back 8 of 20)
    orch.refund_and_record(
        user_id=user.id,
        currency="USD",
        amount=Decimal("8.00"),
        ledger_entry_type="refund",
        account_debit=f"user:{user.id}",
        account_credit="revenue",
        credit_idempotency_key=_ckey("ref"),
        ledger_idempotency_key=_lkey("ref"),
        max_refundable_amount=Decimal("20.00"),
    )

    # Σ-invariant: all entries across both operations
    entries = db.query(LedgerEntry).all()
    total_debit = sum(e.amount for e in entries if e.direction == "debit")
    total_credit = sum(e.amount for e in entries if e.direction == "credit")
    assert total_debit == total_credit, (
        f"Ledger Σ-invariant broken: Σdebits={total_debit} != Σcredits={total_credit}"
    )

    # Balance: 20 purchased − 8 refunded = 12 remaining
    assert CreditService(db).balance(user.id, "USD") == Decimal("12.00")

    # CreditTransactions: one purchase + one refund
    txs = db.query(CreditTransaction).order_by(CreditTransaction.created_at).all()
    assert txs[0].transaction_type == "purchase"
    assert txs[1].transaction_type == "refund"

    # Four LedgerEntries total (2 from purchase, 2 from refund)
    assert db.query(LedgerEntry).count() == 4


# ---------------------------------------------------------------------------
# R8 — No ceiling: max_refundable_amount=None allows any positive amount ≤ balance
# ---------------------------------------------------------------------------


def test_R8_no_ceiling_allows_full_balance_refund(orch, db, user):
    """R8: when max_refundable_amount is None, no ceiling check is applied."""
    _fund(db, user.id, "99.50")

    result = orch.refund_and_record(
        **_refund_kwargs(user.id, amount="99.50"),
        max_refundable_amount=None,
    )
    assert result.transaction.amount == Decimal("99.50")
    assert CreditService(db).balance(user.id, "USD") == Decimal("0.00")


# ---------------------------------------------------------------------------
# R9 — Reference fields propagated to CreditTransaction and both LedgerEntries
# ---------------------------------------------------------------------------


def test_R9_reference_fields_stored_on_tx_and_ledger(orch, db, user):
    """R9: reference_id, reference_type, and note are stored on all written rows."""
    _fund(db, user.id, "20.00")
    order_id = str(uuid.uuid4())

    result = orch.refund_and_record(
        **_refund_kwargs(user.id, amount="10.00"),
        reference_id=order_id,
        reference_type="payment_order",
        note="full refund — order disputed",
    )

    assert result.transaction.reference_id == order_id
    assert result.transaction.reference_type == "payment_order"
    assert result.transaction.note == "full refund — order disputed"
    assert result.ledger_debit.reference_id == order_id
    assert result.ledger_debit.reference_type == "payment_order"
    assert result.ledger_credit.reference_id == order_id
    assert result.ledger_credit.reference_type == "payment_order"


# ---------------------------------------------------------------------------
# R10 — transaction_type is always 'refund'
# ---------------------------------------------------------------------------


def test_R10_transaction_type_is_always_refund(orch, db, user):
    """R10: refund_and_record() always creates CreditTransaction(type='refund')."""
    _fund(db, user.id, "20.00")

    result = orch.refund_and_record(**_refund_kwargs(user.id))
    assert result.transaction.transaction_type == "refund"

    tx_in_db = db.get(CreditTransaction, result.transaction.id)
    assert tx_in_db.transaction_type == "refund"
