"""Request Control — Etapa 5B of the TRINITY SCALE ENGINE.

The entry-point layer that protects the Core process from traffic spikes
and abuse BEFORE any expensive work (session lookup, TCU reservation,
provider/agent calls) happens. Two independent protections:

  1. Rate limiting (RateLimiter) — how many requests a user/IP may make in
     a sliding time window. A one-shot check; nothing is "held" afterward.
  2. Concurrency limiting (ConcurrencyLimiter) — how many requests a
     user/the whole process may have IN FLIGHT at once. Acquired before
     the response starts, held for the entire lifetime of the request —
     for streaming (SSE) requests specifically, that means held for the
     *entire stream*, not just the (near-instant) route handler call that
     returns the StreamingResponse. See the module docstring in
     agent_service.py/chat_service.py's stream_* functions for how the
     token's release is wired into their own `finally` blocks — the
     ROUTE HANDLER'S OWN return does NOT end the request's lifetime for a
     StreamingResponse, so releasing there would be wrong (the classic
     "acquire() / await endpoint() / release()" trap this was deliberately
     designed to avoid).

Rejection is immediate (no queueing/backoff) — try_acquire() either
succeeds or raises RequestRejectedError right away. Queueing heavy work is
the Agent Scheduler's job (Etapa 5D), not this layer's.

IN-MEMORY / SINGLE PROCESS (documented limitation, not fixed here):
RateLimiter and ConcurrencyLimiter state below lives entirely in this
process's memory. It is correct for exactly the deployment this codebase
runs today (one Uvicorn worker, no horizontal scaling — see the Etapa 5
audit). The moment TRINITY CORE runs as multiple replicas/workers, a rate
limit or a concurrency cap enforced independently per-process no longer
bounds the TRUE global rate/concurrency across all of them — at that point
InMemoryRateLimiter and ConcurrencyLimiter's backing state need to move to
a shared store (Redis is the natural choice, matching the project's own
already-planned "Phase 3 — escala" roadmap). The RateLimiter ABC exists
specifically so that swap only touches this module (a new
RedisRateLimiter(RateLimiter) implementation) — no route/service call site
would need to change. ConcurrencyLimiter's synchronous, no-queueing
try_acquire() contract would need the same externalization (e.g. atomic
INCR/DECR against a shared counter with a TTL safety net) when that day
comes; not implemented now, per explicit scope.

Does NOT touch: TCU calculation/reservation/finalize/cancel, the advisory
lock, billing/ledger, the Model Router, or Modo Continuidade. This is a
layer strictly BEFORE all of that.
"""
from __future__ import annotations

import collections
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from src.observability.metrics import core_metrics

logger = logging.getLogger("trinity.request_control")

# Etapa 5G — additive alongside the existing logger.warning() calls below;
# does not change any accept/reject decision.
_rejections_total = core_metrics.counter(
    "request_control_rejections_total", "Request Control rejections, labeled by reason.",
)


# ── Rate limiting ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: float | None = None
    limit: int = 0
    remaining: int = 0


class RateLimiter(ABC):
    """Backend-agnostic rate limit check. `check()` is async so a future
    Redis-backed implementation (real network I/O) is a drop-in replacement
    for InMemoryRateLimiter — no call site needs to change."""

    @abstractmethod
    async def check(self, key: str, *, limit: int, window_seconds: float) -> RateLimitDecision: ...


class InMemoryRateLimiter(RateLimiter):
    """Sliding-window-log rate limiter, process-local in-memory state.

    Exact (not approximated like fixed-window counters): stores the
    timestamp of each accepted request per key in a deque, prunes entries
    older than the window on every check, and admits iff the remaining
    count is still under `limit`.

    Concurrency safety: `check()` contains no `await` between reading and
    mutating a key's deque — under asyncio's single-threaded cooperative
    scheduling that makes the whole read-prune-decide-append sequence
    atomic with respect to every other coroutine, with no lock needed (the
    same invariant BrowserResourceGovernor's counters already rely on).
    This is NOT safe across multiple OS threads or processes — see the
    module docstring's "IN-MEMORY / SINGLE PROCESS" note.

    Known limitation: the per-key dict grows with every distinct key ever
    seen (user_id or IP) and entries are only pruned lazily on that key's
    own next check — a key that stops sending requests keeps an (empty,
    cheap) deque forever. Acceptable for a single long-running process at
    today's scale; a future Redis-backed implementation gets this for free
    via key TTLs.
    """

    def __init__(self) -> None:
        self._windows: dict[str, collections.deque[float]] = {}

    async def check(self, key: str, *, limit: int, window_seconds: float) -> RateLimitDecision:
        now = time.monotonic()
        dq = self._windows.setdefault(key, collections.deque())
        cutoff = now - window_seconds
        while dq and dq[0] < cutoff:
            dq.popleft()

        if len(dq) >= limit:
            retry_after = max(0.0, dq[0] + window_seconds - now)
            return RateLimitDecision(allowed=False, retry_after_seconds=retry_after, limit=limit, remaining=0)

        dq.append(now)
        return RateLimitDecision(allowed=True, limit=limit, remaining=max(0, limit - len(dq)))

    def reset(self) -> None:
        """Test-only: clear all per-key windows. Never called from a
        production code path — see RequestControl.reset()."""
        self._windows.clear()


# ── Concurrency limiting ─────────────────────────────────────────────────────


class ConcurrencyLimitExceededError(Exception):
    """Raised by try_acquire() when the limit is already at capacity.
    Never queues — Request Control rejects fast, it does not wait."""


class _ConcurrencySlot:
    """One acquired unit of concurrency. release() is idempotent — a
    double-release (e.g. once from an error-handling path and again from a
    stream's own `finally`) is a safe no-op, never over-credits the
    underlying counter."""

    __slots__ = ("_release_fn", "_released")

    def __init__(self, release_fn) -> None:
        self._release_fn = release_fn
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._release_fn()


class ConcurrencyLimiter:
    """Bounds concurrent in-flight requests for one dimension (e.g. the
    whole process, or reused per-key for one user — see
    PerKeyConcurrencyLimiter). Synchronous by design: acquiring or
    rejecting is an in-memory decision with no I/O, so there is no `await`
    boundary across which two coroutines could interleave — same
    single-threaded-cooperative-scheduling safety argument as
    InMemoryRateLimiter above.
    """

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError(f"concurrency limit must be >= 1, got {limit!r}")
        self._limit = limit
        self._active = 0

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def active_count(self) -> int:
        return self._active

    def try_acquire(self) -> _ConcurrencySlot:
        if self._active >= self._limit:
            raise ConcurrencyLimitExceededError(
                f"concurrency limit reached ({self._active}/{self._limit})"
            )
        self._active += 1
        return _ConcurrencySlot(self._release_one)

    def _release_one(self) -> None:
        self._active = max(0, self._active - 1)

    def reset(self) -> None:
        """Test-only: force active_count back to 0. Never called from a
        production code path — see RequestControl.reset()."""
        self._active = 0


class PerKeyConcurrencyLimiter:
    """One ConcurrencyLimiter per key (e.g. per user_id), created lazily,
    all sharing the same configured `limit`. Same "grows with unique keys"
    characteristic as InMemoryRateLimiter's window dict — a key with zero
    active requests keeps a near-free ConcurrencyLimiter object around
    rather than being evicted; acceptable at today's scale, see module
    docstring.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._limiters: dict[str, ConcurrencyLimiter] = {}

    def try_acquire(self, key: str) -> _ConcurrencySlot:
        limiter = self._limiters.get(key)
        if limiter is None:
            limiter = ConcurrencyLimiter(self._limit)
            self._limiters[key] = limiter
        return limiter.try_acquire()

    def active_count(self, key: str) -> int:
        limiter = self._limiters.get(key)
        return limiter.active_count if limiter is not None else 0

    def reset(self) -> None:
        """Test-only: drop every per-key limiter. Never called from a
        production code path — see RequestControl.reset()."""
        self._limiters.clear()


# ── Request Control facade ──────────────────────────────────────────────────


class RequestRejectedError(Exception):
    """Raised when Request Control refuses a request. `reason` is one of:
    "rate_limit_user", "rate_limit_ip", "concurrency_user",
    "concurrency_global" — never exposed to the client as-is (route
    handlers translate this to a generic 429), used only for logging/tests.
    """

    def __init__(self, reason: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(f"Request rejected by request control: {reason}")
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds


class RequestControlToken:
    """Holds every concurrency slot acquired for one request (global +
    per-user). release() is idempotent and releases all of them —
    call sites never need to know how many dimensions are involved.

    Lifetime: for a streaming (SSE) endpoint, this token is created in the
    route handler (before StreamingResponse is returned) and released
    inside the streaming generator's own `finally` block — NOT at the end
    of the route handler function, which returns almost immediately and
    would release the slot long before the stream actually finishes. See
    module docstring.
    """

    __slots__ = ("_slots", "_released")

    def __init__(self, slots: list[_ConcurrencySlot]) -> None:
        self._slots = slots
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        for slot in self._slots:
            slot.release()


class RequestControl:
    """Entry-point guard for the chat/agent endpoints.

    Two calls, used together at the top of a route handler:
      1. `await check_rate_limits(user_id, client_ip)` — one-shot, no
         token, raises RequestRejectedError if either limit is exceeded.
      2. `acquire_concurrency(user_id)` — returns a RequestControlToken the
         caller must eventually `.release()` (directly, or by forwarding
         it into stream_chat()/stream_agent()'s `request_control_token`
         parameter for release in their `finally`), raises
         RequestRejectedError if either the global or the per-user
         concurrency limit is already at capacity.

    Order matters and is deliberate: rate limits are checked before any
    concurrency slot is acquired (cheaper check first, nothing to release
    on that path), and global concurrency is acquired before per-user
    concurrency (if per-user acquisition then fails, the already-acquired
    global slot is released before raising — never leaks the global slot
    on a per-user rejection).
    """

    def __init__(
        self,
        *,
        user_rate_limiter: RateLimiter,
        ip_rate_limiter: RateLimiter,
        user_concurrency: PerKeyConcurrencyLimiter,
        global_concurrency: ConcurrencyLimiter,
        user_rate_limit: int,
        user_rate_window_s: float,
        ip_rate_limit: int,
        ip_rate_window_s: float,
    ) -> None:
        self._user_rate_limiter = user_rate_limiter
        self._ip_rate_limiter = ip_rate_limiter
        self._user_concurrency = user_concurrency
        self._global_concurrency = global_concurrency
        self._user_rate_limit = user_rate_limit
        self._user_rate_window_s = user_rate_window_s
        self._ip_rate_limit = ip_rate_limit
        self._ip_rate_window_s = ip_rate_window_s

    async def check_rate_limits(self, *, user_id: str, client_ip: str | None) -> None:
        if client_ip is not None:
            ip_decision = await self._ip_rate_limiter.check(
                client_ip, limit=self._ip_rate_limit, window_seconds=self._ip_rate_window_s,
            )
            if not ip_decision.allowed:
                logger.warning(
                    "request_control: ip rate limit exceeded | limit=%d window_s=%.0f",
                    self._ip_rate_limit, self._ip_rate_window_s,
                )
                _rejections_total.inc(reason="rate_limit_ip")
                raise RequestRejectedError("rate_limit_ip", ip_decision.retry_after_seconds)

        user_decision = await self._user_rate_limiter.check(
            user_id, limit=self._user_rate_limit, window_seconds=self._user_rate_window_s,
        )
        if not user_decision.allowed:
            logger.warning(
                "request_control: user rate limit exceeded | user=%s limit=%d window_s=%.0f",
                user_id, self._user_rate_limit, self._user_rate_window_s,
            )
            _rejections_total.inc(reason="rate_limit_user")
            raise RequestRejectedError("rate_limit_user", user_decision.retry_after_seconds)

    def acquire_concurrency(self, user_id: str) -> RequestControlToken:
        try:
            global_slot = self._global_concurrency.try_acquire()
        except ConcurrencyLimitExceededError:
            logger.warning(
                "request_control: global concurrency limit exceeded | active=%d/%d",
                self._global_concurrency.active_count, self._global_concurrency.limit,
            )
            _rejections_total.inc(reason="concurrency_global")
            raise RequestRejectedError("concurrency_global") from None

        try:
            user_slot = self._user_concurrency.try_acquire(user_id)
        except ConcurrencyLimitExceededError:
            global_slot.release()
            logger.warning(
                "request_control: user concurrency limit exceeded | user=%s active=%d",
                user_id, self._user_concurrency.active_count(user_id),
            )
            _rejections_total.inc(reason="concurrency_user")
            raise RequestRejectedError("concurrency_user") from None

        return RequestControlToken([global_slot, user_slot])

    def reset(self) -> None:
        """Test-only: clear every rate-limit window and concurrency counter
        back to empty. Never called from a production code path — exists
        so tests don't leak state into each other through this
        process-wide singleton (a real, observed failure mode: without
        this, the IP rate-limit window accumulates across every test in a
        pytest session and eventually starts rejecting otherwise-valid
        test requests with a 429 that has nothing to do with what that
        test is actually exercising).
        """
        self._user_rate_limiter.reset()
        self._ip_rate_limiter.reset()
        self._user_concurrency.reset()
        self._global_concurrency.reset()


def _build_default_request_control() -> RequestControl:
    from src.config import settings

    return RequestControl(
        user_rate_limiter=InMemoryRateLimiter(),
        ip_rate_limiter=InMemoryRateLimiter(),
        user_concurrency=PerKeyConcurrencyLimiter(settings.request_control_user_max_concurrent),
        global_concurrency=ConcurrencyLimiter(settings.request_control_global_max_concurrent),
        user_rate_limit=settings.request_control_user_rate_limit,
        user_rate_window_s=settings.request_control_user_rate_window_s,
        ip_rate_limit=settings.request_control_ip_rate_limit,
        ip_rate_window_s=settings.request_control_ip_rate_window_s,
    )


# Process-wide singleton — every route handler acquires against this same
# instance, exactly like _router (trinity_router.ModelRouter) and
# _browser_governor (trinity_agent.browser.governor.BrowserResourceGovernor)
# are already shared singletons elsewhere in this codebase.
request_control = _build_default_request_control()
