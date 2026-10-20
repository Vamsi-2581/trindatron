"""BrowserResourceGovernor — process-wide cap on concurrent Chromium processes.

One governor instance is meant to be shared by every BrowserSession in a
process (see session.py's lazily-constructed default instance, and how
agent_service.py constructs its own settings-driven instance instead). This
is the one piece of intentional shared/global state in the browser
subsystem — it exists specifically to bound total Chromium processes, never
to share any browser/session/page state itself.

Backed by asyncio.Semaphore, deliberately not reimplemented from scratch:
verified against the CPython 3.12 implementation (asyncio/locks.py) that
Semaphore.acquire(), when cancelled while waiting for a permit, removes its
waiter and restores the internal counter *before* re-raising — so a permit
is never consumed by a wait that was cancelled or timed out. That is what
makes `asyncio.wait_for(semaphore.acquire(), timeout=...)` used below safe:
a timeout firing, or an external cancellation while waiting, never leaks a
permit. What the raw Semaphore does NOT protect against is a caller
releasing more times than it acquired (this would silently push `_value`
above the configured capacity) — that is what _SlotToken's one-shot
`release()` exists to prevent.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("trinity_agent.browser.governor")


class BrowserCapacityTimeoutError(Exception):
    """Raised when acquiring a browser slot exceeds the configured wait timeout.

    Distinguishable by type from BrowserSessionError (session.py) — this
    means no Chromium/Playwright failure happened, there simply wasn't a
    free slot in time. Callers (the browser tools) already catch Exception
    broadly, so this still surfaces as a normal failed ToolResult, but any
    caller that cares can `except BrowserCapacityTimeoutError` specifically.
    """


class BrowserGovernorConfigError(Exception):
    """Raised at construction time for a capacity that cannot be honored
    (e.g. < 1) — never silently coerced into something that would hang or
    admit unlimited sessions."""


class _SlotToken:
    """One acquired unit of Chromium capacity.

    release() is idempotent: calling it more than once (e.g. once from a
    normal teardown path and again from an exception handler covering the
    same failure) is a safe no-op — it never lets the governor's active
    count go negative or be double-credited back to the semaphore.
    """

    __slots__ = ("_governor", "_released")

    def __init__(self, governor: "BrowserResourceGovernor") -> None:
        self._governor = governor
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._governor._release_one()


class BrowserResourceGovernor:
    """Bounds how many Chromium processes may exist at once, process-wide.

    Not thread-safe by design — this is an asyncio-cooperative structure
    for a single event loop, matching how the rest of this codebase runs
    (one Uvicorn worker's event loop). All internal counter mutations below
    are plain synchronous statements with no `await` in between, which is
    already atomic under asyncio's single-threaded cooperative scheduling —
    no additional lock is used or needed for them.

    Invariants (see test_browser_governor.py for direct verification):
      active_count + available_capacity == capacity   (always)
      0 <= active_count <= capacity                    (always)
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise BrowserGovernorConfigError(
                f"BrowserResourceGovernor capacity must be >= 1, got {capacity!r}"
            )
        self._capacity = capacity
        self._semaphore = asyncio.Semaphore(capacity)
        self._active = 0
        self._waiting = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def active_count(self) -> int:
        return self._active

    @property
    def available_capacity(self) -> int:
        return self._capacity - self._active

    @property
    def waiting_count(self) -> int:
        return self._waiting

    async def acquire(self, *, timeout: float | None = None) -> _SlotToken:
        """Acquire one unit of Chromium capacity.

        Caller owns the returned token and must call `.release()` exactly
        once when done (release() is idempotent, so "at least once, exactly
        once effectively" is fine too).

        Raises BrowserCapacityTimeoutError if `timeout` elapses first — no
        permit is consumed in that case (see module docstring). Propagates
        asyncio.CancelledError as-is if cancelled while waiting — likewise
        no permit is consumed.
        """
        logger.debug(
            "browser resource: waiting (active=%d/%d, waiting=%d)",
            self._active, self._capacity, self._waiting + 1,
        )
        self._waiting += 1
        try:
            try:
                if timeout is None:
                    await self._semaphore.acquire()
                else:
                    await asyncio.wait_for(self._semaphore.acquire(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                logger.warning(
                    "browser resource: acquisition timeout after %.1fs (active=%d/%d)",
                    timeout, self._active, self._capacity,
                )
                raise BrowserCapacityTimeoutError(
                    f"Timed out after {timeout}s waiting for browser capacity "
                    f"({self._active}/{self._capacity} in use)"
                ) from exc
            except asyncio.CancelledError:
                logger.debug("browser resource: acquisition cancelled while waiting")
                raise
        finally:
            self._waiting -= 1

        self._active += 1
        logger.debug(
            "browser resource: acquired (active=%d/%d)", self._active, self._capacity,
        )
        return _SlotToken(self)

    def _release_one(self) -> None:
        self._active = max(0, self._active - 1)
        self._semaphore.release()
        logger.debug(
            "browser resource: released (active=%d/%d)", self._active, self._capacity,
        )
