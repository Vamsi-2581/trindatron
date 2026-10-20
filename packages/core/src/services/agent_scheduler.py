"""Agent Scheduler — Etapa 5D of the TRINITY SCALE ENGINE.

Bounds concurrent Agent/ReAct executions specifically — a layer ABOVE
Request Control (request_control.py, Etapa 5B) and ABOVE
BrowserResourceGovernor (trinity_agent.browser.governor, controls
Chromium processes specifically, one level deeper inside a single Agent
execution). None of the three is replaced by another:

    Request Control  → protects the whole process / every request
                        (rate limits + generic in-flight concurrency,
                        chat AND agent alike).
    Agent Scheduler   → protects Agent/ReAct executions specifically
                        (global + per-user concurrency, execution
                        timeout) — this module.
    BrowserResourceGovernor
                      → protects concurrent Chromium processes
                        specifically, invoked deeper inside a single
                        Agent execution whenever a browser tool runs.

A single /agent (or /message routed to the Agent executor) request passes
through ALL THREE, in that order: Request Control's slot is acquired first
(sessions.py's _enter_request_control), then this module's slot, then —
only if the Agent's ReAct loop actually invokes a browser tool —
BrowserResourceGovernor's slot, scoped to that one BrowserSession.

── Why gated by executor, not by LoadClass ─────────────────────────────────
LoadClass (load_classification.py, Etapa 5C) is informational only here.
The AUTHORITY for "is this an Agent execution" is which function is about
to run: agent_service.stream_agent() (the ReAct loop, full tool registry)
vs chat_service.stream_chat() (plain completion, no tools). A request whose
Capability is FILES classifies as LoadClass.MEDIUM in 5C, but if its
TaskType is AGENT it still executes the IDENTICAL stream_agent() ReAct loop
as a HEAVY capability — so it MUST still acquire an AgentExecutionToken
here. Never branch scheduler admission on LoadClass; branch it on whether
stream_agent() is about to be called (sessions.py already knows this: it is
`task_type == TaskType.AGENT`, the same condition it already uses to pick
which of stream_chat()/stream_agent() to call).

── No new concurrency primitive ─────────────────────────────────────────────
Reuses ConcurrencyLimiter / PerKeyConcurrencyLimiter / ConcurrencyLimitExceededError
from request_control.py rather than reimplementing them — identical
atomicity argument (no `await` in the critical section, safe under
asyncio's single-threaded cooperative scheduling, no lock needed), identical
idempotent-release contract. AgentExecutionToken here is the Agent-Scheduler
analogue of RequestControlToken: it aggregates the global + per-user slots
for one execution and releases both, exactly once, from wherever the
execution's own cleanup logic calls it.

── IN-MEMORY / SINGLE PROCESS ──────────────────────────────────────────────
Same documented limitation as request_control.py: state lives entirely in
this process's memory, correct for exactly today's deployment (one Uvicorn
worker, no horizontal scaling). InMemoryAgentScheduler implements the
AgentScheduler ABC specifically so a future DistributedAgentScheduler
(Redis-backed, Etapa 5-later) is a drop-in replacement — no call site in
sessions.py would need to change beyond constructing a different instance.
NOT built now, per explicit scope for this etapa.

── Streaming lifecycle ──────────────────────────────────────────────────────
AgentExecutionToken is threaded into stream_agent() as a new parameter
(agent_scheduler_token), released in stream_agent()'s own `finally` block —
the SAME mechanism already used for request_control_token (Etapa 5B) and
browser_session.close(). The slot's lifetime is therefore the whole stream,
not the route handler's near-instant return of a StreamingResponse.

Execution timeout is a SEPARATE, orthogonal concern implemented by
with_execution_timeout() below: a generic async-generator wrapper that
enforces an absolute deadline on ANY async generator, with no knowledge of
tokens at all. sessions.py applies it around stream_agent(...) before
handing the result to StreamingResponse. On timeout it cancels the
underlying execution — which delivers asyncio.CancelledError at whatever
await point stream_agent() is currently suspended, triggering ITS OWN
`finally` block exactly as any other cancellation would (see
test_agent_scheduler_streaming.py) — so timeout cleanup reuses the exact
same release path as every other exit, rather than needing bespoke
timeout-specific cleanup code.

Does NOT touch: TCU, billing, the Model Router, Request Control's own
limits, BrowserResourceGovernor's implementation, or LoadClassifier's
mapping.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from abc import ABC, abstractmethod
from typing import AsyncGenerator

from src.observability.metrics import core_metrics
from src.services.request_control import (
    ConcurrencyLimitExceededError,
    ConcurrencyLimiter,
    PerKeyConcurrencyLimiter,
)

logger = logging.getLogger("trinity.agent_scheduler")

# Etapa 5G — additive alongside the existing logger calls below; does not
# change any accept/reject decision.
_rejections_total = core_metrics.counter(
    "agent_scheduler_rejections_total", "Agent Scheduler rejections, labeled by reason.",
)
_timeouts_total = core_metrics.counter(
    "agent_scheduler_timeouts_total", "Agent executions cut off by with_execution_timeout().",
)
_admitted_total = core_metrics.counter(
    "agent_scheduler_admitted_total", "Agent executions admitted by the scheduler.",
)


class AgentSchedulerRejectedError(Exception):
    """Raised when the scheduler refuses to admit a new Agent execution.
    `reason` is one of "concurrency_global", "concurrency_user",
    "concurrency_plan_limit" — never exposed to the client as-is (route
    handlers translate this to a generic 429), used only for logging/tests."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"Agent execution rejected by scheduler: {reason}")
        self.reason = reason


class AgentExecutionToken:
    """Holds every concurrency slot (global + per-user) acquired for one
    Agent execution. release() is idempotent and releases all of them —
    call sites never need to know how many dimensions are involved. Same
    shape/contract as request_control.py's RequestControlToken.

    Lifetime: created in the route handler (before StreamingResponse is
    returned), released inside stream_agent()'s own `finally` block — NOT
    at the end of the route handler function. See module docstring.
    """

    __slots__ = ("_slots", "_released")

    def __init__(self, slots: list) -> None:
        self._slots = slots
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        for slot in self._slots:
            slot.release()


class AgentScheduler(ABC):
    """Admission-control abstraction for Agent/ReAct executions. Swappable
    for a future distributed (Redis-backed) implementation — see module
    docstring."""

    @abstractmethod
    def try_acquire(self, user_id: str) -> AgentExecutionToken:
        """Acquire one Agent execution slot (global + per-user), or raise
        AgentSchedulerRejectedError immediately. Never queues, never
        blocks — same fail-fast contract as request_control.py's
        acquire_concurrency()."""

    @abstractmethod
    def reset(self) -> None:
        """Test-only: clear all counters back to empty."""


class InMemoryAgentScheduler(AgentScheduler):
    def __init__(self, *, global_max_concurrent: int, user_max_concurrent: int) -> None:
        self._global = ConcurrencyLimiter(global_max_concurrent)
        self._user = PerKeyConcurrencyLimiter(user_max_concurrent)
        # Kept alongside self._user (which only stores this internally,
        # unexposed) so check_plan_concurrency() below can compute
        # min(global default, plan's own max_agents) without reaching into
        # PerKeyConcurrencyLimiter's private state.
        self._user_max_concurrent = user_max_concurrent

    @property
    def global_active_count(self) -> int:
        return self._global.active_count

    @property
    def global_limit(self) -> int:
        return self._global.limit

    def user_active_count(self, user_id: str) -> int:
        return self._user.active_count(user_id)

    def try_acquire(self, user_id: str) -> AgentExecutionToken:
        # Global slot before per-user slot — deliberate, matches
        # request_control.py's acquire_concurrency() ordering (cheaper to
        # reject on the cheap/shared dimension first) and its rollback
        # rule: if per-user acquisition then fails, the already-acquired
        # global slot is released before raising — never leaks the global
        # slot on a per-user rejection.
        try:
            global_slot = self._global.try_acquire()
        except ConcurrencyLimitExceededError:
            logger.warning(
                "agent_scheduler: rejected (global capacity) | active=%d/%d",
                self._global.active_count, self._global.limit,
            )
            _rejections_total.inc(reason="concurrency_global")
            raise AgentSchedulerRejectedError("concurrency_global") from None

        try:
            user_slot = self._user.try_acquire(user_id)
        except ConcurrencyLimitExceededError:
            global_slot.release()
            logger.warning(
                "agent_scheduler: rejected (user capacity) | active=%d",
                self._user.active_count(user_id),
            )
            _rejections_total.inc(reason="concurrency_user")
            raise AgentSchedulerRejectedError("concurrency_user") from None

        logger.info(
            "agent_scheduler: acquired | global_active=%d/%d user_active=%d",
            self._global.active_count, self._global.limit, self._user.active_count(user_id),
        )
        _admitted_total.inc()
        return AgentExecutionToken([global_slot, user_slot])

    def reset(self) -> None:
        self._global.reset()
        self._user.reset()

    def check_plan_concurrency(self, user_id: str, plan_max_agents: int | None) -> None:
        """Plan-level concurrency pre-check (Entitlement/max_agents,
        2026-09-07) — a SEPARATE, narrower concept from try_acquire()'s own
        global/per-user gate above. try_acquire()'s signature is
        deliberately frozen at (self, user_id) only — see
        test_agent_scheduler_executor_authority.py: the scheduler itself
        must never be driven by anything Capability/LoadClass-shaped. A
        plan's own numeric entitlement (PlanEntitlement.max_agents) is
        neither of those — it is checked here, in a dedicated method, by
        the CALLER (sessions.py) BEFORE try_acquire() is ever invoked, so
        try_acquire() itself is completely unchanged.

        effective_limit = min(this scheduler's own per-user default,
        plan_max_agents). plan_max_agents=None (unlimited) leaves the
        scheduler's own default as the only bound — this is a no-op for
        any plan whose declared max_agents is already >= that default
        (start=5, pro=20, max/team=None all fall in this case today,
        given the scheduler's own default of 2 — see limits.py's PR notes
        for the exact numbers). Only a plan with an effective limit BELOW
        the scheduler's own default (FREE=0, today) actually changes
        behavior. Raises the SAME AgentSchedulerRejectedError the real
        capacity check already raises — reason="concurrency_plan_limit"
        distinguishes it in logs/metrics only, never exposed to the
        client differently (see that exception's own docstring)."""
        if plan_max_agents is None:
            return
        effective_limit = min(self._user_max_concurrent, plan_max_agents)
        active = self._user.active_count(user_id)
        if effective_limit <= 0 or active >= effective_limit:
            logger.warning(
                "agent_scheduler: rejected (plan concurrency limit) | user=%s effective_limit=%d active=%d",
                user_id, effective_limit, active,
            )
            _rejections_total.inc(reason="concurrency_plan_limit")
            raise AgentSchedulerRejectedError("concurrency_plan_limit")


def _build_default_agent_scheduler() -> InMemoryAgentScheduler:
    from src.config import settings

    return InMemoryAgentScheduler(
        global_max_concurrent=settings.agent_scheduler_global_max_concurrent,
        user_max_concurrent=settings.agent_scheduler_user_max_concurrent,
    )


# Process-wide singleton — same pattern as request_control.py's
# `request_control`, _router, and _browser_governor elsewhere.
agent_scheduler = _build_default_agent_scheduler()


async def with_execution_timeout(
    agen: AsyncGenerator[str, None], *, timeout_s: float,
) -> AsyncGenerator[str, None]:
    """Enforce an absolute wall-clock deadline on an async generator,
    yielding through everything it produces until either it finishes
    naturally or the deadline passes.

    Revision note (post-Etapa-5D correction): an earlier version of this
    function drove `agen` from a separate background task, reading its
    output through an `asyncio.Queue` consumed via
    `asyncio.wait_for(queue.get(), timeout=remaining)`. That still had a
    real defect: `wait_for(coro)` wraps a bare coroutine in a Task it
    creates and owns internally — if THIS generator's own `__anext__()` is
    itself cancelled or `aclose()`'d by its caller (Starlette, on client
    disconnect) while suspended inside that `wait_for` call, there is no
    guarantee that internal, not-directly-referenced Task is ever awaited
    to completion; the event loop can log "Task was destroyed but it is
    pending!" for it. The fix below eliminates every use of
    `asyncio.wait_for` (and therefore every implicitly-created,
    not-directly-owned Task) from this function entirely.

    How it works: each loop iteration explicitly creates exactly two Tasks
    it holds direct references to — `anext_task` (wraps `agen.__anext__()`)
    and `sleep_task` (the remaining time budget) — and races them with
    `asyncio.wait(..., return_when=FIRST_COMPLETED)`. The `finally`
    immediately wrapping that race unconditionally cancels whichever of
    the two is still pending and awaits BOTH to a terminal state before
    this function's frame is allowed to continue or unwind further —
    regardless of whether the race resolved normally, or this coroutine
    itself was thrown GeneratorExit/CancelledError while suspended at the
    `await asyncio.wait(...)` line (Python's `finally` semantics guarantee
    this block still runs first, and — since it does not itself raise or
    return — the original exception automatically continues propagating
    once it completes; no manual re-raise is needed). Every Task this
    function ever creates is therefore either fully awaited or explicitly
    cancelled-then-awaited before the function returns, on every single
    exit path: success, exception, CancelledError, GeneratorExit/aclose(),
    and timeout. See test_agent_scheduler_task_leak.py for direct proof
    (captures asyncio.all_tasks() before/after each scenario).

    Cancelling `anext_task` delivers asyncio.CancelledError directly at
    whatever await point `agen`'s frame is currently suspended at (the
    exact same, already-verified mechanism used throughout this codebase's
    streaming lifecycle tests — e.g. Etapa 5B's) — so a timeout genuinely
    tears down the in-flight execution (provider call, tool call, browser
    operation) via `agen`'s own `finally` block, not merely the consumer
    on this function's side of the fence.

    The outer `finally: await agen.aclose()` covers the one remaining
    case: the deadline expires (or an external cancellation arrives)
    exactly between two loop iterations, i.e. while `agen` is legitimately
    suspended at one of its OWN yield points with no `anext_task` in
    flight for it at that moment. `aclose()` on an already-finished/
    already-cancelled generator is a documented no-op, so calling it
    unconditionally here is always safe.
    """
    deadline = time.monotonic() + timeout_s
    timed_out = False

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break

            anext_task = asyncio.ensure_future(agen.__anext__())
            sleep_task = asyncio.ensure_future(asyncio.sleep(remaining))
            try:
                done, _pending = await asyncio.wait(
                    {anext_task, sleep_task}, return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                # Unconditional: runs whether the race above resolved
                # normally OR this coroutine itself was interrupted while
                # awaiting it. Cancel whichever of the two never finished,
                # then await both to a terminal state — nothing created in
                # this iteration is ever left pending for the event loop
                # to warn about. Outcomes are inspected explicitly below
                # (in the normal-completion path only), so any exception
                # surfacing from awaiting an already-decided task here is
                # deliberately not re-raised — it would either duplicate
                # (StopAsyncIteration/a real error, re-derived from
                # anext_task.exception() below) or mask (our own
                # self-inflicted CancelledError) the correct outcome.
                for t in (anext_task, sleep_task):
                    if not t.done():
                        t.cancel()
                for t in (anext_task, sleep_task):
                    try:
                        await t
                    except BaseException:
                        pass

            # Reached only on a normal (uninterrupted) race resolution —
            # if this coroutine itself was cancelled/closed above, the
            # `finally` already ran and the interrupting exception is
            # propagating past this point automatically; this line never
            # executes in that case.
            if anext_task in done:
                exc = anext_task.exception()
                if isinstance(exc, StopAsyncIteration):
                    return
                if exc is not None:
                    raise exc
                yield anext_task.result()
                continue

            # sleep_task won the race — anext_task was cancelled above.
            timed_out = True
            break
    finally:
        await agen.aclose()

    if timed_out:
        logger.warning("agent_scheduler: execution timeout after %.1fs", timeout_s)
        _timeouts_total.inc()
        yield f"data: {json.dumps({'type': 'error', 'message': 'Agent execution timed out.'})}\n\n"
        yield "data: [DONE]\n\n"
