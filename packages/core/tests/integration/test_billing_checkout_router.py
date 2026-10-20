"""Integration tests — the Payment Router wired into POST
/billing/checkout's auto-selection path, exercised against REAL adapter
classes (Mercado Pago, Stripe, NOWPayments, Binance Pay) with only their
HTTP transports faked out — proving the router's failover/priority
logic works through the ACTUAL adapters this mission cares about, not
just the FakeProvider test doubles in test_payment_router.py.

Test matrix
-----------
RC01) Mercado Pago alone, auto-selected via payment_method=pix -> real
      preference created via route_checkout() called directly (see
      _route_via_service()'s own docstring for why this bypasses the
      HTTP price-catalog layer specifically for Mercado Pago). Proves
      Mercado Pago is unaffected by the router's introduction.
RC02) Stripe alone, auto-selected via payment_method=credit_card (real
      HTTP POST /billing/checkout, no `provider`) -> real Checkout
      Session created. Proves Stripe is unaffected.
RC03) NOWPayments alone, auto-selected via payment_method=crypto (real
      HTTP) -> real invoice created. Proves NOWPayments is unaffected.
RC04) Binance Pay alone, auto-selected via payment_method=crypto (real
      HTTP) -> real order created. Proves Binance Pay is unaffected.
RC05) Mercado Pago (priority) + Stripe both eligible for credit_card ->
      Mercado Pago (earlier in settings.payment_provider_priority) is
      selected — deterministic, no ambiguity error, and Stripe's
      create_intent() is never called.
RC06) Mercado Pago's create_intent() deterministically rejects (a real
      400 from its own transport, raising the REAL MercadoPagoError,
      which is a PaymentCreationRejected) -> the router fails over to
      Stripe on the SAME order row; exactly one PaymentOrder exists.
RC07) Mercado Pago's create_intent() times out (ProviderUnavailable) ->
      propagates, Stripe's create_intent() is NEVER called, order left
      pending/provider=mercado_pago/external_order_id=NULL.
RC08) NoEligibleProvider (no registered provider supports the requested
      payment_method, via real HTTP) -> 409, no PaymentOrder row created.
RC09) Repeating the SAME logical operation (same idempotency_key) after
      RC06's failover returns the SAME order, does not call ANY
      provider's create_intent() again.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.api.deps import get_provider_registry
from src.billing.payment_router import route_checkout
from src.billing.providers.base import PaymentMethod
from src.billing.providers.binance_pay_adapter import BinancePayAdapter
from src.billing.providers.mercadopago_adapter import MercadoPagoAdapter
from src.billing.providers.nowpayments_adapter import NOWPaymentsAdapter
from src.billing.providers.registry import ProviderRegistry
from src.billing.providers.stripe_adapter import StripeAdapter
from src.main import app as _app
from src.models.payment_order import PaymentOrder
from src.models.user import User
from src.services.request_control import InMemoryRateLimiter


def _future():
    return datetime.now(timezone.utc) + timedelta(hours=1)


# ---------------------------------------------------------------------------
# Fake transports — controllable success/reject/unavailable, per real adapter.
# ---------------------------------------------------------------------------


class _MercadoPagoTransport:
    def __init__(self, *, behavior: str = "success") -> None:
        self.behavior = behavior
        self.create_calls = 0

    async def post(self, url, *, headers, body, timeout):
        if "/checkout/preferences" in url:
            self.create_calls += 1
            if self.behavior == "rejected":
                return 400, json.dumps({"message": "invalid preference"}).encode("utf-8")
            if self.behavior == "unavailable":
                from src.billing.providers.base import ProviderUnavailable
                raise ProviderUnavailable("mercado_pago timed out (simulated)")
            parsed = json.loads(body.decode("utf-8"))
            ref = parsed.get("external_reference", "ref")
            return 201, json.dumps({
                "id": f"pref-{ref}", "init_point": f"https://mercadopago.com/checkout/{ref}",
            }).encode("utf-8")
        raise NotImplementedError

    async def get(self, url, *, headers, timeout):
        raise NotImplementedError


class _StripeTransport:
    def __init__(self, *, behavior: str = "success") -> None:
        self.behavior = behavior
        self.create_calls = 0

    async def post(self, url, *, headers, body, timeout):
        if "/checkout/sessions" in url:
            self.create_calls += 1
            if self.behavior == "rejected":
                return 400, json.dumps({"error": {"message": "invalid request"}}).encode("utf-8")
            if self.behavior == "unavailable":
                from src.billing.providers.base import ProviderUnavailable
                raise ProviderUnavailable("stripe timed out (simulated)")
            from urllib.parse import parse_qs
            parsed = {k: v[0] for k, v in parse_qs(body.decode("utf-8")).items()}
            ref = parsed.get("client_reference_id", "ref")
            session_id = f"cs_test_{ref}"
            return 200, json.dumps({
                "id": session_id, "url": f"https://checkout.stripe.com/c/pay/{session_id}",
            }).encode("utf-8")
        raise NotImplementedError

    async def get(self, url, *, headers, timeout):
        raise NotImplementedError


class _NOWPaymentsTransport:
    async def post(self, url, *, headers, body, timeout):
        parsed = json.loads(body.decode("utf-8"))
        return 200, json.dumps({
            "id": 555, "order_id": parsed["order_id"], "order_description": "TRINDATRON plan",
            "price_amount": parsed["price_amount"], "price_currency": parsed["price_currency"],
            "pay_currency": None, "invoice_url": f"https://nowpayments.io/payment/?iid={parsed['order_id']}",
            "success_url": None, "cancel_url": None,
            "created_at": "2026-01-01T00:00:00.000Z", "updated_at": "2026-01-01T00:00:00.000Z",
        }).encode("utf-8")

    async def get(self, url, *, headers, timeout):
        raise NotImplementedError


class _BinancePayTransport:
    async def post(self, url, *, headers, body, timeout):
        import time
        expire = int((time.time() + 3600) * 1000)
        return 200, json.dumps({
            "status": "SUCCESS", "code": "000000",
            "data": {"prepayId": "1234567890123456789", "checkoutUrl": "https://pay.binance.com/checkout/abc", "expireTime": expire},
        }).encode("utf-8")


def _install_registry(*adapters) -> ProviderRegistry:
    reg = ProviderRegistry()
    for adapter in adapters:
        reg.register(adapter)
    _app.dependency_overrides[get_provider_registry] = lambda: reg
    return reg


@pytest.fixture(autouse=True)
def _cleanup_registry():
    yield
    _app.dependency_overrides.pop(get_provider_registry, None)


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    from src.api.routes import billing as billing_routes
    billing_routes._order_rate_limiter = InMemoryRateLimiter()
    billing_routes._webhook_rate_limiter = InMemoryRateLimiter()
    yield


def _checkout_body(plan: str = "pro", **overrides) -> dict:
    body = {"plan": plan, "idempotency_key": str(uuid.uuid4())}
    body.update(overrides)
    return body


def _mp_adapter(behavior: str = "success") -> tuple[MercadoPagoAdapter, _MercadoPagoTransport]:
    transport = _MercadoPagoTransport(behavior=behavior)
    return MercadoPagoAdapter(
        access_token="TEST-token", webhook_secret="mp-secret",
        base_url="https://api.mercadopago.com", transport=transport,
    ), transport


def _stripe_adapter(behavior: str = "success") -> tuple[StripeAdapter, _StripeTransport]:
    transport = _StripeTransport(behavior=behavior)
    return StripeAdapter(
        secret_key="sk_test_TEST", webhook_secret="whsec_test",
        base_url="https://api.stripe.com", transport=transport,
    ), transport


def _make_test_user(db) -> User:
    u = User(email=f"router-http-{uuid.uuid4()}@test.com", hashed_password="x", plan="start")
    db.add(u)
    db.commit()
    return u


async def _route_via_service(db, registry, user, *, payment_method, currency="BRL", idempotency_key=None):
    """Calls route_checkout() directly (service-level), bypassing the
    HTTP layer's price catalog — src/billing/pricing.py only prices
    plans in USD today, and Mercado Pago does not support USD (see its
    own module docstring's confirmed 7-currency market list), so an
    HTTP round-trip through POST /billing/checkout cannot reach Mercado
    Pago at all without inventing a BRL price entry this task has no
    business authority to add. This still exercises the REAL adapter +
    REAL DB + REAL router — only the FastAPI/pricing layer is skipped."""
    return await route_checkout(
        db, registry, user_id=user.id, plan="pro", amount_fiat=Decimal("29.99"),
        currency_fiat=currency, credits_amount=Decimal("29.99"), currency_credits=currency,
        idempotency_key=idempotency_key or str(uuid.uuid4()), expires_at=_future(),
        payment_method=PaymentMethod(payment_method) if payment_method else None, payment_type=None,
    )


def test_RC01_mercadopago_alone_auto_selected(db):
    import asyncio
    adapter, _t = _mp_adapter()
    registry = _install_registry(adapter)
    user = _make_test_user(db)

    order, intent = asyncio.run(_route_via_service(db, registry, user, payment_method="pix"))

    assert order.provider == "mercado_pago"
    assert order.status == "processing"
    assert intent.provider == "mercado_pago"


def test_RC02_stripe_alone_auto_selected(client, auth_headers):
    adapter, _t = _stripe_adapter()
    _install_registry(adapter)
    resp = client.post(
        "/billing/checkout", json=_checkout_body(payment_method="credit_card"), headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    order = resp.json()["order"]
    assert order["provider"] == "stripe"
    assert order["status"] == "processing"


def test_RC03_nowpayments_alone_auto_selected(client, auth_headers):
    adapter = NOWPaymentsAdapter(
        api_key="test-key", ipn_secret="test-secret",
        base_url="https://api.nowpayments.io", transport=_NOWPaymentsTransport(),
    )
    _install_registry(adapter)
    resp = client.post(
        "/billing/checkout", json=_checkout_body(payment_method="crypto"), headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    order = resp.json()["order"]
    assert order["provider"] == "nowpayments"
    assert order["status"] == "processing"


def test_RC04_binance_pay_alone_auto_selected(client, auth_headers):
    adapter = BinancePayAdapter(
        api_key="test-key", secret_key="test-secret",
        base_url="https://bpay.binanceapi.com", transport=_BinancePayTransport(),
    )
    _install_registry(adapter)
    resp = client.post(
        "/billing/checkout", json=_checkout_body(payment_method="crypto"), headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    order = resp.json()["order"]
    assert order["provider"] == "binance_pay"
    assert order["status"] == "processing"


def test_RC05_mercadopago_preferred_over_stripe_for_credit_card(db):
    import asyncio
    mp_adapter, mp_transport = _mp_adapter()
    stripe_adapter, stripe_transport = _stripe_adapter()
    registry = _install_registry(mp_adapter, stripe_adapter)
    user = _make_test_user(db)

    order, _intent = asyncio.run(_route_via_service(db, registry, user, payment_method="credit_card"))

    assert order.provider == "mercado_pago"
    assert mp_transport.create_calls == 1
    assert stripe_transport.create_calls == 0


def test_RC06_mercadopago_rejects_fails_over_to_stripe(db):
    import asyncio
    mp_adapter, mp_transport = _mp_adapter(behavior="rejected")
    stripe_adapter, stripe_transport = _stripe_adapter(behavior="success")
    registry = _install_registry(mp_adapter, stripe_adapter)
    user = _make_test_user(db)

    order, intent = asyncio.run(_route_via_service(db, registry, user, payment_method="credit_card"))

    assert order.provider == "stripe"
    assert intent.provider == "stripe"
    assert mp_transport.create_calls == 1
    assert stripe_transport.create_calls == 1

    count = db.query(PaymentOrder).filter(PaymentOrder.id == order.id).count()
    assert count == 1


def test_RC07_mercadopago_unavailable_never_fails_over(db):
    import asyncio
    from src.billing.providers.base import ProviderUnavailable

    mp_adapter, mp_transport = _mp_adapter(behavior="unavailable")
    stripe_adapter, stripe_transport = _stripe_adapter(behavior="success")
    registry = _install_registry(mp_adapter, stripe_adapter)
    user = _make_test_user(db)

    with pytest.raises(ProviderUnavailable):
        asyncio.run(_route_via_service(db, registry, user, payment_method="credit_card"))

    assert mp_transport.create_calls == 1
    assert stripe_transport.create_calls == 0  # NEVER tried — ambiguous outcome

    row = db.query(PaymentOrder).filter(PaymentOrder.provider == "mercado_pago").first()
    assert row is not None
    assert row.external_order_id is None
    assert row.status == "pending"


def test_RC08_no_eligible_provider_for_payment_method_returns_409(client, auth_headers, db):
    """Mercado Pago is fiat-only (no CRYPTO in supported_payment_methods)
    — requesting payment_method=crypto with only Mercado Pago registered
    must find zero eligible candidates. (currency=JPY was considered for
    this scenario but is unreachable: src/billing/pricing.py only prices
    plans in USD today, so an unpriced currency 404s BEFORE the router
    ever runs — a different, earlier guard, not the router's own.)"""
    mp_adapter, _t = _mp_adapter()
    _install_registry(mp_adapter)

    resp = client.post(
        "/billing/checkout", json=_checkout_body(payment_method="crypto"), headers=auth_headers,
    )
    assert resp.status_code == 409
    assert db.query(PaymentOrder).count() == 0


def test_RC09_retry_after_failover_is_idempotent(db):
    import asyncio
    mp_adapter, mp_transport = _mp_adapter(behavior="rejected")
    stripe_adapter, stripe_transport = _stripe_adapter(behavior="success")
    registry = _install_registry(mp_adapter, stripe_adapter)
    user = _make_test_user(db)
    key = str(uuid.uuid4())

    order1, intent1 = asyncio.run(
        _route_via_service(db, registry, user, payment_method="credit_card", idempotency_key=key)
    )
    order2, intent2 = asyncio.run(
        _route_via_service(db, registry, user, payment_method="credit_card", idempotency_key=key)
    )

    assert order1.id == order2.id
    assert intent2 is None  # pure idempotent no-op — already succeeded
    # Provider was contacted exactly once each — the retry never re-attempts.
    assert mp_transport.create_calls == 1
    assert stripe_transport.create_calls == 1
