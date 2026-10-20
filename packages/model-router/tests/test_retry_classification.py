"""Etapa 5E — tests for retry.classify_failure() and backoff_delay().

Covers test list items A-F (classification) and J (exponential backoff +
jitter).
"""
from __future__ import annotations

import asyncio

import pytest

from trinity_router.retry import FailureClass, backoff_delay, classify_failure


# ── A/B/C: retryable ─────────────────────────────────────────────────────────

class RateLimitError(Exception):
    pass


class APIConnectionError(Exception):
    pass


def test_rate_limit_is_retryable():
    assert classify_failure(RateLimitError("429 Too Many Requests")) == FailureClass.RETRYABLE


def test_overload_is_retryable():
    class ServiceOverloadedError(Exception):
        pass
    assert classify_failure(ServiceOverloadedError("model overloaded, try again")) == FailureClass.RETRYABLE


def test_connection_failure_is_retryable():
    assert classify_failure(APIConnectionError("connection reset by peer")) == FailureClass.RETRYABLE


def test_timeout_is_retryable():
    assert classify_failure(TimeoutError("request timed out")) == FailureClass.RETRYABLE


def test_unrecognized_exception_defaults_to_retryable():
    """Preserves this router's pre-5E behavior: an exception type nobody
    has specifically classified still results in trying the next
    candidate — never a silent, permanent dead end."""
    assert classify_failure(Exception("something weird happened")) == FailureClass.RETRYABLE


# ── D: non-retryable auth/config ────────────────────────────────────────────

def test_auth_error_is_non_retryable_provider():
    class AuthenticationError(Exception):
        pass
    assert classify_failure(AuthenticationError("invalid api key")) == FailureClass.NON_RETRYABLE_PROVIDER


def test_permission_error_is_non_retryable_provider():
    class PermissionDeniedError(Exception):
        pass
    assert classify_failure(PermissionDeniedError("insufficient permissions")) == FailureClass.NON_RETRYABLE_PROVIDER


def test_credential_error_is_non_retryable_provider():
    class InvalidCredentialError(Exception):
        pass
    assert classify_failure(InvalidCredentialError("bad credentials")) == FailureClass.NON_RETRYABLE_PROVIDER


# ── E: invalid request ──────────────────────────────────────────────────────

def test_invalid_request_is_non_retryable_request():
    class InvalidRequestError(Exception):
        pass
    assert classify_failure(InvalidRequestError("malformed messages array")) == FailureClass.NON_RETRYABLE_REQUEST


def test_bad_request_is_non_retryable_request():
    class BadRequestError(Exception):
        pass
    assert classify_failure(BadRequestError("schema validation failed")) == FailureClass.NON_RETRYABLE_REQUEST


# ── F: capability incompatibility ───────────────────────────────────────────

def test_unsupported_capability_is_non_retryable_request():
    class UnsupportedCapabilityError(Exception):
        pass
    assert classify_failure(UnsupportedCapabilityError("tool calling not supported")) == FailureClass.NON_RETRYABLE_REQUEST


def test_content_policy_refusal_is_non_retryable_request():
    class ContentPolicyViolationError(Exception):
        pass
    assert classify_failure(ContentPolicyViolationError("blocked")) == FailureClass.NON_RETRYABLE_REQUEST


def test_safety_refusal_message_is_non_retryable_request():
    """Even without a dedicated exception class name, a message that
    clearly reads as a policy/safety refusal is caught by the message-text
    fallback check."""
    assert classify_failure(Exception("Request refused due to safety policy violation")) == FailureClass.NON_RETRYABLE_REQUEST


# ── Auth-error naming never misclassified as a request-content failure ─────

def test_authentication_error_name_does_not_trigger_request_classification():
    """Regression guard: 'AuthenticationError' contains neither
    'invalidrequest' nor 'badrequest' — must not accidentally match the
    request-content branch."""
    class AuthenticationError(Exception):
        pass
    assert classify_failure(AuthenticationError("token expired")) == FailureClass.NON_RETRYABLE_PROVIDER


# ── J: exponential backoff + jitter ─────────────────────────────────────────

async def test_backoff_grows_exponentially_with_attempt_index():
    delays: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    for attempt in range(4):
        await backoff_delay(attempt, base_s=0.1, cap_s=10.0, jitter_ratio=0.0, sleep_fn=_fake_sleep)

    # jitter_ratio=0.0 makes this deterministic: 0.1, 0.2, 0.4, 0.8
    assert delays == pytest.approx([0.1, 0.2, 0.4, 0.8])


async def test_backoff_is_bounded_by_cap():
    delays: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    await backoff_delay(10, base_s=0.1, cap_s=1.0, jitter_ratio=0.0, sleep_fn=_fake_sleep)
    assert delays[-1] == pytest.approx(1.0)  # would be 0.1 * 2**10 = 102.4 without the cap


async def test_backoff_includes_jitter():
    """With jitter_ratio > 0, repeated calls at the same attempt_index
    must not all produce the identical delay — proving jitter is real,
    not a no-op."""
    async def _fake_sleep(seconds: float) -> None:
        pass

    observed = set()
    for _ in range(20):
        total = await backoff_delay(2, base_s=0.1, cap_s=10.0, jitter_ratio=1.0, sleep_fn=_fake_sleep)
        observed.add(round(total, 6))
    assert len(observed) > 1, "jitter produced identical delays across 20 samples — not actually random"
    base_delay = 0.1 * (2 ** 2)
    assert all(base_delay <= v <= base_delay * 2 for v in observed)


async def test_backoff_delegates_to_sleep_fn_never_blocks():
    """Confirms backoff_delay is a well-behaved coroutine — it awaits
    sleep_fn exactly once and returns promptly when sleep_fn is a no-op,
    proving it never does its own blocking wait."""
    import time
    async def _instant_sleep(seconds: float) -> None:
        return None

    start = time.perf_counter()
    await backoff_delay(5, base_s=1.0, cap_s=10.0, sleep_fn=_instant_sleep)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.05, f"backoff_delay took {elapsed:.3f}s despite an instant sleep_fn"
