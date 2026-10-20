"""
Integration tests for multiple partial refunds (Billing-2) and migration schema.

Test matrix
-----------
MULTIPLE PARTIAL REFUNDS (R)
  R01) Two sequential partial refunds — balance decremented correctly each time
  R02) Partials summing exactly to credits_amount → final status = 'refunded'
  R03) Third refund exceeding cumulative ceiling → 422 Unprocessable
  R04) Same idempotency_key → second call returns existing order, no double debit
  R05) Idempotent call confirmed — balance unchanged after duplicate key
  R06) No idempotency_key → two independent calls create two refund records
  R07) Refund from partial_refund state succeeds (non-terminal in Billing-2)
  R08) Full refund from partial_refund state → status = 'refunded'
  R09) PaymentOrderRefund rows accumulate per refund call
  R10) Failed refund (amount > available) creates no PaymentOrderRefund row

MIGRATION / SCHEMA (M)
  M01) payment_order_refunds table registered in Base.metadata
  M02) UNIQUE(idempotency_key) constraint enforced at DB level
  M03) CHECK(amount > 0) constraint enforced at DB level
  M04) Migration file has correct revision and down_revision identifiers
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from src.models.payment_order_refund import PaymentOrderRefund


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _future(hours: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _create_order(client, auth_headers, *, credits_amount: str = "30.00") -> dict:
    resp = client.post(
        "/billing/orders",
        json={
            "provider": "test_provider",
            "amount_fiat": credits_amount,
            "currency_fiat": "USD",
            "credits_amount": credits_amount,
            "currency_credits": "USD",
            "idempotency_key": str(uuid.uuid4()),
            "expires_at": _future(),
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.json()
    return resp.json()


def _confirm(client, auth_headers, order_id: str) -> dict:
    resp = client.post(f"/billing/orders/{order_id}/confirm", headers=auth_headers)
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _refund(
    client,
    auth_headers,
    order_id: str,
    amount: str,
    *,
    idempotency_key: str | None = None,
):
    body: dict = {"amount": amount}
    if idempotency_key is not None:
        body["idempotency_key"] = idempotency_key
    return client.post(f"/billing/orders/{order_id}/refund", json=body, headers=auth_headers)


def _balance(client, auth_headers, currency: str = "USD") -> Decimal:
    resp = client.get(f"/billing/balance/{currency}", headers=auth_headers)
    assert resp.status_code == 200
    return Decimal(resp.json()["balance"])


# ---------------------------------------------------------------------------
# Refund tests (R01–R10)
# ---------------------------------------------------------------------------


def test_R01_two_sequential_partial_refunds(client, auth_headers, db):
    order = _create_order(client, auth_headers, credits_amount="30.00")
    _confirm(client, auth_headers, order["id"])

    r1 = _refund(client, auth_headers, order["id"], "10.00")
    assert r1.status_code == 200
    assert r1.json()["status"] == "partial_refund"

    r2 = _refund(client, auth_headers, order["id"], "15.00")
    assert r2.status_code == 200
    assert r2.json()["status"] == "partial_refund"

    assert _balance(client, auth_headers) == Decimal("5.00")


def test_R02_partials_summing_to_full_yield_refunded_status(client, auth_headers, db):
    order = _create_order(client, auth_headers, credits_amount="30.00")
    _confirm(client, auth_headers, order["id"])

    _refund(client, auth_headers, order["id"], "10.00")
    r2 = _refund(client, auth_headers, order["id"], "20.00")

    assert r2.status_code == 200
    assert r2.json()["status"] == "refunded"
    assert _balance(client, auth_headers) == Decimal("0.00")


def test_R03_third_refund_exceeding_cumulative_ceiling_returns_422(client, auth_headers, db):
    order = _create_order(client, auth_headers, credits_amount="30.00")
    _confirm(client, auth_headers, order["id"])

    _refund(client, auth_headers, order["id"], "20.00")  # already_refunded = 20
    r3 = _refund(client, auth_headers, order["id"], "15.00")  # 20 + 15 = 35 > 30

    assert r3.status_code == 422
    assert "ceiling" in r3.json()["detail"].lower() or "eligible" in r3.json()["detail"].lower()


def test_R04_same_idempotency_key_returns_existing_order_no_double_debit(
    client, auth_headers, db
):
    order = _create_order(client, auth_headers, credits_amount="30.00")
    _confirm(client, auth_headers, order["id"])

    key = str(uuid.uuid4())
    r1 = _refund(client, auth_headers, order["id"], "10.00", idempotency_key=key)
    assert r1.status_code == 200
    bal_after_first = _balance(client, auth_headers)

    r2 = _refund(client, auth_headers, order["id"], "10.00", idempotency_key=key)
    assert r2.status_code == 200

    # Balance must not have changed — only one debit
    assert _balance(client, auth_headers) == bal_after_first


def test_R05_idempotent_call_balance_unchanged(client, auth_headers, db):
    order = _create_order(client, auth_headers, credits_amount="20.00")
    _confirm(client, auth_headers, order["id"])

    key = "stable-key-r05"
    _refund(client, auth_headers, order["id"], "5.00", idempotency_key=key)
    bal_first = _balance(client, auth_headers)

    _refund(client, auth_headers, order["id"], "5.00", idempotency_key=key)
    assert _balance(client, auth_headers) == bal_first


def test_R06_no_idempotency_key_allows_independent_refunds(client, auth_headers, db):
    order = _create_order(client, auth_headers, credits_amount="30.00")
    _confirm(client, auth_headers, order["id"])

    r1 = _refund(client, auth_headers, order["id"], "5.00")  # no idempotency_key
    r2 = _refund(client, auth_headers, order["id"], "5.00")  # different auto-generated key

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert _balance(client, auth_headers) == Decimal("20.00")


def test_R07_refund_from_partial_refund_state_succeeds(client, auth_headers, db):
    order = _create_order(client, auth_headers, credits_amount="30.00")
    _confirm(client, auth_headers, order["id"])

    _refund(client, auth_headers, order["id"], "10.00")  # → partial_refund

    r2 = _refund(client, auth_headers, order["id"], "5.00")  # from partial_refund
    assert r2.status_code == 200
    assert r2.json()["status"] == "partial_refund"


def test_R08_full_refund_from_partial_refund_state_yields_refunded(client, auth_headers, db):
    order = _create_order(client, auth_headers, credits_amount="20.00")
    _confirm(client, auth_headers, order["id"])

    _refund(client, auth_headers, order["id"], "8.00")  # → partial_refund

    r2 = _refund(client, auth_headers, order["id"], "12.00")  # 8+12=20 → refunded
    assert r2.status_code == 200
    assert r2.json()["status"] == "refunded"
    assert _balance(client, auth_headers) == Decimal("0.00")


def test_R09_payment_order_refund_rows_accumulate_per_call(client, auth_headers, db):
    order = _create_order(client, auth_headers, credits_amount="30.00")
    _confirm(client, auth_headers, order["id"])

    assert db.query(PaymentOrderRefund).count() == 0

    _refund(client, auth_headers, order["id"], "5.00")
    assert db.query(PaymentOrderRefund).filter(
        PaymentOrderRefund.order_id == order["id"]
    ).count() == 1

    _refund(client, auth_headers, order["id"], "10.00")
    assert db.query(PaymentOrderRefund).filter(
        PaymentOrderRefund.order_id == order["id"]
    ).count() == 2


def test_R10_failed_refund_creates_no_refund_row(client, auth_headers, db):
    order = _create_order(client, auth_headers, credits_amount="10.00")
    _confirm(client, auth_headers, order["id"])

    r = _refund(client, auth_headers, order["id"], "99.99")  # amount > credits_amount
    assert r.status_code == 422

    assert db.query(PaymentOrderRefund).filter(
        PaymentOrderRefund.order_id == order["id"]
    ).count() == 0


# ---------------------------------------------------------------------------
# Migration / schema tests (M01–M04)
# ---------------------------------------------------------------------------


def test_M01_payment_order_refunds_table_registered_in_metadata():
    """PaymentOrderRefund is in Base.metadata — table is created by test conftest."""
    from src.database import Base
    assert "payment_order_refunds" in Base.metadata.tables


def test_M02_idempotency_key_unique_constraint_enforced(client, auth_headers, db):
    """UNIQUE(idempotency_key) raises IntegrityError on duplicate insert."""
    order = _create_order(client, auth_headers, credits_amount="50.00")
    _confirm(client, auth_headers, order["id"])

    key = "unique-test-m02"
    _refund(client, auth_headers, order["id"], "5.00", idempotency_key=key)

    # Directly try to insert a duplicate idempotency_key → IntegrityError
    db.expire_all()
    from src.models.payment_order_refund import PaymentOrderRefund as POR
    dup = POR(
        order_id=order["id"],
        user_id=db.query(POR).filter(POR.idempotency_key == key).first().user_id,
        amount=Decimal("5.00"),
        currency="USD",
        idempotency_key=key,  # duplicate
    )
    db.add(dup)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_M03_check_amount_positive_constraint_enforced(client, auth_headers, db):
    """CHECK(amount > 0) raises IntegrityError on amount = 0."""
    order = _create_order(client, auth_headers, credits_amount="10.00")
    _confirm(client, auth_headers, order["id"])

    from src.models.payment_order_refund import PaymentOrderRefund as POR
    from src.models.user import User

    # Get a valid user_id from the order
    from src.models.payment_order import PaymentOrder
    db.expire_all()
    order_obj = db.query(PaymentOrder).filter(PaymentOrder.id == order["id"]).first()

    bad = POR(
        order_id=order["id"],
        user_id=order_obj.user_id,
        amount=Decimal("0"),  # violates CHECK
        currency="USD",
        idempotency_key=str(uuid.uuid4()),
    )
    db.add(bad)
    with pytest.raises((IntegrityError, Exception)):
        db.flush()
    db.rollback()


def test_M04_migration_file_has_correct_revision_identifiers():
    """0013_payment_order_refunds.py has correct revision and down_revision."""
    migration_path = os.path.join(
        os.path.dirname(__file__),
        "../../alembic/versions/0013_payment_order_refunds.py",
    )
    migration_path = os.path.normpath(migration_path)
    assert os.path.isfile(migration_path), f"Migration file not found: {migration_path}"

    with open(migration_path) as f:
        content = f.read()

    assert 'revision = "0013_payment_order_refunds"' in content
    assert 'down_revision = "0012_payment_orders"' in content
