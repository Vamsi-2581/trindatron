"""Unit tests for src.services.agent_scheduler — pure in-memory primitives,
no HTTP, no DB. Mirrors tests/unit/test_request_control.py's structure
since InMemoryAgentScheduler reuses the exact same ConcurrencyLimiter /
PerKeyConcurrencyLimiter primitives request_control.py already proved
race-free.

Covers items 1-10 of the user's Etapa 5D test list, plus a real
20-concurrent-Agents race-condition proof (asyncio.gather, small global/
user limits) demonstrating active_global <= global_limit and
active_user <= user_limit hold at every observed instant, and that no slot
is left stuck after the race drains.
"""
from __future__ import annotations

import asyncio

import pytest

from src.services.agent_scheduler import (
    AgentExecutionToken,
    AgentSchedulerRejectedError,
    InMemoryAgentScheduler,
)

# Mixed sync/async tests in this file — pytest-asyncio's asyncio_mode="auto"
# (pyproject.toml) picks up every `async def test_*` automatically, so no
# module-level pytestmark is needed here (see test_request_control.py's
# identical note).


# ── 1/2. Global concurrency ──────────────────────────────────────────────────

def test_acquire_global_succeeds_below_limit():
    scheduler = InMemoryAgentScheduler(global_max_concurrent=3, user_max_concurrent=100)
    tokens = [scheduler.try_acquire(f"user-{i}") for i in range(3)]
    assert scheduler.global_active_count == 3
    for t in tokens:
        t.release()
    assert scheduler.global_active_count == 0


def test_reject_when_global_full():
    scheduler = InMemoryAgentScheduler(global_max_concurrent=2, user_max_concurrent=100)
    scheduler.try_acquire("user-a")
    scheduler.try_acquire("user-b")
    with pytest.raises(AgentSchedulerRejectedError) as exc_info:
        scheduler.try_acquire("user-c")
    assert exc_info.value.reason == "concurrency_global"


# ── 3/4. Per-user concurrency ────────────────────────────────────────────────

def test_acquire_per_user_succeeds_below_limit():
    scheduler = InMemoryAgentScheduler(global_max_concurrent=100, user_max_concurrent=2)
    t1 = scheduler.try_acquire("user-a")
    t2 = scheduler.try_acquire("user-a")
    assert scheduler.user_active_count("user-a") == 2
    t1.release()
    t2.release()
    assert scheduler.user_active_count("user-a") == 0


def test_reject_when_user_full():
    scheduler = InMemoryAgentScheduler(global_max_concurrent=100, user_max_concurrent=1)
    scheduler.try_acquire("user-a")
    with pytest.raises(AgentSchedulerRejectedError) as exc_info:
        scheduler.try_acquire("user-a")
    assert exc_info.value.reason == "concurrency_user"


# ── 5. Rollback of the global slot when user acquisition fails ─────────────

def test_global_slot_rolled_back_when_user_acquisition_fails():
    scheduler = InMemoryAgentScheduler(global_max_concurrent=5, user_max_concurrent=1)
    scheduler.try_acquire("user-a")  # user-a now at its own limit (1/1)

    active_before = scheduler.global_active_count
    with pytest.raises(AgentSchedulerRejectedError) as exc_info:
        scheduler.try_acquire("user-a")
    assert exc_info.value.reason == "concurrency_user"
    # The global slot briefly acquired for this attempt must have been
    # released before raising — never leaked on a per-user rejection.
    assert scheduler.global_active_count == active_before


# ── 6. Idempotent release ────────────────────────────────────────────────────

def test_release_is_idempotent():
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)
    token = scheduler.try_acquire("user-a")
    assert scheduler.global_active_count == 1
    token.release()
    assert scheduler.global_active_count == 0
    token.release()  # must not go negative or raise
    token.release()
    assert scheduler.global_active_count == 0


def test_double_release_never_over_credits_capacity():
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)
    token = scheduler.try_acquire("user-a")
    token.release()
    token.release()
    token.release()
    new_token = scheduler.try_acquire("user-b")
    with pytest.raises(AgentSchedulerRejectedError):
        scheduler.try_acquire("user-c")
    new_token.release()


# ── 7/8. Different users vs. same user concurring ───────────────────────────

def test_different_users_concurring_do_not_block_each_other():
    scheduler = InMemoryAgentScheduler(global_max_concurrent=100, user_max_concurrent=1)
    token_a = scheduler.try_acquire("user-a")  # user-a now at its own limit (1/1)
    token_b = scheduler.try_acquire("user-b")  # unaffected — different key
    assert scheduler.user_active_count("user-a") == 1
    assert scheduler.user_active_count("user-b") == 1
    with pytest.raises(AgentSchedulerRejectedError):
        scheduler.try_acquire("user-a")
    token_a.release()
    token_b.release()


def test_same_user_concurring_is_bounded_by_the_user_limit():
    scheduler = InMemoryAgentScheduler(global_max_concurrent=100, user_max_concurrent=2)
    t1 = scheduler.try_acquire("user-a")
    t2 = scheduler.try_acquire("user-a")
    with pytest.raises(AgentSchedulerRejectedError) as exc_info:
        scheduler.try_acquire("user-a")
    assert exc_info.value.reason == "concurrency_user"
    t1.release()
    t2.release()


# ── 9/10. Limits never exceeded under a real concurrent race ────────────────

async def test_global_limit_never_exceeded_under_20_concurrent_agents():
    """Real concurrency proof (not just sequential calls): 20 coroutines
    race via asyncio.gather to acquire against a small global cap and a
    small per-user cap, simultaneously. try_acquire() has no `await` in its
    critical section (same argument as ConcurrencyLimiter itself — see
    request_control.py), so there is no interleaving window for two
    coroutines to both observe capacity and both succeed past the limit."""
    global_limit = 5
    user_limit = 2
    scheduler = InMemoryAgentScheduler(global_max_concurrent=global_limit, user_max_concurrent=user_limit)

    results: list[tuple[str, AgentExecutionToken | None]] = []

    async def attempt(i: int):
        user_id = f"user-{i % 10}"  # 10 distinct users, 2 attempts each
        try:
            token = scheduler.try_acquire(user_id)
            results.append(("ok", token))
        except AgentSchedulerRejectedError:
            results.append(("rejected", None))

    await asyncio.gather(*(attempt(i) for i in range(20)))

    succeeded = [t for kind, t in results if kind == "ok"]
    assert len(succeeded) == global_limit, (
        f"expected exactly {global_limit} acceptances (the global cap), got {len(succeeded)}"
    )
    assert scheduler.global_active_count == global_limit
    assert scheduler.global_active_count <= global_limit  # invariant, redundant but explicit

    for token in succeeded:
        token.release()
    assert scheduler.global_active_count == 0, "a slot is stuck after the race drained"


async def test_user_limit_never_exceeded_under_20_concurrent_agents_same_user():
    """Same race, but concentrated on ONE user against a generous global
    cap — proves the per-user dimension specifically is never exceeded,
    independent of the global one."""
    user_limit = 3
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1000, user_max_concurrent=user_limit)

    results: list[tuple[str, AgentExecutionToken | None]] = []

    async def attempt(i: int):
        try:
            token = scheduler.try_acquire("same-user")
            results.append(("ok", token))
        except AgentSchedulerRejectedError:
            results.append(("rejected", None))

    await asyncio.gather(*(attempt(i) for i in range(20)))

    succeeded = [t for kind, t in results if kind == "ok"]
    assert len(succeeded) == user_limit
    assert scheduler.user_active_count("same-user") == user_limit

    for token in succeeded:
        token.release()
    assert scheduler.user_active_count("same-user") == 0


async def test_mixed_users_and_global_limit_both_hold_under_concurrent_race():
    """Both dimensions active simultaneously — global cap smaller than what
    20 distinct users' own per-user caps would otherwise allow, proving the
    global dimension is the actual binding constraint here and is
    respected even though no single user ever hits their own limit."""
    global_limit = 7
    scheduler = InMemoryAgentScheduler(global_max_concurrent=global_limit, user_max_concurrent=5)

    results: list[AgentExecutionToken | None] = []

    async def attempt(i: int):
        try:
            results.append(scheduler.try_acquire(f"user-{i}"))  # 20 distinct users, own limit never hit
        except AgentSchedulerRejectedError:
            results.append(None)

    await asyncio.gather(*(attempt(i) for i in range(20)))

    succeeded = [t for t in results if t is not None]
    assert len(succeeded) == global_limit
    assert scheduler.global_active_count == global_limit

    for token in succeeded:
        token.release()
    assert scheduler.global_active_count == 0


# ── reset() ──────────────────────────────────────────────────────────────────

def test_reset_clears_global_and_per_user_counters():
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)
    token = scheduler.try_acquire("user-a")

    scheduler.reset()

    assert scheduler.global_active_count == 0
    new_token = scheduler.try_acquire("user-a")
    assert scheduler.global_active_count == 1
    new_token.release()
    token.release()  # pre-reset token: release is still safe (idempotent-ish no-op)


# ── Etapa 5G — metrics instrumentation ──────────────────────────────────────
# A real scheduler instance, actually exhausted — a mocked try_acquire()
# would only prove the mock ran, not that the real rejection/admission
# site in agent_scheduler.py increments the counter.

def test_rejection_increments_metric_with_reason():
    from src.observability.metrics import core_metrics

    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=100)
    token = scheduler.try_acquire("metrics-user")
    try:
        with pytest.raises(AgentSchedulerRejectedError):
            scheduler.try_acquire("metrics-user")
    finally:
        token.release()

    after = core_metrics.counter("agent_scheduler_rejections_total").snapshot()
    assert any(dict(k).get("reason") == "concurrency_global" for k in after)


def test_admission_increments_metric():
    from src.observability.metrics import core_metrics

    scheduler = InMemoryAgentScheduler(global_max_concurrent=5, user_max_concurrent=5)
    before = sum(core_metrics.counter("agent_scheduler_admitted_total").snapshot().values())
    token = scheduler.try_acquire("metrics-user-2")
    token.release()

    after = sum(core_metrics.counter("agent_scheduler_admitted_total").snapshot().values())
    assert after >= before + 1
