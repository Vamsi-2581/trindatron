"""Unit tests for PaymentOrderService — Billing-1 / Passo 5.

Test matrix
-----------
P1)  create_order returns a pending order with correct fields.
P2)  create_order is idempotent — same idempotency_key returns existing order.
P3)  get_order returns the order for the correct user.
P4)  get_order with the wrong user returns None.
P5)  list_orders returns the user's orders, filtered by status.
P6)  mark_processing transitions pending → processing, sets external_order_id.
P7)  mark_processing on a non-pending order raises InvalidStatusTransition.
P8)  confirm_order (pending) credits balance, writes ledger pair, sets confirmed_at.
P9)  confirm_order (processing) → confirmed (same financial result).
P10) confirm_order already confirmed → idempotent (no extra credits).
P11) confirm_order on cancelled/failed/expired raises InvalidStatusTransition.
P12) confirm_order on an expired-by-time order auto-transitions to 'expired', raises OrderExpired.
P13) confirm_order failure rolls back — balance and ledger unchanged.
P14) cancel_order on pending → cancelled.
P15) cancel_order on confirmed raises InvalidStatusTransition.
P16) fail_order on processing → failed, stores webhook_raw.
P17) expire_order on pending → expired (no user_id check).
P18) refund_order (full) debits balance, writes ledger pair, status → 'refunded'.
P19) refund_order (partial) debits partial balance, status → 'partial_refund'.
P20) refund_order on a non-confirmed order raises InvalidStatusTransition.
P21) refund_order with amount > credits_amount raises RefundAmountExceedsEligible before DB.
P22) refund_order failure rolls back — balance and order status unchanged.
P23) Σ-invariant: after confirm + refund, Σdebits == Σcredits across all ledger entries.
P24) confirm_order — concurrent winner commits mid-flight, our credit_flush
     then hits IntegrityError (the DB-level race CreditService.credit_flush's
     own docstring documents) → recovered idempotently, no 500, no double-credit.
P25) confirm_order — IntegrityError that is NOT the documented race (order
     still not confirmed after refresh) → re-raised, not swallowed.

Commercial Checkout V1 — plan-purchase orders
--------------------------------------------------
P26) confirm_order(plan set) activates user.plan, records ledger revenue,
     never touches CreditBalance/CreditTransaction.
P27) confirm_order(plan set) is idempotent.
P28) refund_order on a plan-purchase order raises PlanPurchaseNotRefundable.
P29) expire_overdue_orders bulk-sweeps only overdue 'pending' orders.
P30) confirm_order(plan set) recovers from a concurrent-race IntegrityError
     the same way the credit branch (P24) does.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from src.billing.providers.base import PaymentStatus
from src.database import Base
from src.models import (  # noqa: F401 — registers all tables on Base.metadata
    CreditBalance,
    CreditTransaction,
    LedgerEntry,
    PaymentOrder,
)
from src.models.user import User
from src.services.billing_orchestrator import RefundAmountExceedsEligible
from src.services.credit_service import CreditService
from src.services.ledger_service import LedgerService
from src.services.payment_order_service import (
    InvalidStatusTransition,
    OrderExpired,
    OrderNotFound,
    PaymentOrderService,
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
    u = User(email=f"pos-{uuid.uuid4()}@test.com", hashed_password="x", plan="start")
    db.add(u)
    db.commit()
    return u


@pytest.fixture
def user2(db):
    u = User(email=f"pos2-{uuid.uuid4()}@test.com", hashed_password="x", plan="start")
    db.add(u)
    db.commit()
    return u


@pytest.fixture
def svc(db):
    return PaymentOrderService(db)


@pytest.fixture(autouse=True)
def no_retry_sleep():
    """Zero out exponential back-off in CreditService to keep tests fast."""
    original = CreditService._RETRY_BASE_DELAY_MS
    CreditService._RETRY_BASE_DELAY_MS = 0.0
    yield
    CreditService._RETRY_BASE_DELAY_MS = original


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ikey() -> str:
    return f"ikey-{uuid.uuid4()}"


def _future(seconds: int = 3600) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _past(seconds: int = 1) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


def _make_order(
    svc: PaymentOrderService,
    user: User,
    *,
    credits_amount: str = "10.00",
    expires_at: datetime | None = None,
    **overrides,
) -> PaymentOrder:
    """Create a PaymentOrder with sensible defaults."""
    kwargs = dict(
        user_id=user.id,
        provider="test_provider",
        amount_fiat=Decimal("10.00"),
        currency_fiat="USD",
        credits_amount=Decimal(credits_amount),
        currency_credits="USD",
        idempotency_key=_ikey(),
        expires_at=expires_at or _future(),
    )
    kwargs.update(overrides)
    return svc.create_order(**kwargs)


def _confirm(svc: PaymentOrderService, order: PaymentOrder) -> PaymentOrder:
    """Confirm an order (brings it to 'confirmed' + credits applied)."""
    return svc.confirm_order(order.id, order.user_id)


# ---------------------------------------------------------------------------
# P1 — create_order returns pending order with correct fields
# ---------------------------------------------------------------------------


def test_P1_create_order_returns_pending_with_correct_fields(svc, db, user):
    expires = _future(7200)
    order = svc.create_order(
        user_id=user.id,
        provider="binance_pay",
        amount_fiat=Decimal("25.00"),
        currency_fiat="USD",
        credits_amount=Decimal("25.00"),
        currency_credits="USD",
        idempotency_key="P1-key",
        expires_at=expires,
    )

    assert order.id is not None
    assert order.user_id == user.id
    assert order.provider == "binance_pay"
    assert order.amount_fiat == Decimal("25.00")
    assert order.currency_fiat == "USD"
    assert order.credits_amount == Decimal("25.00")
    assert order.currency_credits == "USD"
    assert order.idempotency_key == "P1-key"
    assert order.status == PaymentStatus.PENDING.value
    assert order.confirmed_at is None
    assert order.external_order_id is None
    assert order.webhook_raw is None
    assert db.query(PaymentOrder).count() == 1


# ---------------------------------------------------------------------------
# P2 — create_order is idempotent
# ---------------------------------------------------------------------------


def test_P2_create_order_idempotent_same_key_returns_existing(svc, db, user):
    key = "P2-idem-key"
    o1 = svc.create_order(
        user_id=user.id, provider="p", amount_fiat=Decimal("1"), currency_fiat="USD",
        credits_amount=Decimal("1"), currency_credits="USD", idempotency_key=key,
        expires_at=_future(),
    )
    o2 = svc.create_order(
        user_id=user.id, provider="p_other", amount_fiat=Decimal("999"), currency_fiat="BTC",
        credits_amount=Decimal("999"), currency_credits="TCU", idempotency_key=key,
        expires_at=_future(9999),
    )

    assert o1.id == o2.id
    assert db.query(PaymentOrder).count() == 1
    # Fields from first creation preserved — not overwritten
    assert o2.provider == "p"
    assert o2.credits_amount == Decimal("1")


# ---------------------------------------------------------------------------
# P3 — get_order returns order for the correct user
# ---------------------------------------------------------------------------


def test_P3_get_order_returns_order_for_correct_user(svc, user):
    order = _make_order(svc, user)
    fetched = svc.get_order(order.id, user.id)

    assert fetched is not None
    assert fetched.id == order.id


# ---------------------------------------------------------------------------
# P4 — get_order with wrong user returns None
# ---------------------------------------------------------------------------


def test_P4_get_order_wrong_user_returns_none(svc, user, user2):
    order = _make_order(svc, user)
    fetched = svc.get_order(order.id, user2.id)

    assert fetched is None


# ---------------------------------------------------------------------------
# P5 — list_orders returns user's orders, optionally filtered by status
# ---------------------------------------------------------------------------


def test_P5_list_orders_filters_by_status(svc, db, user):
    o_pending = _make_order(svc, user)
    o_cancel = _make_order(svc, user)
    svc.cancel_order(o_cancel.id, user.id)

    all_orders = svc.list_orders(user.id)
    pending_only = svc.list_orders(user.id, status=PaymentStatus.PENDING.value)
    cancelled_only = svc.list_orders(user.id, status=PaymentStatus.CANCELLED.value)

    assert len(all_orders) == 2
    assert len(pending_only) == 1
    assert pending_only[0].id == o_pending.id
    assert len(cancelled_only) == 1
    assert cancelled_only[0].id == o_cancel.id


# ---------------------------------------------------------------------------
# P6 — mark_processing transitions pending → processing
# ---------------------------------------------------------------------------


def test_P6_mark_processing_sets_status_and_external_id(svc, user):
    order = _make_order(svc, user)
    updated = svc.mark_processing(order.id, user.id, external_order_id="EXT-001")

    assert updated.status == PaymentStatus.PROCESSING.value
    assert updated.external_order_id == "EXT-001"


# ---------------------------------------------------------------------------
# P7 — mark_processing on non-pending raises InvalidStatusTransition
# ---------------------------------------------------------------------------


def test_P7_mark_processing_invalid_status_raises(svc, user):
    order = _make_order(svc, user)
    _confirm(svc, order)

    with pytest.raises(InvalidStatusTransition) as exc_info:
        svc.mark_processing(order.id, user.id)

    assert exc_info.value.from_status == PaymentStatus.CONFIRMED.value
    assert exc_info.value.to_status == PaymentStatus.PROCESSING.value


# ---------------------------------------------------------------------------
# P8 — confirm_order (pending) credits balance, writes ledger, sets confirmed_at
# ---------------------------------------------------------------------------


def test_P8_confirm_pending_credits_balance_and_writes_ledger(svc, db, user):
    order = _make_order(svc, user, credits_amount="15.00")
    confirmed = _confirm(svc, order)

    assert confirmed.status == PaymentStatus.CONFIRMED.value
    assert confirmed.confirmed_at is not None

    # Financial state
    balance = CreditService(db).balance(user.id, "USD")
    assert balance == Decimal("15.00")

    # Credit transaction
    tx = db.query(CreditTransaction).filter_by(transaction_type="purchase").one()
    assert tx.amount == Decimal("15.00")
    assert tx.reference_id == order.id
    assert tx.reference_type == "payment_order"

    # Ledger pair: canonical direction
    entries = db.query(LedgerEntry).all()
    assert len(entries) == 2
    assert {e.direction for e in entries} == {"debit", "credit"}
    for e in entries:
        assert e.account_from == f"user:{user.id}"
        assert e.account_to == "revenue"
        assert e.amount == Decimal("15.00")
        assert e.reference_id == order.id
        assert e.reference_type == "payment_order"


# ---------------------------------------------------------------------------
# P9 — confirm_order (processing) → confirmed (same financial result)
# ---------------------------------------------------------------------------


def test_P9_confirm_from_processing_succeeds(svc, db, user):
    order = _make_order(svc, user, credits_amount="5.00")
    svc.mark_processing(order.id, user.id)

    confirmed = svc.confirm_order(order.id, user.id)

    assert confirmed.status == PaymentStatus.CONFIRMED.value
    assert CreditService(db).balance(user.id, "USD") == Decimal("5.00")
    assert db.query(LedgerEntry).count() == 2


# ---------------------------------------------------------------------------
# P10 — confirm_order already confirmed → idempotent (no extra credits)
# ---------------------------------------------------------------------------


def test_P10_confirm_idempotent_already_confirmed(svc, db, user):
    order = _make_order(svc, user, credits_amount="10.00")
    first = _confirm(svc, order)
    second = svc.confirm_order(order.id, user.id)

    assert second.id == first.id
    assert second.status == PaymentStatus.CONFIRMED.value

    # Credits applied exactly once
    assert CreditService(db).balance(user.id, "USD") == Decimal("10.00")
    assert db.query(LedgerEntry).count() == 2


# ---------------------------------------------------------------------------
# P11 — confirm_order on cancelled/failed/expired raises InvalidStatusTransition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("terminal_fn,terminal_status", [
    (lambda svc, o: svc.cancel_order(o.id, o.user_id), "cancelled"),
    (lambda svc, o: svc.fail_order(o.id, o.user_id), "failed"),
])
def test_P11_confirm_terminal_order_raises(svc, db, user, terminal_fn, terminal_status):
    order = _make_order(svc, user)
    terminal_fn(svc, order)

    with pytest.raises(InvalidStatusTransition) as exc_info:
        svc.confirm_order(order.id, user.id)

    assert exc_info.value.from_status == terminal_status


# ---------------------------------------------------------------------------
# P12 — confirm_order expired-by-time auto-transitions to 'expired', raises OrderExpired
# ---------------------------------------------------------------------------


def test_P12_confirm_expired_by_time_raises_order_expired(svc, db, user):
    order = _make_order(svc, user, expires_at=_past(10))

    with pytest.raises(OrderExpired) as exc_info:
        svc.confirm_order(order.id, user.id)

    assert exc_info.value.order_id == order.id

    # Side effect: order status updated to 'expired'
    refreshed = svc.get_order(order.id, user.id)
    assert refreshed.status == PaymentStatus.EXPIRED.value

    # No credits applied
    assert CreditService(db).balance(user.id, "USD") == Decimal("0.00")
    assert db.query(LedgerEntry).count() == 0


# ---------------------------------------------------------------------------
# P13 — confirm_order failure rolls back — balance and ledger unchanged
# ---------------------------------------------------------------------------


def test_P13_confirm_failure_rolls_back_balance_and_ledger(svc, db, user):
    order = _make_order(svc, user, credits_amount="10.00")

    with patch.object(
        svc._ledger_svc, "record_double_entry_flush",
        side_effect=RuntimeError("simulated ledger failure"),
    ):
        with pytest.raises(RuntimeError, match="simulated ledger failure"):
            svc.confirm_order(order.id, user.id)

    # Rollback: no credits, no ledger entries
    assert CreditService(db).balance(user.id, "USD") == Decimal("0.00")
    assert db.query(LedgerEntry).count() == 0
    assert db.query(CreditTransaction).filter_by(transaction_type="purchase").count() == 0

    # Order status unchanged — still pending
    refreshed = svc.get_order(order.id, user.id)
    assert refreshed.status == PaymentStatus.PENDING.value


# ---------------------------------------------------------------------------
# P14 — cancel_order on pending → cancelled
# ---------------------------------------------------------------------------


def test_P14_cancel_pending_succeeds(svc, user):
    order = _make_order(svc, user)
    cancelled = svc.cancel_order(order.id, user.id)

    assert cancelled.status == PaymentStatus.CANCELLED.value


# ---------------------------------------------------------------------------
# P15 — cancel_order on confirmed raises InvalidStatusTransition
# ---------------------------------------------------------------------------


def test_P15_cancel_confirmed_raises(svc, user):
    order = _make_order(svc, user)
    _confirm(svc, order)

    with pytest.raises(InvalidStatusTransition) as exc_info:
        svc.cancel_order(order.id, user.id)

    assert exc_info.value.from_status == PaymentStatus.CONFIRMED.value


# ---------------------------------------------------------------------------
# P16 — fail_order on processing → failed, stores webhook_raw
# ---------------------------------------------------------------------------


def test_P16_fail_order_stores_webhook_raw(svc, user):
    order = _make_order(svc, user)
    svc.mark_processing(order.id, user.id)

    payload = {"event": "payment.failed", "reason": "card_declined"}
    failed = svc.fail_order(order.id, user.id, webhook_raw=payload)

    assert failed.status == PaymentStatus.FAILED.value

    import json
    stored = json.loads(failed.webhook_raw)
    assert stored["reason"] == "card_declined"


# ---------------------------------------------------------------------------
# P17 — expire_order on pending → expired (no user_id check)
# ---------------------------------------------------------------------------


def test_P17_expire_order_no_user_check(svc, user):
    order = _make_order(svc, user)
    expired = svc.expire_order(order.id)

    assert expired.status == PaymentStatus.EXPIRED.value


def test_P17_expire_order_unknown_raises(svc):
    with pytest.raises(OrderNotFound):
        svc.expire_order("nonexistent-id")


# ---------------------------------------------------------------------------
# P18 — refund_order (full) debits balance, ledger pair, status → 'refunded'
# ---------------------------------------------------------------------------


def test_P18_full_refund_debits_balance_and_writes_ledger(svc, db, user):
    order = _make_order(svc, user, credits_amount="20.00")
    _confirm(svc, order)

    assert CreditService(db).balance(user.id, "USD") == Decimal("20.00")

    refunded = svc.refund_order(order.id, user.id, Decimal("20.00"), note="full refund")

    assert refunded.status == PaymentStatus.REFUNDED.value
    assert CreditService(db).balance(user.id, "USD") == Decimal("0.00")

    # 2 from confirm + 2 from refund
    assert db.query(LedgerEntry).count() == 4

    # Refund CreditTransaction
    refund_tx = db.query(CreditTransaction).filter_by(transaction_type="refund").one()
    assert refund_tx.amount == Decimal("20.00")
    assert refund_tx.note == "full refund"
    assert refund_tx.reference_id == order.id

    # Ledger pair for refund has canonical direction
    refund_entries = [e for e in db.query(LedgerEntry).all() if e.entry_type == "refund"]
    assert len(refund_entries) == 2
    for e in refund_entries:
        assert e.account_from == f"user:{user.id}"
        assert e.account_to == "revenue"


# ---------------------------------------------------------------------------
# P19 — refund_order (partial) status → 'partial_refund'
# ---------------------------------------------------------------------------


def test_P19_partial_refund_status_and_balance(svc, db, user):
    order = _make_order(svc, user, credits_amount="20.00")
    _confirm(svc, order)

    partial = svc.refund_order(order.id, user.id, Decimal("7.00"))

    assert partial.status == PaymentStatus.PARTIAL_REFUND.value
    assert CreditService(db).balance(user.id, "USD") == Decimal("13.00")  # 20 - 7


# ---------------------------------------------------------------------------
# P20 — refund_order on non-confirmed raises InvalidStatusTransition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("setup_fn", [
    lambda svc, o: None,                                              # pending
    lambda svc, o: svc.cancel_order(o.id, o.user_id),                # cancelled
    lambda svc, o: svc.fail_order(o.id, o.user_id),                  # failed
])
def test_P20_refund_non_confirmed_raises(svc, user, setup_fn):
    order = _make_order(svc, user)
    setup_fn(svc, order)

    with pytest.raises(InvalidStatusTransition):
        svc.refund_order(order.id, user.id, Decimal("5.00"))


# ---------------------------------------------------------------------------
# P21 — refund_order amount > credits_amount raises RefundAmountExceedsEligible
# ---------------------------------------------------------------------------


def test_P21_refund_exceeds_credits_amount_raises_before_db(svc, db, user):
    order = _make_order(svc, user, credits_amount="10.00")
    _confirm(svc, order)
    ledger_count_before = db.query(LedgerEntry).count()

    with pytest.raises(RefundAmountExceedsEligible) as exc_info:
        svc.refund_order(order.id, user.id, Decimal("10.01"))

    assert exc_info.value.amount == Decimal("10.01")
    assert exc_info.value.max_refundable == Decimal("10.00")
    # Guard fires before any DB writes — no new ledger entries
    assert db.query(LedgerEntry).count() == ledger_count_before
    # Credits unchanged
    assert CreditService(db).balance(user.id, "USD") == Decimal("10.00")


# ---------------------------------------------------------------------------
# P22 — refund_order failure rolls back — balance and order unchanged
# ---------------------------------------------------------------------------


def test_P22_refund_failure_rolls_back(svc, db, user):
    order = _make_order(svc, user, credits_amount="10.00")
    _confirm(svc, order)

    assert CreditService(db).balance(user.id, "USD") == Decimal("10.00")
    ledger_count_after_confirm = db.query(LedgerEntry).count()

    with patch.object(
        svc._ledger_svc, "record_double_entry_flush",
        side_effect=RuntimeError("simulated ledger failure"),
    ):
        with pytest.raises(RuntimeError, match="simulated ledger failure"):
            svc.refund_order(order.id, user.id, Decimal("10.00"))

    # Balance unchanged — credit debit was rolled back
    assert CreditService(db).balance(user.id, "USD") == Decimal("10.00")
    # No new ledger entries
    assert db.query(LedgerEntry).count() == ledger_count_after_confirm
    # Order status unchanged
    refreshed = svc.get_order(order.id, user.id)
    assert refreshed.status == PaymentStatus.CONFIRMED.value


# ---------------------------------------------------------------------------
# P23 — Σ-invariant: confirm + full refund → Σdebits == Σcredits
# ---------------------------------------------------------------------------


def test_P23_sigma_invariant_confirm_and_full_refund(svc, db, user):
    order = _make_order(svc, user, credits_amount="18.00")
    _confirm(svc, order)
    svc.refund_order(order.id, user.id, Decimal("18.00"))

    entries = db.query(LedgerEntry).all()
    total_debit = sum(e.amount for e in entries if e.direction == "debit")
    total_credit = sum(e.amount for e in entries if e.direction == "credit")

    assert total_debit == total_credit, (
        f"Ledger Σ-invariant broken: Σdebits={total_debit} Σcredits={total_credit}"
    )

    # Four entries total: 2 from confirm + 2 from refund
    assert len(entries) == 4

    # Balance after full cycle is zero
    assert CreditService(db).balance(user.id, "USD") == Decimal("0.00")

    # Two distinct CreditTransaction records
    txs = db.query(CreditTransaction).all()
    types = {tx.transaction_type for tx in txs}
    assert types == {"purchase", "refund"}


# ---------------------------------------------------------------------------
# P24/P25 — confirm_order concurrent-race recovery (webhook × polling)
# ---------------------------------------------------------------------------


def test_P24_confirm_order_recovers_idempotently_from_concurrent_race(svc, db, user):
    """Simulates two callers racing to confirm the same order (e.g. the
    webhook route and a future polling job). The mocked credit_flush plays
    the role of "a concurrent winner just fully committed" — real
    CreditService.credit_flush(), real LedgerService.record_double_entry_flush(),
    real order.status flip, real commit — then raises the exact exception type
    our own credit_flush() would see afterward: IntegrityError, because the
    winner's row for this order's idempotency key already exists (see
    CreditService.credit_flush's own docstring: "IntegrityError: concurrent
    idempotency race — propagated; caller should rollback. A retry will find
    the winning transaction.").

    Before this fix, this exact scenario propagated as an unhandled
    IntegrityError (500 to an HTTP caller) instead of the idempotent 200 a
    genuine race should produce.
    """
    order = _make_order(svc, user, credits_amount="12.00")

    def _winner_commits_then_raises_integrity_error(**_kwargs):
        real_credit_svc = CreditService(db)
        real_credit_svc.credit_flush(
            user_id=order.user_id,
            currency=order.currency_credits,
            amount=order.credits_amount,
            idempotency_key=f"po:{order.id}:confirm:credit",
            transaction_type="purchase",
            reference_id=order.id,
            reference_type="payment_order",
        )
        LedgerService(db).record_double_entry_flush(
            entry_type="credit_purchase",
            account_debit=f"user:{order.user_id}",
            account_credit="revenue",
            amount=order.credits_amount,
            currency=order.currency_credits,
            reference_id=order.id,
            reference_type="payment_order",
            idempotency_key=f"po:{order.id}:confirm:ledger",
        )
        order.status = PaymentStatus.CONFIRMED.value
        order.confirmed_at = datetime.now(timezone.utc)
        db.commit()
        raise IntegrityError(
            "simulated concurrent idempotency race", {}, Exception("UNIQUE constraint failed")
        )

    with patch.object(
        svc._credit_svc, "credit_flush", side_effect=_winner_commits_then_raises_integrity_error,
    ):
        result = svc.confirm_order(order.id, user.id)  # must NOT raise

    assert result.status == PaymentStatus.CONFIRMED.value
    # Credited exactly once — by the simulated winner, not twice.
    assert CreditService(db).balance(user.id, "USD") == Decimal("12.00")
    assert db.query(CreditTransaction).filter_by(transaction_type="purchase").count() == 1
    assert db.query(LedgerEntry).count() == 2


def test_P25_confirm_order_non_race_integrity_error_is_reraised(svc, db, user):
    """An IntegrityError that is NOT the documented idempotency race (the
    order genuinely was not confirmed by anyone) must not be swallowed as a
    false success — it should propagate, same as any other unexpected error."""
    order = _make_order(svc, user, credits_amount="5.00")

    with patch.object(
        svc._credit_svc, "credit_flush",
        side_effect=IntegrityError(
            "simulated non-race integrity error", {}, Exception("some other constraint")
        ),
    ):
        with pytest.raises(IntegrityError):
            svc.confirm_order(order.id, user.id)

    refreshed = svc.get_order(order.id, user.id)
    assert refreshed.status == PaymentStatus.PENDING.value
    assert CreditService(db).balance(user.id, "USD") == Decimal("0.00")


# ---------------------------------------------------------------------------
# Commercial Checkout V1 — plan-purchase orders
#
# P26) confirm_order(plan set) activates user.plan, writes ONE ledger entry
#      pair (entry_type="plan_purchase"), and creates NO CreditBalance/
#      CreditTransaction row at all.
# P27) confirm_order(plan set) is idempotent — a second call is a no-op
#      fast-path return, plan/ledger unaffected further.
# P28) refund_order on a plan-purchase order raises PlanPurchaseNotRefundable
#      — checked even with a fresh/unused idempotency_key.
# P29) expire_overdue_orders bulk-sweeps only 'pending' orders whose
#      expires_at has passed — a non-overdue pending order and an
#      already-confirmed order are both left untouched.
# P30) confirm_order(plan set) recovers idempotently from the SAME
#      concurrent-race IntegrityError shape P24 covers for the credit
#      branch — the ledger idempotency key format is identical either way.
# ---------------------------------------------------------------------------
from src.services.payment_order_service import PlanPurchaseNotRefundable, expire_overdue_orders  # noqa: E402


def test_P26_confirm_order_with_plan_activates_user_plan_no_credit_balance(svc, db, user):
    assert user.plan == "start"
    order = _make_order(svc, user, plan="pro")

    result = svc.confirm_order(order.id, user.id)

    assert result.status == PaymentStatus.CONFIRMED.value
    assert result.plan == "pro"
    db.refresh(user)
    assert user.plan == "pro"

    # No spendable balance was created for this purchase — see
    # PaymentOrder.plan's own docstring on why a plan purchase must never
    # touch CreditBalance.
    assert CreditService(db).balance(user.id, "USD") == Decimal("0.00")
    assert db.query(CreditTransaction).count() == 0
    assert db.query(CreditBalance).count() == 0

    entries = db.query(LedgerEntry).filter(LedgerEntry.reference_id == order.id).all()
    assert len(entries) == 2  # debit + credit pair
    assert all(e.entry_type == "plan_purchase" for e in entries)
    assert all(e.amount == order.amount_fiat for e in entries)


def test_P27_confirm_order_with_plan_is_idempotent(svc, db, user):
    order = _make_order(svc, user, plan="max")
    first = svc.confirm_order(order.id, user.id)
    db.refresh(user)
    assert user.plan == "max"

    # A second confirm — fast-path idempotent return, no new writes.
    second = svc.confirm_order(order.id, user.id)

    assert second.status == PaymentStatus.CONFIRMED.value
    assert second.confirmed_at == first.confirmed_at
    entries = db.query(LedgerEntry).filter(LedgerEntry.reference_id == order.id).all()
    assert len(entries) == 2  # still just the original pair


def test_P28_refund_order_on_plan_purchase_is_refused(svc, db, user):
    order = _make_order(svc, user, plan="start")
    svc.confirm_order(order.id, user.id)

    with pytest.raises(PlanPurchaseNotRefundable):
        svc.refund_order(order.id, user.id, Decimal("9.99"), idempotency_key=_ikey())

    # Refused even before any idempotency-key lookup — a fresh key changes nothing.
    with pytest.raises(PlanPurchaseNotRefundable):
        svc.refund_order(order.id, user.id, Decimal("9.99"), idempotency_key=_ikey())

    refreshed = svc.get_order(order.id, user.id)
    assert refreshed.status == PaymentStatus.CONFIRMED.value  # untouched


def test_P29_expire_overdue_orders_sweeps_only_overdue_pending(svc, db, user):
    overdue = _make_order(svc, user, expires_at=_past())
    not_overdue = _make_order(svc, user, expires_at=_future())
    confirmed = _make_order(svc, user)
    svc.confirm_order(confirmed.id, user.id)

    count = expire_overdue_orders(db, now=datetime.now(timezone.utc))

    assert count == 1
    assert svc.get_order(overdue.id, user.id).status == PaymentStatus.EXPIRED.value
    assert svc.get_order(not_overdue.id, user.id).status == PaymentStatus.PENDING.value
    assert svc.get_order(confirmed.id, user.id).status == PaymentStatus.CONFIRMED.value

    # Idempotent — sweeping again finds nothing new to expire.
    assert expire_overdue_orders(db, now=datetime.now(timezone.utc)) == 0


def test_P30_confirm_order_with_plan_recovers_from_concurrent_race(svc, db, user):
    order = _make_order(svc, user, plan="pro")

    def _winner_commits_then_raises_integrity_error(**_kwargs):
        LedgerService(db).record_double_entry_flush(
            entry_type="plan_purchase",
            account_debit=f"user:{order.user_id}",
            account_credit="revenue",
            amount=order.amount_fiat,
            currency=order.currency_fiat,
            reference_id=order.id,
            reference_type="payment_order",
            idempotency_key=f"po:{order.id}:confirm:ledger",
        )
        order.user.plan = order.plan
        order.status = PaymentStatus.CONFIRMED.value
        order.confirmed_at = datetime.now(timezone.utc)
        db.commit()
        raise IntegrityError(
            "simulated concurrent idempotency race", {}, Exception("UNIQUE constraint failed")
        )

    with patch.object(
        svc._ledger_svc, "record_double_entry_flush", side_effect=_winner_commits_then_raises_integrity_error,
    ):
        result = svc.confirm_order(order.id, user.id)  # must NOT raise

    assert result.status == PaymentStatus.CONFIRMED.value
    db.refresh(user)
    assert user.plan == "pro"
    assert db.query(LedgerEntry).filter(LedgerEntry.reference_id == order.id).count() == 2


# ---------------------------------------------------------------------------
# P31-P32 — Hardening: expire_order() vs. create_provider_order() race
# ---------------------------------------------------------------------------
# P31) expire_order() now acquires the same row lock as
#      lock_order_for_provider_attempt() — confirmed here indirectly by
#      P29 (unchanged pass) and directly by asserting expire_order()
#      still transitions correctly on a plain, uncontested order.
# P32) DETERMINISTIC reproduction of the exact race the audit flagged:
#      if a background expire_order() call wins BEFORE
#      create_provider_order() ever reaches create_intent() (simulated
#      here by expiring the order directly first, rather than via real
#      thread timing — the guard's job is to detect "already not
#      pending", regardless of HOW that happened), the adapter's
#      create_intent() must NEVER be called, and OrderExpired must be
#      raised — never an unhandled InvalidStatusTransition from
#      mark_processing(), and never a real external session left
#      orphaned with no local order able to track it.
# ---------------------------------------------------------------------------
from typing import Mapping  # noqa: E402

from src.billing.providers.base import (  # noqa: E402
    PaymentIntent,
    PaymentProvider,
    PaymentResult,
    WebhookEvent,
)
from src.services.payment_order_service import create_provider_order  # noqa: E402


class _MinimalFakeAdapter(PaymentProvider):
    """The smallest legal PaymentProvider — tracks whether create_intent()
    was ever called, without needing to succeed/fail in any particular way
    (P32 only cares that it's NEVER invoked)."""

    def __init__(self, name: str = "fake_hardening_provider") -> None:
        self._name = name
        self.create_intent_calls = 0

    @property
    def provider_name(self) -> str:
        return self._name

    async def create_intent(self, *, amount, currency, user_id, idempotency_key, metadata=None) -> PaymentIntent:
        self.create_intent_calls += 1
        return PaymentIntent(
            id=f"ext-{idempotency_key}", provider=self._name, amount=amount,
            currency=currency.upper(), status=PaymentStatus.PENDING, idempotency_key=idempotency_key,
        )

    async def get_intent(self, intent_id: str) -> PaymentIntent:
        raise NotImplementedError

    async def verify_webhook(self, payload: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        raise NotImplementedError

    async def refund(self, payment_id: str, **kwargs) -> PaymentResult:
        raise NotImplementedError


def test_P31_expire_order_still_transitions_normally(svc, user):
    """Sanity check that the row-lock hardening didn't change
    expire_order()'s ordinary, uncontested behavior."""
    order = _make_order(svc, user, expires_at=_past())
    expired = svc.expire_order(order.id)
    assert expired.status == PaymentStatus.EXPIRED.value


@pytest.mark.asyncio
async def test_P32_create_provider_order_never_calls_create_intent_for_expired_order(svc, db, user):
    """DETERMINISTIC reproduction of the audited race: an order that a
    concurrent expire_order() call already won (status='expired') before
    create_provider_order() got to attempt it must be refused BEFORE
    create_intent() is ever invoked."""
    adapter = _MinimalFakeAdapter()
    key = str(uuid.uuid4())

    order = svc.create_order(
        user_id=user.id, provider=adapter.provider_name, amount_fiat=Decimal("9.99"),
        currency_fiat="USD", credits_amount=Decimal("9.99"), currency_credits="USD",
        idempotency_key=key, expires_at=_future(),
    )
    # Simulates the background sweep winning the race — see module
    # docstring above for why this is a valid deterministic stand-in.
    svc.expire_order(order.id)

    with pytest.raises(OrderExpired):
        await create_provider_order(
            db, adapter, user_id=user.id, amount_fiat=Decimal("9.99"), currency_fiat="USD",
            credits_amount=Decimal("9.99"), currency_credits="USD",
            idempotency_key=key, expires_at=_future(),
        )

    assert adapter.create_intent_calls == 0, "create_intent() must NEVER be called for an already-expired order"

    reloaded = db.query(PaymentOrder).filter(PaymentOrder.id == order.id).first()
    assert reloaded.status == PaymentStatus.EXPIRED.value
    assert reloaded.external_order_id is None
