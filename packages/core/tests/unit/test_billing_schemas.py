"""Unit tests for billing Pydantic schemas — Billing-1 / Passo 6.

Schema validation is pure Python — no database required.

Test matrix
-----------
S1)  CreateOrderRequest — valid full payload accepted.
S2)  CreateOrderRequest.expires_at (past)  → ValidationError.
S3)  CreateOrderRequest.expires_at (naive) → normalized to UTC-aware.
S4)  CreateOrderRequest.credits_amount (zero / negative) → ValidationError.
S5)  CreateOrderRequest.amount_fiat (zero / negative) → ValidationError.
S6)  CreateOrderRequest.idempotency_key (empty string / > 128 chars) → ValidationError.
S7)  CreateOrderRequest.provider (empty string) → ValidationError.
S8)  CreateOrderRequest.currency_fiat / currency_credits (> 10 chars) → ValidationError.
S9)  RefundOrderRequest — valid payload accepted.
S10) RefundOrderRequest.amount (zero / negative) → ValidationError.
S11) RefundOrderRequest.note (> 500 chars) → ValidationError.
S12) PaymentOrderResponse — constructed from dict, all expected fields present.
S13) PaymentOrderResponse — webhook_raw field intentionally absent from model.
S14) PaymentOrderResponse — constructed via from_attributes (ORM-compatible).
S15) PaymentOrderListResponse — orders list and total correct.
S16) CreditBalanceResponse — fields and Decimal precision correct.
S17) UserCreditSummaryResponse — empty balances list accepted.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.schemas.billing import (
    CreateOrderRequest,
    CreditBalanceResponse,
    PaymentOrderListResponse,
    PaymentOrderResponse,
    RefundOrderRequest,
    UserCreditSummaryResponse,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _future(seconds: int = 3600) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _past(seconds: int = 1) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


def _valid_create_kwargs(**overrides) -> dict:
    """Return a valid CreateOrderRequest payload with overrides applied."""
    base = dict(
        provider="test_provider",
        amount_fiat=Decimal("10.00"),
        currency_fiat="USD",
        credits_amount=Decimal("10.00"),
        currency_credits="USD",
        idempotency_key="idem-key-abc",
        expires_at=_future(),
    )
    base.update(overrides)
    return base


def _order_response_dict(**overrides) -> dict:
    """Return a dict that can build a valid PaymentOrderResponse."""
    base = dict(
        id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        provider="test_provider",
        external_order_id=None,
        status="pending",
        amount_fiat=Decimal("10.00"),
        currency_fiat="USD",
        credits_amount=Decimal("10.00"),
        currency_credits="USD",
        idempotency_key="idem-key-abc",
        expires_at=_future(),
        confirmed_at=None,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# S1 — CreateOrderRequest: valid full payload
# ---------------------------------------------------------------------------


def test_S1_create_order_request_valid_payload():
    req = CreateOrderRequest(**_valid_create_kwargs())

    assert req.provider == "test_provider"
    assert req.amount_fiat == Decimal("10.00")
    assert req.credits_amount == Decimal("10.00")
    assert req.idempotency_key == "idem-key-abc"
    # expires_at is always UTC-aware after validation
    assert req.expires_at.tzinfo is not None


# ---------------------------------------------------------------------------
# S2 — CreateOrderRequest.expires_at (past) → ValidationError
# ---------------------------------------------------------------------------


def test_S2_create_order_expires_at_past_raises():
    with pytest.raises(ValidationError) as exc_info:
        CreateOrderRequest(**_valid_create_kwargs(expires_at=_past(10)))

    errors = exc_info.value.errors()
    assert any("expires_at" in str(e.get("loc", "")) for e in errors)


# ---------------------------------------------------------------------------
# S3 — CreateOrderRequest.expires_at (naive) → rejected with clear error
# ---------------------------------------------------------------------------
#
# Design decision: financial APIs must require timezone-aware datetimes.
# Silently interpreting a naive datetime as UTC causes billing errors when
# clients are in non-UTC timezones.  Rejecting forces explicitness.


def test_S3_create_order_expires_at_naive_raises_with_timezone_message():
    naive_future = datetime.now() + timedelta(hours=2)  # no tzinfo
    assert naive_future.tzinfo is None

    with pytest.raises(ValidationError) as exc_info:
        CreateOrderRequest(**_valid_create_kwargs(expires_at=naive_future))

    errors = exc_info.value.errors()
    assert any("expires_at" in str(e.get("loc", "")) for e in errors)
    # Error message must mention 'timezone' to help the caller fix the issue
    all_messages = " ".join(str(e.get("msg", "")) for e in errors)
    assert "timezone" in all_messages.lower(), (
        f"Expected 'timezone' in error message, got: {all_messages!r}"
    )


def test_S3_create_order_expires_at_aware_utc_accepted():
    """Explicit UTC-aware datetime (Z suffix) is the required format."""
    aware_future = datetime.now(timezone.utc) + timedelta(hours=2)
    assert aware_future.tzinfo is not None

    req = CreateOrderRequest(**_valid_create_kwargs(expires_at=aware_future))
    assert req.expires_at.tzinfo is not None


# ---------------------------------------------------------------------------
# S4 — CreateOrderRequest.credits_amount (zero / negative) → ValidationError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_amount", [Decimal("0"), Decimal("-1.00"), Decimal("-0.00000001")])
def test_S4_create_order_credits_amount_nonpositive_raises(bad_amount):
    with pytest.raises(ValidationError) as exc_info:
        CreateOrderRequest(**_valid_create_kwargs(credits_amount=bad_amount))

    errors = exc_info.value.errors()
    assert any("credits_amount" in str(e.get("loc", "")) for e in errors)


# ---------------------------------------------------------------------------
# S5 — CreateOrderRequest.amount_fiat (zero / negative) → ValidationError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_amount", [Decimal("0"), Decimal("-0.01")])
def test_S5_create_order_amount_fiat_nonpositive_raises(bad_amount):
    with pytest.raises(ValidationError) as exc_info:
        CreateOrderRequest(**_valid_create_kwargs(amount_fiat=bad_amount))

    errors = exc_info.value.errors()
    assert any("amount_fiat" in str(e.get("loc", "")) for e in errors)


# ---------------------------------------------------------------------------
# S6 — CreateOrderRequest.idempotency_key validation
# ---------------------------------------------------------------------------


def test_S6_create_order_idempotency_key_empty_raises():
    with pytest.raises(ValidationError) as exc_info:
        CreateOrderRequest(**_valid_create_kwargs(idempotency_key=""))

    errors = exc_info.value.errors()
    assert any("idempotency_key" in str(e.get("loc", "")) for e in errors)


def test_S6_create_order_idempotency_key_too_long_raises():
    long_key = "x" * 129  # one over the 128-char limit
    with pytest.raises(ValidationError) as exc_info:
        CreateOrderRequest(**_valid_create_kwargs(idempotency_key=long_key))

    errors = exc_info.value.errors()
    assert any("idempotency_key" in str(e.get("loc", "")) for e in errors)


def test_S6_create_order_idempotency_key_max_128_accepted():
    key = "k" * 128
    req = CreateOrderRequest(**_valid_create_kwargs(idempotency_key=key))
    assert len(req.idempotency_key) == 128


# ---------------------------------------------------------------------------
# S7 — CreateOrderRequest.provider (empty string) → ValidationError
# ---------------------------------------------------------------------------


def test_S7_create_order_provider_empty_raises():
    with pytest.raises(ValidationError) as exc_info:
        CreateOrderRequest(**_valid_create_kwargs(provider=""))

    errors = exc_info.value.errors()
    assert any("provider" in str(e.get("loc", "")) for e in errors)


# ---------------------------------------------------------------------------
# S8 — CreateOrderRequest.currency fields (> 10 chars) → ValidationError
# ---------------------------------------------------------------------------


def test_S8_create_order_currency_fiat_too_long_raises():
    with pytest.raises(ValidationError):
        CreateOrderRequest(**_valid_create_kwargs(currency_fiat="USD_TOO_LONG_X"))


def test_S8_create_order_currency_credits_too_long_raises():
    with pytest.raises(ValidationError):
        CreateOrderRequest(**_valid_create_kwargs(currency_credits="TCU_TOO_LONG_X"))


def test_S8_create_order_currency_empty_raises():
    with pytest.raises(ValidationError):
        CreateOrderRequest(**_valid_create_kwargs(currency_fiat=""))


# ---------------------------------------------------------------------------
# S9 — RefundOrderRequest: valid payload
# ---------------------------------------------------------------------------


def test_S9_refund_order_request_valid():
    req = RefundOrderRequest(amount=Decimal("5.00"), note="test refund")
    assert req.amount == Decimal("5.00")
    assert req.note == "test refund"


def test_S9_refund_order_request_note_optional():
    req = RefundOrderRequest(amount=Decimal("1.00"))
    assert req.note is None


# ---------------------------------------------------------------------------
# S10 — RefundOrderRequest.amount (zero / negative) → ValidationError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_amount", [Decimal("0"), Decimal("-5.00"), Decimal("-0.00000001")])
def test_S10_refund_amount_nonpositive_raises(bad_amount):
    with pytest.raises(ValidationError) as exc_info:
        RefundOrderRequest(amount=bad_amount)

    errors = exc_info.value.errors()
    assert any("amount" in str(e.get("loc", "")) for e in errors)


# ---------------------------------------------------------------------------
# S11 — RefundOrderRequest.note (> 500 chars) → ValidationError
# ---------------------------------------------------------------------------


def test_S11_refund_note_too_long_raises():
    with pytest.raises(ValidationError) as exc_info:
        RefundOrderRequest(amount=Decimal("1.00"), note="x" * 501)

    errors = exc_info.value.errors()
    assert any("note" in str(e.get("loc", "")) for e in errors)


def test_S11_refund_note_exactly_500_accepted():
    req = RefundOrderRequest(amount=Decimal("1.00"), note="n" * 500)
    assert len(req.note) == 500


# ---------------------------------------------------------------------------
# S12 — PaymentOrderResponse: constructed from dict
# ---------------------------------------------------------------------------


def test_S12_payment_order_response_from_dict():
    data = _order_response_dict()
    resp = PaymentOrderResponse(**data)

    assert resp.id == data["id"]
    assert resp.status == "pending"
    assert resp.amount_fiat == Decimal("10.00")
    assert resp.confirmed_at is None
    assert resp.external_order_id is None


# ---------------------------------------------------------------------------
# S13 — PaymentOrderResponse: webhook_raw intentionally absent
# ---------------------------------------------------------------------------


def test_S13_payment_order_response_has_no_webhook_raw_field():
    """webhook_raw must not appear in PaymentOrderResponse — audit-only field."""
    field_names = set(PaymentOrderResponse.model_fields.keys())
    assert "webhook_raw" not in field_names, (
        "webhook_raw must NEVER be exposed in PaymentOrderResponse "
        "(it is an audit-only field that may contain provider event data)"
    )


# ---------------------------------------------------------------------------
# S14 — PaymentOrderResponse: from_attributes (ORM-compatible)
# ---------------------------------------------------------------------------


def test_S14_payment_order_response_from_attributes():
    """Simulate construction from an ORM object via from_attributes=True."""
    # Build a simple namespace that mimics the ORM model's attribute access
    order = types.SimpleNamespace(**_order_response_dict())

    resp = PaymentOrderResponse.model_validate(order)

    assert resp.id == order.id
    assert resp.provider == order.provider
    assert resp.credits_amount == order.credits_amount
    # webhook_raw is not on the SimpleNamespace — should not cause an error
    assert not hasattr(resp, "webhook_raw")


# ---------------------------------------------------------------------------
# S15 — PaymentOrderListResponse
# ---------------------------------------------------------------------------


def test_S15_payment_order_list_response():
    orders = [PaymentOrderResponse(**_order_response_dict()) for _ in range(3)]
    resp = PaymentOrderListResponse(orders=orders, total=3)

    assert len(resp.orders) == 3
    assert resp.total == 3


def test_S15_payment_order_list_response_empty():
    resp = PaymentOrderListResponse(orders=[], total=0)
    assert resp.orders == []
    assert resp.total == 0


# ---------------------------------------------------------------------------
# S16 — CreditBalanceResponse: fields and Decimal precision
# ---------------------------------------------------------------------------


def test_S16_credit_balance_response_preserves_decimal_precision():
    user_id = str(uuid.uuid4())
    bal = CreditBalanceResponse(user_id=user_id, currency="USD", balance=Decimal("123.45678900"))

    assert bal.user_id == user_id
    assert bal.currency == "USD"
    assert bal.balance == Decimal("123.45678900")


def test_S16_credit_balance_zero_balance_accepted():
    bal = CreditBalanceResponse(user_id="u1", currency="TCU", balance=Decimal("0.00"))
    assert bal.balance == Decimal("0")


# ---------------------------------------------------------------------------
# S17 — UserCreditSummaryResponse: empty balances list accepted
# ---------------------------------------------------------------------------


def test_S17_user_credit_summary_empty_balances():
    user_id = str(uuid.uuid4())
    resp = UserCreditSummaryResponse(user_id=user_id, balances=[])

    assert resp.user_id == user_id
    assert resp.balances == []


def test_S17_user_credit_summary_multiple_currencies():
    user_id = str(uuid.uuid4())
    balances = [
        CreditBalanceResponse(user_id=user_id, currency="USD", balance=Decimal("100.00")),
        CreditBalanceResponse(user_id=user_id, currency="TCU", balance=Decimal("500.00")),
    ]
    resp = UserCreditSummaryResponse(user_id=user_id, balances=balances)

    assert len(resp.balances) == 2
    currencies = {b.currency for b in resp.balances}
    assert currencies == {"USD", "TCU"}
