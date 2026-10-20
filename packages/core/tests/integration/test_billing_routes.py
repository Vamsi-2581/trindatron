"""
Integration tests for the billing API router — Passo 7 / Billing-1.

All tests use the conftest.py fixtures:
  db            — SQLite in-memory session (per function)
  client        — TestClient(app) with get_db overridden
  registered_user / auth_headers — authenticated user via /auth/register

Test matrix (≥ 15 scenarios as per Passo 7 requirements)
---------------------------------------------------------
AUTH GATE
  B01) Missing auth header → 401 on every billing endpoint
  B02) Invalid token → 401

ORDERS — CREATE (POST /billing/orders)
  B03) Valid order → 201, response matches request
  B04) Invalid body (amount = 0, naive datetime, etc.) → 422
  B05) Duplicate idempotency_key → 201, same order_id returned (idempotent)

ORDERS — GET (GET /billing/orders/{order_id})
  B06) Owner retrieves own order → 200
  B07) Wrong user retrieves order → 404 (no ownership leak)
  B08) Non-existent order_id → 404

ORDERS — LIST (GET /billing/orders)
  B09) Empty list before any orders → 200 {orders: [], total: 0}
  B10) List filtered by status=pending → only pending returned

ORDERS — CONFIRM (POST /billing/orders/{id}/confirm)
  B11) Valid confirm → 200, status=confirmed, balance credited atomically
  B12) Confirm already-confirmed order → 200, idempotent, no double-credit
  B13) Confirm an expired order → 409 Conflict

ORDERS — CANCEL (POST /billing/orders/{id}/cancel)
  B14) Valid cancel of pending order → 200, status=cancelled
  B15) Cancel already-cancelled (terminal) order → 409 Conflict
  B16) Cancel another user's order → 404

ORDERS — REFUND (POST /billing/orders/{id}/refund)
  B17) Full refund of confirmed order → 200, status=refunded, balance debited
  B18) Partial refund → 200, status=partial_refund
  B19) Refund amount > credits_amount → 422 Unprocessable
  B20) Refund before confirm (wrong status) → 409 Conflict
  B21) Refund wrong-user order → 404

BALANCE (GET /billing/balance, /billing/balance/{currency})
  B22) Balance before any credits → 0.00
  B23) Balance after confirm → reflects credited amount
  B24) Summary lists all currencies; single-currency endpoint works
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from decimal import Decimal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _future(hours: int = 1) -> str:
    """ISO-8601 UTC string for a datetime `hours` from now."""
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _order_body(**overrides) -> dict:
    """Return a valid CreateOrderRequest payload as a plain dict."""
    base = {
        "provider": "test_provider",
        "amount_fiat": "10.00",
        "currency_fiat": "USD",
        "credits_amount": "10.00",
        "currency_credits": "USD",
        "idempotency_key": "idem-test-001",
        "expires_at": _future(hours=1),
    }
    if ("credits_amount" in overrides and "amount_fiat" not in overrides
            and overrides.get("currency_credits", "USD") == "USD"):
        # The server caps USD credits at the amount paid, so a test that buys
        # N credits pays N (TCU credits keep the default price: 10 USD).
        base["amount_fiat"] = overrides["credits_amount"]
    base.update(overrides)
    return base


def _create_order(client, headers: dict, **overrides) -> dict:
    """Create an order and return its parsed JSON body."""
    body = _order_body(**overrides)
    resp = client.post("/billing/orders", json=body, headers=headers)
    assert resp.status_code == 201, f"create_order failed: {resp.status_code} {resp.text}"
    return resp.json()


def _confirm_order(client, order_id: str, headers: dict) -> dict:
    resp = client.post(f"/billing/orders/{order_id}/confirm", headers=headers)
    assert resp.status_code == 200, f"confirm_order failed: {resp.status_code} {resp.text}"
    return resp.json()


# ---------------------------------------------------------------------------
# B01 — Auth gate: missing Authorization header → 401 on all billing endpoints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path", [
    ("GET",  "/billing/orders"),
    ("POST", "/billing/orders"),
    ("GET",  "/billing/orders/some-id"),
    ("POST", "/billing/orders/some-id/confirm"),
    ("POST", "/billing/orders/some-id/cancel"),
    ("POST", "/billing/orders/some-id/refund"),
    ("GET",  "/billing/balance"),
    ("GET",  "/billing/balance/USD"),
])
def test_B01_missing_auth_returns_401(client, method, path):
    if method == "GET":
        resp = client.get(path)
    elif method == "POST":
        resp = client.post(path, json={})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# B02 — Invalid token → 401
# ---------------------------------------------------------------------------


def test_B02_invalid_token_returns_401(client):
    bad_headers = {"Authorization": "Bearer totally.invalid.token"}
    resp = client.get("/billing/orders", headers=bad_headers)
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# B03 — Valid order create → 201, response fields match request
# ---------------------------------------------------------------------------


def test_B03_create_order_valid_returns_201(client, auth_headers, registered_user):
    body = _order_body(idempotency_key="B03-key")
    resp = client.post("/billing/orders", json=body, headers=auth_headers)

    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "pending"
    assert data["provider"] == "test_provider"
    assert data["currency_fiat"] == "USD"
    assert data["currency_credits"] == "USD"
    assert data["user_id"] == registered_user["user_id"]
    assert "id" in data
    # webhook_raw must NOT appear in the response
    assert "webhook_raw" not in data


# ---------------------------------------------------------------------------
# B04 — Invalid body → 422
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_body", [
    # credits_amount = 0
    {"provider": "p", "amount_fiat": "10", "currency_fiat": "USD",
     "credits_amount": "0", "currency_credits": "USD",
     "idempotency_key": "k", "expires_at": _future()},
    # expires_at in the past
    {"provider": "p", "amount_fiat": "10", "currency_fiat": "USD",
     "credits_amount": "10", "currency_credits": "USD",
     "idempotency_key": "k",
     "expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()},
    # missing required field
    {"provider": "p", "amount_fiat": "10", "currency_fiat": "USD"},
])
def test_B04_invalid_body_returns_422(client, auth_headers, bad_body):
    resp = client.post("/billing/orders", json=bad_body, headers=auth_headers)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# B05 — Duplicate idempotency_key → 201, same order_id returned
# ---------------------------------------------------------------------------


def test_B05_duplicate_idempotency_key_returns_same_order(client, auth_headers):
    body = _order_body(idempotency_key="B05-dedup-key")

    resp1 = client.post("/billing/orders", json=body, headers=auth_headers)
    resp2 = client.post("/billing/orders", json=body, headers=auth_headers)

    assert resp1.status_code == 201
    assert resp2.status_code == 201
    assert resp1.json()["id"] == resp2.json()["id"]


# ---------------------------------------------------------------------------
# B06 — Owner retrieves own order → 200
# ---------------------------------------------------------------------------


def test_B06_owner_can_get_own_order(client, auth_headers):
    order = _create_order(client, auth_headers, idempotency_key="B06-key")
    order_id = order["id"]

    resp = client.get(f"/billing/orders/{order_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == order_id


# ---------------------------------------------------------------------------
# B07 — Wrong user → 404 (ownership leak prevention)
# ---------------------------------------------------------------------------


def test_B07_wrong_user_gets_404_not_403(client, auth_headers):
    # Create an order for the registered user
    order = _create_order(client, auth_headers, idempotency_key="B07-key")
    order_id = order["id"]

    # Register a second user
    resp = client.post("/auth/register", json={
        "email": "other@test.com",
        "password": "securepass456",
        "full_name": "Other User",
    })
    assert resp.status_code == 201
    other_token = resp.json()["access_token"]
    other_headers = {"Authorization": f"Bearer {other_token}"}

    # Second user tries to access first user's order
    resp = client.get(f"/billing/orders/{order_id}", headers=other_headers)
    # Must be 404 — not 403 — to avoid revealing order existence
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# B08 — Non-existent order_id → 404
# ---------------------------------------------------------------------------


def test_B08_nonexistent_order_returns_404(client, auth_headers):
    resp = client.get("/billing/orders/nonexistent-order-id", headers=auth_headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# B09 — List: empty before any orders
# ---------------------------------------------------------------------------


def test_B09_list_orders_empty(client, auth_headers):
    resp = client.get("/billing/orders", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["orders"] == []
    assert data["total"] == 0


# ---------------------------------------------------------------------------
# B10 — List filtered by status=pending
# ---------------------------------------------------------------------------


def test_B10_list_orders_filtered_by_status(client, auth_headers):
    # Create two orders, confirm one
    order1 = _create_order(client, auth_headers, idempotency_key="B10-key1")
    order2 = _create_order(client, auth_headers, idempotency_key="B10-key2")
    _confirm_order(client, order1["id"], auth_headers)

    # Only order2 should remain pending
    resp = client.get("/billing/orders?status=pending", headers=auth_headers)
    assert resp.status_code == 200
    ids = [o["id"] for o in resp.json()["orders"]]
    assert order2["id"] in ids
    assert order1["id"] not in ids


# ---------------------------------------------------------------------------
# B11 — Confirm valid → 200, status=confirmed, balance credited
# ---------------------------------------------------------------------------


def test_B11_confirm_order_credits_balance(client, auth_headers, registered_user):
    order = _create_order(client, auth_headers,
                          idempotency_key="B11-key",
                          credits_amount="25.00",
                          currency_credits="USD")
    order_id = order["id"]

    resp = client.post(f"/billing/orders/{order_id}/confirm", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "confirmed"
    assert data["confirmed_at"] is not None

    # Balance must reflect the credited amount
    bal_resp = client.get("/billing/balance/USD", headers=auth_headers)
    assert bal_resp.status_code == 200
    bal = Decimal(str(bal_resp.json()["balance"]))
    assert bal == Decimal("25.00")


# ---------------------------------------------------------------------------
# B12 — Confirm already-confirmed → 200, idempotent, no double-credit
# ---------------------------------------------------------------------------


def test_B12_confirm_idempotent_no_double_credit(client, auth_headers):
    order = _create_order(client, auth_headers,
                          idempotency_key="B12-key",
                          credits_amount="10.00",
                          currency_credits="USD")
    order_id = order["id"]

    # Confirm twice
    resp1 = client.post(f"/billing/orders/{order_id}/confirm", headers=auth_headers)
    resp2 = client.post(f"/billing/orders/{order_id}/confirm", headers=auth_headers)

    assert resp1.status_code == 200
    assert resp2.status_code == 200

    # Balance must be exactly 10.00, not 20.00
    bal_resp = client.get("/billing/balance/USD", headers=auth_headers)
    bal = Decimal(str(bal_resp.json()["balance"]))
    assert bal == Decimal("10.00")


# ---------------------------------------------------------------------------
# B13 — Confirm expired order → 409
# ---------------------------------------------------------------------------


def test_B13_confirm_expired_order_returns_409(client, auth_headers):
    # Create an order that already expired (1 second in the future is still valid at
    # creation time; we need a past expires_at).
    # We can't create with past expires_at (schema rejects), so we create and then
    # directly manipulate the DB via the service, or we need an already-expired order.
    # Instead we rely on the service exposing the OrderExpired path by passing
    # an expires_at only just in the future, then confirming after sleeping.
    # For test isolation, we directly create via service, bypassing schema validation.

    # Short-circuit: use the service to create an order with past expires_at.
    from src.services.payment_order_service import PaymentOrderService

    # Reach the DB override by getting it from the dependency override key
    from src.database import get_db
    db_gen = client.app.dependency_overrides[get_db]()
    db = next(db_gen)

    svc = PaymentOrderService(db)
    from src.models.payment_order import PaymentOrder

    order = PaymentOrder(
        user_id=client.app.dependency_overrides  # need user_id
    )
    # More elegant: register user, get user_id, then create via service
    # Let's do this properly:

    # Get user_id from a GET /users/me style approach (we have registered_user in auth_headers fixture)
    # But we don't have registered_user here. Let's just register and use the token.
    # Actually registered_user fixture IS on the client/conftest... we just need it.
    # Since this test doesn't use the registered_user fixture, let's re-register and pull user_id.

    # Register in the client
    reg_resp = client.post("/auth/register", json={
        "email": "expired_test@test.com",
        "password": "pass12345678",
        "full_name": "Expired Test",
    })

    # This fixture doesn't have registered_user injected, so auth_headers uses
    # the default registered_user. We need to use auth_headers user_id.
    # Let's not fight it — just skip this implementation and use a better approach.
    # We'll reach the service indirectly:

    pytest.skip("B13 requires order with past expires_at — tested via service layer")
    # Covered by unit tests P5/P6 in test_payment_order_service.py


# ---------------------------------------------------------------------------
# B13b — Confirm expired order via direct service path, integrated at HTTP level
# ---------------------------------------------------------------------------


def test_B13b_confirm_expired_order_returns_409(client, auth_headers, registered_user):
    """Test the expired-order 409 path via the HTTP API.

    We create an expired PaymentOrder directly via the service (bypassing schema
    validation that rejects past expires_at) and then confirm via HTTP.
    """
    from src.database import get_db
    from src.services.payment_order_service import PaymentOrderService
    from src.models.payment_order import PaymentOrder
    from src.billing.providers.base import PaymentStatus

    # Grab the DB session from the overridden dependency
    db_gen = client.app.dependency_overrides[get_db]()
    db = next(db_gen)

    # Insert an already-expired order directly
    past_expires = datetime.now(timezone.utc) - timedelta(hours=2)
    expired_order = PaymentOrder(
        user_id=registered_user["user_id"],
        provider="test_provider",
        amount_fiat=Decimal("10.00"),
        currency_fiat="USD",
        credits_amount=Decimal("10.00"),
        currency_credits="USD",
        idempotency_key="B13b-expired-key",
        expires_at=past_expires,
    )
    db.add(expired_order)
    db.commit()
    db.refresh(expired_order)
    order_id = expired_order.id

    # Now confirm via HTTP — should get 409
    resp = client.post(f"/billing/orders/{order_id}/confirm", headers=auth_headers)
    assert resp.status_code == 409
    assert "expired" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# B14 — Cancel pending order → 200, status=cancelled
# ---------------------------------------------------------------------------


def test_B14_cancel_pending_order(client, auth_headers):
    order = _create_order(client, auth_headers, idempotency_key="B14-key")
    order_id = order["id"]

    resp = client.post(f"/billing/orders/{order_id}/cancel", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# B15 — Cancel already-cancelled (terminal) order → 409 Conflict
# ---------------------------------------------------------------------------


def test_B15_cancel_terminal_order_returns_409(client, auth_headers):
    order = _create_order(client, auth_headers, idempotency_key="B15-key")
    order_id = order["id"]

    # First cancel succeeds
    client.post(f"/billing/orders/{order_id}/cancel", headers=auth_headers)

    # Second cancel on a terminal order → 409
    resp = client.post(f"/billing/orders/{order_id}/cancel", headers=auth_headers)
    assert resp.status_code == 409
    assert "transition" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# B16 — Cancel another user's order → 404
# ---------------------------------------------------------------------------


def test_B16_cancel_wrong_user_order_returns_404(client, auth_headers):
    order = _create_order(client, auth_headers, idempotency_key="B16-key")
    order_id = order["id"]

    # Second user
    resp = client.post("/auth/register", json={
        "email": "b16other@test.com",
        "password": "securepass789",
        "full_name": "B16 Other",
    })
    assert resp.status_code == 201
    other_headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    resp = client.post(f"/billing/orders/{order_id}/cancel", headers=other_headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# B17 — Full refund of confirmed order → 200, status=refunded, balance=0
# ---------------------------------------------------------------------------


def test_B17_full_refund_of_confirmed_order(client, auth_headers):
    order = _create_order(client, auth_headers,
                          idempotency_key="B17-key",
                          credits_amount="20.00",
                          currency_credits="USD")
    order_id = order["id"]
    _confirm_order(client, order_id, auth_headers)

    # Full refund
    resp = client.post(f"/billing/orders/{order_id}/refund",
                       json={"amount": "20.00"},
                       headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "refunded"

    # Balance must be 0 after full refund
    bal = Decimal(str(client.get("/billing/balance/USD", headers=auth_headers).json()["balance"]))
    assert bal == Decimal("0")


# ---------------------------------------------------------------------------
# B18 — Partial refund → 200, status=partial_refund
# ---------------------------------------------------------------------------


def test_B18_partial_refund_returns_partial_refund_status(client, auth_headers):
    order = _create_order(client, auth_headers,
                          idempotency_key="B18-key",
                          credits_amount="30.00",
                          currency_credits="USD")
    order_id = order["id"]
    _confirm_order(client, order_id, auth_headers)

    # Partial refund (10 out of 30)
    resp = client.post(f"/billing/orders/{order_id}/refund",
                       json={"amount": "10.00"},
                       headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "partial_refund"

    # Balance should be 20 remaining
    bal = Decimal(str(client.get("/billing/balance/USD", headers=auth_headers).json()["balance"]))
    assert bal == Decimal("20.00")


# ---------------------------------------------------------------------------
# B19 — Refund amount > credits_amount → 422
# ---------------------------------------------------------------------------


def test_B19_refund_exceeds_credits_returns_422(client, auth_headers):
    order = _create_order(client, auth_headers,
                          idempotency_key="B19-key",
                          credits_amount="10.00",
                          currency_credits="USD")
    order_id = order["id"]
    _confirm_order(client, order_id, auth_headers)

    # Attempt to refund more than the order's credits_amount
    resp = client.post(f"/billing/orders/{order_id}/refund",
                       json={"amount": "50.00"},
                       headers=auth_headers)
    assert resp.status_code == 422
    assert "eligible" in resp.json()["detail"].lower() or "ceiling" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# B20 — Refund before confirm (wrong status pending → refunded) → 409
# ---------------------------------------------------------------------------


def test_B20_refund_pending_order_returns_409(client, auth_headers):
    order = _create_order(client, auth_headers, idempotency_key="B20-key")
    order_id = order["id"]

    # Try to refund a pending order (wrong status)
    resp = client.post(f"/billing/orders/{order_id}/refund",
                       json={"amount": "10.00"},
                       headers=auth_headers)
    assert resp.status_code == 409
    assert "transition" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# B21 — Refund wrong-user order → 404
# ---------------------------------------------------------------------------


def test_B21_refund_wrong_user_order_returns_404(client, auth_headers):
    order = _create_order(client, auth_headers,
                          idempotency_key="B21-key",
                          credits_amount="10.00",
                          currency_credits="USD")
    order_id = order["id"]
    _confirm_order(client, order_id, auth_headers)

    # Second user
    resp = client.post("/auth/register", json={
        "email": "b21other@test.com",
        "password": "securepass000",
        "full_name": "B21 Other",
    })
    assert resp.status_code == 201
    other_headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    resp = client.post(f"/billing/orders/{order_id}/refund",
                       json={"amount": "10.00"},
                       headers=other_headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# B22 — Balance before any credits → 0.00
# ---------------------------------------------------------------------------


def test_B22_balance_zero_before_any_credits(client, auth_headers):
    resp = client.get("/billing/balance/USD", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert Decimal(str(data["balance"])) == Decimal("0")
    assert data["currency"] == "USD"


# ---------------------------------------------------------------------------
# B23 — Balance after confirm reflects credited amount
# ---------------------------------------------------------------------------


def test_B23_balance_after_confirm_reflects_credits(client, auth_headers):
    _create_order(client, auth_headers,
                  idempotency_key="B23-key-a",
                  credits_amount="15.00",
                  currency_credits="USD")
    order_b = _create_order(client, auth_headers,
                             idempotency_key="B23-key-b",
                             credits_amount="5.00",
                             currency_credits="USD")
    # Only confirm order_b
    _confirm_order(client, order_b["id"], auth_headers)

    resp = client.get("/billing/balance/USD", headers=auth_headers)
    assert resp.status_code == 200
    bal = Decimal(str(resp.json()["balance"]))
    assert bal == Decimal("5.00")


# ---------------------------------------------------------------------------
# B24 — Summary lists all currencies; single-currency endpoint correct
# ---------------------------------------------------------------------------


def test_B24_balance_summary_lists_all_currencies(client, auth_headers):
    # Credit USD via one order
    usd_order = _create_order(client, auth_headers,
                              idempotency_key="B24-usd-key",
                              credits_amount="10.00",
                              currency_credits="USD")
    _confirm_order(client, usd_order["id"], auth_headers)

    # Credit TCU via another order
    tcu_order = _create_order(client, auth_headers,
                              idempotency_key="B24-tcu-key",
                              credits_amount="500.00",
                              currency_credits="TCU")
    _confirm_order(client, tcu_order["id"], auth_headers)

    # Summary must include both currencies
    resp = client.get("/billing/balance", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    currencies = {b["currency"] for b in data["balances"]}
    assert "USD" in currencies
    assert "TCU" in currencies

    # Single-currency check
    tcu_resp = client.get("/billing/balance/TCU", headers=auth_headers)
    assert tcu_resp.status_code == 200
    assert Decimal(str(tcu_resp.json()["balance"])) == Decimal("500.00")
