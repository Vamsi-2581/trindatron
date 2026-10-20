"""Etapa 5D, section 20 — local mocked stress test: at least 10, 50, and
100 simultaneous Agent executions, proving the Agent Scheduler specifically
bounds concurrency under load (no limit exceeded, no leaked slot, clean
recovery after the burst, no orphaned execution) — no real providers.

Design note, same rationale as Etapa 5B's
test_request_control_stress.py: pushing 50-100 genuinely concurrent
DB-writing HTTP requests through SQLite (this suite's test database) hits a
SQLite-specific wall (single-writer serialization) that has nothing to do
with the Agent Scheduler itself — production runs on PostgreSQL with a real
connection pool, already exercised by the separate Postgres integration
suite. These tests therefore drive the process-wide `agent_scheduler`
singleton directly with the exact same `try_acquire()` call sessions.py
uses, each simulated execution holding its slot for a short realistic
duration via asyncio.sleep(), all launched together via asyncio.gather() so
all N genuinely race for capacity at once. A companion test exercises the
real /agent HTTP endpoint end-to-end at a scale (10) that stays clear of
the SQLite wall, confirming the two layers are correctly wired together.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from trinity_router import RoutingResult

from src.services.agent_scheduler import AgentSchedulerRejectedError, agent_scheduler

pytestmark = pytest.mark.asyncio


# ── Scheduler-only bursts (no DB/HTTP) — the core of section 20 ────────────

async def _run_scheduler_only_burst(monkeypatch, n: int, global_cap: int, user_cap: int, hold_s: float = 0.05):
    monkeypatch.setattr(agent_scheduler._global, "_limit", global_cap)
    monkeypatch.setattr(agent_scheduler._user, "_limit", user_cap)

    results: list[str] = []
    max_observed_global = 0
    stop = asyncio.Event()

    async def observer():
        nonlocal max_observed_global
        while not stop.is_set():
            max_observed_global = max(max_observed_global, agent_scheduler.global_active_count)
            await asyncio.sleep(0.002)

    async def one_simulated_execution(i: int):
        user_id = f"stress-agent-user-{i}"
        try:
            token = agent_scheduler.try_acquire(user_id)
        except AgentSchedulerRejectedError:
            results.append("rejected")
            return
        try:
            await asyncio.sleep(hold_s)  # simulate an in-flight ReAct loop
            results.append("accepted")
        finally:
            token.release()

    observer_task = asyncio.create_task(observer())
    await asyncio.gather(*(one_simulated_execution(i) for i in range(n)))
    stop.set()
    await observer_task

    accepted = results.count("accepted")
    rejected = results.count("rejected")
    assert accepted + rejected == n
    assert accepted <= global_cap, f"{accepted} accepted but global cap was {global_cap}"
    assert max_observed_global <= global_cap, f"active_count peaked at {max_observed_global}, above cap {global_cap}"
    assert agent_scheduler.global_active_count == 0, "a slot leaked after the burst drained"
    return accepted, rejected


@pytest.mark.parametrize("n,global_cap,user_cap", [(10, 5, 100), (50, 10, 100), (100, 20, 100)])
async def test_agent_scheduler_bounds_concurrency_under_a_simultaneous_burst(monkeypatch, n, global_cap, user_cap):
    accepted, rejected = await _run_scheduler_only_burst(monkeypatch, n, global_cap, user_cap)
    assert accepted >= 1
    assert rejected >= 1, "burst must actually exceed capacity for this to be a meaningful test"


async def test_agent_scheduler_recovers_after_a_100_execution_burst(monkeypatch):
    await _run_scheduler_only_burst(monkeypatch, n=100, global_cap=20, user_cap=100)

    monkeypatch.setattr(agent_scheduler._global, "_limit", 50)
    monkeypatch.setattr(agent_scheduler._user, "_limit", 50)
    token = agent_scheduler.try_acquire("recovery-user")
    assert agent_scheduler.global_active_count == 1
    token.release()
    assert agent_scheduler.global_active_count == 0


async def test_no_orphaned_execution_after_a_burst_every_slot_is_accounted_for(monkeypatch):
    """Cross-check accepted+rejected against the actual number of attempts,
    and confirm every accepted token's user dimension also drains to zero —
    proving no execution silently vanished mid-accounting."""
    n, global_cap, user_cap = 50, 15, 3
    monkeypatch.setattr(agent_scheduler._global, "_limit", global_cap)
    monkeypatch.setattr(agent_scheduler._user, "_limit", user_cap)

    tokens = []
    results = []

    async def attempt(i: int):
        user_id = f"user-{i % 8}"  # 8 distinct users
        try:
            tokens.append((user_id, agent_scheduler.try_acquire(user_id)))
            results.append("ok")
        except AgentSchedulerRejectedError:
            results.append("rejected")

    await asyncio.gather(*(attempt(i) for i in range(n)))
    assert len(results) == n
    assert results.count("ok") == len(tokens)

    for user_id, token in tokens:
        token.release()

    assert agent_scheduler.global_active_count == 0
    for i in range(8):
        assert agent_scheduler.user_active_count(f"user-{i}") == 0


# ── One real end-to-end HTTP burst, small enough to avoid the SQLite wall ──

def _routing_result() -> RoutingResult:
    return RoutingResult(
        content="done", tool_calls=None, provider="anthropic", model="claude-sonnet-test",
        input_tokens=3, output_tokens=3, cached_tokens=0, cost_usd=0.0, tcu=0.0,
        failover_used=False, failover_reason=None,
    )


async def test_10_simultaneous_agent_requests_through_the_real_http_endpoint(client, auth_headers, monkeypatch):
    """Synchronous TestClient calls are inherently sequential (one request
    fully completes before the next starts), so this cannot race 10 truly
    concurrent requests the way the scheduler-only tests above do — but it
    still proves the real /agent endpoint, wired to the real process-wide
    scheduler, correctly acquires and releases a slot on every call, N
    times in a row, with zero leakage accumulating across them."""
    n = 10
    monkeypatch.setattr(agent_scheduler._global, "_limit", 3)
    monkeypatch.setattr(agent_scheduler._user, "_limit", 3)

    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    with patch("src.services.agent_service._router.complete", new=AsyncMock(return_value=_routing_result())):
        for i in range(n):
            resp = client.post(f"/sessions/{session_id}/agent", json={"content": f"msg {i}"}, headers=auth_headers)
            assert resp.status_code == 200

    assert agent_scheduler.global_active_count == 0
