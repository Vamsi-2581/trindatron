"""Health of each engine@runner route and of each runner, so a failing path is not hammered.

A route that fails in a way that will keep failing for a while (no quota, no
GPU, repeated timeouts) cools down: the orchestrator skips it until the cooldown
ends, and every other eligible route keeps serving. One success clears it.

Two scopes, because failures have two owners:
  route   "wan22_i2v_a14b@zerogpu" — this model on this infrastructure
          (no GPU for that Space, a timeout, a transient error);
  runner  "runner:zerogpu" — the whole infrastructure account
          (quota spent, rate limited): every engine on it is affected.

Request-specific failures — an unsupported format or duration, out of memory at
that size, a refused input, a filtered output — never cool anything down: the
next request may fit.

State is per process and deliberately simple (a single-instance deployment
runs media jobs in-process, media_service.schedule). It is internal: nothing
here is ever sent to a user.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from src.media.engines import FailureKind

# Seconds a scope is skipped once its threshold is reached.
COOLDOWN_SECONDS: dict[str, float] = {
    FailureKind.QUOTA: 900.0,
    FailureKind.RATE_LIMITED: 60.0,
    FailureKind.GPU_UNAVAILABLE: 90.0,
    FailureKind.UNAVAILABLE: 60.0,
    FailureKind.COLD_START_TIMEOUT: 300.0,
    FailureKind.TIMEOUT: 300.0,
    FailureKind.TEMPORARY: 120.0,
}
# Consecutive failures of a kind (since the last success) that start a cooldown.
FAILURES_BEFORE_COOLDOWN: dict[str, int] = {
    FailureKind.QUOTA: 1,
    FailureKind.RATE_LIMITED: 1,
    FailureKind.GPU_UNAVAILABLE: 1,
    FailureKind.UNAVAILABLE: 2,
    FailureKind.COLD_START_TIMEOUT: 2,
    FailureKind.TIMEOUT: 2,
    FailureKind.TEMPORARY: 3,
}
# Kinds owned by the runner account rather than by one route.
RUNNER_WIDE_KINDS = frozenset({FailureKind.QUOTA, FailureKind.RATE_LIMITED})
MAX_COOLDOWN_SECONDS = 3600.0


def runner_scope(runner_name: str) -> str:
    return f"runner:{runner_name}"


@dataclass
class _Scope:
    consecutive: dict[str, int] = field(default_factory=dict)
    cooling_until: float = 0.0
    last_failure_kind: str | None = None
    successes: int = 0
    failures: int = 0


class HealthRegistry:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._scopes: dict[str, _Scope] = {}
        self._lock = threading.Lock()

    def _scope(self, key: str) -> _Scope:
        return self._scopes.setdefault(key, _Scope())

    def cooling_seconds(self, key: str) -> float:
        with self._lock:
            scope = self._scopes.get(key)
            return max(0.0, scope.cooling_until - self._clock()) if scope else 0.0

    def is_cooling(self, key: str) -> bool:
        return self.cooling_seconds(key) > 0

    def record_success(self, key: str) -> None:
        with self._lock:
            scope = self._scope(key)
            scope.consecutive.clear()
            scope.cooling_until = 0.0
            scope.successes += 1

    def record_failure(self, key: str, kind: str, *, retry_after: float | None = None) -> bool:
        """Count a failure. True when it started a cooldown."""
        with self._lock:
            scope = self._scope(key)
            scope.failures += 1
            scope.last_failure_kind = kind
            threshold = FAILURES_BEFORE_COOLDOWN.get(kind)
            if threshold is None:
                return False
            scope.consecutive[kind] = scope.consecutive.get(kind, 0) + 1
            if scope.consecutive[kind] < threshold:
                return False
            seconds = min(MAX_COOLDOWN_SECONDS, max(COOLDOWN_SECONDS[kind], retry_after or 0.0))
            scope.cooling_until = max(scope.cooling_until, self._clock() + seconds)
            scope.consecutive[kind] = 0
            return True

    def state(self, key: str) -> str:
        """"healthy" | "degraded" (failing, not cooling yet) | "cooling"."""
        if self.is_cooling(key):
            return "cooling"
        with self._lock:
            scope = self._scopes.get(key)
            return "degraded" if scope and any(scope.consecutive.values()) else "healthy"

    def snapshot(self) -> dict[str, dict]:
        """Every scope seen so far, for internal status reports."""
        with self._lock:
            keys = list(self._scopes)
        return {key: {"state": self.state(key), "cooling_seconds": round(self.cooling_seconds(key), 1),
                      "last_failure_kind": self._scopes[key].last_failure_kind,
                      "successes": self._scopes[key].successes, "failures": self._scopes[key].failures}
                for key in keys}

    def reset(self) -> None:
        with self._lock:
            self._scopes.clear()


health = HealthRegistry()
