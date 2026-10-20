"""Unit tests for Billing-2 schema additions.

Test matrix
-----------
S01) RefundOrderRequest.idempotency_key is optional; None accepted
S02) RefundOrderRequest.idempotency_key accepts up to 128 chars
S03) RefundOrderRequest.idempotency_key > 128 chars → ValidationError
S04) WebhookEventResponse serializes correctly with optional fields
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.schemas.billing import RefundOrderRequest, WebhookEventResponse


def test_S01_idempotency_key_is_optional_defaults_to_none():
    """RefundOrderRequest without idempotency_key is valid; field defaults to None."""
    req = RefundOrderRequest(amount=Decimal("5.00"))
    assert req.idempotency_key is None
    assert req.amount == Decimal("5.00")


def test_S02_idempotency_key_accepts_128_chars():
    """idempotency_key at max length (128 chars) is accepted."""
    key = "k" * 128
    req = RefundOrderRequest(amount=Decimal("1.00"), idempotency_key=key)
    assert req.idempotency_key == key


def test_S03_idempotency_key_over_128_chars_raises_validation_error():
    """idempotency_key with 129+ chars raises ValidationError."""
    with pytest.raises(ValidationError):
        RefundOrderRequest(amount=Decimal("1.00"), idempotency_key="k" * 129)


def test_S04_webhook_event_response_serializes_with_optional_fields():
    """WebhookEventResponse serializes correctly; optional fields may be None."""
    resp = WebhookEventResponse(
        received=True,
        provider="test_provider",
        event_type="payment.confirmed",
        order_id="order-123",
        new_status="confirmed",
    )
    d = resp.model_dump()
    assert d["received"] is True
    assert d["provider"] == "test_provider"
    assert d["order_id"] == "order-123"
    assert d["new_status"] == "confirmed"

    # Optional fields may be absent (None)
    minimal = WebhookEventResponse(received=True, provider="p", event_type="e")
    assert minimal.order_id is None
    assert minimal.new_status is None
