"""
Router audit logging.

Audit events record what happened during a routing decision.
They NEVER contain API keys, auth tokens, raw provider error messages,
or any other sensitive/secret material.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger("trinity.router.audit")


@dataclass
class RouterAuditEvent:
    """One routing decision, safe for external log aggregators.

    Fields intentionally absent: api_key, Authorization header, raw exception
    messages from providers (which may contain key material), user PII.

    Etapa 5E additions (all optional/defaulted — no existing caller or test
    constructing this dataclass positionally or checking a fixed field set
    needs to change): `logical_request_id` correlates every attempt made
    for the SAME user-facing request — one logical request can now produce
    several of these events (one per provider_attempt) before a final
    "success"/"all_failed" event, whereas pre-5E only ever logged a single
    terminal event per call. `attempt_number` is 1-based. `retryable` and
    `failure_class` come from retry.classify_failure() and are populated
    only on attempt-failure events (None/"" on success). `latency_ms` is
    the wall-clock time of that one provider round-trip (or the local
    pre-flight check, for a "blocked"/"circuit_open" status, in which case
    it is ~0).
    """
    event_id: str
    timestamp: str          # ISO 8601 UTC
    session_id: str
    user_plan: str          # "start" | "pro" | "max"
    task_type: str          # TaskType value
    provider: str           # "anthropic" | "openai" | "google"
    model: str              # litellm model string, e.g. "claude-sonnet-4-5"
    status: str             # "success" | "failover" | "blocked" | "all_failed" | "attempt_failed"
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    tcu: float = 0.0
    failover_used: bool = False
    failover_reason: str = ""   # sanitized — no raw exception text
    limit_applied: str = ""     # e.g., "tokens_capped" | "cost_per_request"
    logical_request_id: str = ""
    attempt_number: int = 1
    retryable: bool | None = None
    failure_class: str = ""     # retry.FailureClass value, or "" on success
    latency_ms: float = 0.0
    # Adaptive Intelligence Router additions (all optional/defaulted — same
    # non-breaking convention as the Etapa 5E fields above). None/"" when
    # complexity-aware ordering didn't run (e.g. forced_provider_key /
    # Continuity Mode, or a single-candidate pool with nothing to reorder)
    # — answers "why did TRINDATRON choose this model?" (mission
    # requirement 14) without ever recording prompt content.
    capability: str = ""
    complexity_score: float | None = None
    required_quality_level: str = ""
    eligible_model_count: int | None = None
    selection_reason: str = ""  # "dynamic_eligible" | "no_capable_model_controlled_failure" | "static_fallback_error" | ""
    # Adaptive Intelligence Router V2 addition (optional/defaulted, same
    # convention as the fields above): size of the GLOBAL technical
    # candidate universe (providers.get_candidate_models()) BEFORE
    # quality/tool filtering — answers "how many models did TRINDATRON
    # even consider?" alongside eligible_model_count's "how many could
    # actually do it?".
    total_candidate_count: int | None = None


def create_audit_event(
    *,
    session_id: str,
    user_plan: str,
    task_type: str,
    provider: str,
    model: str,
    status: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    tcu: float = 0.0,
    failover_used: bool = False,
    failover_reason: str = "",
    limit_applied: str = "",
    logical_request_id: str = "",
    attempt_number: int = 1,
    retryable: bool | None = None,
    failure_class: str = "",
    latency_ms: float = 0.0,
    capability: str = "",
    complexity_score: float | None = None,
    required_quality_level: str = "",
    eligible_model_count: int | None = None,
    selection_reason: str = "",
    total_candidate_count: int | None = None,
) -> RouterAuditEvent:
    return RouterAuditEvent(
        event_id=str(uuid.uuid4()),
        timestamp=datetime.now(timezone.utc).isoformat(),
        session_id=session_id,
        user_plan=user_plan,
        task_type=task_type,
        provider=provider,
        model=model,
        status=status,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        tcu=tcu,
        failover_used=failover_used,
        failover_reason=failover_reason,
        limit_applied=limit_applied,
        logical_request_id=logical_request_id,
        attempt_number=attempt_number,
        retryable=retryable,
        failure_class=failure_class,
        latency_ms=latency_ms,
        capability=capability,
        complexity_score=complexity_score,
        required_quality_level=required_quality_level,
        eligible_model_count=eligible_model_count,
        selection_reason=selection_reason,
        total_candidate_count=total_candidate_count,
    )


def log_audit_event(event: RouterAuditEvent) -> None:
    """Write audit event to the structured log stream.

    Safe for external SIEM/log aggregators — no secret material.
    """
    logger.info(
        "router_audit "
        "event_id=%s logical_request_id=%s attempt=%d session=%s plan=%s task=%s "
        "provider=%s model=%s status=%s "
        "tokens_in=%d tokens_out=%d cost_usd=%.8f tcu=%.4f "
        "failover=%s failover_reason=%r limit=%r retryable=%s failure_class=%r latency_ms=%.1f "
        "capability=%r complexity_score=%s required_quality_level=%r eligible_model_count=%s "
        "total_candidate_count=%s selection_reason=%r",
        event.event_id, event.logical_request_id, event.attempt_number,
        event.session_id, event.user_plan, event.task_type,
        event.provider, event.model, event.status,
        event.input_tokens, event.output_tokens, event.cost_usd, event.tcu,
        event.failover_used, event.failover_reason, event.limit_applied,
        event.retryable, event.failure_class, event.latency_ms,
        event.capability, event.complexity_score, event.required_quality_level,
        event.eligible_model_count, event.total_candidate_count, event.selection_reason,
    )
