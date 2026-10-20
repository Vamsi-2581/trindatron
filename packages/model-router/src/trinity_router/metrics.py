"""Minimal in-process metrics registry — Etapa 5G (Observability).

No external dependency (no prometheus_client, no Redis/Kafka/statsd) —
counters and histograms live entirely in this process's memory, exactly
like every other in-memory governor already in this codebase
(request_control.py's limiters, circuit_breaker.py's per-key state).
Every mutation is a single dict/lock operation — cheap enough to call from
a hot path, safe to read concurrently.

NOT a replacement for the existing structured audit log
(audit.py's RouterAuditEvent/log_audit_event) — that remains the source of
truth for per-attempt detail (provider, model, failure_class, latency,
cost, tcu, correlated by logical_request_id). This registry exists for
cheap, aggregatable counts/sums/latency stats a monitoring system (or a
human hitting /health/metrics) can read without parsing log lines. Values
recorded here are never invented — every .inc()/.observe() call site
passes a number that was already computed by existing business logic
(a real byte count, a real cost_usd, a real duration) — this module only
aggregates, it never estimates.

Not bucketed-histogram (no percentiles) by design: count/sum/min/max is
enough to compute an average and see the extremes without pulling in a
metrics library, and any call site here can grow real buckets later
without changing its signature.
"""
from __future__ import annotations

import threading


def _label_key(labels: dict[str, str]) -> tuple:
    return tuple(sorted(labels.items()))


class Counter:
    """Monotonically increasing count, optionally labeled."""

    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description
        self._lock = threading.Lock()
        self._values: dict[tuple, float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = _label_key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def snapshot(self) -> dict[tuple, float]:
        with self._lock:
            return dict(self._values)

    def reset(self) -> None:
        """Test-only: clear every labeled value back to empty. Never
        called from a production code path — mirrors the .reset() contract
        already established by request_control.py/agent_scheduler.py/
        circuit_breaker.py for exactly the same cross-test-leakage reason."""
        with self._lock:
            self._values.clear()


class Histogram:
    """Cheap distribution stats (count/sum/min/max/avg) per label set."""

    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description
        self._lock = threading.Lock()
        self._count: dict[tuple, int] = {}
        self._sum: dict[tuple, float] = {}
        self._min: dict[tuple, float] = {}
        self._max: dict[tuple, float] = {}

    def observe(self, value: float, **labels: str) -> None:
        key = _label_key(labels)
        with self._lock:
            self._count[key] = self._count.get(key, 0) + 1
            self._sum[key] = self._sum.get(key, 0.0) + value
            self._min[key] = value if key not in self._min else min(self._min[key], value)
            self._max[key] = value if key not in self._max else max(self._max[key], value)

    def snapshot(self) -> dict[tuple, dict]:
        with self._lock:
            return {
                key: {
                    "count": self._count[key],
                    "sum": self._sum[key],
                    "min": self._min[key],
                    "max": self._max[key],
                    "avg": self._sum[key] / self._count[key] if self._count[key] else 0.0,
                }
                for key in self._count
            }

    def reset(self) -> None:
        with self._lock:
            self._count.clear()
            self._sum.clear()
            self._min.clear()
            self._max.clear()


class MetricsRegistry:
    """A named collection of Counters/Histograms with a JSON-safe snapshot.

    Counters/histograms are created lazily on first use (`.counter(name)`/
    `.histogram(name)` — same "get or create" idiom already used by
    PerKeyConcurrencyLimiter for per-key state) so instrumentation call
    sites never need a separate registration step.
    """

    def __init__(self) -> None:
        self._counters: dict[str, Counter] = {}
        self._histograms: dict[str, Histogram] = {}
        self._lock = threading.Lock()

    def counter(self, name: str, description: str = "") -> Counter:
        with self._lock:
            c = self._counters.get(name)
            if c is None:
                c = Counter(name, description)
                self._counters[name] = c
            return c

    def histogram(self, name: str, description: str = "") -> Histogram:
        with self._lock:
            h = self._histograms.get(name)
            if h is None:
                h = Histogram(name, description)
                self._histograms[name] = h
            return h

    def snapshot(self) -> dict:
        out: dict = {"counters": {}, "histograms": {}}
        for name, c in self._counters.items():
            out["counters"][name] = [
                {"labels": dict(key), "value": value} for key, value in c.snapshot().items()
            ]
        for name, h in self._histograms.items():
            out["histograms"][name] = [
                {"labels": dict(key), **stats} for key, stats in h.snapshot().items()
            ]
        return out

    def reset(self) -> None:
        """Test-only: reset every counter/histogram this registry owns."""
        for c in self._counters.values():
            c.reset()
        for h in self._histograms.values():
            h.reset()


# Process-wide singleton — same pattern as ModelRouter's shared instance in
# Core (_router) and every other module-level singleton in this codebase.
# Metrics recorded here span every ModelRouter instance/request in this
# process, unlike CircuitBreakerRegistry (deliberately per-ModelRouter).
router_metrics = MetricsRegistry()
