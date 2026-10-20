"""Etapa 5E — tests for circuit_breaker.CircuitBreakerRegistry.

Covers test list items K (circuit breaker) and L (HALF_OPEN recovery),
plus per-key independence (a provider tripping its breaker must not
affect any other provider's breaker).
"""
from __future__ import annotations

import time

import pytest

from trinity_router.circuit_breaker import CircuitBreakerRegistry, CircuitState


def test_starts_closed():
    reg = CircuitBreakerRegistry()
    assert reg.state_of("claude-sonnet") == CircuitState.CLOSED
    assert reg.is_available("claude-sonnet") is True


def test_opens_after_failure_threshold_consecutive_failures():
    reg = CircuitBreakerRegistry(failure_threshold=3, cooldown_s=30.0)
    reg.record_failure("claude-sonnet")
    assert reg.state_of("claude-sonnet") == CircuitState.CLOSED
    reg.record_failure("claude-sonnet")
    assert reg.state_of("claude-sonnet") == CircuitState.CLOSED
    reg.record_failure("claude-sonnet")
    assert reg.state_of("claude-sonnet") == CircuitState.OPEN
    assert reg.is_available("claude-sonnet") is False


def test_success_resets_consecutive_failure_count():
    reg = CircuitBreakerRegistry(failure_threshold=3, cooldown_s=30.0)
    reg.record_failure("claude-sonnet")
    reg.record_failure("claude-sonnet")
    reg.record_success("claude-sonnet")  # resets the streak
    reg.record_failure("claude-sonnet")
    reg.record_failure("claude-sonnet")
    assert reg.state_of("claude-sonnet") == CircuitState.CLOSED, (
        "two failures after a success must not open the breaker — the "
        "streak was reset, this is only the 2nd consecutive failure"
    )


def test_open_transitions_to_half_open_after_cooldown():
    reg = CircuitBreakerRegistry(failure_threshold=1, cooldown_s=0.1)
    reg.record_failure("claude-sonnet")
    assert reg.state_of("claude-sonnet") == CircuitState.OPEN
    assert reg.is_available("claude-sonnet") is False

    time.sleep(0.2)
    assert reg.is_available("claude-sonnet") is True
    assert reg.state_of("claude-sonnet") == CircuitState.HALF_OPEN


def test_half_open_success_closes_the_breaker():
    """L: HALF_OPEN recovery — a successful trial call fully recovers the breaker."""
    reg = CircuitBreakerRegistry(failure_threshold=1, cooldown_s=0.1)
    reg.record_failure("claude-sonnet")
    time.sleep(0.2)
    assert reg.is_available("claude-sonnet") is True  # promotes to HALF_OPEN
    assert reg.state_of("claude-sonnet") == CircuitState.HALF_OPEN

    reg.record_success("claude-sonnet")
    assert reg.state_of("claude-sonnet") == CircuitState.CLOSED
    assert reg.is_available("claude-sonnet") is True


def test_half_open_failure_reopens_the_breaker():
    reg = CircuitBreakerRegistry(failure_threshold=1, cooldown_s=0.1)
    reg.record_failure("claude-sonnet")
    time.sleep(0.2)
    assert reg.is_available("claude-sonnet") is True  # HALF_OPEN trial begins
    reg.record_failure("claude-sonnet")  # trial fails
    assert reg.state_of("claude-sonnet") == CircuitState.OPEN
    assert reg.is_available("claude-sonnet") is False


def test_reopened_breaker_gets_a_fresh_cooldown_window():
    # A generous 0.1s cooldown with 0.2s sleeps — Windows' default timer
    # resolution (~15ms) makes a tighter margin flaky under system load.
    reg = CircuitBreakerRegistry(failure_threshold=1, cooldown_s=0.1)
    reg.record_failure("claude-sonnet")
    time.sleep(0.2)
    reg.is_available("claude-sonnet")  # -> HALF_OPEN
    reg.record_failure("claude-sonnet")  # -> OPEN again, cooldown clock reset

    assert reg.is_available("claude-sonnet") is False  # cooldown just restarted
    time.sleep(0.2)
    assert reg.is_available("claude-sonnet") is True  # new cooldown has now elapsed


# ── Per-key independence ────────────────────────────────────────────────────

def test_breaker_state_is_independent_per_key():
    reg = CircuitBreakerRegistry(failure_threshold=1, cooldown_s=30.0)
    reg.record_failure("google/gemini-25-flash")
    assert reg.state_of("google/gemini-25-flash") == CircuitState.OPEN
    assert reg.is_available("google/gemini-25-flash") is False

    # A different key is completely unaffected.
    assert reg.state_of("anthropic/claude-sonnet") == CircuitState.CLOSED
    assert reg.is_available("anthropic/claude-sonnet") is True


# ── reset() ──────────────────────────────────────────────────────────────────

def test_reset_clears_every_key_to_closed():
    reg = CircuitBreakerRegistry(failure_threshold=1, cooldown_s=30.0)
    reg.record_failure("a")
    reg.record_failure("b")
    assert reg.state_of("a") == CircuitState.OPEN
    assert reg.state_of("b") == CircuitState.OPEN

    reg.reset()
    assert reg.state_of("a") == CircuitState.CLOSED
    assert reg.state_of("b") == CircuitState.CLOSED
    assert reg.is_available("a") is True
    assert reg.is_available("b") is True


# ── Construction validation ──────────────────────────────────────────────────

def test_rejects_invalid_failure_threshold():
    with pytest.raises(ValueError):
        CircuitBreakerRegistry(failure_threshold=0)


def test_rejects_invalid_cooldown():
    with pytest.raises(ValueError):
        CircuitBreakerRegistry(cooldown_s=0)
    with pytest.raises(ValueError):
        CircuitBreakerRegistry(cooldown_s=-1)
