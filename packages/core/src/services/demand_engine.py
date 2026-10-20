"""TRINDATRON Demand Engine — aggregated capability-demand telemetry.

Answers ONE product/monetization question: which capabilities and task
types are people actually asking for, and which of those requests the
current plan refused. Nothing else. It is deliberately not an analytics
subsystem — no event store, no pipeline, no external service, no new
dependency, no schema change.

WHAT IS RECORDED
    Two aggregated counters in the process's existing metrics registry
    (src/observability/metrics.py, already surfaced by /health/metrics):

      demand_capability_total   capability x task_type x endpoint x plan
      demand_unmet_total        capability x task_type x reason x plan

    A counter is a single integer per label combination. There is no row
    per event, so there is nothing per-request to store, join, export, or
    leak.

PRIVACY IS THE DESIGN, NOT A DISCLAIMER
    * Message content never reaches this module. Callers pass the ALREADY
      CLASSIFIED `Capability` enum — a fixed, closed set of labels from
      src/capability/classifier.py — never the user's text, never a
      substring of it, never anything derived from it beyond that label.
    * No user_id, no session_id, no message_id, no IP address, no
      per-event timestamp. `plan` is a coarse tier shared by many users
      ("free"/"start"/"pro"/"max"/"team"), recorded to answer the only
      question it is here for: which demand a plan is turning away.
    * Because the storage IS the aggregate, a recorded signal cannot be
      attributed back to a person or to a single request even by whoever
      reads the metrics endpoint.

WHY IN-PROCESS AGGREGATION AND NOT A TABLE
    A `demand_events` table would mean a migration, a retention policy, a
    deletion path for account closure, and a growing store of rows that
    each describe one person's one request — i.e. exactly the privacy
    surface this design avoids, in exchange for precision this question
    does not need. Product direction needs proportions ("FILES is 3x
    IMAGE on free"), which a counter already gives. The trade-off is that
    counters are per-process and reset on restart; when a longer horizon
    is genuinely needed, the scrape interval of whatever reads
    /health/metrics is the place to add it — not this module.

NEVER RAISES
    Observability must not be able to fail a user's request. Every public
    function here swallows its own exceptions, exactly like
    sessions.py's `_log_load_class` (which cannot fail) and for the same
    reason.
"""
from __future__ import annotations

import logging

from src.observability.metrics import core_metrics

logger = logging.getLogger("trinity.demand")

_demand_capability_total = core_metrics.counter(
    "demand_capability_total",
    "Classified capability demand, labeled by capability/task_type/endpoint/plan. "
    "Aggregate only — no user, session, or message content.",
)

_demand_unmet_total = core_metrics.counter(
    "demand_unmet_total",
    "Demand refused by the current plan or by an operator switch, labeled by "
    "capability/task_type/reason/plan. The monetization signal: what people "
    "asked for and did not get.",
)


def _label(value) -> str:
    """Coerce an enum/str label to its plain string form.

    Accepts the enum itself so call sites don't have to remember `.value`
    for one argument and not another; anything else becomes "unknown"
    rather than raising inside an observability path.
    """
    if value is None:
        return "unknown"
    inner = getattr(value, "value", value)
    return inner if isinstance(inner, str) else str(inner)


def record_demand(*, endpoint: str, capability, task_type, user_plan: str) -> None:
    """Record one classified request. Call right after `classify()`.

    `capability`/`task_type` accept either the enum or its string value.
    Counts the request as ASKED — independent of whether it is later
    allowed, rejected, or fails, so that `demand_unmet_total` is always a
    subset of this and the two can be compared directly.
    """
    try:
        _demand_capability_total.inc(
            capability=_label(capability),
            task_type=_label(task_type),
            endpoint=endpoint,
            plan=_label(user_plan),
        )
    except Exception:  # pragma: no cover - observability must never fail a request
        logger.debug("demand_engine: record_demand suppressed", exc_info=True)


def record_unmet_demand(*, capability, task_type, reason: str, user_plan: str) -> None:
    """Record demand that was refused.

    `reason` is a short, fixed operator-facing token chosen by the call
    site (e.g. "agent_mode_not_entitled", "capability_disabled") — never
    a message, never anything derived from user input.
    """
    try:
        _demand_unmet_total.inc(
            capability=_label(capability),
            task_type=_label(task_type),
            reason=reason,
            plan=_label(user_plan),
        )
    except Exception:  # pragma: no cover - observability must never fail a request
        logger.debug("demand_engine: record_unmet_demand suppressed", exc_info=True)


def demand_snapshot() -> dict:
    """Aggregated demand, shaped for a human or a dashboard.

    Returns `{"demand": [...], "unmet": [...]}` where each entry is
    `{labels..., "count": N}`, sorted by count descending so the top
    signals read first. Same aggregate values /health/metrics already
    exposes — this is the convenience shape, not a second source.
    """
    def _rows(counter) -> list[dict]:
        rows = [
            {**dict(labels), "count": int(count)}
            for labels, count in counter.snapshot().items()
        ]
        rows.sort(key=lambda row: row["count"], reverse=True)
        return rows

    return {"demand": _rows(_demand_capability_total), "unmet": _rows(_demand_unmet_total)}
