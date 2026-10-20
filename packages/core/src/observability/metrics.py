"""Core-level metrics registry — Etapa 5G (Observability).

Reuses the same Counter/Histogram/MetricsRegistry primitives already built
for trinity_router (packages/model-router/src/trinity_router/metrics.py)
rather than duplicating an in-process metrics implementation across two
packages Core already depends on. `core_metrics` is a SEPARATE registry
instance from `trinity_router.router_metrics` — Core's own components
(HTTP requests, Request Control, Agent Scheduler, TCU/entitlement,
Continuity) record into this one; the Model Router's own components record
into its own. `combined_snapshot()` merges both for the /health/metrics
endpoint so a single call surfaces the whole process's observability
state, while keeping each package free of a reverse dependency on the
other (model-router never imports from Core).
"""
from __future__ import annotations

from trinity_router.metrics import MetricsRegistry, router_metrics

core_metrics = MetricsRegistry()


def combined_snapshot() -> dict:
    core_snap = core_metrics.snapshot()
    router_snap = router_metrics.snapshot()
    return {
        "counters": {**core_snap["counters"], **router_snap["counters"]},
        "histograms": {**core_snap["histograms"], **router_snap["histograms"]},
    }


def reset_all() -> None:
    """Test-only: reset both registries. Never called from a production
    code path — mirrors request_control.reset()/agent_scheduler.reset()."""
    core_metrics.reset()
    router_metrics.reset()
