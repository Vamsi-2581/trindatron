"""Unit tests for SubscriptionService —
packages/core/src/services/subscription_service.py.

All tests use FakeProvider test doubles declaring PaymentType.RECURRING
(NONE of the four real adapters do — see subscription_service.py's own
module docstring) — never a real network call, never real credentials.
Reuses route_checkout()/confirm_order() for provider selection and
ledger/entitlement effects, both already exhaustively tested in
test_payment_router.py/test_payment_order_service.py — this file proves
the SUBSCRIPTION-specific orchestration on top of them, not those
mechanisms a second time.

Test matrix
-----------
SE01) create_subscription — new row, status='active', validated against
      the real pricing catalog (unknown plan -> ValueError).
SE02) attempt_renewal — creates a real (fake-provider) PaymentOrder
      linked via subscription_id, payment_type='recurring'; subscription
      status is NOT changed by intent-creation alone (only confirmation
      changes it).
SE03) confirm_renewal — ACTIVATES: ledger pair written, user.plan set,
      subscription status='active', consecutive_failed_cycles reset.
SE04) Renewal is idempotent per cycle — retrying attempt_renewal() for
      the SAME cycle_start returns the SAME PaymentOrder; the provider's
      create_intent() is called exactly once total.
SE05) Two "concurrent" renewal attempts for the same cycle (sequential
      stand-in — same rationale as every other adapter's own MPC14/SC14
      pattern) never produce two PaymentOrders or two provider calls.
SE06) Payment explicitly rejected (AllProvidersRejected) ->
      record_renewal_failure() -> ACTIVE -> PAST_DUE.
SE07) Provider unavailable (timeout) -> Subscription status COMPLETELY
      unchanged — never treated as a failure.
SE08) NoEligibleProvider (ambiguous eligibility, e.g. currency/method
      mismatch) is ALSO treated as an explicit rejection -> PAST_DUE
      (distinct from SE07's true network ambiguity).
SE09) Entering grace period — repeated explicit failures beyond
      settings.subscription_max_past_due_cycles -> PAST_DUE -> GRACE_PERIOD,
      with grace_period_started_at/ends_at set from the configurable
      duration.
SE10) Expiration — expire_subscription() only succeeds once
      grace_period_ends_at has passed (GracePeriodNotElapsed raised
      otherwise); succeeds afterward -> EXPIRED (terminal).
SE11) Cancellation — ACTIVE/PAST_DUE/GRACE_PERIOD -> CANCELLED,
      cancelled_at set; idempotent on a second call; cross-user
      cancellation attempt raises SubscriptionNotFound (no ownership leak).
SE12) "Duplicate webhook" stand-in — confirm_renewal() called twice for
      the SAME order is idempotent (PaymentOrderService.confirm_order()'s
      own fast path) — no double ledger entry, no double activation.
SE13) "Out-of-order webhook" stand-in — a failure signal recorded BEFORE
      a later-arriving success signal for the SAME order still converges
      to the CORRECT final state (ACTIVE, ledger written once) — the
      system is order-independent, not order-dependent.
SE14) Retry of the same cycle after a PROVIDER-UNAVAILABLE (ambiguous)
      outcome resumes against the SAME order/provider — never silently
      creates a second attempt (reuses route_checkout()'s own guarantee,
      proven again here specifically through the Subscription Engine's
      own call path).
SE15) No ledger duplication — exactly one debit+credit pair after a
      successful renewal, regardless of how many times confirm_renewal()
      is called afterward.
SE16) No entitlement duplication — user.plan is set to the SAME value
      idempotently; a subscription with plan="pro" confirmed twice never
      leaves user.plan in an inconsistent state.
SE17) Coexistence with the Payment Router's priority ordering — two
      RECURRING-capable fake providers registered; the higher-priority
      one (per settings.payment_provider_priority) is selected for the
      renewal, proving the Subscription Engine reuses the SAME
      eligibility+priority logic, not a parallel selection mechanism.
SE18) Regression coverage for the four existing (non-recurring) real
      adapters is NOT re-tested here — see the full billing suite run
      instead (test_billing_checkout_mercadopago.py,
      test_billing_checkout_stripe.py, test_nowpayments_checkout.py,
      test_billing_binance_pay_integration.py — all unaffected, since
      none of them declare PaymentType.RECURRING and this module never
      touches any adapter file).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Mapping

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.billing.payment_router import AllProvidersRejected, NoEligibleProvider
from src.billing.providers.base import (
    PaymentCreationRejected,
    PaymentIntent,
    PaymentMethod,
    PaymentProvider,
    PaymentResult,
    PaymentStatus,
    PaymentType,
    ProviderUnavailable,
    WebhookEvent,
)
from src.billing.providers.registry import ProviderRegistry
from src.database import Base
from src.models import PaymentOrder, Subscription  # noqa: F401 — registers tables
from src.models.ledger import LedgerEntry
from src.models.subscription import SubscriptionStatus
from src.models.user import User
from src.services.payment_order_service import PaymentOrderService
from src.services.subscription_service import (
    GracePeriodNotElapsed,
    SubscriptionNotFound,
    SubscriptionNotRenewable,
    SubscriptionService,
    expire_overdue_subscriptions,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeRejected(PaymentCreationRejected):
    pass


class FakeRecurringProvider(PaymentProvider):
    """The only fake provider in this file that declares
    PaymentType.RECURRING — see module docstring for why none of the
    real adapters do yet."""

    def __init__(
        self, name: str = "fake_recurring", *,
        methods: frozenset[PaymentMethod] = frozenset({PaymentMethod.PIX}),
        behavior: str = "success",
    ) -> None:
        self._name = name
        self._methods = methods
        self.behavior = behavior
        self.create_intent_calls: list[dict] = []

    @property
    def provider_name(self) -> str:
        return self._name

    @property
    def supported_payment_methods(self) -> frozenset[PaymentMethod]:
        return self._methods

    @property
    def supported_payment_types(self) -> frozenset[PaymentType]:
        return frozenset({PaymentType.RECURRING})

    async def create_intent(self, *, amount, currency, user_id, idempotency_key, metadata=None) -> PaymentIntent:
        self.create_intent_calls.append({"idempotency_key": idempotency_key, "amount": amount})
        if self.behavior == "rejected":
            raise _FakeRejected(f"{self._name} rejected the renewal")
        if self.behavior == "unavailable":
            raise ProviderUnavailable(f"{self._name} timed out")
        return PaymentIntent(
            id=f"ext-{self._name}-{idempotency_key}", provider=self._name, amount=amount,
            currency=currency.upper(), status=PaymentStatus.PENDING, idempotency_key=idempotency_key,
        )

    async def get_intent(self, intent_id: str) -> PaymentIntent:
        raise NotImplementedError

    async def verify_webhook(self, payload: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        raise NotImplementedError

    async def refund(self, payment_id: str, **kwargs) -> PaymentResult:
        raise NotImplementedError


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


@pytest.fixture
def user(db):
    u = User(email=f"sub-{uuid.uuid4()}@test.com", hashed_password="x", plan="start")
    db.add(u)
    db.commit()
    return u


@pytest.fixture
def svc(db):
    return SubscriptionService(db)


def _registry(*providers: PaymentProvider) -> ProviderRegistry:
    reg = ProviderRegistry()
    for p in providers:
        reg.register(p)
    return reg


def _cycle() -> tuple[datetime, datetime]:
    start = datetime.now(timezone.utc)
    return start, start + timedelta(days=30)


def _make_subscription(svc, user, provider="fake_recurring", plan="pro", amount=Decimal("29.99")) -> Subscription:
    return svc.create_subscription(
        user_id=user.id, plan=plan, provider=provider, payment_method=PaymentMethod.PIX,
        currency="USD", amount=amount,
    )


# ---------------------------------------------------------------------------
# SE01 — creation
# ---------------------------------------------------------------------------


def test_SE01_create_subscription(svc, user):
    sub = _make_subscription(svc, user)
    assert sub.status == SubscriptionStatus.ACTIVE.value
    assert sub.plan == "pro"
    assert sub.payment_type == PaymentType.RECURRING.value
    assert sub.consecutive_failed_cycles == 0

    with pytest.raises(ValueError):
        svc.create_subscription(
            user_id=user.id, plan="not-a-real-plan", provider="fake_recurring",
            payment_method=PaymentMethod.PIX, currency="USD", amount=Decimal("9.99"),
        )


def test_SE01b_create_subscription_idempotent(db, svc, user):
    """Final-review hardening: a double-click/retried creation request
    with the SAME idempotency_key must never produce two Subscription
    rows for the same logical intent."""
    key = str(uuid.uuid4())
    first = svc.create_subscription(
        user_id=user.id, plan="pro", provider="fake_recurring", payment_method=PaymentMethod.PIX,
        currency="USD", amount=Decimal("29.99"), idempotency_key=key,
    )
    second = svc.create_subscription(
        user_id=user.id, plan="pro", provider="fake_recurring", payment_method=PaymentMethod.PIX,
        currency="USD", amount=Decimal("29.99"), idempotency_key=key,
    )
    assert first.id == second.id
    assert db.query(Subscription).filter(Subscription.user_id == user.id).count() == 1

    # No key at all -> no protection, by design (documented) — two calls
    # legitimately create two independent subscriptions.
    a = svc.create_subscription(
        user_id=user.id, plan="pro", provider="fake_recurring", payment_method=PaymentMethod.PIX,
        currency="USD", amount=Decimal("29.99"),
    )
    b = svc.create_subscription(
        user_id=user.id, plan="pro", provider="fake_recurring", payment_method=PaymentMethod.PIX,
        currency="USD", amount=Decimal("29.99"),
    )
    assert a.id != b.id


# ---------------------------------------------------------------------------
# SE02-SE05 — renewal + idempotency + "concurrency"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_SE02_attempt_renewal_creates_linked_order_no_status_change(db, svc, user):
    sub = _make_subscription(svc, user)
    provider = FakeRecurringProvider()
    registry = _registry(provider)
    cycle_start, cycle_end = _cycle()

    sub_after, order, intent = await svc.attempt_renewal(registry, sub.id, cycle_start=cycle_start, cycle_end=cycle_end)

    assert order.subscription_id == sub.id
    assert order.payment_type == PaymentType.RECURRING.value
    assert order.plan == "pro"
    assert order.status == "processing"
    assert intent is not None
    db.refresh(sub)
    assert sub.status == SubscriptionStatus.ACTIVE.value  # intent creation alone never changes status


@pytest.mark.asyncio
async def test_SE03_confirm_renewal_activates(db, svc, user):
    sub = _make_subscription(svc, user)
    registry = _registry(FakeRecurringProvider())
    cycle_start, cycle_end = _cycle()
    _sub, order, _intent = await svc.attempt_renewal(registry, sub.id, cycle_start=cycle_start, cycle_end=cycle_end)

    confirmed = svc.confirm_renewal(sub.id, order.id)

    assert confirmed.status == SubscriptionStatus.ACTIVE.value
    assert confirmed.consecutive_failed_cycles == 0
    db.refresh(user)
    assert user.plan == "pro"
    entries = db.query(LedgerEntry).filter(LedgerEntry.reference_id == order.id).all()
    assert len(entries) == 2


@pytest.mark.asyncio
async def test_SE04_renewal_idempotent_per_cycle(db, svc, user):
    sub = _make_subscription(svc, user)
    provider = FakeRecurringProvider()
    registry = _registry(provider)
    cycle_start, cycle_end = _cycle()

    _s1, order1, intent1 = await svc.attempt_renewal(registry, sub.id, cycle_start=cycle_start, cycle_end=cycle_end)
    _s2, order2, intent2 = await svc.attempt_renewal(registry, sub.id, cycle_start=cycle_start, cycle_end=cycle_end)

    assert order1.id == order2.id
    assert intent2 is None  # idempotent no-op — already succeeded
    assert len(provider.create_intent_calls) == 1
    assert db.query(PaymentOrder).filter(PaymentOrder.subscription_id == sub.id).count() == 1


@pytest.mark.asyncio
async def test_SE05_sequential_concurrent_renewal_attempts_single_order(db, svc, user):
    """Sequential stand-in for concurrent duplicate calls — same
    rationale/caveat as every other adapter's own MPC14/SC14 pattern."""
    sub = _make_subscription(svc, user)
    provider = FakeRecurringProvider()
    registry = _registry(provider)
    cycle_start, cycle_end = _cycle()

    for _ in range(3):
        await svc.attempt_renewal(registry, sub.id, cycle_start=cycle_start, cycle_end=cycle_end)

    assert len(provider.create_intent_calls) == 1
    assert db.query(PaymentOrder).filter(PaymentOrder.subscription_id == sub.id).count() == 1


# ---------------------------------------------------------------------------
# SE06-SE08 — failure taxonomy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_SE06_explicit_rejection_moves_to_past_due(db, svc, user):
    sub = _make_subscription(svc, user)
    registry = _registry(FakeRecurringProvider(behavior="rejected"))
    cycle_start, cycle_end = _cycle()

    with pytest.raises(AllProvidersRejected):
        await svc.attempt_renewal(registry, sub.id, cycle_start=cycle_start, cycle_end=cycle_end)

    db.refresh(sub)
    assert sub.status == SubscriptionStatus.PAST_DUE.value
    assert sub.consecutive_failed_cycles == 1


@pytest.mark.asyncio
async def test_SE07_provider_unavailable_never_changes_status(db, svc, user):
    sub = _make_subscription(svc, user)
    registry = _registry(FakeRecurringProvider(behavior="unavailable"))
    cycle_start, cycle_end = _cycle()

    with pytest.raises(ProviderUnavailable):
        await svc.attempt_renewal(registry, sub.id, cycle_start=cycle_start, cycle_end=cycle_end)

    db.refresh(sub)
    assert sub.status == SubscriptionStatus.ACTIVE.value  # completely unchanged
    assert sub.consecutive_failed_cycles == 0


@pytest.mark.asyncio
async def test_SE08_no_eligible_provider_treated_as_explicit_rejection(db, svc, user):
    sub = _make_subscription(svc, user, provider="fake_recurring")
    registry = _registry()  # nothing registered at all
    cycle_start, cycle_end = _cycle()

    with pytest.raises(NoEligibleProvider):
        await svc.attempt_renewal(registry, sub.id, cycle_start=cycle_start, cycle_end=cycle_end)

    db.refresh(sub)
    assert sub.status == SubscriptionStatus.PAST_DUE.value


# ---------------------------------------------------------------------------
# SE09-SE11 — grace period / expiration / cancellation
# ---------------------------------------------------------------------------


def test_SE09_entering_grace_period(db, svc, user, monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "subscription_max_past_due_cycles", 1)
    monkeypatch.setattr(settings, "subscription_grace_period_days", 3)

    sub = _make_subscription(svc, user)
    svc.record_renewal_failure(sub.id)  # ACTIVE -> PAST_DUE (cycle 1)
    db.refresh(sub)
    assert sub.status == SubscriptionStatus.PAST_DUE.value

    svc.record_renewal_failure(sub.id)  # cycle 2 > threshold(1) -> GRACE_PERIOD
    db.refresh(sub)
    assert sub.status == SubscriptionStatus.GRACE_PERIOD.value
    assert sub.grace_period_started_at is not None
    assert sub.grace_period_ends_at is not None
    delta = sub.grace_period_ends_at - sub.grace_period_started_at
    assert timedelta(days=2, hours=23) < delta < timedelta(days=3, hours=1)


def test_SE10_expiration_requires_grace_period_elapsed(db, svc, user, monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "subscription_max_past_due_cycles", 0)

    sub = _make_subscription(svc, user)
    svc.record_renewal_failure(sub.id)  # threshold 0 -> straight to GRACE_PERIOD
    db.refresh(sub)
    assert sub.status == SubscriptionStatus.GRACE_PERIOD.value

    with pytest.raises(GracePeriodNotElapsed):
        svc.expire_subscription(sub.id, now=datetime.now(timezone.utc))

    future = sub.grace_period_ends_at + timedelta(seconds=1)
    expired = svc.expire_subscription(sub.id, now=future)
    assert expired.status == SubscriptionStatus.EXPIRED.value

    # Terminal — a second expire attempt is rejected, not silently repeated.
    with pytest.raises(Exception):
        svc.expire_subscription(sub.id, now=future + timedelta(days=1))


def test_SE10b_expire_overdue_subscriptions_sweep(db, svc, user, monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "subscription_max_past_due_cycles", 0)

    sub = _make_subscription(svc, user)
    svc.record_renewal_failure(sub.id)
    db.refresh(sub)
    ends_at = sub.grace_period_ends_at
    if ends_at.tzinfo is None:  # SQLite returns naive datetimes after a refresh
        ends_at = ends_at.replace(tzinfo=timezone.utc)
    past_deadline = ends_at + timedelta(seconds=1)

    count = expire_overdue_subscriptions(db, now=past_deadline)
    assert count == 1
    db.refresh(sub)
    assert sub.status == SubscriptionStatus.EXPIRED.value

    assert expire_overdue_subscriptions(db, now=past_deadline) == 0  # idempotent, nothing left to sweep


async def test_SE11_cancellation(db, svc, user):
    # No provider_subscription_id was ever set on these subscriptions
    # (created via _make_subscription() with no external activation) —
    # cancel_subscription() takes its "nothing to cancel externally"
    # fast path, so an empty registry is fine here. See
    # test_stripe_subscription.py's R10/R22-R26 for the
    # externally-confirmed-mandate cancellation scenarios.
    registry = _registry()
    sub = _make_subscription(svc, user)
    cancelled = await svc.cancel_subscription(registry, sub.id, user_id=user.id)
    assert cancelled.status == SubscriptionStatus.CANCELLED.value
    assert cancelled.cancelled_at is not None

    # Idempotent — cancelling again is a no-op, not an error.
    again = await svc.cancel_subscription(registry, sub.id, user_id=user.id)
    assert again.status == SubscriptionStatus.CANCELLED.value
    assert again.cancelled_at == cancelled.cancelled_at

    # Cross-user cancellation attempt — no ownership leak.
    other = User(email=f"other-{uuid.uuid4()}@test.com", hashed_password="x", plan="start")
    db.add(other)
    db.commit()
    sub2 = _make_subscription(svc, user)
    with pytest.raises(SubscriptionNotFound):
        await svc.cancel_subscription(registry, sub2.id, user_id=other.id)


# ---------------------------------------------------------------------------
# SE12-SE14 — duplicate / out-of-order "webhook" stand-ins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_SE12_duplicate_confirm_renewal_idempotent(db, svc, user):
    sub = _make_subscription(svc, user)
    registry = _registry(FakeRecurringProvider())
    cycle_start, cycle_end = _cycle()
    _s, order, _i = await svc.attempt_renewal(registry, sub.id, cycle_start=cycle_start, cycle_end=cycle_end)

    first = svc.confirm_renewal(sub.id, order.id)
    second = svc.confirm_renewal(sub.id, order.id)

    assert first.status == second.status == SubscriptionStatus.ACTIVE.value
    entries = db.query(LedgerEntry).filter(LedgerEntry.reference_id == order.id).all()
    assert len(entries) == 2  # never duplicated


@pytest.mark.asyncio
async def test_SE13_out_of_order_failure_then_success_converges_correctly(db, svc, user):
    """A failure signal recorded BEFORE a later-arriving success signal
    for the SAME order still converges to the correct final state."""
    sub = _make_subscription(svc, user)
    registry = _registry(FakeRecurringProvider())
    cycle_start, cycle_end = _cycle()
    _s, order, _i = await svc.attempt_renewal(registry, sub.id, cycle_start=cycle_start, cycle_end=cycle_end)

    svc.record_renewal_failure(sub.id)  # "early" failure webhook
    db.refresh(sub)
    assert sub.status == SubscriptionStatus.PAST_DUE.value

    confirmed = svc.confirm_renewal(sub.id, order.id)  # "late" success webhook for the same order
    assert confirmed.status == SubscriptionStatus.ACTIVE.value
    assert confirmed.consecutive_failed_cycles == 0
    entries = db.query(LedgerEntry).filter(LedgerEntry.reference_id == order.id).all()
    assert len(entries) == 2


@pytest.mark.asyncio
async def test_SE14_retry_after_ambiguous_resumes_same_order(db, svc, user):
    sub = _make_subscription(svc, user)
    provider = FakeRecurringProvider(behavior="unavailable")
    registry = _registry(provider)
    cycle_start, cycle_end = _cycle()

    with pytest.raises(ProviderUnavailable):
        await svc.attempt_renewal(registry, sub.id, cycle_start=cycle_start, cycle_end=cycle_end)

    provider.behavior = "success"
    _s, order, intent = await svc.attempt_renewal(registry, sub.id, cycle_start=cycle_start, cycle_end=cycle_end)

    assert order.provider == "fake_recurring"
    assert intent is not None
    assert len(provider.create_intent_calls) == 2  # first (unavailable) + retry (success)
    assert db.query(PaymentOrder).filter(PaymentOrder.subscription_id == sub.id).count() == 1


# ---------------------------------------------------------------------------
# SE15-SE16 — no ledger/entitlement duplication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_SE15_no_ledger_duplication_across_repeated_confirms(db, svc, user):
    sub = _make_subscription(svc, user)
    registry = _registry(FakeRecurringProvider())
    cycle_start, cycle_end = _cycle()
    _s, order, _i = await svc.attempt_renewal(registry, sub.id, cycle_start=cycle_start, cycle_end=cycle_end)

    for _ in range(3):
        svc.confirm_renewal(sub.id, order.id)

    entries = db.query(LedgerEntry).filter(LedgerEntry.reference_id == order.id).all()
    assert len(entries) == 2


@pytest.mark.asyncio
async def test_SE16_no_entitlement_duplication(db, svc, user):
    sub = _make_subscription(svc, user, plan="pro")
    registry = _registry(FakeRecurringProvider())
    cycle_start, cycle_end = _cycle()
    _s, order, _i = await svc.attempt_renewal(registry, sub.id, cycle_start=cycle_start, cycle_end=cycle_end)

    svc.confirm_renewal(sub.id, order.id)
    svc.confirm_renewal(sub.id, order.id)
    db.refresh(user)
    assert user.plan == "pro"  # stable, never flapped or duplicated


# ---------------------------------------------------------------------------
# SE17 — coexistence with Payment Router priority
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_SE17_router_priority_reused_for_renewal(db, svc, user, monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "payment_provider_priority", ["preferred_recurring", "other_recurring"])

    sub = svc.create_subscription(
        user_id=user.id, plan="pro", provider="preferred_recurring", payment_method=PaymentMethod.PIX,
        currency="USD", amount=Decimal("29.99"),
    )
    preferred = FakeRecurringProvider("preferred_recurring")
    other = FakeRecurringProvider("other_recurring")
    registry = _registry(other, preferred)  # registration order deliberately reversed
    cycle_start, cycle_end = _cycle()

    _s, order, _i = await svc.attempt_renewal(registry, sub.id, cycle_start=cycle_start, cycle_end=cycle_end)

    assert order.provider == "preferred_recurring"
    assert len(preferred.create_intent_calls) == 1
    assert len(other.create_intent_calls) == 0
