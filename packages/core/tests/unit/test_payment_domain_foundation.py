"""Unit tests for the Payments Foundation etapa (2026-09-07) — the
canonical, provider-agnostic domain built BEFORE any Fiat adapter
(Mercado Pago/Stripe/Adyen) or PaymentRouter exists.

Scope: PaymentMethod/PaymentType enums, the currency-aware pricing
catalog, PaymentOrder's new (nullable, backward-compatible) columns,
the new Subscription model and its relationships, and
PaymentProvider.supported_payment_methods/supported_payment_types.

Explicitly NOT covered here (out of scope for this phase, see
test_billing_checkout*.py for HTTP-level coverage):
  - Any Fiat adapter
  - PaymentRouter / cost-based routing / automatic failover
  - Automatic recurring charging

Test matrix
-----------
E01) PaymentMethod / PaymentType enum values are exactly the specified set.
E02) A PaymentMethod value is never a provider name — payment_method and
     provider are independent strings on the same PaymentOrder.
PR01) get_plan_price(plan) defaults to CHECKOUT_CURRENCY (USD) — every
      pre-existing single-argument call site is unaffected.
PR02) get_plan_price(plan, "USD") returns the same value as PR01.
PR03) get_plan_price(plan, "EUR") returns None — no real-time FX, no
      invented EUR figure (BRL was this same "unpriced" example until the
      2026-09-24 go-live checkout-currency fix added real BRL prices).
PR04) get_plan_price("free", ...) is always None (never purchasable).
PR05) supported_currencies("start") == ["BRL", "USD"] since the
      2026-09-24 BRL prices were added.
PR06) purchasable_plans() is unaffected by the currency-catalog change
      (same plans, same USD ascending order).
PR07) currency_candidates(plan) puts CHECKOUT_CURRENCY (USD) first, then
      every other priced currency alphabetically; a plan with only USD
      priced (hypothetically) would return just ["USD"].
PO01) create_order() with no payment_method/payment_type/subscription_id
      given — all three are None (backward compatible with every
      pre-existing caller).
PO02) create_order() with payment_method + payment_type given persists
      both.
PO03) DB CHECK constraint rejects an invalid payment_method string.
PO04) DB CHECK constraint rejects an invalid payment_type string.
PO05) payment_method/payment_type are NULL-safe (constraint allows NULL).
SU01) Subscription can be created with a valid, minimal field set;
      defaults (payment_type='recurring', status='active') apply.
SU02) Subscription.status CHECK rejects an invalid value.
SU03) Subscription.payment_method CHECK rejects an invalid value.
SU04) Subscription.amount CHECK rejects a non-positive amount.
SU05) User.subscriptions relationship returns the user's subscriptions.
SU06) Subscription.payment_orders / PaymentOrder.subscription
      relationship round-trips correctly.
SU07) A PaymentOrder with subscription_id=None (every one-time/crypto
      order) works exactly as before — Subscription is NOT required.
SU08) uq_subscription_provider_ref allows multiple NULL
      provider_subscription_id rows for the same provider (NULLs are
      distinct), but rejects a true duplicate (provider, ref) pair.
CAP01) A minimal PaymentProvider subclass with no override declares
       empty capability sets (non-abstract, safe default).
CAP02) NOWPaymentsAdapter declares {CRYPTO} / {ONE_TIME}.
CAP03) BinancePayAdapter declares {CRYPTO} / {ONE_TIME}.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Mapping

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from src.billing.pricing import (
    CHECKOUT_CURRENCY,
    PLAN_PRICES_USD,
    currency_candidates,
    get_plan_price,
    purchasable_plans,
    supported_currencies,
)
from src.billing.providers.base import (
    PaymentIntent,
    PaymentMethod,
    PaymentProvider,
    PaymentResult,
    PaymentStatus,
    PaymentType,
    WebhookEvent,
)
from src.database import Base
from src.models import PaymentOrder, Subscription  # noqa: F401 — registers tables
from src.models.subscription import SubscriptionStatus
from src.models.user import User
from src.services.payment_order_service import PaymentOrderService


# ---------------------------------------------------------------------------
# E — enums
# ---------------------------------------------------------------------------


def test_E01_payment_method_and_type_values():
    assert {m.value for m in PaymentMethod} == {
        "pix", "credit_card", "debit_card", "boleto", "bank_transfer", "wallet", "crypto",
    }
    assert {t.value for t in PaymentType} == {"one_time", "recurring"}


def test_E02_payment_method_never_conflated_with_provider(db):
    svc = PaymentOrderService(db)
    u = _make_user(db)
    order = svc.create_order(
        user_id=u.id, provider="mercado_pago",
        amount_fiat=Decimal("29.99"), currency_fiat="BRL",
        credits_amount=Decimal("29.99"), currency_credits="BRL",
        idempotency_key=str(uuid.uuid4()), expires_at=_future(),
        payment_method=PaymentMethod.PIX.value,
    )
    assert order.provider == "mercado_pago"
    assert order.payment_method == "pix"
    assert order.payment_method != order.provider


# ---------------------------------------------------------------------------
# PR — pricing catalog
# ---------------------------------------------------------------------------


def test_PR01_get_plan_price_defaults_to_checkout_currency():
    # Compared against the catalog rather than a literal: this test owns the
    # defaulting behaviour, not the price. The prices themselves are argued
    # for in tests/unit/test_plan_economics.py.
    from src.billing.pricing import PLAN_PRICES

    assert get_plan_price("start") == PLAN_PRICES["start"]["USD"]
    assert get_plan_price("start") > Decimal("0")
    assert CHECKOUT_CURRENCY == "USD"


def test_PR02_get_plan_price_explicit_usd_matches_default():
    assert get_plan_price("start", "USD") == get_plan_price("start")


def test_PR03_get_plan_price_unpriced_currency_returns_none():
    # BRL added 2026-09-24 (go-live checkout-currency fix) — EUR remains
    # genuinely unpriced, so it is what this test now owns.
    assert get_plan_price("start", "EUR") is None
    assert get_plan_price("start", "eur") is None  # case-insensitive currency lookup


def test_PR04_get_plan_price_free_plan_always_none():
    assert get_plan_price("free") is None
    assert get_plan_price("free", "USD") is None


def test_PR05_supported_currencies_start():
    assert supported_currencies("start") == ["BRL", "USD"]
    assert supported_currencies("nonexistent-plan") == []


def test_PR06_purchasable_plans_unaffected():
    assert purchasable_plans() == sorted(PLAN_PRICES_USD, key=lambda p: PLAN_PRICES_USD[p])
    # TEAM left the flat price list: it is bought per seat (src/billing/team_pricing.py).
    assert set(purchasable_plans()) == {"start", "pro", "max"}


def test_PR07_currency_candidates_puts_usd_first_then_alphabetical():
    assert currency_candidates("start") == ["USD", "BRL"]
    assert currency_candidates("nonexistent-plan") == []


# ---------------------------------------------------------------------------
# PO — PaymentOrder new columns
# ---------------------------------------------------------------------------


def test_PO01_create_order_defaults_new_columns_to_none(db):
    svc = PaymentOrderService(db)
    u = _make_user(db)
    order = svc.create_order(
        user_id=u.id, provider="fake", amount_fiat=Decimal("9.99"), currency_fiat="USD",
        credits_amount=Decimal("9.99"), currency_credits="USD",
        idempotency_key=str(uuid.uuid4()), expires_at=_future(),
    )
    assert order.payment_method is None
    assert order.payment_type is None
    assert order.subscription_id is None


def test_PO02_create_order_persists_payment_method_and_type(db):
    svc = PaymentOrderService(db)
    u = _make_user(db)
    order = svc.create_order(
        user_id=u.id, provider="nowpayments", amount_fiat=Decimal("9.99"), currency_fiat="USD",
        credits_amount=Decimal("9.99"), currency_credits="USD",
        idempotency_key=str(uuid.uuid4()), expires_at=_future(),
        payment_method=PaymentMethod.CRYPTO.value, payment_type=PaymentType.ONE_TIME.value,
    )
    db.refresh(order)
    assert order.payment_method == "crypto"
    assert order.payment_type == "one_time"


def test_PO03_check_constraint_rejects_invalid_payment_method(db):
    u = _make_user(db)
    order = PaymentOrder(
        user_id=u.id, provider="fake", amount_fiat=Decimal("9.99"), currency_fiat="USD",
        credits_amount=Decimal("9.99"), currency_credits="USD",
        idempotency_key=str(uuid.uuid4()), expires_at=_future(),
        payment_method="bogus_method",
    )
    db.add(order)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_PO04_check_constraint_rejects_invalid_payment_type(db):
    u = _make_user(db)
    order = PaymentOrder(
        user_id=u.id, provider="fake", amount_fiat=Decimal("9.99"), currency_fiat="USD",
        credits_amount=Decimal("9.99"), currency_credits="USD",
        idempotency_key=str(uuid.uuid4()), expires_at=_future(),
        payment_type="bogus_type",
    )
    db.add(order)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_PO05_null_payment_method_and_type_allowed(db):
    u = _make_user(db)
    order = PaymentOrder(
        user_id=u.id, provider="fake", amount_fiat=Decimal("9.99"), currency_fiat="USD",
        credits_amount=Decimal("9.99"), currency_credits="USD",
        idempotency_key=str(uuid.uuid4()), expires_at=_future(),
    )
    db.add(order)
    db.commit()  # must not raise
    assert order.payment_method is None and order.payment_type is None


# ---------------------------------------------------------------------------
# SU — Subscription model
# ---------------------------------------------------------------------------


def test_SU01_create_minimal_subscription_applies_defaults(db):
    u = _make_user(db)
    sub = Subscription(
        user_id=u.id, plan="pro", provider="mercado_pago",
        payment_method=PaymentMethod.PIX.value, currency="BRL", amount=Decimal("29.99"),
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    assert sub.payment_type == PaymentType.RECURRING.value
    assert sub.status == SubscriptionStatus.ACTIVE.value


def test_SU02_status_check_rejects_invalid_value(db):
    u = _make_user(db)
    sub = Subscription(
        user_id=u.id, plan="pro", provider="mercado_pago",
        payment_method=PaymentMethod.PIX.value, currency="BRL", amount=Decimal("29.99"),
        status="bogus_status",
    )
    db.add(sub)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_SU03_payment_method_check_rejects_invalid_value(db):
    u = _make_user(db)
    sub = Subscription(
        user_id=u.id, plan="pro", provider="mercado_pago",
        payment_method="bogus_method", currency="BRL", amount=Decimal("29.99"),
    )
    db.add(sub)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_SU04_amount_check_rejects_non_positive(db):
    u = _make_user(db)
    sub = Subscription(
        user_id=u.id, plan="pro", provider="mercado_pago",
        payment_method=PaymentMethod.PIX.value, currency="BRL", amount=Decimal("0"),
    )
    db.add(sub)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_SU05_user_subscriptions_relationship(db):
    u = _make_user(db)
    sub = Subscription(
        user_id=u.id, plan="pro", provider="mercado_pago",
        payment_method=PaymentMethod.PIX.value, currency="BRL", amount=Decimal("29.99"),
    )
    db.add(sub)
    db.commit()
    db.refresh(u)
    assert [s.id for s in u.subscriptions] == [sub.id]


def test_SU06_subscription_payment_orders_round_trip(db):
    u = _make_user(db)
    sub = Subscription(
        user_id=u.id, plan="pro", provider="mercado_pago",
        payment_method=PaymentMethod.PIX.value, currency="BRL", amount=Decimal("29.99"),
    )
    db.add(sub)
    db.commit()

    svc = PaymentOrderService(db)
    order = svc.create_order(
        user_id=u.id, provider="mercado_pago", amount_fiat=Decimal("29.99"), currency_fiat="BRL",
        credits_amount=Decimal("29.99"), currency_credits="BRL",
        idempotency_key=str(uuid.uuid4()), expires_at=_future(), plan="pro",
        payment_method=PaymentMethod.PIX.value, payment_type=PaymentType.RECURRING.value,
    )
    order.subscription_id = sub.id
    db.commit()
    db.refresh(sub)
    db.refresh(order)

    assert order.subscription.id == sub.id
    assert [o.id for o in sub.payment_orders] == [order.id]


def test_SU07_payment_order_independent_of_subscription(db):
    """A one-time/crypto PaymentOrder must keep working with
    subscription_id=None — Subscription is never required."""
    svc = PaymentOrderService(db)
    u = _make_user(db)
    order = svc.create_order(
        user_id=u.id, provider="nowpayments", amount_fiat=Decimal("5.00"), currency_fiat="USD",
        credits_amount=Decimal("5.00"), currency_credits="USD",
        idempotency_key=str(uuid.uuid4()), expires_at=_future(),
        payment_method=PaymentMethod.CRYPTO.value, payment_type=PaymentType.ONE_TIME.value,
    )
    assert order.subscription_id is None
    assert order.subscription is None


def test_SU08_provider_ref_uniqueness_allows_multiple_nulls(db):
    u = _make_user(db)
    sub1 = Subscription(
        user_id=u.id, plan="pro", provider="mercado_pago",
        payment_method=PaymentMethod.PIX.value, currency="BRL", amount=Decimal("29.99"),
    )
    sub2 = Subscription(
        user_id=u.id, plan="max", provider="mercado_pago",
        payment_method=PaymentMethod.PIX.value, currency="BRL", amount=Decimal("89.99"),
    )
    db.add_all([sub1, sub2])
    db.commit()  # two NULL provider_subscription_id rows for the same provider — must not collide

    sub3 = Subscription(
        user_id=u.id, plan="team", provider="mercado_pago",
        payment_method=PaymentMethod.PIX.value, currency="BRL", amount=Decimal("249.99"),
        provider_subscription_id="mp-sub-1",
    )
    db.add(sub3)
    db.commit()

    dup = Subscription(
        user_id=u.id, plan="team", provider="mercado_pago",
        payment_method=PaymentMethod.PIX.value, currency="BRL", amount=Decimal("249.99"),
        provider_subscription_id="mp-sub-1",
    )
    db.add(dup)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# ---------------------------------------------------------------------------
# CAP — provider capability declarations
# ---------------------------------------------------------------------------


class _BareMinimumProvider(PaymentProvider):
    """The smallest legal PaymentProvider subclass — proves capabilities
    are non-abstract and default to empty (no fabricated claim)."""

    @property
    def provider_name(self) -> str:
        return "bare_minimum"

    async def create_intent(self, *, amount, currency, user_id, idempotency_key, metadata=None) -> PaymentIntent:
        raise NotImplementedError

    async def get_intent(self, intent_id: str) -> PaymentIntent:
        raise NotImplementedError

    async def verify_webhook(self, payload: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        raise NotImplementedError

    async def refund(self, payment_id: str, *, amount=None, reason: str = "") -> PaymentResult:
        raise NotImplementedError


def test_CAP01_default_capabilities_are_empty():
    adapter = _BareMinimumProvider()
    assert adapter.supported_payment_methods == frozenset()
    assert adapter.supported_payment_types == frozenset()


def test_CAP02_nowpayments_declares_crypto_one_time():
    from src.billing.providers.nowpayments_adapter import NOWPaymentsAdapter

    class _FakeTransport:
        async def post(self, *a, **k):
            raise NotImplementedError

        async def get(self, *a, **k):
            raise NotImplementedError

    adapter = NOWPaymentsAdapter(
        api_key="k", ipn_secret="s", base_url="https://api.nowpayments.io",
        transport=_FakeTransport(),
    )
    assert adapter.supported_payment_methods == frozenset({PaymentMethod.CRYPTO})
    assert adapter.supported_payment_types == frozenset({PaymentType.ONE_TIME})


def test_CAP03_binance_pay_declares_crypto_one_time():
    from src.billing.providers.binance_pay_adapter import BinancePayAdapter

    class _FakeTransport:
        async def post(self, *a, **k):
            raise NotImplementedError

    adapter = BinancePayAdapter(
        api_key="k", secret_key="s", base_url="https://bpay.binanceapi.com",
        transport=_FakeTransport(),
    )
    assert adapter.supported_payment_methods == frozenset({PaymentMethod.CRYPTO})
    assert adapter.supported_payment_types == frozenset({PaymentType.ONE_TIME})


# ---------------------------------------------------------------------------
# Fixtures / helpers
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


def _make_user(db) -> User:
    u = User(email=f"pdf-{uuid.uuid4()}@test.com", hashed_password="x", plan="start")
    db.add(u)
    db.commit()
    return u


def _future() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=1)
