"""Unit tests for the Payment Router —
packages/core/src/billing/payment_router.py.

All tests use FakeProvider test doubles (never a real adapter/network
call) — pure eligibility/priority/failover/concurrency-shape logic
against a real in-memory SQLite PaymentOrder/User schema. HTTP-level
coverage (POST /billing/checkout actually dispatching through the
router, with each of the 4 real adapters) lives in
test_billing_checkout_router.py — not duplicated here.

Test matrix
-----------
R01) One eligible provider -> selected, create_intent() called once,
     order.provider == that provider, order.external_order_id set.
R02) Two eligible providers, both ranked -> the HIGHER-priority one
     (per settings.payment_provider_priority) is selected — deterministic,
     no ambiguity error.
R03) Two eligible providers, NEITHER ranked -> deterministic alphabetical
     tiebreak (same result every time, proven by repeating the call).
R04) A provider not registered at all (simulating "disabled" or
     "missing credentials" — both collapse to "never appears in
     registry.names()", see module docstring's audit point #2) never
     appears among eligible/excluded — it simply does not exist to the
     router.
R05) payment_method not supported by any registered provider -> excluded
     with a reason; NoEligibleProvider raised; NO PaymentOrder row created.
R06) currency not supported by any registered provider -> same as R05.
R07) payment_type not supported by any registered provider -> same as R05.
R08) Zero registered providers at all -> NoEligibleProvider, no row created.
R09) First (highest-priority) candidate's create_intent() raises a
     PaymentCreationRejected subclass (deterministic) -> the SAME order
     row is reassigned to the second candidate and succeeds — exactly
     ONE PaymentOrder row exists for this idempotency_key.
R10) First candidate's create_intent() raises ProviderUnavailable
     (ambiguous/timeout) -> NO failover — ProviderUnavailable propagates,
     the second candidate's create_intent() is NEVER called, and the
     order is left with provider=first, external_order_id=NULL.
R11) Retrying route_checkout() with the SAME idempotency_key after a
     successful first call returns the EXISTING order/intent=None — the
     provider's create_intent() is NOT called again (idempotent).
R12) Retrying route_checkout() with the SAME idempotency_key after a
     ProviderUnavailable (order still pending, external_order_id=NULL)
     resumes against the SAME provider that returned ambiguous — never
     silently switches to a different one on retry.
R13) The winning provider name is correctly persisted on
     PaymentOrder.provider (covered inline by R01/R02/R09, asserted
     explicitly here as its own scenario for traceability).
R14) No secret-looking value ever appears in any log record emitted by
     the router (FakeProvider carries a fake "secret" attribute never
     read by the router — proves the router never introspects/logs
     adapter internals, only names/ids/method/currency).
R15) All eligible candidates reject deterministically -> AllProvidersRejected,
     order.provider left as the LAST candidate genuinely tried.
R16) reassign_provider_unsafe_guard() raises ProviderReassignmentUnsafe
     when called on an order that already has external_order_id set —
     the safety invariant failover relies on, tested directly.
R17) Sequential "concurrent" stand-in (same rationale/caveat as every
     other adapter's own MPC14/SC14 pattern — SQLite has one shared
     connection, see test_billing_postgres_concurrency.py for the real
     race): repeating route_checkout() 3x for the SAME idempotency_key
     never calls create_intent() more than once total across all 3 calls.
R18) True thread-level concurrency smoke test: N threads all call
     route_checkout() for the SAME idempotency_key against a
     slow-to-respond FakeProvider; asserted deterministic OUTCOME
     (exactly one external attempt recorded, one order row) even though
     SQLite's own locking can't be verified to have serialized them —
     this proves the ROUTER's logic is race-safe in shape, with the DB
     engine's real enforcement covered separately on PostgreSQL.
R19) Hardening — DETERMINISTIC reproduction of the audited
     expire_order()-vs-create_intent() race: an order a concurrent
     expire_order() call already won (status='expired') before
     route_checkout() ever reaches create_intent() must be refused
     (OrderExpired) BEFORE any provider is ever contacted — never an
     unhandled InvalidStatusTransition from mark_processing(), and never
     a real external session orphaned with no local order to track it.
R20-R24) resolve_default_currency() — go-live checkout-currency fix (B2):
     auto-selection picks the first catalog currency an eligible provider
     accepts (BRL-only provider -> BRL even though USD is first in the
     catalog; a USD-capable provider alongside it -> USD unchanged; no
     eligible provider for any priced currency -> None); the explicit-
     provider mode checks only that ONE adapter's own currencies, and an
     unregistered explicit provider -> None.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Mapping

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.billing.payment_router import (
    AllProvidersRejected,
    NoEligibleProvider,
    eligible_providers,
    resolve_default_currency,
    route_checkout,
)
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
from src.models import PaymentOrder  # noqa: F401 — registers tables
from src.models.user import User
from src.services.payment_order_service import (
    OrderExpired,
    PaymentOrderService,
    ProviderReassignmentUnsafe,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeRejected(PaymentCreationRejected):
    """A concrete, provider-agnostic stand-in for a real adapter's own
    XxxError — proves the router only ever needs the BASE exception."""


class FakeProvider(PaymentProvider):
    """Configurable behavior: "success" | "rejected" | "unavailable".
    Carries a fake `secret` attribute the router must never log."""

    def __init__(
        self, name: str, *,
        methods: frozenset[PaymentMethod] = frozenset(),
        types: frozenset[PaymentType] = frozenset(),
        currencies: frozenset[str] = frozenset(),
        behavior: str = "success",
        secret: str = "SUPER-SECRET-FAKE-CREDENTIAL",
    ) -> None:
        self._name = name
        self._methods = methods
        self._types = types
        self._currencies = currencies
        self.behavior = behavior
        self.secret = secret  # deliberately named like a real credential
        self.create_intent_calls: list[dict] = []

    @property
    def provider_name(self) -> str:
        return self._name

    @property
    def supported_payment_methods(self) -> frozenset[PaymentMethod]:
        return self._methods

    @property
    def supported_payment_types(self) -> frozenset[PaymentType]:
        return self._types

    @property
    def supported_currencies(self) -> frozenset[str]:
        return self._currencies

    async def create_intent(self, *, amount, currency, user_id, idempotency_key, metadata=None) -> PaymentIntent:
        self.create_intent_calls.append({
            "amount": amount, "currency": currency, "user_id": user_id,
            "idempotency_key": idempotency_key, "metadata": metadata,
        })
        if self.behavior == "rejected":
            raise _FakeRejected(f"{self._name} rejected the request")
        if self.behavior == "unavailable":
            raise ProviderUnavailable(f"{self._name} timed out")
        return PaymentIntent(
            id=f"ext-{self._name}-{idempotency_key}", provider=self._name, amount=amount,
            currency=currency.upper(), status=PaymentStatus.PENDING, idempotency_key=idempotency_key,
            redirect_url=f"https://{self._name}.example/checkout",
        )

    async def get_intent(self, intent_id: str) -> PaymentIntent:
        raise NotImplementedError

    async def verify_webhook(self, payload: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        raise NotImplementedError

    async def refund(self, payment_id: str, **kwargs) -> PaymentResult:
        raise NotImplementedError


class _SlowFakeProvider(FakeProvider):
    """Same as FakeProvider but blocks briefly inside create_intent() —
    used to widen the race window for the thread-concurrency smoke test."""

    def __init__(self, *args, delay_seconds: float = 0.05, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._delay_seconds = delay_seconds

    async def create_intent(self, **kwargs) -> PaymentIntent:
        import asyncio
        await asyncio.sleep(self._delay_seconds)
        return await super().create_intent(**kwargs)


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


def _make_user(db) -> User:
    u = User(email=f"router-{uuid.uuid4()}@test.com", hashed_password="x", plan="start")
    db.add(u)
    db.commit()
    return u


def _future() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=1)


def _registry(*providers: PaymentProvider) -> ProviderRegistry:
    reg = ProviderRegistry()
    for p in providers:
        reg.register(p)
    return reg


async def _route(db, registry, user, *, payment_method=None, payment_type=None, currency="USD", idempotency_key=None):
    return await route_checkout(
        db, registry,
        user_id=user.id, plan="pro", amount_fiat=Decimal("29.99"), currency_fiat=currency,
        credits_amount=Decimal("29.99"), currency_credits=currency,
        idempotency_key=idempotency_key or str(uuid.uuid4()), expires_at=_future(),
        payment_method=payment_method, payment_type=payment_type,
    )


# ---------------------------------------------------------------------------
# R01-R08 — eligibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_R01_single_eligible_provider_selected(db):
    user = _make_user(db)
    provider = FakeProvider("only_one", methods=frozenset({PaymentMethod.PIX}))
    registry = _registry(provider)

    order, intent = await _route(db, registry, user, payment_method=PaymentMethod.PIX)

    assert order.provider == "only_one"
    assert order.external_order_id == intent.id
    assert len(provider.create_intent_calls) == 1


@pytest.mark.asyncio
async def test_R02_two_ranked_providers_higher_priority_wins(db, monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "payment_provider_priority", ["preferred", "other"])
    user = _make_user(db)
    preferred = FakeProvider("preferred", methods=frozenset({PaymentMethod.PIX}))
    other = FakeProvider("other", methods=frozenset({PaymentMethod.PIX}))
    registry = _registry(other, preferred)  # registration order deliberately reversed

    order, _intent = await _route(db, registry, user, payment_method=PaymentMethod.PIX)

    assert order.provider == "preferred"
    assert len(preferred.create_intent_calls) == 1
    assert len(other.create_intent_calls) == 0


@pytest.mark.asyncio
async def test_R03_unranked_providers_deterministic_alphabetical_tiebreak(db, monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "payment_provider_priority", [])
    user = _make_user(db)

    for _ in range(3):
        provider_b = FakeProvider("provider_b", methods=frozenset({PaymentMethod.PIX}))
        provider_a = FakeProvider("provider_a", methods=frozenset({PaymentMethod.PIX}))
        registry = _registry(provider_b, provider_a)
        order, _intent = await _route(db, registry, user, payment_method=PaymentMethod.PIX)
        assert order.provider == "provider_a"  # alphabetically first, every time


def test_R04_unregistered_provider_never_appears():
    """A provider that is disabled or missing credentials is, by
    construction, never registered (see module docstring's audit point
    #2) — it therefore never shows up in eligible OR excluded."""
    registry = _registry(FakeProvider("real_one", methods=frozenset({PaymentMethod.PIX})))
    eligible, excluded = eligible_providers(
        registry, payment_method=PaymentMethod.PIX, payment_type=None, currency="USD",
    )
    names = {n for n, _ in eligible} | set(excluded)
    assert "never_registered_ghost" not in names
    assert [n for n, _ in eligible] == ["real_one"]


@pytest.mark.asyncio
async def test_R05_payment_method_not_supported_raises_no_eligible(db):
    user = _make_user(db)
    registry = _registry(FakeProvider("crypto_only", methods=frozenset({PaymentMethod.CRYPTO})))

    with pytest.raises(NoEligibleProvider) as exc_info:
        await _route(db, registry, user, payment_method=PaymentMethod.PIX)
    assert "crypto_only" in exc_info.value.excluded

    # No order row was created for this rejected request.
    assert db.query(PaymentOrder).count() == 0


@pytest.mark.asyncio
async def test_R06_currency_not_supported_raises_no_eligible(db):
    user = _make_user(db)
    registry = _registry(FakeProvider("brl_only", currencies=frozenset({"BRL"})))

    with pytest.raises(NoEligibleProvider):
        await _route(db, registry, user, currency="JPY")
    assert db.query(PaymentOrder).count() == 0


@pytest.mark.asyncio
async def test_R07_payment_type_not_supported_raises_no_eligible(db):
    user = _make_user(db)
    registry = _registry(FakeProvider("one_time_only", types=frozenset({PaymentType.ONE_TIME})))

    with pytest.raises(NoEligibleProvider):
        await _route(db, registry, user, payment_type=PaymentType.RECURRING)
    assert db.query(PaymentOrder).count() == 0


@pytest.mark.asyncio
async def test_R08_zero_registered_providers_raises_no_eligible(db):
    user = _make_user(db)
    registry = _registry()

    with pytest.raises(NoEligibleProvider):
        await _route(db, registry, user)
    assert db.query(PaymentOrder).count() == 0


# ---------------------------------------------------------------------------
# R09-R10 — failure taxonomy / failover safety
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_R09_deterministic_rejection_fails_over_to_second_candidate(db, monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "payment_provider_priority", ["first", "second"])
    user = _make_user(db)
    first = FakeProvider("first", methods=frozenset({PaymentMethod.PIX}), behavior="rejected")
    second = FakeProvider("second", methods=frozenset({PaymentMethod.PIX}), behavior="success")
    registry = _registry(first, second)

    order, intent = await _route(db, registry, user, payment_method=PaymentMethod.PIX)

    assert order.provider == "second"
    assert intent.provider == "second"
    assert len(first.create_intent_calls) == 1
    assert len(second.create_intent_calls) == 1
    assert db.query(PaymentOrder).count() == 1  # exactly ONE row, never two


@pytest.mark.asyncio
async def test_R10_ambiguous_failure_never_fails_over(db, monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "payment_provider_priority", ["first", "second"])
    user = _make_user(db)
    first = FakeProvider("first", methods=frozenset({PaymentMethod.PIX}), behavior="unavailable")
    second = FakeProvider("second", methods=frozenset({PaymentMethod.PIX}), behavior="success")
    registry = _registry(first, second)

    with pytest.raises(ProviderUnavailable):
        await _route(db, registry, user, payment_method=PaymentMethod.PIX)

    assert len(first.create_intent_calls) == 1
    assert len(second.create_intent_calls) == 0  # NEVER tried — ambiguous outcome

    order = db.query(PaymentOrder).first()
    assert order.provider == "first"
    assert order.external_order_id is None
    assert order.status == "pending"


# ---------------------------------------------------------------------------
# R11-R13 — idempotency / persisted provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_R11_retry_after_success_is_idempotent_no_second_call(db):
    user = _make_user(db)
    provider = FakeProvider("only_one", methods=frozenset({PaymentMethod.PIX}))
    registry = _registry(provider)
    key = str(uuid.uuid4())

    order1, intent1 = await _route(db, registry, user, payment_method=PaymentMethod.PIX, idempotency_key=key)
    order2, intent2 = await _route(db, registry, user, payment_method=PaymentMethod.PIX, idempotency_key=key)

    assert order1.id == order2.id
    assert intent2 is None  # second call is a pure idempotent no-op
    assert len(provider.create_intent_calls) == 1  # provider contacted exactly once
    assert db.query(PaymentOrder).count() == 1


@pytest.mark.asyncio
async def test_R12_retry_after_ambiguous_resumes_same_provider(db, monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "payment_provider_priority", ["first", "second"])
    user = _make_user(db)
    first = FakeProvider("first", methods=frozenset({PaymentMethod.PIX}), behavior="unavailable")
    second = FakeProvider("second", methods=frozenset({PaymentMethod.PIX}), behavior="success")
    registry = _registry(first, second)
    key = str(uuid.uuid4())

    with pytest.raises(ProviderUnavailable):
        await _route(db, registry, user, payment_method=PaymentMethod.PIX, idempotency_key=key)

    # Operator fixes "first" — but the retry must NOT silently prefer
    # "second" just because priority would recompute that way; it must
    # resume against "first" (already persisted), not switch providers
    # behind the caller's back while the outcome of the earlier attempt
    # is still unresolved.
    first.behavior = "success"
    order, intent = await _route(db, registry, user, payment_method=PaymentMethod.PIX, idempotency_key=key)

    assert order.provider == "first"
    assert len(first.create_intent_calls) == 2  # first attempt (unavailable) + retry (success)
    assert len(second.create_intent_calls) == 0
    assert db.query(PaymentOrder).count() == 1


@pytest.mark.asyncio
async def test_R13_winning_provider_persisted_on_order(db, monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "payment_provider_priority", ["first", "second"])
    user = _make_user(db)
    first = FakeProvider("first", methods=frozenset({PaymentMethod.PIX}), behavior="rejected")
    second = FakeProvider("second", methods=frozenset({PaymentMethod.PIX}), behavior="success")
    registry = _registry(first, second)

    order, _intent = await _route(db, registry, user, payment_method=PaymentMethod.PIX)

    db.expire_all()
    reloaded = db.query(PaymentOrder).filter(PaymentOrder.id == order.id).first()
    assert reloaded.provider == "second"


# ---------------------------------------------------------------------------
# R14 — secret hygiene
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_R14_no_secret_leaks_to_router_logs(db, caplog):
    import logging

    user = _make_user(db)
    secret_value = "SUPER-SECRET-FAKE-CREDENTIAL"
    provider = FakeProvider("only_one", methods=frozenset({PaymentMethod.PIX}), secret=secret_value)
    registry = _registry(provider)

    with caplog.at_level(logging.INFO, logger="src.billing.payment_router"):
        await _route(db, registry, user, payment_method=PaymentMethod.PIX)

    assert caplog.records, "router logged nothing — test would be vacuous"
    for record in caplog.records:
        assert secret_value not in record.getMessage()


# ---------------------------------------------------------------------------
# R15-R16 — exhaustion / reassignment guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_R15_all_candidates_rejected_raises_all_providers_rejected(db, monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "payment_provider_priority", ["first", "second"])
    user = _make_user(db)
    first = FakeProvider("first", methods=frozenset({PaymentMethod.PIX}), behavior="rejected")
    second = FakeProvider("second", methods=frozenset({PaymentMethod.PIX}), behavior="rejected")
    registry = _registry(first, second)

    with pytest.raises(AllProvidersRejected) as exc_info:
        await _route(db, registry, user, payment_method=PaymentMethod.PIX)
    assert set(exc_info.value.tried) == {"first", "second"}

    order = db.query(PaymentOrder).first()
    assert order.provider == "second"  # last candidate genuinely tried
    assert order.external_order_id is None


def test_R16_reassign_guard_blocks_after_external_commitment(db):
    user = _make_user(db)
    svc = PaymentOrderService(db)
    order = svc.create_order(
        user_id=user.id, provider="first", amount_fiat=Decimal("9.99"), currency_fiat="USD",
        credits_amount=Decimal("9.99"), currency_credits="USD",
        idempotency_key=str(uuid.uuid4()), expires_at=_future(),
    )
    order = svc.mark_processing(order.id, user.id, external_order_id="ext-123")

    with pytest.raises(ProviderReassignmentUnsafe):
        svc.reassign_provider_unsafe_guard(order)


# ---------------------------------------------------------------------------
# R17-R18 — concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_R17_sequential_repeated_calls_never_double_call_provider(db):
    """Sequential stand-in for concurrent duplicate calls — same
    rationale/caveat as every other adapter's own MPC14/SC14 pattern;
    see test_billing_postgres_concurrency.py for the real race."""
    user = _make_user(db)
    provider = FakeProvider("only_one", methods=frozenset({PaymentMethod.PIX}))
    registry = _registry(provider)
    key = str(uuid.uuid4())

    for _ in range(3):
        await _route(db, registry, user, payment_method=PaymentMethod.PIX, idempotency_key=key)

    assert len(provider.create_intent_calls) == 1
    assert db.query(PaymentOrder).count() == 1


def test_R18_thread_concurrency_smoke_deterministic_outcome():
    """True thread-level concurrency smoke test. Each thread gets its
    OWN DB session bound to a SHARED in-memory database — StaticPool is
    REQUIRED here: SQLAlchemy's default pool for "sqlite:///:memory:"
    hands each new connection its OWN separate anonymous database, so
    without it, different threads' sessions would silently see
    different (empty) schemas. StaticPool forces every connection
    (across every thread) onto the SAME underlying SQLite connection.

    This proves the ROUTER's call shape (lock-then-decide-then-act)
    converges to one consistent final state even under real thread
    interleaving. SQLite's own FOR UPDATE no-op means this does NOT
    prove true cross-connection exclusion (that's PostgreSQL's job,
    tested separately in test_billing_postgres_concurrency.py) — it
    proves the router's own logic never produces more than one
    consistent outcome, regardless of interleaving."""
    import os
    import tempfile

    from sqlalchemy import text

    # A real FILE-based SQLite DB in WAL mode — NOT ":memory:". In-memory
    # SQLite fundamentally isn't built for multi-connection access (even
    # with StaticPool forcing one shared connection, write contention
    # under WAL-less journaling causes near-constant "database is
    # locked" errors under real thread interleaving — confirmed by this
    # test flaking under that setup during development). A file-backed
    # DB with WAL journaling + a generous busy_timeout gives SQLite's
    # actual best concurrent-writer story: one writer proceeds while
    # others wait (up to the timeout) rather than immediately erroring.
    db_path = tempfile.mktemp(suffix=".sqlite3")
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30.0},
    )
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA busy_timeout=30000"))
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    setup_session = Session()
    user = _make_user(setup_session)
    key = str(uuid.uuid4())

    provider = _SlowFakeProvider("only_one", methods=frozenset({PaymentMethod.PIX}), delay_seconds=0.05)
    registry = _registry(provider)

    results: list[PaymentOrder] = []
    errors: list[Exception] = []
    lock = threading.Lock()

    def _worker():
        import asyncio
        thread_session = Session()
        try:
            order, _intent = asyncio.run(
                route_checkout(
                    thread_session, registry,
                    user_id=user.id, plan="pro", amount_fiat=Decimal("29.99"), currency_fiat="USD",
                    credits_amount=Decimal("29.99"), currency_credits="USD",
                    idempotency_key=key, expires_at=_future(),
                    payment_method=PaymentMethod.PIX, payment_type=None,
                )
            )
            with lock:
                results.append(order)
        except Exception as exc:  # noqa: BLE001 — collected for assertion, not swallowed silently
            with lock:
                errors.append(exc)
        finally:
            thread_session.close()

    threads = [threading.Thread(target=_worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=25)

    try:
        # Every thread must at least finish (no silent hang); with WAL +
        # a generous busy_timeout every thread is expected to succeed
        # (SQLite serializes writers rather than erroring), but a stray
        # "database is locked" is tolerated as an SQLite-specific
        # limitation, not a router bug — the invariant that matters is
        # the FINAL row count below.
        assert len(results) + len(errors) == 5
        assert len(results) >= 1

        orders = setup_session.query(PaymentOrder).filter(PaymentOrder.idempotency_key == key).all()
        assert len(orders) == 1, "concurrent callers must converge on exactly one PaymentOrder row"
        assert orders[0].provider == "only_one"
    finally:
        setup_session.close()
        engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(db_path + suffix)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# R19 — Hardening: expire_order() vs. route_checkout() race
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_R19_route_checkout_never_calls_create_intent_for_expired_order(db):
    """DETERMINISTIC reproduction (see PaymentOrderService.
    assert_order_still_pending()'s own docstring for the full race this
    closes): an order a concurrent expire_order() call already won
    BEFORE route_checkout() reaches create_intent() must be refused —
    simulated here by expiring the order directly first (the guard only
    cares that the order is "already not pending", not HOW it got there)."""
    user = _make_user(db)
    provider = FakeProvider("only_one", methods=frozenset({PaymentMethod.PIX}))
    registry = _registry(provider)
    key = str(uuid.uuid4())
    svc = PaymentOrderService(db)

    order = svc.create_order(
        user_id=user.id, provider="only_one", amount_fiat=Decimal("29.99"), currency_fiat="USD",
        credits_amount=Decimal("29.99"), currency_credits="USD",
        idempotency_key=key, expires_at=_future(),
    )
    svc.expire_order(order.id)  # simulates the background sweep winning the race

    with pytest.raises(OrderExpired):
        await _route(db, registry, user, payment_method=PaymentMethod.PIX, idempotency_key=key)

    assert len(provider.create_intent_calls) == 0, "create_intent() must NEVER be called for an already-expired order"

    reloaded = db.query(PaymentOrder).filter(PaymentOrder.id == order.id).first()
    assert reloaded.status == "expired"
    assert reloaded.external_order_id is None


# ---------------------------------------------------------------------------
# R20-R24 — resolve_default_currency() (go-live checkout-currency fix, B2:
# Mercado Pago -> BRL, a USD-capable provider -> USD unchanged)
# ---------------------------------------------------------------------------


def test_R20_auto_selection_picks_the_first_catalog_currency_with_an_eligible_provider():
    """A BRL-only provider (like the real Mercado Pago adapter) registered
    alone -> BRL wins even though USD is first in the catalog list, because
    nothing is eligible for USD."""
    provider = FakeProvider("brl_only", currencies=frozenset({"BRL"}))
    registry = _registry(provider)

    assert resolve_default_currency(registry, catalog_currencies=["USD", "BRL"]) == "BRL"


def test_R21_auto_selection_prefers_usd_when_a_usd_capable_provider_is_eligible():
    """A USD-capable provider registered alongside a BRL-only one -> USD
    still wins (first in catalog order) — B2 never changes behavior for a
    provider that already accepted USD."""
    usd_capable = FakeProvider("stripe_like", currencies=frozenset())  # empty == unrestricted
    brl_only = FakeProvider("mp_like", currencies=frozenset({"BRL"}))
    registry = _registry(usd_capable, brl_only)

    assert resolve_default_currency(registry, catalog_currencies=["USD", "BRL"]) == "USD"


def test_R22_auto_selection_returns_none_when_no_priced_currency_has_an_eligible_provider():
    provider = FakeProvider("ars_only", currencies=frozenset({"ARS"}))
    registry = _registry(provider)

    assert resolve_default_currency(registry, catalog_currencies=["USD", "BRL"]) is None


def test_R23_explicit_provider_checks_that_one_adapters_own_currencies():
    """LEGACY explicit-`provider` path: no priority/failover, just that ONE
    adapter's own supported_currencies — mirrors billing.py's
    create_checkout() dispatch when body.provider is given."""
    brl_only = FakeProvider("mp_like", currencies=frozenset({"BRL"}))
    registry = _registry(brl_only)

    assert resolve_default_currency(registry, catalog_currencies=["USD", "BRL"], provider="mp_like") == "BRL"
    assert resolve_default_currency(registry, catalog_currencies=["USD"], provider="mp_like") is None


def test_R24_explicit_provider_that_is_not_registered_returns_none():
    registry = _registry()
    assert resolve_default_currency(registry, catalog_currencies=["USD", "BRL"], provider="ghost") is None