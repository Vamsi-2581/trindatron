"""End-to-end integration tests for the Binance Pay adapter wired into the
real HTTP billing routes — POST /billing/orders and POST /billing/webhook/binance_pay.

Uses the REAL BinancePayAdapter (real HMAC/RSA signing and verification)
with a FakeTransport (from test_binance_pay_adapter.py) standing in for the
network — no real Binance API call is ever made. This complements:
  - tests/unit/test_binance_pay_adapter.py — adapter logic in isolation
  - tests/integration/test_billing_webhook.py — generic webhook route with
    FakeAdapter (no ack-format override, proves the default path unchanged)
  - tests/integration/test_billing_confirm_guard.py — the confirm-guard
    with a minimal fake adapter

Test matrix
-----------
I01) POST /billing/orders with binance_pay registered — calls create_intent(),
     order lands in 'processing' with external_order_id from the fake response.
I02) POST /billing/orders with an unregistered provider — unaffected, still
     'pending', adapter never called (transport.calls stays empty).
I03) POST /billing/webhook/binance_pay — valid signed CONFIRMED webhook -> 200
     with Binance's exact ack body {"returnCode": "SUCCESS", "returnMessage": null},
     order confirmed, balance credited.
I04) POST /billing/webhook/binance_pay — invalid signature -> 400 with the
     standard (non-Binance-shaped) FastAPI error envelope — ack override only
     applies to 200 responses, not error paths.
I05) POST /billing/orders/{id}/confirm on a binance_pay order still 409s
     (confirm guard) even after the create_provider_order() integration.
"""
from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.api.deps import get_provider_registry
from src.billing.providers.binance_pay_adapter import BinancePayAdapter
from src.billing.providers.registry import ProviderRegistry
from src.main import app as _app

from tests.unit.test_binance_pay_adapter import (
    FakeTransport,
    _create_order_success,
    _rsa_keypair,
    _sign_webhook,
)


def _install_binance_pay(transport: FakeTransport, *, public_key_pem: str) -> ProviderRegistry:
    adapter = BinancePayAdapter(
        api_key="test-api-key",
        secret_key="test-secret-key",
        base_url="https://bpay.binanceapi.com",
        transport=transport,
        public_key_pem=public_key_pem,
    )
    reg = ProviderRegistry()
    reg.register(adapter)
    _app.dependency_overrides[get_provider_registry] = lambda: reg
    return reg


def _order_body(**overrides) -> dict:
    base = {
        "provider": "binance_pay",
        "amount_fiat": "25.00",
        "currency_fiat": "USD",
        "credits_amount": "25.00",
        "currency_credits": "USD",
        "idempotency_key": str(uuid.uuid4()),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    base.update(overrides)
    return base


def test_I01_create_order_with_binance_pay_calls_adapter_and_marks_processing(client, auth_headers):
    _private_key, public_pem = _rsa_keypair()
    transport = FakeTransport()
    transport.queue_json(200, _create_order_success(prepay_id="prepay-abc"))
    _install_binance_pay(transport, public_key_pem=public_pem)

    resp = client.post("/billing/orders", json=_order_body(), headers=auth_headers)

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "processing"
    assert data["external_order_id"]  # set from the adapter's PaymentIntent.id
    assert len(transport.calls) == 1
    assert transport.calls[0]["url"].endswith("/binancepay/openapi/v3/order")


def test_I02_create_order_unregistered_provider_unaffected(client, auth_headers):
    transport = FakeTransport()  # not registered under any name that matters here
    _install_binance_pay(transport, public_key_pem="irrelevant")  # binance_pay IS registered...

    # ...but this order uses a DIFFERENT provider string, never registered.
    resp = client.post(
        "/billing/orders", json=_order_body(provider="manual_test_provider"), headers=auth_headers,
    )

    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "pending"  # unchanged legacy path — no adapter call
    assert data["external_order_id"] is None
    assert len(transport.calls) == 0  # adapter never touched


def test_I03_webhook_confirmed_returns_binance_ack_and_credits_balance(client, auth_headers, db):
    private_key, public_pem = _rsa_keypair()
    transport = FakeTransport()
    transport.queue_json(200, _create_order_success(prepay_id="prepay-i03"))
    _install_binance_pay(transport, public_key_pem=public_pem)

    create_resp = client.post("/billing/orders", json=_order_body(credits_amount="25.00"), headers=auth_headers)
    assert create_resp.status_code == 201, create_resp.text
    order = create_resp.json()
    external_order_id = order["external_order_id"]

    timestamp = str(int(time.time() * 1000))
    nonce = "n" * 32
    inner = {
        "merchantTradeNo": external_order_id,
        "totalFee": "25.00000000",
        "currency": "USD",
        "transactTime": int(time.time() * 1000),
    }
    outer = {
        "bizType": "PAY", "bizId": 1, "bizIdStr": "1",
        "bizStatus": "PAY_SUCCESS",
        "data": json.dumps(inner, separators=(",", ":")),
    }
    body = json.dumps(outer, separators=(",", ":")).encode("utf-8")
    signature_b64 = _sign_webhook(private_key, timestamp=timestamp, nonce=nonce, body=body)
    headers = {
        "BinancePay-Timestamp": timestamp,
        "BinancePay-Nonce": nonce,
        "BinancePay-Certificate-SN": "cert-serial-123",
        "BinancePay-Signature": signature_b64,
        "Content-Type": "application/json",
    }

    resp = client.post("/billing/webhook/binance_pay", content=body, headers=headers)

    assert resp.status_code == 200
    assert resp.json() == {"returnCode": "SUCCESS", "returnMessage": None}

    bal = client.get("/billing/balance/USD", headers=auth_headers)
    assert Decimal(bal.json()["balance"]) == Decimal("25.00")

    order_after = client.get(f"/billing/orders/{order['id']}", headers=auth_headers)
    assert order_after.json()["status"] == "confirmed"


def test_I04_webhook_invalid_signature_keeps_standard_error_envelope(client):
    _private_key, public_pem = _rsa_keypair()
    transport = FakeTransport()
    _install_binance_pay(transport, public_key_pem=public_pem)

    body = b'{"bizType":"PAY","bizStatus":"PAY_SUCCESS","data":"{}"}'
    headers = {
        "BinancePay-Timestamp": str(int(time.time() * 1000)),
        "BinancePay-Nonce": "n" * 32,
        "BinancePay-Certificate-SN": "cert-serial-123",
        "BinancePay-Signature": base64.b64encode(b"not-a-real-signature").decode(),
    }

    resp = client.post("/billing/webhook/binance_pay", content=body, headers=headers)

    assert resp.status_code == 400
    assert "signature" in resp.json()["detail"].lower()  # standard {"detail": "..."} envelope, not Binance's shape


def test_I05_client_confirm_still_refused_for_binance_pay_order(client, auth_headers):
    _private_key, public_pem = _rsa_keypair()
    transport = FakeTransport()
    transport.queue_json(200, _create_order_success(prepay_id="prepay-i05"))
    _install_binance_pay(transport, public_key_pem=public_pem)

    create_resp = client.post("/billing/orders", json=_order_body(), headers=auth_headers)
    order_id = create_resp.json()["id"]

    resp = client.post(f"/billing/orders/{order_id}/confirm", headers=auth_headers)

    assert resp.status_code == 409
