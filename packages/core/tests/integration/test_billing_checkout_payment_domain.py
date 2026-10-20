"""Integration tests — Payments Foundation etapa (2026-09-07), the HTTP
surface of POST /billing/checkout's new optional fields
(payment_method, payment_type, currency) and provider resolution when
`provider` is omitted.

Everything already covered by test_billing_checkout.py (CO01-CO15) is
NOT re-tested here — this file covers only what changed: currency
selection, and (as of the Payment Router etapa, 2026-09-08) provider
auto-selection. CD06 was updated in that later etapa: two-or-more
eligible candidates no longer 400s as "ambiguous" — the Payment Router
(src/billing/payment_router.py) now resolves deterministically by
priority. Full Payment Router coverage (failover, concurrency,
eligibility reasons, persisted-provider semantics) lives in
test_payment_router.py/test_billing_checkout_router.py — not duplicated
here.

Test matrix
-----------
CD01) provider explicit (LEGACY) + payment_method/payment_type given ->
      unchanged existing dispatch; new fields persisted on the order.
CD02) currency omitted -> defaults to USD, identical to pre-existing
      behavior (amount_fiat/currency_fiat unchanged).
CD03) currency given but unpriced for the plan -> 404.
CD04) provider omitted, payment_method matches exactly ONE registered
      provider's declared capability -> auto-resolved, dispatches to
      that provider's create_intent().
CD05) provider omitted, payment_method matches ZERO registered
      providers -> 409 (never silently falls back to a local order).
CD06) provider omitted, payment_method matches MORE THAN ONE registered
      provider -> resolved DETERMINISTICALLY (Payment Router priority;
      neither test candidate is in the priority list, so alphabetical
      tiebreak applies) — no longer a 400.
CD07) provider AND payment_method both omitted, exactly one provider
      registered total -> resolved to that provider.
CD08) provider AND payment_method both omitted, zero providers
      registered -> 409 (the honest production-today answer).
CD09) Regression: an explicit, unregistered provider string still takes
      the pre-existing local-only-order fallback (same as CO06) — the
      refactor did not change LEGACY behavior.
CD10) GET-equivalent — PaymentOrderResponse for a legacy-shaped order
      (no payment_method/type/subscription_id) round-trips those fields
      as None, not an error.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Mapping

import pytest

from src.api.deps import get_provider_registry
from src.billing.providers.base import (
    PaymentIntent,
    PaymentMethod,
    PaymentProvider,
    PaymentResult,
    PaymentStatus,
    PaymentType,
    WebhookEvent,
)
from src.billing.providers.registry import ProviderRegistry
from src.main import app as _app
from src.services.request_control import InMemoryRateLimiter
from src.billing.pricing import PLAN_PRICES
# Amounts below come from the catalog rather than from literals: what these
# tests check is that an order, a webhook or an invoice carries the SAME
# price the server published, and a repeated literal quietly stops testing
# that the moment prices move. The prices themselves are argued for in
# tests/unit/test_plan_economics.py.


class _CapableFakeAdapter(PaymentProvider):
    """Same minimal shape as test_billing_checkout.py's
    _FakeCheckoutAdapter, plus configurable declared capabilities — the
    one thing this file actually needs to exercise."""

    def __init__(
        self,
        name: str,
        *,
        methods: frozenset[PaymentMethod] = frozenset(),
        types: frozenset[PaymentType] = frozenset(),
    ) -> None:
        self._name = name
        self._methods = methods
        self._types = types

    @property
    def provider_name(self) -> str:
        return self._name

    @property
    def supported_payment_methods(self) -> frozenset[PaymentMethod]:
        return self._methods

    @property
    def supported_payment_types(self) -> frozenset[PaymentType]:
        return self._types

    async def create_intent(self, *, amount, currency, user_id, idempotency_key, metadata=None):
        return PaymentIntent(
            id=f"ext-{idempotency_key}",
            provider=self.provider_name,
            amount=amount,
            currency=currency,
            status=PaymentStatus.PENDING,
            idempotency_key=idempotency_key,
        )

    async def get_intent(self, intent_id: str):
        raise NotImplementedError

    async def verify_webhook(self, payload: bytes, headers: Mapping[str, str]) -> WebhookEvent:
        raise NotImplementedError

    async def refund(self, payment_id: str, **kwargs) -> PaymentResult:
        raise NotImplementedError


def _install_registry(*adapters: PaymentProvider) -> ProviderRegistry:
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
def _reset_order_rate_limiter():
    from src.api.routes import billing as billing_routes
    billing_routes._order_rate_limiter = InMemoryRateLimiter()
    yield


def _body(**overrides) -> dict:
    body = {"plan": "pro", "idempotency_key": str(uuid.uuid4())}
    body.update(overrides)
    return body


def test_CD01_legacy_provider_with_payment_method_type(client, auth_headers):
    _install_registry(_CapableFakeAdapter("fake_provider", methods=frozenset({PaymentMethod.PIX})))
    resp = client.post(
        "/billing/checkout",
        json=_body(provider="fake_provider", payment_method="pix", payment_type="recurring"),
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    order = resp.json()["order"]
    assert order["payment_method"] == "pix"
    assert order["payment_type"] == "recurring"
    assert order["provider"] == "fake_provider"


def test_CD02_currency_omitted_defaults_to_usd(client, auth_headers):
    _install_registry()
    resp = client.post("/billing/checkout", json=_body(provider="none"), headers=auth_headers)
    assert resp.status_code == 201, resp.text
    order = resp.json()["order"]
    assert order["currency_fiat"] == "USD"
    assert Decimal(order["amount_fiat"]) == PLAN_PRICES["pro"]["USD"]


def test_CD03_unpriced_currency_returns_404(client, auth_headers):
    # BRL added 2026-09-24 (go-live checkout-currency fix) — EUR remains
    # genuinely unpriced for "pro", so it is what this test now owns.
    _install_registry()
    resp = client.post("/billing/checkout", json=_body(provider="none", currency="EUR"), headers=auth_headers)
    assert resp.status_code == 404


def test_CD04_provider_omitted_resolves_single_capable_candidate(client, auth_headers):
    _install_registry(
        _CapableFakeAdapter("pix_provider", methods=frozenset({PaymentMethod.PIX})),
        _CapableFakeAdapter("crypto_provider", methods=frozenset({PaymentMethod.CRYPTO})),
    )
    resp = client.post("/billing/checkout", json=_body(payment_method="pix"), headers=auth_headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["order"]["provider"] == "pix_provider"


def test_CD05_provider_omitted_zero_candidates_returns_409(client, auth_headers):
    _install_registry(_CapableFakeAdapter("crypto_provider", methods=frozenset({PaymentMethod.CRYPTO})))
    resp = client.post("/billing/checkout", json=_body(payment_method="pix"), headers=auth_headers)
    assert resp.status_code == 409


def test_CD06_provider_omitted_multiple_eligible_resolves_deterministically(client, auth_headers):
    """Payment Router etapa: two-or-more eligible candidates no longer
    400 as "ambiguous" — the router picks deterministically (priority,
    then alphabetical for unranked names — see payment_router.py's own
    _priority_index()). Full priority-ordering coverage lives in
    test_payment_router.py; this only proves the HTTP surface no longer
    errors out."""
    _install_registry(
        _CapableFakeAdapter("pix_provider_a", methods=frozenset({PaymentMethod.PIX})),
        _CapableFakeAdapter("pix_provider_b", methods=frozenset({PaymentMethod.PIX})),
    )
    resp = client.post("/billing/checkout", json=_body(payment_method="pix"), headers=auth_headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["order"]["provider"] in ("pix_provider_a", "pix_provider_b")


def test_CD07_everything_omitted_single_provider_registered(client, auth_headers):
    _install_registry(_CapableFakeAdapter("only_provider"))
    resp = client.post("/billing/checkout", json=_body(), headers=auth_headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["order"]["provider"] == "only_provider"


def test_CD08_everything_omitted_zero_providers_returns_409(client, auth_headers):
    _install_registry()
    resp = client.post("/billing/checkout", json=_body(), headers=auth_headers)
    assert resp.status_code == 409


def test_CD09_explicit_unregistered_provider_still_local_only(client, auth_headers):
    """Regression: LEGACY explicit-provider dispatch is unchanged by the
    resolver refactor — same as test_billing_checkout.py's CO06."""
    _install_registry()
    resp = client.post(
        "/billing/checkout", json=_body(provider="nonexistent"), headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    order = resp.json()["order"]
    assert order["status"] == "pending"
    assert order["provider"] == "nonexistent"


def test_CD10_legacy_shaped_order_response_has_none_new_fields(client, auth_headers):
    _install_registry()
    resp = client.post("/billing/checkout", json=_body(provider="none"), headers=auth_headers)
    assert resp.status_code == 201, resp.text
    order = resp.json()["order"]
    assert order["payment_method"] is None
    assert order["payment_type"] is None
    assert order["subscription_id"] is None
