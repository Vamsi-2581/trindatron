"""Provider/model-specific circuit breaker — Etapa 5E.

CLOSED -> OPEN -> HALF_OPEN -> CLOSED, keyed by the internal provider-catalog
alias (e.g. "claude-sonnet") — never global. One provider/model tripping its
breaker (Google/gemini-25-flash having a bad afternoon, say) never affects
Anthropic/claude-sonnet's own breaker; each key's state is independent.

IN-MEMORY, per-`CircuitBreakerRegistry`-instance (not a module-level
singleton by itself — see router.py: each `ModelRouter()` instance owns
one registry, exactly like this whole package's existing "stateless,
safe-to-instantiate-as-a-singleton" design; Core constructs exactly one
`ModelRouter()` at import time and reuses it for the process's lifetime,
so breaker state naturally persists across requests there, while a test
constructing a fresh `ModelRouter()` gets fresh, isolated breaker state —
no cross-test reset fixture needed, unlike Core's own module-level
singletons in request_control.py/agent_scheduler.py).

Prepared for a future distributed (Redis-backed) implementation via the
same ABC-swap pattern already used by request_control.py/agent_scheduler.py
in Core — NOT implemented here, per explicit Etapa 5E scope (no Redis).
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

from .metrics import router_metrics

logger = logging.getLogger("trinity.router.circuit_breaker")

_transitions_total = router_metrics.counter(
    "router_circuit_breaker_transitions_total",
    "Circuit breaker state transitions, labeled by key and to_state.",
)


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class _BreakerState:
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: float | None = None  # time.monotonic() timestamp


class CircuitBreaker(ABC):
    """Per-key circuit breaker abstraction. Swappable for a future
    distributed implementation without changing router.py's call sites."""

    @abstractmethod
    def is_available(self, key: str) -> bool:
        """True if a call to `key` should be attempted right now."""

    @abstractmethod
    def record_success(self, key: str) -> None: ...

    @abstractmethod
    def record_failure(self, key: str) -> None: ...

    @abstractmethod
    def state_of(self, key: str) -> CircuitState: ...

    @abstractmethod
    def reset(self) -> None:
        """Test-only: clear every key back to CLOSED."""


class CircuitBreakerRegistry(CircuitBreaker):
    """In-memory circuit breaker, one independent state machine per key.

    Not thread-safe by design — same asyncio-single-event-loop argument as
    every other in-memory governor in this codebase (request_control.py,
    agent_scheduler.py's ConcurrencyLimiter): every method here is plain
    synchronous code with no `await` in its body, so it's already atomic
    under asyncio's cooperative scheduling — no lock needed.
    """

    def __init__(self, *, failure_threshold: int = 3, cooldown_s: float = 30.0) -> None:
        if failure_threshold < 1:
            raise ValueError(f"failure_threshold must be >= 1, got {failure_threshold!r}")
        if cooldown_s <= 0:
            raise ValueError(f"cooldown_s must be > 0, got {cooldown_s!r}")
        self._failure_threshold = failure_threshold
        self._cooldown_s = cooldown_s
        self._states: dict[str, _BreakerState] = {}

    def _get(self, key: str) -> _BreakerState:
        state = self._states.get(key)
        if state is None:
            state = _BreakerState()
            self._states[key] = state
        return state

    def is_available(self, key: str) -> bool:
        """CLOSED -> always available. OPEN -> unavailable until the
        cooldown elapses, at which point exactly this call promotes the
        breaker to HALF_OPEN and returns True (a single trial through).
        HALF_OPEN -> available (the router only ever makes one attempt per
        candidate per call, so there is no concurrent-trial race to guard
        against here — see router.py)."""
        st = self._get(key)
        if st.state is CircuitState.CLOSED:
            return True
        if st.state is CircuitState.OPEN:
            if st.opened_at is not None and (time.monotonic() - st.opened_at) >= self._cooldown_s:
                st.state = CircuitState.HALF_OPEN
                logger.info("circuit_breaker: half_open (cooldown elapsed) | key=%s", key)
                _transitions_total.inc(key=key, to_state="half_open")
                return True
            return False
        return True  # HALF_OPEN

    def record_success(self, key: str) -> None:
        st = self._get(key)
        if st.state is not CircuitState.CLOSED:
            logger.info("circuit_breaker: closed (trial succeeded) | key=%s", key)
            _transitions_total.inc(key=key, to_state="closed")
        st.state = CircuitState.CLOSED
        st.consecutive_failures = 0
        st.opened_at = None

    def record_failure(self, key: str) -> None:
        st = self._get(key)
        if st.state is CircuitState.HALF_OPEN:
            # Trial failed — reopen immediately, reset the cooldown clock.
            st.state = CircuitState.OPEN
            st.opened_at = time.monotonic()
            st.consecutive_failures += 1
            logger.warning("circuit_breaker: reopened (half_open trial failed) | key=%s", key)
            _transitions_total.inc(key=key, to_state="open")
            return
        st.consecutive_failures += 1
        if st.consecutive_failures >= self._failure_threshold:
            st.state = CircuitState.OPEN
            st.opened_at = time.monotonic()
            logger.warning(
                "circuit_breaker: opened | key=%s consecutive_failures=%d threshold=%d",
                key, st.consecutive_failures, self._failure_threshold,
            )
            _transitions_total.inc(key=key, to_state="open")

    def state_of(self, key: str) -> CircuitState:
        return self._get(key).state

    def reset(self) -> None:
        self._states.clear()
