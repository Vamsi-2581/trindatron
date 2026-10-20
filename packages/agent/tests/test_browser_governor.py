"""Tests for BrowserResourceGovernor — pure asyncio primitive, no real
Chromium involved anywhere in this file (unlike test_browser_session.py,
which exercises the governor together with real browsers).

Concurrency tests use asyncio.Event-gated workers, never arbitrary sleeps,
to deterministically control "N holders currently active" / "a request is
currently waiting" without racing on timing. The one exception is a single
`asyncio.sleep(0)` used purely as a scheduling yield (let already-runnable
tasks take their next step) — not a timing assumption.
"""
from __future__ import annotations

import asyncio

import pytest

from trinity_agent.browser.governor import (
    BrowserCapacityTimeoutError,
    BrowserGovernorConfigError,
    BrowserResourceGovernor,
)

# No module-level pytestmark: a few tests below are synchronous. asyncio_mode
# is "auto" in pyproject.toml, so async tests run correctly without a decorator.


async def _acquire_and_hold(gov, acquired_event: asyncio.Event, release_event: asyncio.Event):
    """Acquire a slot, signal `acquired_event`, hold until `release_event`,
    then release. Returns nothing — intended to run as a background task."""
    token = await gov.acquire()
    acquired_event.set()
    try:
        await release_event.wait()
    finally:
        token.release()


async def _start_n_holders(gov, n: int):
    """Start n holder tasks, wait until all n have actually acquired (via
    their own Events, not a sleep), and return (tasks, shared_release_event)."""
    acquired = [asyncio.Event() for _ in range(n)]
    release = asyncio.Event()
    tasks = [
        asyncio.create_task(_acquire_and_hold(gov, acquired[i], release))
        for i in range(n)
    ]
    await asyncio.wait_for(asyncio.gather(*(e.wait() for e in acquired)), timeout=5)
    return tasks, release


# ── A. Global concurrency ───────────────────────────────────────────────────

async def test_n_sessions_within_limit_all_acquire_immediately():
    gov = BrowserResourceGovernor(capacity=3)
    tasks, release = await _start_n_holders(gov, 3)
    assert gov.active_count == 3
    assert gov.available_capacity == 0
    release.set()
    await asyncio.gather(*tasks)
    assert gov.active_count == 0


async def test_capacity_is_never_exceeded_by_a_waiting_extra_request():
    gov = BrowserResourceGovernor(capacity=2)
    tasks, release = await _start_n_holders(gov, 2)

    extra_acquired = asyncio.Event()

    async def extra_holder():
        token = await gov.acquire()
        extra_acquired.set()
        token.release()

    extra_task = asyncio.create_task(extra_holder())
    await asyncio.sleep(0)  # let extra_holder run up to its blocking acquire()

    assert not extra_acquired.is_set()
    assert gov.active_count == 2  # never 3 — the whole point of the governor
    assert gov.waiting_count == 1

    release.set()
    await asyncio.gather(*tasks)
    await asyncio.wait_for(extra_acquired.wait(), timeout=5)
    await extra_task
    assert gov.active_count == 0


async def test_release_unblocks_exactly_one_waiter_and_admits_it():
    gov = BrowserResourceGovernor(capacity=1)
    tasks, release = await _start_n_holders(gov, 1)

    waiter_acquired = asyncio.Event()

    async def waiter():
        token = await gov.acquire()
        waiter_acquired.set()
        token.release()

    w = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    assert gov.waiting_count == 1
    assert not waiter_acquired.is_set()

    release.set()
    await asyncio.gather(*tasks)
    await asyncio.wait_for(waiter_acquired.wait(), timeout=5)
    await w
    assert gov.waiting_count == 0
    assert gov.active_count == 0


async def test_queue_serves_multiple_waiters_in_sequence():
    gov = BrowserResourceGovernor(capacity=1)
    holder_tasks, holder_release = await _start_n_holders(gov, 1)

    n_waiters = 4
    acquired_order: list[int] = []
    acquired_events = [asyncio.Event() for _ in range(n_waiters)]
    release_gates = [asyncio.Event() for _ in range(n_waiters)]

    async def waiter(i):
        token = await gov.acquire()
        acquired_order.append(i)
        acquired_events[i].set()
        await release_gates[i].wait()
        token.release()

    waiter_tasks = [asyncio.create_task(waiter(i)) for i in range(n_waiters)]
    await asyncio.sleep(0)
    assert gov.waiting_count == n_waiters

    holder_release.set()
    await asyncio.gather(*holder_tasks)

    # Exactly one waiter should now hold the single slot — wait on the
    # *union* of all acquired-events; whichever fires is the one admitted.
    await asyncio.wait(
        [asyncio.ensure_future(e.wait()) for e in acquired_events],
        timeout=5, return_when=asyncio.FIRST_COMPLETED,
    )
    assert len(acquired_order) == 1
    assert gov.active_count == 1
    assert gov.waiting_count == n_waiters - 1

    # Release them one at a time; each release admits exactly the next one,
    # confirmed by waiting on that specific next waiter's own event.
    for _ in range(n_waiters - 1):
        just_admitted = acquired_order[-1]
        release_gates[just_admitted].set()
        remaining_events = [acquired_events[i] for i in range(n_waiters) if i != just_admitted and not acquired_events[i].is_set()]
        await asyncio.wait(
            [asyncio.ensure_future(e.wait()) for e in remaining_events],
            timeout=5, return_when=asyncio.FIRST_COMPLETED,
        )

    release_gates[acquired_order[-1]].set()
    await asyncio.gather(*waiter_tasks)
    assert gov.active_count == 0
    assert len(acquired_order) == n_waiters
    assert len(set(acquired_order)) == n_waiters  # each waiter admitted exactly once


async def test_acquisition_timeout_when_no_capacity_frees_up():
    gov = BrowserResourceGovernor(capacity=1)
    tasks, release = await _start_n_holders(gov, 1)
    try:
        with pytest.raises(BrowserCapacityTimeoutError):
            await gov.acquire(timeout=0.05)
    finally:
        release.set()
        await asyncio.gather(*tasks)


# ── C. Cancellation while waiting ───────────────────────────────────────────

async def test_cancellation_while_waiting_does_not_consume_a_permit():
    gov = BrowserResourceGovernor(capacity=1)
    tasks, release = await _start_n_holders(gov, 1)

    waiting_started = asyncio.Event()

    async def waiter():
        waiting_started.set()
        await gov.acquire()  # never returns — cancelled while waiting

    w = asyncio.create_task(waiter())
    await asyncio.wait_for(waiting_started.wait(), timeout=5)
    await asyncio.sleep(0)  # let it actually reach the blocking acquire()
    assert gov.waiting_count == 1

    w.cancel()
    with pytest.raises(asyncio.CancelledError):
        await w
    assert gov.waiting_count == 0
    assert gov.active_count == 1  # still just the original holder — no leak, no phantom grant

    release.set()
    await asyncio.gather(*tasks)
    assert gov.active_count == 0


async def test_timeout_does_not_consume_a_permit_either():
    """Same invariant as the cancellation test above, via the timeout path
    instead of explicit .cancel() — asyncio.wait_for's internal cancellation
    on timeout must be equally clean."""
    gov = BrowserResourceGovernor(capacity=1)
    tasks, release = await _start_n_holders(gov, 1)

    with pytest.raises(BrowserCapacityTimeoutError):
        await gov.acquire(timeout=0.05)
    assert gov.active_count == 1  # unaffected by the failed waiter

    release.set()
    await asyncio.gather(*tasks)
    assert gov.active_count == 0
    assert gov.waiting_count == 0


# ── F. Resource accounting / leak checks ────────────────────────────────────

async def test_repeated_acquire_release_returns_to_initial_state():
    gov = BrowserResourceGovernor(capacity=2)
    for _ in range(50):
        token = await gov.acquire()
        assert gov.active_count == 1
        token.release()
    assert gov.active_count == 0
    assert gov.available_capacity == 2
    assert gov.waiting_count == 0


async def test_double_release_is_a_safe_noop():
    gov = BrowserResourceGovernor(capacity=1)
    token = await gov.acquire()
    assert gov.active_count == 1
    token.release()
    assert gov.active_count == 0
    token.release()  # must not go negative or over-credit the semaphore
    token.release()
    assert gov.active_count == 0

    # And capacity must still be exactly 1, not inflated by the extra releases.
    t1 = await gov.acquire()
    started_second = asyncio.Event()

    async def second():
        tok = await gov.acquire()
        started_second.set()
        tok.release()

    task = asyncio.create_task(second())
    await asyncio.sleep(0)
    assert not started_second.is_set()  # still blocked — capacity is genuinely 1
    t1.release()
    await asyncio.wait_for(started_second.wait(), timeout=5)
    await task


async def test_active_never_negative_invariant():
    gov = BrowserResourceGovernor(capacity=5)
    token = await gov.acquire()
    for _ in range(10):
        token.release()  # only the first should count
    assert gov.active_count >= 0
    assert gov.active_count == 0


async def test_capacity_invariant_active_plus_available_equals_capacity():
    gov = BrowserResourceGovernor(capacity=4)
    tokens = []
    for i in range(4):
        tokens.append(await gov.acquire())
        assert gov.active_count + gov.available_capacity == gov.capacity
        assert gov.active_count <= gov.capacity
    for t in tokens:
        t.release()
        assert gov.active_count + gov.available_capacity == gov.capacity


# ── H. Governor-level configuration validation ──────────────────────────────

@pytest.mark.parametrize("capacity", [0, -1, -100])
def test_invalid_capacity_raises_config_error(capacity):
    with pytest.raises(BrowserGovernorConfigError):
        BrowserResourceGovernor(capacity=capacity)


def test_capacity_one_is_the_minimum_valid_value():
    gov = BrowserResourceGovernor(capacity=1)
    assert gov.capacity == 1


# ── I. Timeout behavior — no orphaned tasks ─────────────────────────────────

async def test_no_orphaned_tasks_after_many_timeouts():
    gov = BrowserResourceGovernor(capacity=1)
    tasks, release = await _start_n_holders(gov, 1)
    try:
        before = len(asyncio.all_tasks())
        for _ in range(10):
            with pytest.raises(BrowserCapacityTimeoutError):
                await gov.acquire(timeout=0.01)
        # Each failed acquire() must clean up after itself — no growth in
        # the set of live tasks caused by repeated timeouts.
        after = len(asyncio.all_tasks())
        assert after <= before + 1  # +1 tolerance for the current test task itself
        assert gov.waiting_count == 0
    finally:
        release.set()
        await asyncio.gather(*tasks)
