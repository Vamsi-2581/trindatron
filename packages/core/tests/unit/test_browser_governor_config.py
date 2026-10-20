"""Unit tests for the browser resource governor's configuration surface —
src.config.Settings' agent_browser_* fields. Constructs fresh Settings
instances directly (not the module-level singleton) so each test is
isolated and doesn't depend on process env vars."""
from __future__ import annotations

import pytest

from src.config import Settings


def _settings(**overrides) -> Settings:
    """Build a Settings instance with just the fields under test overridden,
    values required for non-production construction otherwise left at
    their defaults."""
    return Settings(**overrides)


# ── Defaults ─────────────────────────────────────────────────────────────────

def test_default_max_concurrent_sessions_is_four():
    assert _settings().agent_browser_max_concurrent_sessions == 4


def test_default_capacity_wait_timeout_is_30s():
    assert _settings().agent_browser_capacity_wait_timeout_s == 30.0


def test_default_shutdown_timeout_is_15s():
    assert _settings().agent_browser_shutdown_timeout_s == 15.0


# ── Valid values ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [1, 2, 8, 32])
def test_valid_max_concurrent_sessions_accepted_as_is(value):
    assert _settings(agent_browser_max_concurrent_sessions=value).agent_browser_max_concurrent_sessions == value


@pytest.mark.parametrize("value", [0, 0.5, 30.0, 120])
def test_valid_capacity_wait_timeout_accepted(value):
    assert _settings(agent_browser_capacity_wait_timeout_s=value).agent_browser_capacity_wait_timeout_s == value


@pytest.mark.parametrize("value", [0.1, 1, 15.0, 300])
def test_valid_shutdown_timeout_accepted(value):
    assert _settings(agent_browser_shutdown_timeout_s=value).agent_browser_shutdown_timeout_s == value


# ── max_concurrent_sessions: invalid / out-of-range ─────────────────────────

@pytest.mark.parametrize("value", [0, -1, -100])
def test_zero_or_negative_max_sessions_raises(value):
    with pytest.raises(ValueError, match="must be >= 1"):
        _settings(agent_browser_max_concurrent_sessions=value)


def test_max_sessions_above_hard_cap_is_clamped_not_rejected():
    """A too-large value is a safety issue (unbounded Chromium growth), not
    a nonsensical one — clamp to the hard cap with a visible warning rather
    than refusing to start the whole app over an operator typo."""
    result = _settings(agent_browser_max_concurrent_sessions=999)
    assert result.agent_browser_max_concurrent_sessions == 32  # the hard cap


def test_max_sessions_exactly_at_hard_cap_is_not_clamped():
    result = _settings(agent_browser_max_concurrent_sessions=32)
    assert result.agent_browser_max_concurrent_sessions == 32


def test_max_sessions_invalid_string_raises_validation_error():
    with pytest.raises(Exception):  # pydantic ValidationError — type coercion failure
        _settings(agent_browser_max_concurrent_sessions="not-a-number")


# ── capacity_wait_timeout: invalid ──────────────────────────────────────────

@pytest.mark.parametrize("value", [-0.01, -1, -100])
def test_negative_capacity_wait_timeout_raises(value):
    with pytest.raises(ValueError, match="must be >= 0"):
        _settings(agent_browser_capacity_wait_timeout_s=value)


def test_capacity_wait_timeout_invalid_string_raises():
    with pytest.raises(Exception):
        _settings(agent_browser_capacity_wait_timeout_s="soon")


# ── shutdown_timeout: invalid ────────────────────────────────────────────────

@pytest.mark.parametrize("value", [0, -0.01, -1])
def test_zero_or_negative_shutdown_timeout_raises(value):
    with pytest.raises(ValueError, match="must be > 0"):
        _settings(agent_browser_shutdown_timeout_s=value)


def test_shutdown_timeout_invalid_string_raises():
    with pytest.raises(Exception):
        _settings(agent_browser_shutdown_timeout_s="never")
