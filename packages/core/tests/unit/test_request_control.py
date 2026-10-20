"""Unit tests for src.services.request_control — pure in-memory primitives,
no HTTP, no DB. Concurrency tests use asyncio.Event-gated coroutines and
real asyncio.gather() races, never arbitrary sleeps, to deterministically
prove limits are never exceeded.
"""
from __future__ import annotations

import asyncio

import pytest

from src.services.request_control import (
    ConcurrencyLimitExceededError,
    ConcurrencyLimiter,
    InMemoryRateLimiter,
    PerKeyConcurrencyLimiter,
    RequestControl,
    RequestControlToken,
    RequestRejectedError,
)

# Mixed sync/async tests in this file — pytest-asyncio's asyncio_mode="auto"
# (pyproject.toml) picks up every `async def test_*` automatically, so no
# module-level pytestmark is needed (and applying one would incorrectly
# also tag the plain sync tests below, which pytest-asyncio then warns
# about).


def _make_control(
    *,
    user_rate_limit=1000,
    user_rate_window_s=60.0,
    ip_rate_limit=1000,
    ip_rate_window_s=60.0,
    user_max_concurrent=1000,
    global_max_concurrent=1000,
) -> RequestControl:
    """Build an isolated RequestControl with generous defaults, overridden
    per test for the one dimension under test — never shares state with the
    process-wide singleton in src.services.request_control.request_control.
    """
    return RequestControl(
        user_rate_limiter=InMemoryRateLimiter(),
        ip_rate_limiter=InMemoryRateLimiter(),
        user_concurrency=PerKeyConcurrencyLimiter(user_max_concurrent),
        global_concurrency=ConcurrencyLimiter(global_max_concurrent),
        user_rate_limit=user_rate_limit,
        user_rate_window_s=user_rate_window_s,
        ip_rate_limit=ip_rate_limit,
        ip_rate_window_s=ip_rate_window_s,
    )


# ── 1/2. Rate limiting — user and IP dimensions ─────────────────────────────

async def test_user_below_rate_limit_is_allowed():
    limiter = InMemoryRateLimiter()
    for _ in range(5):
        decision = await limiter.check("user-1", limit=5, window_seconds=60)
        assert decision.allowed
    # sixth exceeds


async def test_user_above_rate_limit_is_rejected():
    limiter = InMemoryRateLimiter()
    for _ in range(5):
        assert (await limiter.check("user-1", limit=5, window_seconds=60)).allowed
    decision = await limiter.check("user-1", limit=5, window_seconds=60)
    assert not decision.allowed
    assert decision.retry_after_seconds is not None
    assert decision.retry_after_seconds >= 0


async def test_rate_limit_keys_are_independent():
    limiter = InMemoryRateLimiter()
    for _ in range(3):
        assert (await limiter.check("user-a", limit=3, window_seconds=60)).allowed
    # user-a is now exhausted, user-b is unaffected
    assert not (await limiter.check("user-a", limit=3, window_seconds=60)).allowed
    assert (await limiter.check("user-b", limit=3, window_seconds=60)).allowed


async def test_rate_limit_window_expiry_admits_again():
    limiter = InMemoryRateLimiter()
    assert (await limiter.check("user-1", limit=1, window_seconds=0.05)).allowed
    assert not (await limiter.check("user-1", limit=1, window_seconds=0.05)).allowed
    await asyncio.sleep(0.06)
    assert (await limiter.check("user-1", limit=1, window_seconds=0.05)).allowed


async def test_request_control_check_rate_limits_raises_for_user():
    control = _make_control(user_rate_limit=1)
    await control.check_rate_limits(user_id="u1", client_ip="1.2.3.4")
    with pytest.raises(RequestRejectedError) as exc_info:
        await control.check_rate_limits(user_id="u1", client_ip="1.2.3.4")
    assert exc_info.value.reason == "rate_limit_user"


async def test_request_control_check_rate_limits_raises_for_ip():
    control = _make_control(ip_rate_limit=1)
    await control.check_rate_limits(user_id="u1", client_ip="1.2.3.4")
    with pytest.raises(RequestRejectedError) as exc_info:
        await control.check_rate_limits(user_id="u2", client_ip="1.2.3.4")  # different user, same IP
    assert exc_info.value.reason == "rate_limit_ip"


async def test_ip_rate_limit_skipped_when_ip_is_none():
    control = _make_control(ip_rate_limit=1)
    await control.check_rate_limits(user_id="u1", client_ip=None)
    await control.check_rate_limits(user_id="u2", client_ip=None)  # would fail if IP were tracked as "None"


# ── 4/5/6/7. Concurrency — per-user and global ──────────────────────────────

def test_concurrency_below_limit_succeeds():
    limiter = ConcurrencyLimiter(3)
    tokens = [limiter.try_acquire() for _ in range(3)]
    assert limiter.active_count == 3
    for t in tokens:
        t.release()
    assert limiter.active_count == 0


def test_concurrency_above_limit_raises():
    limiter = ConcurrencyLimiter(2)
    limiter.try_acquire()
    limiter.try_acquire()
    with pytest.raises(ConcurrencyLimitExceededError):
        limiter.try_acquire()


def test_per_user_concurrency_does_not_block_another_user():
    limiter = PerKeyConcurrencyLimiter(1)
    token_a = limiter.try_acquire("user-a")
    # user-a is now at capacity (1/1) — user-b is completely unaffected.
    token_b = limiter.try_acquire("user-b")
    assert limiter.active_count("user-a") == 1
    assert limiter.active_count("user-b") == 1
    with pytest.raises(ConcurrencyLimitExceededError):
        limiter.try_acquire("user-a")
    token_a.release()
    token_b.release()


def test_global_limit_rejects_once_reached():
    control = _make_control(user_max_concurrent=1000, global_max_concurrent=2)
    t1 = control.acquire_concurrency("user-a")
    t2 = control.acquire_concurrency("user-b")
    with pytest.raises(RequestRejectedError) as exc_info:
        control.acquire_concurrency("user-c")
    assert exc_info.value.reason == "concurrency_global"
    t1.release()
    t2.release()


def test_user_limit_rejects_before_global_is_reached():
    control = _make_control(user_max_concurrent=1, global_max_concurrent=1000)
    token = control.acquire_concurrency("user-a")
    with pytest.raises(RequestRejectedError) as exc_info:
        control.acquire_concurrency("user-a")
    assert exc_info.value.reason == "concurrency_user"
    token.release()


def test_user_rejection_does_not_leak_the_global_slot_it_briefly_held():
    """If global acquisition succeeds but per-user acquisition then fails,
    the already-acquired global slot must be released before raising —
    otherwise a single over-limit user could silently eat into the global
    budget for everyone else."""
    control = _make_control(user_max_concurrent=1, global_max_concurrent=5)
    control.acquire_concurrency("user-a")  # user-a now at its own limit (1/1)

    global_limiter = control._global_concurrency  # white-box: confirm no leak
    active_before = global_limiter.active_count
    with pytest.raises(RequestRejectedError):
        control.acquire_concurrency("user-a")
    assert global_limiter.active_count == active_before  # unchanged — released cleanly


# ── 13. Race-condition-free concurrent acquisition ──────────────────────────

async def test_many_concurrent_acquires_never_exceed_the_limit():
    """20 coroutines race to acquire a 5-slot global limiter simultaneously
    via asyncio.gather — exactly 5 must succeed, exactly 15 must be
    rejected, and active_count must never have exceeded 5 at any point.
    Deterministic: try_acquire() has no `await` inside it (see class
    docstring), so there is no interleaving window for two coroutines to
    both observe capacity and both succeed past the limit.
    """
    limiter = ConcurrencyLimiter(5)
    results = []

    async def attempt(i):
        try:
            token = limiter.try_acquire()
            results.append(("ok", i, limiter.active_count))
            return token
        except ConcurrencyLimitExceededError:
            results.append(("rejected", i, None))
            return None

    tokens = await asyncio.gather(*(attempt(i) for i in range(20)))
    succeeded = [t for t in tokens if t is not None]
    assert len(succeeded) == 5
    assert limiter.active_count == 5
    assert all(active <= 5 for kind, _, active in results if kind == "ok")

    for t in succeeded:
        t.release()
    assert limiter.active_count == 0


async def test_many_concurrent_acquires_on_request_control_never_exceed_limits():
    control = _make_control(user_max_concurrent=1000, global_max_concurrent=10)

    async def attempt(i):
        try:
            return control.acquire_concurrency(f"user-{i}")
        except RequestRejectedError:
            return None

    tokens = await asyncio.gather(*(attempt(i) for i in range(50)))
    succeeded = [t for t in tokens if t is not None]
    assert len(succeeded) == 10
    assert control._global_concurrency.active_count == 10

    for t in succeeded:
        t.release()
    assert control._global_concurrency.active_count == 0


# ── Token semantics ──────────────────────────────────────────────────────────

def test_request_control_token_release_is_idempotent():
    control = _make_control(global_max_concurrent=1)
    token = control.acquire_concurrency("user-a")
    assert control._global_concurrency.active_count == 1
    token.release()
    assert control._global_concurrency.active_count == 0
    token.release()  # must not go negative or raise
    token.release()
    assert control._global_concurrency.active_count == 0


def test_double_release_never_over_credits_capacity():
    limiter = ConcurrencyLimiter(1)
    token = limiter.try_acquire()
    token.release()
    token.release()
    token.release()
    # capacity is still genuinely 1, not inflated by the extra releases
    t1 = limiter.try_acquire()
    with pytest.raises(ConcurrencyLimitExceededError):
        limiter.try_acquire()
    t1.release()


# ── RequestControl.reset() ──────────────────────────────────────────────────

async def test_reset_clears_rate_limits_and_concurrency():
    control = _make_control(user_rate_limit=1, global_max_concurrent=1)
    await control.check_rate_limits(user_id="u1", client_ip="1.2.3.4")
    token = control.acquire_concurrency("u1")

    control.reset()

    # Both dimensions must be usable again immediately after reset.
    await control.check_rate_limits(user_id="u1", client_ip="1.2.3.4")
    new_token = control.acquire_concurrency("u1")
    assert control._global_concurrency.active_count == 1  # only the new token
    new_token.release()
    token.release()  # pre-reset token: release is still safe (idempotent, no-op-ish)


# ── Etapa 5G — metrics instrumentation ──────────────────────────────────────
# Uses a real, small-limit RequestControl (never the module-level singleton
# or a mock) so the rejection actually runs through the instrumented code
# path in request_control.py itself — a mocked check_rate_limits()/
# acquire_concurrency() would only prove a mock was called, not that the
# real rejection site increments the counter.

async def test_rate_limit_rejection_increments_metric():
    from src.observability.metrics import core_metrics

    control = _make_control(user_rate_limit=1)
    await control.check_rate_limits(user_id="metrics-u1", client_ip="9.9.9.9")  # consumes the only slot
    before = sum(core_metrics.counter("request_control_rejections_total").snapshot().values())

    with pytest.raises(RequestRejectedError):
        await control.check_rate_limits(user_id="metrics-u1", client_ip="9.9.9.9")

    after = core_metrics.counter("request_control_rejections_total").snapshot()
    assert sum(after.values()) >= before + 1
    assert any(dict(k).get("reason") == "rate_limit_user" for k in after)


def test_concurrency_rejection_increments_metric():
    from src.observability.metrics import core_metrics

    control = _make_control(global_max_concurrent=1)
    token = control.acquire_concurrency("metrics-u2")
    try:
        with pytest.raises(RequestRejectedError):
            control.acquire_concurrency("metrics-u2")
    finally:
        token.release()

    after = core_metrics.counter("request_control_rejections_total").snapshot()
    assert any(dict(k).get("reason") == "concurrency_global" for k in after)
