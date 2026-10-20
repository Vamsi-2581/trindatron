"""Integration tests for webhook replay protection over real HTTP —
Production Payment Activation hardening.

Mirrors test_billing_webhook.py's own FakeAdapter/registry-install
pattern (kept local/self-contained rather than importing across test
files, matching this codebase's own convention) — extended with a
`nonce` on the returned WebhookEvent, which test_billing_webhook.py's
existing tests never set (nonce defaults to None there, so none of them
exercise this mechanism — verified by re-running that whole file
unmodified after this phase's changes; all pass).

Test matrix
-----------
I01) The exact same webhook delivery (same nonce) sent TWICE ->
     confirm_order() is only genuinely invoked once (proven by a call-
     count spy, not just relying on confirm_order()'s own pre-existing
     idempotency) — exactly one WebhookNonce row, exactly one ledger
     entry pair.
I02) The same delivery sent TEN times -> still exactly one confirmation,
     one nonce row.
I03) POST /billing/webhook/{provider} is rate-limited per client IP —
     mission requirement 12, generous enough to never block a handful of
     legitimate retries.
I04) Crash-safety: if confirm_order() raises an unexpected error on the
     FIRST delivery, no nonce is recorded — a retried (identical) delivery
     is NOT short-circuited and is genuinely reprocessed, succeeding.
I05) Two DIFFERENT nonces for the SAME order (e.g. a PROCESSING event
     then a CONFIRMED event) are never confused with each other — nonce
     scoping is per-delivery, not per-order.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Mapping

import pytest

from src.api.deps import get_provider_registry
from src.billing.providers.base import (
    PaymentIntent, PaymentProvider, PaymentResult, PaymentStatus, WebhookEvent,
)
from src.billing.providers.registry import ProviderRegistry
from src.main import app as _app
from src.models.ledger import LedgerEntry
from src.models.webhook_nonce import WebhookNonce
from src.services.payment_order_service import PaymentOrderService


class _FakeReplayAdapter(PaymentProvider):
    _name = "test_provider"

    def __init__(self) -> None:
        self._response: WebhookEvent | None = None

    def configure(self, response: WebhookEvent) -> None:
        self._response = response

    @property
    def provider_name(self) -> str:
        return self._name

    async def verify_webhook(self, payload: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        return self._response

    async def create_intent(self, **kwargs) -> PaymentIntent:
        raise NotImplementedError

    async def get_intent(self, intent_id: str) -> PaymentIntent:
        raise NotImplementedError

    async def refund(self, payment_id: str, **kwargs) -> PaymentResult:
        raise NotImplementedError


def _install_registry(adapter: _FakeReplayAdapter) -> ProviderRegistry:
    reg = ProviderRegistry()
    reg.register(adapter)
    _app.dependency_overrides[get_provider_registry] = lambda: reg
    return reg


@pytest.fixture(autouse=True)
def _cleanup_registry():
    yield
    _app.dependency_overrides.pop(get_provider_registry, None)


@pytest.fixture(autouse=True)
def _reset_webhook_rate_limiter():
    """Module-level singleton (deliberately — see billing.py's own
    docstring) — reset between tests so one test's requests never count
    against another's budget."""
    from src.api.routes import billing as billing_routes
    from src.services.request_control import InMemoryRateLimiter
    billing_routes._webhook_rate_limiter = InMemoryRateLimiter()
    yield


def _make_order_in_processing(db, user_id: str, *, ext_ref: str = "prov-ref-001") -> "PaymentOrder":  # noqa: F821
    from src.models.payment_order import PaymentOrder

    svc = PaymentOrderService(db)
    order = svc.create_order(
        user_id=user_id, provider="test_provider",
        amount_fiat=Decimal("10.00"), currency_fiat="USD",
        credits_amount=Decimal("10.00"), currency_credits="USD",
        idempotency_key=str(uuid.uuid4()), expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    svc.mark_processing(order.id, user_id, external_order_id=ext_ref)
    db.expire_all()
    return db.query(PaymentOrder).filter(PaymentOrder.id == order.id).first()


def _confirmed_event(*, nonce: str, provider_ref: str = "prov-ref-001") -> WebhookEvent:
    return WebhookEvent(
        provider="test_provider", event_type="payment.confirmed", payment_id=None,
        provider_ref=provider_ref, status=PaymentStatus.CONFIRMED,
        amount=Decimal("10.00"), currency="USD", raw_payload={}, nonce=nonce,
    )


def _post_webhook(client, *, provider: str = "test_provider") -> object:
    return client.post(f"/billing/webhook/{provider}", content=b'{"event":"test"}', headers={"X-Signature": "sig"})


def test_I01_same_nonce_delivered_twice_confirms_once(client, db, registered_user, monkeypatch):
    order = _make_order_in_processing(db, registered_user["user_id"])
    adapter = _FakeReplayAdapter()
    _install_registry(adapter)

    call_count = {"n": 0}
    real_confirm = PaymentOrderService.confirm_order

    def _counting_confirm(self, *a, **k):
        call_count["n"] += 1
        return real_confirm(self, *a, **k)

    monkeypatch.setattr(PaymentOrderService, "confirm_order", _counting_confirm)

    adapter.configure(_confirmed_event(nonce="nonce-001"))
    first = _post_webhook(client)
    adapter.configure(_confirmed_event(nonce="nonce-001"))
    second = _post_webhook(client)

    assert first.status_code == 200 and second.status_code == 200
    assert call_count["n"] == 1  # confirm_order() genuinely invoked only ONCE

    db.expire_all()
    assert db.query(WebhookNonce).filter(WebhookNonce.nonce == "nonce-001").count() == 1
    assert db.query(LedgerEntry).filter(LedgerEntry.reference_id == order.id).count() == 2  # exactly one pair


def test_I02_same_nonce_delivered_ten_times_confirms_once(client, db, registered_user, monkeypatch):
    _make_order_in_processing(db, registered_user["user_id"])
    adapter = _FakeReplayAdapter()
    _install_registry(adapter)

    call_count = {"n": 0}
    real_confirm = PaymentOrderService.confirm_order

    def _counting_confirm(self, *a, **k):
        call_count["n"] += 1
        return real_confirm(self, *a, **k)

    monkeypatch.setattr(PaymentOrderService, "confirm_order", _counting_confirm)

    for _ in range(10):
        adapter.configure(_confirmed_event(nonce="nonce-repeat"))
        resp = _post_webhook(client)
        assert resp.status_code == 200

    assert call_count["n"] == 1
    db.expire_all()
    assert db.query(WebhookNonce).filter(WebhookNonce.nonce == "nonce-repeat").count() == 1


def test_I03_webhook_rate_limited(client, monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "billing_webhook_rate_limit", 3)
    adapter = _FakeReplayAdapter()
    _install_registry(adapter)
    adapter.configure(WebhookEvent(
        provider="test_provider", event_type="unknown_type_absorbed", payment_id=None,
        provider_ref=None, status=None, amount=None, currency=None, raw_payload={},
    ))

    for _ in range(3):
        resp = _post_webhook(client)
        assert resp.status_code == 200

    resp = _post_webhook(client)
    assert resp.status_code == 429


def test_I04_crash_safety_no_nonce_recorded_on_failure_allows_retry(client, db, registered_user, monkeypatch):
    """If confirm_order() raises an UNEXPECTED error on the first
    delivery, the nonce must NOT be recorded — a subsequent, identical
    retry must genuinely reprocess (not be short-circuited) and succeed."""
    _make_order_in_processing(db, registered_user["user_id"])
    adapter = _FakeReplayAdapter()
    _install_registry(adapter)

    real_confirm = PaymentOrderService.confirm_order
    call_state = {"n": 0}

    def _fail_once_then_succeed(self, *a, **k):
        call_state["n"] += 1
        if call_state["n"] == 1:
            raise RuntimeError("simulated crash mid-processing")
        return real_confirm(self, *a, **k)

    monkeypatch.setattr(PaymentOrderService, "confirm_order", _fail_once_then_succeed)

    adapter.configure(_confirmed_event(nonce="nonce-crash"))
    with pytest.raises(RuntimeError):
        _post_webhook(client)  # the route lets an unexpected exception propagate (500), not absorbed

    db.expire_all()
    assert db.query(WebhookNonce).filter(WebhookNonce.nonce == "nonce-crash").count() == 0  # NOT recorded

    # Retry — same nonce, now genuinely reprocessed (not skipped) and succeeds.
    adapter.configure(_confirmed_event(nonce="nonce-crash"))
    resp = _post_webhook(client)
    assert resp.status_code == 200
    assert call_state["n"] == 2  # confirm_order really was invoked again

    db.expire_all()
    assert db.query(WebhookNonce).filter(WebhookNonce.nonce == "nonce-crash").count() == 1


def test_I05_different_nonces_for_same_order_not_confused(client, db, registered_user):
    order = _make_order_in_processing(db, registered_user["user_id"])
    adapter = _FakeReplayAdapter()
    _install_registry(adapter)

    adapter.configure(_confirmed_event(nonce="nonce-a"))
    first = _post_webhook(client)
    assert first.status_code == 200

    # A second, genuinely different delivery (different nonce) for the
    # SAME order — confirm_order()'s own idempotent fast-path handles
    # this (order already confirmed), but critically it must NOT be
    # short-circuited by replay detection (different nonce = not a replay).
    adapter.configure(_confirmed_event(nonce="nonce-b"))
    second = _post_webhook(client)
    assert second.status_code == 200

    db.expire_all()
    nonces = {row.nonce for row in db.query(WebhookNonce).filter(WebhookNonce.provider == "test_provider").all()}
    assert nonces == {"nonce-a", "nonce-b"}


def test_I06_adapter_raising_unexpected_error_is_rejected_safely(client, db, registered_user):
    """A malformed/unparseable payload that somehow reaches the adapter
    (e.g. it passed whatever transport-level check but is not valid for
    THIS adapter's own parsing) must be rejected with a safe 400 — never
    a raw 500, and never anything confirmed/activated."""
    order = _make_order_in_processing(db, registered_user["user_id"])

    class _BrokenAdapter(_FakeReplayAdapter):
        async def verify_webhook(self, payload, headers):
            raise ValueError("simulated malformed payload the adapter could not parse")

    _install_registry(_BrokenAdapter())
    resp = _post_webhook(client)

    assert resp.status_code == 400
    db.expire_all()
    from src.models.payment_order import PaymentOrder
    refreshed = db.query(PaymentOrder).filter(PaymentOrder.id == order.id).first()
    assert refreshed.status == "processing"  # untouched
