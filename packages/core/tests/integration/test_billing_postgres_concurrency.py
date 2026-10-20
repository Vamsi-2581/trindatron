"""Real-concurrency proof for NOWPayments/billing race conditions —
mission requirement: "SQLite sozinho não é suficiente para declarar
billing production-ready", "executar os testes de concorrência em
PostgreSQL real" before any real money moves through NOWPayments.

Mirrors tests/integration/test_idempotency_concurrency.py exactly: gated
by tests/postgres_helpers.py::postgres_available() (TEST_POSTGRES_URL),
own engine/session per thread, entirely independent of the SQLite
`db`/`client` fixtures the rest of the suite uses. SQLite serializes all
writes (single connection, single writer) so none of these races can be
reproduced there — only a real multi-connection PostgreSQL proves the
DB-level constraints (UNIQUE on webhook_nonces, UNIQUE on the ledger's
idempotency keys) are what actually make these races safe, not just
"the code happens to run in one order in tests".

Test matrix
-----------
PGC01) Two threads call PaymentOrderService.confirm_order() on the SAME
       plan-purchase PaymentOrder at the same instant (simulates two
       genuinely concurrent webhook deliveries that both passed the
       nonce pre-check before either committed — see confirm_order()'s
       own "Concurrent confirmation" docstring) -> exactly one ledger
       entry pair, user.plan set exactly once, both threads return
       without an unhandled exception.
PGC02) Two threads call webhook_replay_service.record_nonce() for the
       exact SAME (provider, nonce) pair at the same instant -> exactly
       one row in webhook_nonces (UNIQUE(provider, nonce)), neither call
       raises (record_nonce() already catches IntegrityError as the
       documented concurrent-race outcome — this proves it under a real
       second connection, not just a mocked exception).
PGC03) "Confirm vs expire" race: one thread calls confirm_order(), the
       other calls expire_order(), concurrently, on the same still-valid
       PENDING order. Outcome must be deterministic and mutually
       exclusive: EITHER the order ends up 'confirmed' with exactly one
       ledger pair and plan activated, OR it ends up 'expired' with zero
       ledger entries and plan never touched — never both, never a
       corrupted in-between state, and no exception escapes beyond the
       documented ones (InvalidStatusTransition/OrderExpired).
"""
from __future__ import annotations

import threading
import uuid as uuid_module
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.models  # noqa: F401 — registers all models on Base.metadata
from src.database import Base
from src.models.ledger import LedgerEntry
from src.models.user import User
from src.services import webhook_replay_service
from src.services.payment_order_service import (
    InvalidStatusTransition,
    OrderExpired,
    PaymentOrderService,
)
from tests.postgres_helpers import get_test_postgres_url, postgres_available


@pytest.fixture
def pg_session_factory():
    url = get_test_postgres_url()
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    yield Session
    engine.dispose()


def _make_user(Session, *, plan: str = "free") -> str:
    db = Session()
    try:
        user = User(
            id=str(uuid_module.uuid4()),
            email=f"pgbilling_{uuid_module.uuid4().hex[:8]}@test.com",
            hashed_password="x",
            plan=plan,
        )
        db.add(user)
        db.commit()
        return user.id
    finally:
        db.close()


def _make_plan_purchase_order(Session, *, user_id: str, plan: str = "pro", expires_in_seconds: float = 60.0) -> str:
    db = Session()
    try:
        svc = PaymentOrderService(db)
        order = svc.create_order(
            user_id=user_id, provider="nowpayments",
            amount_fiat=Decimal("29.99"), currency_fiat="USD",
            credits_amount=Decimal("29.99"), currency_credits="USD",
            idempotency_key=str(uuid_module.uuid4()),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds),
            plan=plan,
        )
        return order.id
    finally:
        db.close()


@pytest.mark.skipif(not postgres_available(), reason="TEST_POSTGRES_URL not set or PostgreSQL unreachable — see tests/postgres_helpers.py")
def test_PGC01_concurrent_confirm_order_activates_plan_exactly_once(pg_session_factory):
    Session = pg_session_factory
    user_id = _make_user(Session)
    order_id = _make_plan_purchase_order(Session, user_id=user_id, plan="pro")

    results: dict[str, tuple] = {}
    barrier = threading.Barrier(2)

    def attempt(label: str) -> None:
        session = Session()
        try:
            svc = PaymentOrderService(session)
            barrier.wait(timeout=10)
            order = svc.confirm_order(order_id, user_id)
            results[label] = ("confirmed", order.status)
        except Exception as exc:  # pragma: no cover — only hit on an unexpected failure
            results[label] = ("error", exc)
        finally:
            session.close()

    t1 = threading.Thread(target=attempt, args=("t1",))
    t2 = threading.Thread(target=attempt, args=("t2",))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    # Both callers must observe a genuinely confirmed order — the loser
    # of the race recovers via confirm_order()'s own IntegrityError
    # handling and returns the winner's result, never an error.
    assert results["t1"] == ("confirmed", "confirmed"), results
    assert results["t2"] == ("confirmed", "confirmed"), results

    verify = Session()
    try:
        entries = verify.query(LedgerEntry).filter(LedgerEntry.reference_id == order_id).all()
        assert len(entries) == 2, f"expected exactly one debit+credit pair, got {len(entries)}"
        user = verify.query(User).filter(User.id == user_id).first()
        assert user.plan == "pro"
    finally:
        verify.close()


@pytest.mark.skipif(not postgres_available(), reason="TEST_POSTGRES_URL not set or PostgreSQL unreachable — see tests/postgres_helpers.py")
def test_PGC02_concurrent_record_nonce_same_delivery_only_one_row(pg_session_factory):
    Session = pg_session_factory
    provider = "nowpayments"
    nonce = f"race-nonce-{uuid_module.uuid4()}"

    results: dict[str, str] = {}
    barrier = threading.Barrier(2)

    def attempt(label: str) -> None:
        session = Session()
        try:
            barrier.wait(timeout=10)
            webhook_replay_service.record_nonce(session, provider, nonce)
            results[label] = "ok"
        except Exception as exc:  # pragma: no cover
            results[label] = f"error: {exc}"
        finally:
            session.close()

    t1 = threading.Thread(target=attempt, args=("t1",))
    t2 = threading.Thread(target=attempt, args=("t2",))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert results["t1"] == "ok", results
    assert results["t2"] == "ok", results

    verify = Session()
    try:
        from src.models.webhook_nonce import WebhookNonce
        rows = verify.query(WebhookNonce).filter(
            WebhookNonce.provider == provider, WebhookNonce.nonce == nonce,
        ).all()
        assert len(rows) == 1, f"expected exactly one nonce row, got {len(rows)}"
    finally:
        verify.close()


@pytest.mark.skipif(not postgres_available(), reason="TEST_POSTGRES_URL not set or PostgreSQL unreachable — see tests/postgres_helpers.py")
def test_PGC03_confirm_vs_expire_race_is_mutually_exclusive(pg_session_factory):
    Session = pg_session_factory
    user_id = _make_user(Session)
    # Still-valid window at fire time — this exercises the raw state
    # machine under real concurrent writers, not the (separate,
    # already-safe-by-construction) case where confirm_order()'s own
    # internal expiry check would deterministically win regardless of
    # any race (see this file's module docstring).
    order_id = _make_plan_purchase_order(Session, user_id=user_id, plan="max", expires_in_seconds=30.0)

    results: dict[str, tuple] = {}
    barrier = threading.Barrier(2)

    def confirm() -> None:
        session = Session()
        try:
            svc = PaymentOrderService(session)
            barrier.wait(timeout=10)
            order = svc.confirm_order(order_id, user_id)
            results["confirm"] = ("ok", order.status)
        except OrderExpired:
            results["confirm"] = ("expired", None)
        except InvalidStatusTransition as exc:
            results["confirm"] = ("invalid_transition", str(exc))
        except Exception as exc:  # pragma: no cover
            results["confirm"] = ("error", exc)
        finally:
            session.close()

    def expire() -> None:
        session = Session()
        try:
            svc = PaymentOrderService(session)
            barrier.wait(timeout=10)
            order = svc.expire_order(order_id)
            results["expire"] = ("ok", order.status)
        except InvalidStatusTransition as exc:
            results["expire"] = ("invalid_transition", str(exc))
        except Exception as exc:  # pragma: no cover
            results["expire"] = ("error", exc)
        finally:
            session.close()

    t1 = threading.Thread(target=confirm)
    t2 = threading.Thread(target=expire)
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    # Neither side may surface a raw, undocumented error.
    assert results["confirm"][0] != "error", results
    assert results["expire"][0] != "error", results

    verify = Session()
    try:
        from src.models.payment_order import PaymentOrder
        order = verify.query(PaymentOrder).filter(PaymentOrder.id == order_id).first()
        entries = verify.query(LedgerEntry).filter(LedgerEntry.reference_id == order_id).all()
        user = verify.query(User).filter(User.id == user_id).first()

        if order.status == "confirmed":
            assert len(entries) == 2, f"confirmed order must have exactly one ledger pair, got {len(entries)}"
            assert user.plan == "max"
        elif order.status == "expired":
            assert len(entries) == 0, "an expired order must never have written a ledger entry"
            assert user.plan != "max"
        else:  # pragma: no cover — the state machine allows no other terminal outcome here
            pytest.fail(f"unexpected terminal order status: {order.status!r}")
    finally:
        verify.close()
