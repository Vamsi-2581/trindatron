"""Unit tests for ProviderRegistry — Billing-2.

Test matrix
-----------
P01) register() stores adapter by provider_name
P02) get() returns the correct adapter
P03) get() raises ProviderNotFound for unknown name
P04) is_registered() returns True for registered, False otherwise
P05) names() returns sorted list; register() with same name replaces adapter
"""
from __future__ import annotations

import pytest

from src.billing.providers.base import (
    PaymentIntent,
    PaymentProvider,
    PaymentResult,
    WebhookEvent,
)
from src.billing.providers.registry import ProviderNotFound, ProviderRegistry


# ---------------------------------------------------------------------------
# Minimal fake adapter
# ---------------------------------------------------------------------------


class _FakeAdapter(PaymentProvider):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def provider_name(self) -> str:
        return self._name

    async def verify_webhook(self, payload, signature) -> WebhookEvent:
        raise NotImplementedError

    async def create_intent(self, **kwargs) -> PaymentIntent:
        raise NotImplementedError

    async def get_intent(self, intent_id) -> PaymentIntent:
        raise NotImplementedError

    async def refund(self, payment_id, **kwargs) -> PaymentResult:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_P01_register_stores_adapter():
    """register() makes the adapter retrievable by provider_name."""
    reg = ProviderRegistry()
    adapter = _FakeAdapter("stripe")
    reg.register(adapter)
    assert reg.is_registered("stripe")


def test_P02_get_returns_registered_adapter():
    """get() returns the exact adapter instance that was registered."""
    reg = ProviderRegistry()
    adapter = _FakeAdapter("binance_pay")
    reg.register(adapter)
    assert reg.get("binance_pay") is adapter


def test_P03_get_raises_provider_not_found_for_unknown_name():
    """get() raises ProviderNotFound with the unknown name in the exception."""
    reg = ProviderRegistry()
    with pytest.raises(ProviderNotFound) as exc_info:
        reg.get("does_not_exist")
    assert exc_info.value.name == "does_not_exist"


def test_P04_is_registered_true_and_false():
    """is_registered() returns True for registered names, False otherwise."""
    reg = ProviderRegistry()
    reg.register(_FakeAdapter("paypal"))
    assert reg.is_registered("paypal") is True
    assert reg.is_registered("stripe") is False


def test_P05_names_sorted_and_register_replaces_existing():
    """names() returns a sorted list; re-registering the same name replaces the adapter."""
    reg = ProviderRegistry()
    a1 = _FakeAdapter("stripe")
    a2 = _FakeAdapter("stripe")  # same name, different instance
    a3 = _FakeAdapter("binance_pay")

    reg.register(a1)
    reg.register(a3)
    assert reg.names() == ["binance_pay", "stripe"]

    # Re-register stripe → replaces a1 with a2
    reg.register(a2)
    assert reg.get("stripe") is a2
    assert len(reg.names()) == 2
