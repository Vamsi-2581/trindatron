"""
TRINITY Model Router — main entry point.

Security invariants:
- RoutingRequest has NO provider/model/api_key fields. Clients cannot specify them.
- RoutingResult.tcu and .cost_usd are always server-calculated from real token counts.
- Failover error messages are sanitized — no API keys, no raw provider errors.
- All decisions are deterministic and auditable via RouterAuditEvent.
- Failover is bounded by PlanLimits.max_failover_attempts.

Provider Resilience (Etapa 5E) — layering, on top of the pre-existing
chain-based failover above:
- retry.classify_failure() decides whether a failure is RETRYABLE (try the
  next candidate, after a bounded exponential backoff+jitter delay),
  NON_RETRYABLE_PROVIDER (try the next candidate immediately, no backoff —
  this provider's own credentials/config are broken, but a different
  provider is an independent failure domain), or NON_RETRYABLE_REQUEST
  (abort the WHOLE chain immediately via RequestRejectedByProviderError —
  the failure is about the request content, not the provider, so no other
  candidate would fare any better).
- circuit_breaker.CircuitBreakerRegistry tracks CLOSED/OPEN/HALF_OPEN per
  provider-catalog key, owned one-per-ModelRouter-instance (see
  circuit_breaker.py's module docstring for why that's the right scope).
  A candidate whose breaker is OPEN is skipped without even attempting it.
- Every attempt (success or failure) now produces its own RouterAuditEvent,
  correlated by a per-call `logical_request_id` — see audit.py.
This etapa does NOT touch TCU/cost calculation (tcu.py, untouched) or how
RoutingResult/RouterStreamChunk report cost — those still reflect only the
attempt that actually succeeded, exactly as before.

Adaptive Intelligence Router — scope note (V2): candidate discovery is
now GLOBAL — providers.get_candidate_models() draws from the ENTIRE
PROVIDER_CATALOG (filtered only by objective capability/task_type/
disabled-key facts), not from selector.select_provider_chain()'s
hand-curated list. "Cheapest capable" means cheapest capable across
every model the catalog mechanically supports for this
(task_type, capability), subject to the plan's own quality floor
(PlanLimits.min_quality_class) and cost ceiling — never "cheapest in
whatever subset a chain happened to list." select_provider_chain() is
NOT removed: it remains the legacy/back-compat candidate source used
(a) when adaptive routing cannot run at all (no `messages` supplied —
the one legitimate case this module has always had), and (b) as a safe,
conservative fallback if complexity computation itself raises. See
providers.py's own module docstring for the full three-stage pipeline
this now implements (Provider Catalog → Eligibility → Selection).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Awaitable, AsyncIterator, Callable

import litellm

from .audit import RouterAuditEvent, create_audit_event, log_audit_event
from .circuit_breaker import CircuitBreakerRegistry
from .complexity import ComplexityResult, compute_complexity
from .limits import BudgetExceededError, PlanLimits, effective_max_output_tokens, enforce_request_limits, get_plan_limits
from .metrics import router_metrics
from .providers import (
    PROVIDER_CATALOG,
    QUALITY_CLASS_ORDER,
    ProviderModel,
    get_candidate_models,
    get_eligible_models,
    rank_by_cost,
)
from . import quality_gate
from . import free_pool
from .retry import FailureClass, backoff_delay, classify_failure
from .selector import TaskType, select_provider_chain
from .tcu import calculate_actual_cost_usd, calculate_tcu, estimate_cost_usd

logger = logging.getLogger("trinity.router")

# Etapa 5G — cheap, aggregatable counters/histograms alongside the existing
# per-attempt RouterAuditEvent log (audit.py). Every value recorded below
# was already computed by the routing logic above/below it — nothing here
# estimates or invents a number; it only aggregates what already exists.
_attempts_total = router_metrics.counter(
    "router_provider_attempts_total",
    "Provider attempts, labeled by provider/model/outcome (success|failure|skipped_circuit_open|skipped_budget_exceeded).",
)
_retries_total = router_metrics.counter(
    "router_retries_total", "Retryable failures that triggered a backoff+next-candidate attempt.",
)
_failover_total = router_metrics.counter(
    "router_failover_total", "Successful completions that required at least one failover.",
)
_chain_exhausted_total = router_metrics.counter(
    "router_chain_exhausted_total", "Routing calls where every candidate in the chain failed/was skipped.",
)
_attempt_latency_ms = router_metrics.histogram(
    "router_attempt_latency_ms", "Per-attempt provider round-trip latency, labeled by provider/model/outcome.",
)
_cost_usd = router_metrics.histogram(
    "router_cost_usd", "Real, server-calculated cost per successful call, labeled by provider/model.",
)
_discarded_attempt_cost_usd = router_metrics.histogram(
    "router_discarded_attempt_cost_usd",
    "Cost of provider answers the quality gate discarded, labeled by provider/model. "
    "Billed by the provider and carried into the request's own cost.",
)
_tcu = router_metrics.histogram(
    "router_tcu", "Real, server-calculated TCU per successful call, labeled by provider/model.",
)

# Prompt caching — counts and rates only, never prompt content. These are
# what prove the provider is actually reusing the prefix: a hit rate that
# quietly falls to zero (a policy edit, a provider change, a prefix that
# stops being stable) is otherwise invisible, because nothing fails.
_cached_input_tokens = router_metrics.histogram(
    "router_cached_input_tokens", "Input tokens the provider served from its prompt cache, labeled by provider/model.",
)
_uncached_input_tokens = router_metrics.histogram(
    "router_uncached_input_tokens", "Input tokens billed at the standard rate, labeled by provider/model.",
)
_cache_hit_rate = router_metrics.histogram(
    "router_cache_hit_rate", "Fraction of input tokens served from the prompt cache (0-1), labeled by provider/model.",
)

# Adaptive Intelligence Router — observability for "why did TRINDATRON
# choose this model?" (mission requirement 14). Never records prompt
# content — only the already-computed score/counts/labels.
_complexity_score = router_metrics.histogram(
    "router_complexity_score", "Computed complexity score (0-100) per routing decision, labeled by capability/band.",
)
_eligible_model_count = router_metrics.histogram(
    "router_eligible_model_count", "Number of models that survived complexity-aware eligibility filtering, labeled by capability/required_quality_class.",
)
_dynamic_chain_used_total = router_metrics.counter(
    "router_dynamic_chain_used_total", "Chain-building outcome, labeled by selection_reason (dynamic_eligible|no_capable_model_controlled_failure|static_fallback_error|quality_not_entitled).",
)

# Quality Recovery Layer — response CONTENT failed quality_gate.evaluate()
# (bogus refusal to an ordinary request, or answered in the wrong
# language) despite the provider call itself succeeding (no exception).
# Labeled by provider/model/reason so an operator can see which model
# produces this and how often — never records prompt or response text.
_quality_recovery_total = router_metrics.counter(
    "router_quality_recovery_total",
    "Successful provider responses rejected by the Quality Gate and retried on the next chain candidate, labeled by provider/model/reason.",
)
# Streaming path: a response the Quality Gate rejected AFTER text had
# already reached the caller. Recovery is impossible at that point (see
# stream()'s own docstring), so this stays observability-only.
_quality_gate_flagged_total = router_metrics.counter(
    "router_quality_gate_flagged_total",
    "Streamed responses the Quality Gate rejected too late to recover, labeled by provider/model/reason.",
)

# How much text stream() holds back before releasing the first chunk.
#
# The chat endpoint streams, so until this existed a bogus refusal reached
# the user verbatim and was only written to a log. The fix is not to break
# the "no failover once content has shipped" invariant — it is to notice
# BEFORE anything ships. A boilerplate refusal is short ("I'm sorry, but I
# can't help with that." is 38 characters), so holding a small prefix is
# enough to catch one while a real answer is released almost immediately.
#
# Deliberately judged on the COMPLETE text of a short response, never on a
# truncated prefix of a long one: an answer that merely opens with an
# apology ("Desculpe, mas não posso confirmar essa data — o que posso
# fazer é…") must not be mistaken for a refusal.
#
# 140 is measured, not guessed. The boilerplate refusals this gate knows
# run 24-91 characters (the longest being a terse decline plus one
# follow-up sentence), so this clears the worst observed case by ~50% while
# keeping the window a user waits on as small as that allows. Raising it
# buys nothing but latency; lowering it past ~100 starts releasing verbose
# refusals before they can be caught.
_STREAM_QUALITY_HOLD_CHARS = 140


# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------


@dataclass
class RoutingRequest:
    """Input to the model router.

    Intentionally missing: provider, model, api_key, endpoint.
    These are determined server-side and are never accepted from clients —
    `forced_provider_key` below is NOT an exception to this: it is never
    populated from any client-facing field, only by a caller (Core's
    Continuity Mode, Etapa 5F) that already made its own server-side
    economic-model decision via select_cheapest_capable().
    """
    messages: list[dict]
    task_type: TaskType
    user_plan: str
    session_id: str
    max_output_tokens: int | None = None  # will be capped by plan limits
    tools: list[dict] | None = None
    tool_choice: str | None = None
    capability: str | None = None  # Capability.value forwarded from the classifier
    # Etapa 5F — Continuity Mode escape hatch. None (default) preserves the
    # NORMAL path byte-for-byte: _build_chain() calls select_provider_chain()
    # exactly as before. When set to a PROVIDER_CATALOG key, _build_chain()
    # uses a single-candidate chain of exactly that model instead of the
    # curated multi-candidate chain — Continuity already picked its one
    # economic model server-side and must never fail over to a DIFFERENT
    # (possibly premium) one. The single candidate still goes through every
    # other Etapa 5E mechanism unchanged: circuit breaker, budget preflight,
    # failure classification (a retryable failure on the one candidate just
    # has nowhere left to fail over to, so ProviderUnavailableError is
    # raised — a controlled rejection, not a hang or a premium fallback).
    forced_provider_key: str | None = None
    # Etapa 5H — operational provider/model kill switch. A plain,
    # server-computed set of PROVIDER_CATALOG keys Core's runtime settings
    # currently mark disabled (src/services/runtime_settings.py) — this
    # package has no database access and never will; it only ever receives
    # this as an opaque, already-resolved frozenset of strings it doesn't
    # need to interpret. Empty by default, preserving the NORMAL path byte-
    # for-byte. _build_chain() excludes a disabled key from the returned
    # chain entirely — the exact same treatment as a key simply not being
    # present in PROVIDER_CATALOG (one more condition in that same
    # existing filter, not a second routing implementation) — so it never
    # gets attempted, never appears in providers_tried, and normal
    # failover to the next candidate proceeds unaffected. Applied to BOTH
    # the forced_provider_key branch and the normal chain-building branch
    # of _build_chain() (defense in depth): even though Continuity's own
    # economic-model selection is expected to already avoid picking a
    # disabled key, a forced_provider_key that also happens to be disabled
    # still resolves to an empty chain rather than silently calling it.
    disabled_provider_keys: frozenset[str] = field(default_factory=frozenset)
    # This call's cost ceiling in USD, from the caller's own reservation
    # (entitlement_service.get_reservation_budget_tcu). It can only ever
    # LOWER the plan's static cap, never raise it — see _effective_limits().
    # Without it the router would authorise a call costing more than the
    # window the caller reserved against, and the entitlement layer would
    # only discover the overshoot on the NEXT request.
    max_cost_usd_override: float | None = None
    # A quality floor this call must meet, set by a caller that knows more than the request text does — e.g.
    # the agent after a web search, whose answer is only as good as the model that reads the results. It can
    # only RAISE the plan's floor and never past the plan's own ceiling — see _effective_limits().
    min_quality_class: str | None = None


@dataclass
class RoutingResult:
    """Output from a non-streaming routing call.

    All financial/accounting fields (cost_usd, tcu) are server-calculated.
    Clients receive this as a read-only result — they cannot influence these values.
    """
    content: str | None
    tool_calls: list | None
    provider: str           # "anthropic" | "openai" | "google"
    model: str              # litellm model string used
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_usd: float         # server-calculated from real token counts
    tcu: float              # server-calculated from cost_usd
    failover_used: bool
    failover_reason: str | None


@dataclass
class RouterStreamChunk:
    """One chunk from a streaming routing call.

    Text chunks: text is non-empty, is_final is False.
    Final chunk: is_final is True, text is empty, metadata fields are populated.
    """
    text: str = ""
    is_final: bool = False
    # Metadata — only populated on the final chunk
    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    tcu: float = 0.0
    failover_used: bool = False
    failover_reason: str | None = None


@dataclass
class _ChainDecision:
    """Internal: the result of _build_chain() — the ordered candidate
    list plus WHY it's ordered that way, threaded into every audit event
    for this logical request (mission requirement 14). `complexity` is
    None whenever complexity-aware ordering didn't run at all (forced_provider_key,
    a single-or-empty pool with nothing to reorder) — distinct from
    `selection_reason == "static_fallback_error"`, which means it DID run
    but failed and fell back safely."""
    chain: list[tuple[str, ProviderModel]]
    complexity: ComplexityResult | None = None
    eligible_model_count: int | None = None
    selection_reason: str = ""
    # V2 — size of the GLOBAL technical candidate universe (providers.
    # get_candidate_models()'s output) BEFORE quality/tool filtering.
    # None when global discovery didn't run at all (messages absent,
    # complexity error, forced_provider_key).
    total_candidate_count: int | None = None


class ProviderUnavailableError(Exception):
    """All providers in the chain failed (or were skipped — budget/circuit
    breaker) with no candidate left to try. No further failover possible."""
    def __init__(self, providers_tried: list[str]) -> None:
        # Show provider names only — no raw errors (which may contain API keys)
        super().__init__(
            f"All AI providers temporarily unavailable. "
            f"Tried: {', '.join(providers_tried)}"
        )
        self.providers_tried = providers_tried


class RequestRejectedByProviderError(Exception):
    """A provider rejected the request for a reason classified as
    NON_RETRYABLE_REQUEST (retry.classify_failure) — malformed request, an
    unsupported capability, or a policy/safety refusal. Trying a different
    provider with the identical request content would very likely fail the
    same way, so the chain is aborted immediately rather than wasting
    further attempts/cost. `reason` is the same sanitized string used for
    RouterAuditEvent.failover_reason — never the raw provider error."""
    def __init__(self, reason: str) -> None:
        super().__init__(f"Request rejected by provider: {reason}")
        self.reason = reason


class QualityNotEntitledError(Exception):
    """This request's required quality tier exceeds what `user_plan` is
    entitled to (PlanLimits.max_quality_class) — an entitlement/policy
    decision, not a provider failure. Raised in
    _apply_complexity_aware_ordering() BEFORE get_candidate_models()/
    get_eligible_models() ever run, so zero candidates are considered,
    zero providers are attempted, and zero cost is incurred — see that
    method's own docstring and PlanLimits.max_quality_class's docstring
    for the full rationale (root-caused 2026-09-07: without this gate,
    a plan below the required tier would reach its ONE eligible
    candidate, fail that candidate's own cost preflight, and surface as
    a generic ProviderUnavailableError with no audit trail at all —
    indistinguishable from a real outage).

    Deliberately does NOT downgrade `required_quality_class` to the
    plan's ceiling and silently continue with a lower-tier model — the
    task's real requirement is preserved on this exception exactly as
    computed, never quietly substituted with an answer from a model that
    was never capable of it (see this etapa's own "no silent downgrade"
    requirement)."""
    def __init__(self, required_quality_class: str, plan_max_quality_class: str, plan: str) -> None:
        super().__init__(
            f"This request requires quality tier '{required_quality_class}', which exceeds "
            f"the '{plan}' plan's entitlement (max '{plan_max_quality_class}')."
        )
        self.required_quality_class = required_quality_class
        self.plan_max_quality_class = plan_max_quality_class
        self.plan = plan


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def _effective_limits(request: "RoutingRequest"):
    """The plan's limits, narrowed by whatever budget this request carries.

    `min` is deliberate and load-bearing: an override may only ever tighten
    the ceiling. A caller that passes a larger number — by mistake or
    otherwise — cannot buy itself more room than its plan allows.
    """
    limits = get_plan_limits(request.user_plan)
    override = request.max_cost_usd_override
    if override is not None:
        limits = replace(
            limits,
            max_estimated_cost_per_request_usd=min(
                limits.max_estimated_cost_per_request_usd, max(0.0, float(override))
            ),
        )
    floor = request.min_quality_class
    if floor in QUALITY_CLASS_ORDER:
        # Raise the floor, never lower it, and never past what the plan is entitled to: a floor above the
        # plan's ceiling is clamped to that ceiling instead of turning the request into a refusal.
        rank = QUALITY_CLASS_ORDER.index
        wanted = min(floor, limits.max_quality_class, key=rank)
        if rank(wanted) > rank(limits.min_quality_class):
            limits = replace(limits, min_quality_class=wanted)
    return limits


#: Characters per token assumed when a prompt's size is estimated from its
#: text. English runs about 4, the Claude 4.7+ tokenizer produces ~30% more
#: tokens than earlier ones; 3 keeps the estimate on the high side, which is
#: the side a budget check has to err on.
_CHARS_PER_TOKEN_ESTIMATE = 3
#: Floor for the input estimate — the figure preflight always used, so a
#: short prompt is budgeted exactly as before.
_MIN_INPUT_TOKENS_ESTIMATE = 2_000


def _estimate_input_tokens(messages: list[dict] | None, tools: list[dict] | None) -> int:
    """How many input tokens a request will be billed for, estimated from its text.

    Preflight used to price every request at 2,000 input tokens, so a prompt
    carrying a long document passed a cost check its real input could never
    pass, and was billed far beyond the ceiling it was authorised for.
    """
    chars = 0
    for message in messages or ():
        if not isinstance(message, dict):
            chars += len(str(message))
            continue
        content = message.get("content")
        if content is not None:
            chars += len(content) if isinstance(content, str) else len(str(content))
        if message.get("tool_calls"):
            chars += len(str(message["tool_calls"]))
    if tools:
        chars += len(str(tools))
    return max(_MIN_INPUT_TOKENS_ESTIMATE, -(-chars // _CHARS_PER_TOKEN_ESTIMATE))


def _free_list_usd(key: str, input_tokens: int, output_tokens: int) -> float:
    """What a free call would cost at the source's paid list price: how a dollar allowance is counted."""
    found = free_pool.source(key)
    if found is None:
        return 0.0
    model = found.model
    return (input_tokens * model.cost_per_1m_input_usd + output_tokens * model.cost_per_1m_output_usd) / 1_000_000


def _limits_after_spend(limits: PlanLimits, spent_usd: float) -> PlanLimits:
    """What is left of a request's ceiling once part of it has been spent.

    A quality-rejected answer is billed by the provider. Checking each
    failover attempt against the WHOLE ceiling let a request that tripped the
    gate on every candidate cost (max_failover_attempts + 1) ceilings.
    """
    if spent_usd <= 0:
        return limits
    return replace(
        limits,
        max_estimated_cost_per_request_usd=max(0.0, limits.max_estimated_cost_per_request_usd - spent_usd),
    )


class ModelRouter:
    """Routes completion requests to the appropriate provider/model.

    Configuration comes from PROVIDER_CATALOG and PLAN_LIMITS. Safe to
    instantiate as a module-level singleton (Core does exactly this — see
    chat_service.py/agent_service.py's `_router = ModelRouter()`) — the one
    piece of per-instance mutable state, the circuit breaker registry
    (Etapa 5E), is specifically DESIGNED to persist across every call made
    through that one shared instance (that is the entire point of a
    circuit breaker: remembering recent provider health across requests).
    A fresh `ModelRouter()` — as most tests construct — gets a fresh,
    empty circuit breaker with no cross-test leakage risk.
    """

    def __init__(
        self,
        *,
        circuit_breaker_failure_threshold: int = 3,
        circuit_breaker_cooldown_s: float = 30.0,
        backoff_base_s: float = 0.1,
        backoff_cap_s: float = 1.0,
        backoff_jitter_ratio: float = 0.5,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """All parameters are optional tuning knobs with production-sane
        defaults — `ModelRouter()` with no arguments (used everywhere in
        Core) is unaffected. `sleep_fn` is test-only dependency injection
        for backoff_delay() (see retry.py) — defaults to asyncio.sleep.
        """
        self._circuit_breakers = CircuitBreakerRegistry(
            failure_threshold=circuit_breaker_failure_threshold,
            cooldown_s=circuit_breaker_cooldown_s,
        )
        self._backoff_base_s = backoff_base_s
        self._backoff_cap_s = backoff_cap_s
        self._backoff_jitter_ratio = backoff_jitter_ratio
        self._sleep_fn = sleep_fn
        # Free Compute Pool (free_pool.py): how much of each free allowance this instance has used.
        self._free_pool = free_pool.FreePoolTracker()

    async def complete(self, request: RoutingRequest) -> RoutingResult:
        """Non-streaming completion with automatic failover.

        Used by the agent loop (tool calls require the full response before acting).
        """
        plan_limits = _effective_limits(request)
        input_tokens_estimate = _estimate_input_tokens(request.messages, request.tools)
        decision = self._build_chain_decision(
            request.task_type, request.user_plan, plan_limits, request.capability,
            forced_provider_key=request.forced_provider_key,
            disabled_provider_keys=request.disabled_provider_keys,
            messages=request.messages,
            tools=request.tools,
            requested_max_output_tokens=request.max_output_tokens,
        )
        chain = self._with_free_prefix(decision, request, input_tokens_estimate)
        logical_request_id = str(uuid.uuid4())
        _audit_extra = dict(
            capability=request.capability or "",
            complexity_score=decision.complexity.score if decision.complexity else None,
            required_quality_level=decision.complexity.required_quality_class if decision.complexity else "",
            eligible_model_count=decision.eligible_model_count,
            total_candidate_count=decision.total_candidate_count,
            selection_reason=decision.selection_reason,
        )

        last_reason: str | None = None
        providers_tried: list[str] = []
        backoff_before_next = False
        backoff_attempt = 0
        # Most recent response the Quality Gate rejected, kept so the chain
        # can still fail open with it. The `is_last_candidate` check below
        # only covers a chain whose LAST candidate was actually called —
        # it does nothing when the remaining candidates are all skipped
        # without being attempted (circuit open, budget exceeded), which
        # would otherwise report a generic outage to a caller for whom a
        # real answer had already been obtained. Never a retry budget of
        # its own: this is the same single attempt, remembered.
        quality_fallback: dict | None = None
        rejected_costs_usd: list[float] = []

        for i, (key, cfg) in enumerate(chain):
            if not self._circuit_breakers.is_available(key):
                last_reason = "circuit_open"
                providers_tried.append(key)
                _attempts_total.inc(provider=cfg.provider, model=key, outcome="skipped_circuit_open")
                continue

            free_blocked = self._free_blocked(key, input_tokens_estimate, request.max_output_tokens)
            if free_blocked:
                # A free allowance spent or cooling down: skipped at no cost, the chain goes on.
                last_reason = f"free_{free_blocked}"
                providers_tried.append(key)
                continue

            effective_tokens, skip = self._preflight(
                _limits_after_spend(plan_limits, sum(rejected_costs_usd)), cfg,
                request.max_output_tokens, key, input_tokens_estimate=input_tokens_estimate,
            )
            if skip:
                # Cost exceeds limit — don't try this provider
                last_reason = "budget_exceeded"
                providers_tried.append(key)
                _attempts_total.inc(provider=cfg.provider, model=key, outcome="skipped_budget_exceeded")
                continue

            if backoff_before_next:
                await backoff_delay(
                    backoff_attempt, base_s=self._backoff_base_s, cap_s=self._backoff_cap_s,
                    jitter_ratio=self._backoff_jitter_ratio, sleep_fn=self._sleep_fn,
                )
                backoff_attempt += 1
                backoff_before_next = False

            failover_used = i > 0
            providers_tried.append(key)
            attempt_number = i + 1
            start = time.monotonic()

            try:
                # None, not litellm.NOT_GIVEN, when absent: live-verified
                # 2026-09-07 that Mistral's litellm handler raises
                # ("'NotGiven' object is not iterable" for tools;
                # "Invalid tool choice" for tool_choice) on that sentinel,
                # while every other provider in this catalog (Anthropic,
                # OpenAI, Gemini, Groq, xAI) accepts both None and
                # NOT_GIVEN identically. None is the universally-safe
                # "omit this" value here — when the caller DOES supply
                # tools/tool_choice, those real values still pass through
                # unchanged via the same `or` short-circuit as before.
                response = await litellm.acompletion(
                    model=cfg.model_id,
                    messages=request.messages,
                    tools=request.tools or None,
                    tool_choice=request.tool_choice or None,
                    max_tokens=effective_tokens,
                    # A free source reached on its own endpoint (Cloudflare) adds its base URL and key here.
                    **free_pool.call_kwargs(key),
                    **dict(cfg.call_params),
                )

            except Exception as exc:
                latency_ms = (time.monotonic() - start) * 1000
                last_reason = _sanitize_error(exc)
                failure_class = classify_failure(exc)
                logger.warning(
                    "router failover | key=%s reason=%s class=%s attempt=%d/%d",
                    key, last_reason, failure_class.value, attempt_number, len(chain),
                )
                log_audit_event(create_audit_event(
                    session_id=request.session_id, user_plan=request.user_plan,
                    task_type=request.task_type.value, provider=cfg.provider, model=cfg.model_id,
                    status="attempt_failed", failover_used=failover_used, failover_reason=last_reason,
                    logical_request_id=logical_request_id, attempt_number=attempt_number,
                    retryable=(failure_class == FailureClass.RETRYABLE), failure_class=failure_class.value,
                    latency_ms=latency_ms,
                    **_audit_extra,
                ))
                _attempts_total.inc(provider=cfg.provider, model=key, outcome="failure")
                _attempt_latency_ms.observe(latency_ms, provider=cfg.provider, model=key, outcome="failure")
                # Audited and counted like any attempt (above); a free source's quota or failure then only
                # moves the chain on — no breaker trip, no backoff, and never an error to the caller.
                if self._free_on_error(key, exc):
                    continue
                if failure_class == FailureClass.NON_RETRYABLE_REQUEST:
                    raise RequestRejectedByProviderError(last_reason) from exc
                self._circuit_breakers.record_failure(key)
                if failure_class == FailureClass.RETRYABLE:
                    backoff_before_next = True
                    _retries_total.inc(provider=cfg.provider, model=key)
                continue

            # Quality Gate — the provider call itself succeeded (no
            # exception), but the CONTENT may still be unusable: a bogus
            # refusal to an ordinary request, an answer in the wrong
            # language, or no text at all (see quality_gate.py's module
            # docstring for the full rationale and the "respect a
            # legitimate refusal" safety carve-out). `has_tool_calls` is
            # passed explicitly: a tool-call turn legitimately has no prose
            # and is never judged — nothing here ever second-guesses a tool
            # call, since recovering would discard tool calls already
            # produced. The gate itself is pure and does no I/O, so it is
            # evaluated unconditionally rather than pre-filtered here.
            self._free_on_success(key, response)
            response_message = response.choices[0].message
            gate = quality_gate.evaluate_from_messages(
                messages=request.messages,
                response_text=getattr(response_message, "content", None),
                has_tool_calls=bool(getattr(response_message, "tool_calls", None)),
            )

            # A rejected answer is only worth replacing while a later
            # candidate can still be paid for out of what this request has
            # left; otherwise it is the best answer this budget can buy.
            attempt_cost_usd = _completion_cost_usd(cfg, response)
            is_last_candidate = i == len(chain) - 1 or not self._has_affordable_successor(
                chain, i, plan_limits, sum(rejected_costs_usd) + attempt_cost_usd, input_tokens_estimate,
            )
            if not gate.passed and not is_last_candidate:
                # Soft, content-level failure — NOT a provider failure: the
                # provider answered successfully, so its circuit breaker is
                # recorded as a SUCCESS (never punish a healthy provider for
                # this router's own quality bar), no backoff is applied
                # (there is no reason to believe waiting helps), and the
                # SAME bounded chain simply moves to its next candidate —
                # never a second router, never a fixed recovery chain, never
                # extra attempts beyond what max_failover_attempts already
                # allows for this request.
                latency_ms = (time.monotonic() - start) * 1000
                last_reason = f"quality_gate:{gate.primary_reason}"
                logger.info(
                    "router quality_recovery | key=%s reasons=%s attempt=%d/%d",
                    key, gate.reasons, attempt_number, len(chain),
                )
                log_audit_event(create_audit_event(
                    session_id=request.session_id, user_plan=request.user_plan,
                    task_type=request.task_type.value, provider=cfg.provider, model=cfg.model_id,
                    status="quality_recovery", failover_used=failover_used, failover_reason=last_reason,
                    logical_request_id=logical_request_id, attempt_number=attempt_number,
                    retryable=True, failure_class="quality_gate", latency_ms=latency_ms,
                    **_audit_extra,
                ))
                self._circuit_breakers.record_success(key)
                _attempts_total.inc(provider=cfg.provider, model=key, outcome="quality_rejected")
                _quality_recovery_total.inc(provider=cfg.provider, model=key, reason=gate.primary_reason)
                # The provider billed this answer even though it is discarded.
                # Its cost is carried into whatever this request finally
                # returns, so TCU keeps matching real spend. Without it a
                # request that trips the gate on every candidate costs up to
                # (max_failover_attempts + 1) times what it charges, and no
                # quota ever sees the difference.
                rejected_cost_usd = attempt_cost_usd
                rejected_costs_usd.append(rejected_cost_usd)
                _discarded_attempt_cost_usd.observe(rejected_cost_usd, provider=cfg.provider, model=key)
                quality_fallback = dict(
                    response=response, cfg=cfg, provider_key=key, gate=gate,
                    failover_used=failover_used, attempt_number=attempt_number, start=start,
                    cost_usd=rejected_cost_usd,
                )
                continue

            # Accept this response — either it passed the Quality Gate, or
            # it's the last remaining candidate: a controlled fail-open
            # (return the best answer actually obtained) rather than
            # ProviderUnavailableError, since a legitimate refusal (or a
            # persistent quality issue across every eligible candidate)
            # must still be returned to the caller, never masked as an
            # outage. `final_reason` keeps the exhausted-quality-gate case
            # visible in the audit trail even though it's being accepted.
            final_reason = last_reason
            if not gate.passed:
                final_reason = f"quality_gate_exhausted:{gate.primary_reason}"
            self._circuit_breakers.record_success(key)
            if failover_used:
                _failover_total.inc(provider=cfg.provider, model=key)
            _attempts_total.inc(provider=cfg.provider, model=key, outcome="success")
            _attempt_latency_ms.observe(
                (time.monotonic() - start) * 1000, provider=cfg.provider, model=key, outcome="success",
            )
            return self._build_result(
                response=response,
                cfg=cfg,
                provider_key=key,
                failover_used=failover_used,
                failover_reason=final_reason,
                request=request,
                logical_request_id=logical_request_id,
                attempt_number=attempt_number,
                latency_ms=(time.monotonic() - start) * 1000,
                audit_extra=_audit_extra,
                extra_cost_usd=sum(rejected_costs_usd),
            )

        if quality_fallback is not None:
            # Every remaining candidate was skipped without being called, so
            # there is nothing better coming. Returning the rejected answer
            # is strictly better than ProviderUnavailableError: the caller
            # gets what the provider actually said (and the audit trail says
            # it was quality-rejected first), instead of an outage that did
            # not happen. Metrics/breaker state for this attempt were
            # already recorded on the rejection path above — not repeated.
            fb_gate = quality_fallback["gate"]
            fb_cfg = quality_fallback["cfg"]
            fb_key = quality_fallback["provider_key"]
            logger.info(
                "router quality_gate_exhausted (no attemptable candidate left) | key=%s reason=%s",
                fb_key, fb_gate.primary_reason,
            )
            return self._build_result(
                response=quality_fallback["response"],
                cfg=fb_cfg,
                provider_key=fb_key,
                failover_used=quality_fallback["failover_used"],
                failover_reason=f"quality_gate_exhausted:{fb_gate.primary_reason}",
                request=request,
                logical_request_id=logical_request_id,
                attempt_number=quality_fallback["attempt_number"],
                latency_ms=(time.monotonic() - quality_fallback["start"]) * 1000,
                audit_extra=_audit_extra,
                # The returned answer's own cost is counted by _build_result;
                # every OTHER rejected answer is added on top.
                extra_cost_usd=sum(rejected_costs_usd) - quality_fallback["cost_usd"],
            )

        _chain_exhausted_total.inc(task_type=request.task_type.value)
        raise ProviderUnavailableError(providers_tried)

    async def stream(self, request: RoutingRequest) -> AsyncIterator[RouterStreamChunk]:
        """Streaming completion. Yields text chunks then a final metadata chunk.

        Used by the chat endpoint. Failover triggers if the initial connection
        fails, using the exact same circuit-breaker/backoff/classification
        machinery as complete() — see the loop below. Once ANY chunk has been
        yielded from a provider's response, this function is intentionally
        NOT wrapped in a retry/failover try/except (see the `async for chunk
        in response:` loop below) — a failure after useful content has
        started propagates directly to the caller, unmodified. This is
        deliberate, not an oversight: restarting on a different provider
        after real content has already reached the client would duplicate
        or corrupt the response (see this module's Etapa 5E docstring and
        test_router_streaming_resilience.py).
        """
        plan_limits = _effective_limits(request)
        # Spend on streamed answers the quality gate discarded before anything
        # reached the caller — billed by the provider, so carried into the
        # final chunk's cost exactly as complete() does.
        stream_discarded_cost_usd = 0.0
        input_tokens_estimate = _estimate_input_tokens(request.messages, request.tools)
        decision = self._build_chain_decision(
            request.task_type, request.user_plan, plan_limits, request.capability,
            forced_provider_key=request.forced_provider_key,
            disabled_provider_keys=request.disabled_provider_keys,
            messages=request.messages,
            tools=request.tools,
            requested_max_output_tokens=request.max_output_tokens,
        )
        chain = self._with_free_prefix(decision, request, input_tokens_estimate)
        logical_request_id = str(uuid.uuid4())
        _audit_extra = dict(
            capability=request.capability or "",
            complexity_score=decision.complexity.score if decision.complexity else None,
            required_quality_level=decision.complexity.required_quality_class if decision.complexity else "",
            eligible_model_count=decision.eligible_model_count,
            total_candidate_count=decision.total_candidate_count,
            selection_reason=decision.selection_reason,
        )

        last_reason: str | None = None
        providers_tried: list[str] = []
        backoff_before_next = False
        backoff_attempt = 0

        for i, (key, cfg) in enumerate(chain):
            if not self._circuit_breakers.is_available(key):
                last_reason = "circuit_open"
                providers_tried.append(key)
                _attempts_total.inc(provider=cfg.provider, model=key, outcome="skipped_circuit_open")
                continue

            free_blocked = self._free_blocked(key, input_tokens_estimate, request.max_output_tokens)
            if free_blocked:
                # A free allowance spent or cooling down: skipped at no cost, the chain goes on.
                last_reason = f"free_{free_blocked}"
                providers_tried.append(key)
                continue

            effective_tokens, skip = self._preflight(
                _limits_after_spend(plan_limits, stream_discarded_cost_usd), cfg,
                request.max_output_tokens, key, input_tokens_estimate=input_tokens_estimate,
            )
            if skip:
                last_reason = "budget_exceeded"
                providers_tried.append(key)
                _attempts_total.inc(provider=cfg.provider, model=key, outcome="skipped_budget_exceeded")
                continue

            if backoff_before_next:
                await backoff_delay(
                    backoff_attempt, base_s=self._backoff_base_s, cap_s=self._backoff_cap_s,
                    jitter_ratio=self._backoff_jitter_ratio, sleep_fn=self._sleep_fn,
                )
                backoff_attempt += 1
                backoff_before_next = False

            failover_used = i > 0
            providers_tried.append(key)
            attempt_number = i + 1
            start = time.monotonic()

            try:
                response = await litellm.acompletion(
                    model=cfg.model_id,
                    messages=request.messages,
                    max_tokens=effective_tokens,
                    stream=True,
                    stream_options={"include_usage": True},
                    **free_pool.call_kwargs(key),
                    **dict(cfg.call_params),
                )
            except Exception as exc:
                latency_ms = (time.monotonic() - start) * 1000
                last_reason = _sanitize_error(exc)
                failure_class = classify_failure(exc)
                logger.warning(
                    "router stream failover | key=%s reason=%s class=%s attempt=%d/%d",
                    key, last_reason, failure_class.value, attempt_number, len(chain),
                )
                log_audit_event(create_audit_event(
                    session_id=request.session_id, user_plan=request.user_plan,
                    task_type=request.task_type.value, provider=cfg.provider, model=cfg.model_id,
                    status="attempt_failed", failover_used=failover_used, failover_reason=last_reason,
                    logical_request_id=logical_request_id, attempt_number=attempt_number,
                    retryable=(failure_class == FailureClass.RETRYABLE), failure_class=failure_class.value,
                    latency_ms=latency_ms,
                    **_audit_extra,
                ))
                _attempts_total.inc(provider=cfg.provider, model=key, outcome="failure")
                _attempt_latency_ms.observe(latency_ms, provider=cfg.provider, model=key, outcome="failure")
                # Audited and counted like any attempt (above); a free source's quota or failure then only
                # moves the chain on — no breaker trip, no backoff, and never an error to the caller.
                if self._free_on_error(key, exc):
                    continue
                if failure_class == FailureClass.NON_RETRYABLE_REQUEST:
                    raise RequestRejectedByProviderError(last_reason) from exc
                self._circuit_breakers.record_failure(key)
                if failure_class == FailureClass.RETRYABLE:
                    backoff_before_next = True
                    _retries_total.inc(provider=cfg.provider, model=key)
                continue

            self._circuit_breakers.record_success(key)
            _attempts_total.inc(provider=cfg.provider, model=key, outcome="success")
            # Counted when the stream is accepted, on the request's own estimate (the exact usage only arrives
            # with the last chunk, after the caller already has the answer): a free allowance errs on spent.
            self._free_on_success(key, None, tokens=input_tokens_estimate + (effective_tokens or 0) // 2)
            # Provider accepted — stream chunks. From here on, no failover:
            # see this method's own docstring.
            input_tokens = output_tokens = cached_tokens = 0
            _full_text_parts: list[str] = []
            # Nothing has been handed to the caller yet, so the response is
            # still recoverable. `released` flips the moment the first chunk
            # goes out and never flips back — every failover decision below
            # is gated on it being False.
            released = False
            held_chars = 0
            try:
                async for chunk in response:
                    delta = chunk.choices[0].delta if chunk.choices else None
                    if delta and delta.content:
                        _full_text_parts.append(delta.content)
                        if released:
                            yield RouterStreamChunk(text=delta.content)
                        else:
                            held_chars += len(delta.content)
                            if held_chars >= _STREAM_QUALITY_HOLD_CHARS:
                                # Long enough to be a real answer. Release what
                                # was held as one chunk and stream normally from
                                # here — this response is no longer recoverable.
                                released = True
                                yield RouterStreamChunk(text="".join(_full_text_parts))
                    # Accumulate usage across all chunks using max() — never overwrite a
                    # non-zero value with a later zero. Anthropic streaming sends input_tokens
                    # in message_start and output_tokens in message_delta (separate events);
                    # assignment would lose whichever arrived first.
                    if hasattr(chunk, "usage") and chunk.usage is not None:
                        it = int(getattr(chunk.usage, "prompt_tokens", None) or 0)
                        ot = int(getattr(chunk.usage, "completion_tokens", None) or 0)
                        details = getattr(chunk.usage, "prompt_tokens_details", None)
                        ct = int((getattr(details, "cached_tokens", 0) or 0) if details else 0)
                        if it > input_tokens:
                            input_tokens = it
                        if ot > output_tokens:
                            output_tokens = ot
                        if ct > cached_tokens:
                            cached_tokens = ct
            except BaseException:
                # The stream died mid-flight. Whatever was held really was
                # produced by the provider, so flush it rather than drop it,
                # then let the error propagate untouched. Emphatically NOT a
                # retry: this is the same attempt, and no second candidate is
                # reached (see test_router_streaming_resilience.py).
                if not released and _full_text_parts:
                    released = True
                    yield RouterStreamChunk(text="".join(_full_text_parts))
                raise

            # ── The one point on this path where failover is still safe ──
            # The stream finished while its whole response still fit inside
            # the hold, which means NOTHING has been handed to the caller.
            # Recovering here duplicates nothing and corrupts nothing, so
            # the "no failover once content has shipped" invariant is
            # untouched: it is about content that shipped, and none did.
            # (An exception raised while consuming the stream still
            # propagates with no retry — that path is not caught here.)
            stream_gate = quality_gate.evaluate_from_messages(
                messages=request.messages,
                response_text="".join(_full_text_parts),
                # stream() never sends `tools` (see the acompletion call
                # above), so a text-less stream really is an empty response.
                has_tool_calls=False,
            )
            if input_tokens == 0 and output_tokens == 0:
                resp_usage = getattr(response, "usage", None)
                if resp_usage is not None:
                    input_tokens = int(getattr(resp_usage, "prompt_tokens", None) or 0)
                    output_tokens = int(getattr(resp_usage, "completion_tokens", None) or 0)
                    details = getattr(resp_usage, "prompt_tokens_details", None)
                    cached_tokens = int((getattr(details, "cached_tokens", 0) or 0) if details else 0)
            attempt_cost_usd = _priced_cost_usd(cfg, input_tokens, output_tokens, cached_tokens)
            # Same rule as complete(): replace a rejected answer only while
            # the rest of this request's budget can pay for a replacement.
            is_last_candidate = i == len(chain) - 1 or not self._has_affordable_successor(
                chain, i, plan_limits, stream_discarded_cost_usd + attempt_cost_usd, input_tokens_estimate,
            )

            if not released and not stream_gate.passed and not is_last_candidate:
                # Same soft-failure handling complete() uses: the provider
                # answered fine at the transport level, so its breaker
                # records a SUCCESS, no backoff is applied, and the SAME
                # bounded chain moves on. No separate retry budget.
                latency_ms = (time.monotonic() - start) * 1000
                last_reason = f"quality_gate:{stream_gate.primary_reason}"
                logger.info(
                    "router stream quality_recovery (nothing yielded yet) | "
                    "key=%s reasons=%s attempt=%d/%d",
                    key, stream_gate.reasons, attempt_number, len(chain),
                )
                log_audit_event(create_audit_event(
                    session_id=request.session_id, user_plan=request.user_plan,
                    task_type=request.task_type.value, provider=cfg.provider, model=cfg.model_id,
                    status="quality_recovery", failover_used=failover_used, failover_reason=last_reason,
                    logical_request_id=logical_request_id, attempt_number=attempt_number,
                    retryable=True, failure_class="quality_gate", latency_ms=latency_ms,
                    **_audit_extra,
                ))
                self._circuit_breakers.record_success(key)
                _attempts_total.inc(provider=cfg.provider, model=key, outcome="quality_rejected")
                _quality_recovery_total.inc(
                    provider=cfg.provider, model=key, reason=stream_gate.primary_reason,
                )
                rejected_cost_usd = attempt_cost_usd
                stream_discarded_cost_usd += rejected_cost_usd
                _discarded_attempt_cost_usd.observe(rejected_cost_usd, provider=cfg.provider, model=key)
                continue

            # Accepted. Anything still held goes out now, before the final
            # metadata chunk — either it passed, or it is the last candidate
            # and failing open with a real answer beats an outage.
            if not released:
                released = True
                held_text = "".join(_full_text_parts)
                if held_text:
                    yield RouterStreamChunk(text=held_text)

            # Post-loop fallback: litellm's CustomStreamWrapper accumulates usage
            # internally and exposes it via .usage after the async-for completes.
            # Providers/configurations that don't emit usage in individual chunks
            # (Anthropic streaming without stream_options, some litellm versions)
            # are handled here as the authoritative total.
            if input_tokens == 0 and output_tokens == 0:
                resp_usage = getattr(response, "usage", None)
                if resp_usage is not None:
                    input_tokens = int(getattr(resp_usage, "prompt_tokens", None) or 0)
                    output_tokens = int(getattr(resp_usage, "completion_tokens", None) or 0)
                    details = getattr(resp_usage, "prompt_tokens_details", None)
                    cached_tokens = int((getattr(details, "cached_tokens", 0) or 0) if details else 0)

            # Cached prompt tokens bill below the standard input rate.
            # Streaming reports them exactly as complete() does (measured
            # 2026-09-12), so both paths price through the same helper or the
            # same request would cost different amounts depending on whether
            # the caller streamed it.
            call_cost_usd = _priced_cost_usd(cfg, input_tokens, output_tokens, cached_tokens)
            _observe_cache(cfg, provider_key=key, input_tokens=input_tokens,
                           cached_tokens=cached_tokens)
            cost_usd = round(call_cost_usd + stream_discarded_cost_usd, 8)
            tcu = calculate_tcu(cost_usd)

            # What is left for this block is the case the hold cannot cover:
            # a response that grew past _STREAM_QUALITY_HOLD_CHARS, shipped,
            # and only then turned out to fail the gate — plus the last
            # candidate failing open. Both are already in the caller's hands,
            # so this is observability, exactly as before.
            quality_gate_reason = ""
            if not stream_gate.passed:
                quality_gate_reason = stream_gate.primary_reason
                _quality_gate_flagged_total.inc(
                    provider=cfg.provider, model=key, reason=quality_gate_reason,
                )
                logger.warning(
                    "router quality_gate_flagged (stream, too late to recover) | "
                    "key=%s reasons=%s released=%s",
                    key, stream_gate.reasons, released,
                )

            _attempt_latency_ms.observe(
                (time.monotonic() - start) * 1000, provider=cfg.provider, model=key, outcome="success",
            )
            _cost_usd.observe(call_cost_usd, provider=cfg.provider, model=key)
            _tcu.observe(tcu, provider=cfg.provider, model=key)
            if failover_used:
                _failover_total.inc(provider=cfg.provider, model=key)

            event = create_audit_event(
                session_id=request.session_id,
                user_plan=request.user_plan,
                task_type=request.task_type.value,
                provider=cfg.provider,
                model=cfg.model_id,
                status="failover" if failover_used else "success",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                tcu=tcu,
                failover_used=failover_used,
                failover_reason=last_reason or (
                    f"quality_gate_flagged:{quality_gate_reason}" if quality_gate_reason else ""
                ),
                logical_request_id=logical_request_id,
                attempt_number=attempt_number,
                latency_ms=(time.monotonic() - start) * 1000,
                **_audit_extra,
            )
            log_audit_event(event)

            yield RouterStreamChunk(
                is_final=True,
                provider=cfg.provider,
                model=cfg.model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                cost_usd=cost_usd,
                tcu=tcu,
                failover_used=failover_used,
                failover_reason=last_reason,
            )
            return

        _chain_exhausted_total.inc(task_type=request.task_type.value)
        raise ProviderUnavailableError(providers_tried)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    # Free Compute Pool (free_pool.py)
    # ------------------------------------------------------------------

    def _with_free_prefix(self, decision, request: RoutingRequest, input_tokens_estimate: int) -> list:
        """The chain with the free sources that can stand in for its first model put in front of it.

        Never for a forced model (Continuity chose it on purpose). The prefix is not part of the plan's failover
        allowance: the paid chain behind it is exactly the one it always was.
        """
        chain = list(decision.chain)
        if request.forced_provider_key:
            return chain
        # Simple work only: facts that must be right, research and critical analysis go to the chosen model.
        precision = free_pool.precision_reason(request.messages, request.capability)
        if precision:
            if free_pool.configured_sources():
                free_pool.count(f"skipped_precision_{precision}", "all")
            return chain
        prefix = free_pool.prefix_for(
            chain,
            required_quality_class=decision.complexity.required_quality_class if decision.complexity else None,
            needs_tools=bool(request.tools),
            input_tokens_estimate=input_tokens_estimate,
            messages=request.messages,
            disabled_keys=frozenset(request.disabled_provider_keys or ()),
        )
        return prefix + chain

    def _free_blocked(self, key: str, input_tokens_estimate: int, max_output_tokens: int | None) -> str | None:
        """Why a free source must be skipped now ("quota" or "cooldown"), or None."""
        if not free_pool.is_free(key):
            return None
        if free_pool.source(key) is None:
            # A model the paid chain always had (START's gpt-oss-20b on the free Groq tier): skipped only once
            # the provider itself said it is out (a 429, or its own remaining count at zero) — never on a local
            # estimate, which would send traffic it used to carry to a paid model instead.
            reason = self._free_pool.cooling_down(key)
            if reason:
                free_pool.count("skipped_cooldown", key)
            return reason
        tokens = input_tokens_estimate + (max_output_tokens or 1024)
        reason = self._free_pool.can_use(key, tokens_estimate=tokens,
                                         usd_estimate=_free_list_usd(key, input_tokens_estimate,
                                                                     max_output_tokens or 1024))
        if reason:
            free_pool.count(f"skipped_{reason}", key)
        return reason

    def _free_on_error(self, key: str, exc: Exception) -> bool:
        """True when the failure belongs to the free pool and the chain should simply move on.

        A 429 or a quota answer from a free allowance cools that source down until its reset; it is not a
        provider failure, so the circuit breaker is untouched. Any other failure of a free prefix source is
        recorded on its breaker and never surfaces: the person never depends on a free source.
        """
        if not free_pool.is_free(key):
            return False
        if free_pool.is_rate_limit(exc):
            wait = self._free_pool.record_rate_limited(key, retry_after_s=free_pool.retry_after(exc),
                                                       daily=free_pool.is_daily_exhaustion(exc, key))
            free_pool.count("rate_limited", key)
            logger.info("router free pool | key=%s rate limited, cooling down %.0fs", key, wait)
            return True
        if free_pool.source(key) is not None:
            free_pool.count("failed", key)
            self._circuit_breakers.record_failure(key)
            logger.info("router free pool | key=%s failed (%s), paid route continues", key, type(exc).__name__)
            return True
        return False

    def _free_on_success(self, key: str, response, *, tokens: int | None = None) -> None:
        if not free_pool.is_free(key):
            return
        usage = getattr(response, "usage", None) if response is not None else None
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
        completion = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
        used = prompt + completion if usage else int(tokens or 0)
        hidden = getattr(response, "_hidden_params", None) if response is not None else None
        headers = (hidden or {}).get("additional_headers") if isinstance(hidden, dict) else None
        self._free_pool.record_success(key, tokens=used, usd=_free_list_usd(key, prompt or used, completion),
                                       headers=headers)
        free_pool.count("served", key)

    def _build_chain(
        self,
        task_type: TaskType,
        user_plan: str,
        plan_limits: PlanLimits,
        capability: str | None = None,
        forced_provider_key: str | None = None,
        disabled_provider_keys: frozenset[str] = frozenset(),
        messages: list[dict] | None = None,
        tools: list[dict] | None = None,
        requested_max_output_tokens: int | None = None,
    ) -> list[tuple[str, ProviderModel]]:
        """Thin wrapper preserving this method's original bare-list
        contract (several existing tests call it directly) — see
        _build_chain_decision() for the full decision object (chain +
        complexity metadata) complete()/stream() actually use."""
        return self._build_chain_decision(
            task_type, user_plan, plan_limits, capability,
            forced_provider_key=forced_provider_key,
            disabled_provider_keys=disabled_provider_keys,
            messages=messages, tools=tools,
            requested_max_output_tokens=requested_max_output_tokens,
        ).chain

    def _build_chain_decision(
        self,
        task_type: TaskType,
        user_plan: str,
        plan_limits: PlanLimits,
        capability: str | None = None,
        forced_provider_key: str | None = None,
        disabled_provider_keys: frozenset[str] = frozenset(),
        messages: list[dict] | None = None,
        tools: list[dict] | None = None,
        requested_max_output_tokens: int | None = None,
    ) -> "_ChainDecision":
        """Build the bounded failover chain: [(key, cfg), ...].

        forced_provider_key (Etapa 5F): when set, bypasses
        select_provider_chain() entirely and returns a single-candidate
        chain of exactly that key — see RoutingRequest.forced_provider_key
        for why (Continuity Mode already chose its one economic model;
        failing over to a different, uncontrolled candidate would defeat
        the whole point). NORMAL callers never set this, so this branch
        never executes for them — select_provider_chain() still runs
        exactly as before in every other case. The Adaptive Intelligence
        Router logic below NEVER runs on this branch either, for the
        exact same reason.

        disabled_provider_keys (Etapa 5H): operationally-disabled catalog
        keys are excluded from the returned chain entirely — same
        treatment as a key simply not being in PROVIDER_CATALOG at all
        (the `if k in PROVIDER_CATALOG` filter below already works this
        way; this is one more condition in that same filter, not a
        parallel mechanism). A disabled key therefore never appears in
        `providers_tried`/never gets attempted, and normal failover to the
        next candidate proceeds exactly as if that key didn't exist in the
        catalog for this call — no change to the attempt loop in
        complete()/stream() at all. Applied to BOTH branches below
        (defense in depth): even though Core's Continuity model selection
        is expected to already avoid picking a disabled key (see
        src/services/runtime_settings.py's docstring in Core), a
        forced_provider_key that happens to also be disabled still
        resolves to an empty chain here rather than silently calling it.

        Adaptive Intelligence Router (V2 — this etapa): `legacy_pool`
        below is the SAME curated, capability/task_type/plan-derived
        candidate SET selector.select_provider_chain() has always
        produced. It is now used ONLY as (a) the byte-for-byte legacy
        result when adaptive routing cannot run at all (`messages` is
        absent — the one case this has always had), and (b) the safe,
        conservative fallback if complexity computation itself raises.
        The REAL adaptive decision — whenever `messages` is present —
        instead builds its candidate set from
        providers.get_candidate_models(), the GLOBAL technical universe
        (the whole PROVIDER_CATALOG, filtered only by objective
        capability/task_type/disabled-key facts), then filters by
        quality/tools (get_eligible_models()) and ranks by cost
        (rank_by_cost()) exactly as before. select_provider_chain() is
        still called and still governs the legacy path — it is not
        removed, only no longer what BOUNDS the real candidate universe
        (mission: "não criar um segundo router" — see
        _apply_complexity_aware_ordering() and providers.py's own module
        docstring for the full pipeline).
        """
        if forced_provider_key is not None:
            if forced_provider_key in disabled_provider_keys and forced_provider_key in PROVIDER_CATALOG:
                self._log_disabled_provider(forced_provider_key)
            if forced_provider_key not in PROVIDER_CATALOG or forced_provider_key in disabled_provider_keys:
                return _ChainDecision(chain=[])
            return _ChainDecision(chain=[(forced_provider_key, PROVIDER_CATALOG[forced_provider_key])])

        keys = select_provider_chain(task_type, user_plan, capability)
        max_attempts = plan_limits.max_failover_attempts + 1  # +1 for primary
        keys = keys[:max_attempts]
        for k in keys:
            if k in PROVIDER_CATALOG and k in disabled_provider_keys:
                self._log_disabled_provider(k)
        legacy_pool = [
            (k, PROVIDER_CATALOG[k])
            for k in keys
            if k in PROVIDER_CATALOG and k not in disabled_provider_keys
        ]

        output_tokens_estimate = effective_max_output_tokens(plan_limits, requested_max_output_tokens)
        return self._apply_complexity_aware_ordering(
            legacy_pool, capability=capability, task_type=task_type, messages=messages, tools=tools,
            output_tokens_estimate=output_tokens_estimate, plan_limits=plan_limits,
            disabled_provider_keys=disabled_provider_keys,
        )

    def _apply_complexity_aware_ordering(
        self,
        legacy_pool: list[tuple[str, ProviderModel]],
        *,
        capability: str | None,
        task_type: TaskType,
        messages: list[dict] | None,
        tools: list[dict] | None,
        plan_limits: PlanLimits,
        disabled_provider_keys: frozenset[str] = frozenset(),
        output_tokens_estimate: int = 4_096,
    ) -> "_ChainDecision":
        """Build the REAL adaptive chain from the GLOBAL candidate
        universe, filtered by quality/tools and ranked by cost — with a
        safe fallback to `legacy_pool` (the curated, pre-V2 candidate
        set) on any condition that prevents a confident decision (mission
        requirement 20: "quando nenhuma complexity-aware rule puder ser
        aplicada, usar a chain atual como fallback" — never break an
        existing call). ONLY `messages` absent (the nonexistent-today,
        defensive case a future caller builds a chain without a real
        request) skips straight through with the legacy result unchanged
        — there is nothing to score. An EMPTY `legacy_pool` deliberately
        does NOT short-circuit here (a real, caught bug during this
        etapa's own regression run): every legacy candidate being
        disabled does NOT imply the GLOBAL catalog has nothing left —
        global discovery must still get a chance to find a technically
        capable candidate the legacy curation never even listed.

        Hardening (carried forward from 9987ea9): a candidate that fails
        min_quality_class/requires_tools is EXCLUDED from the returned
        chain outright, never appended as a "remainder" to fall back on.
        Failover therefore only ever moves between candidates that were
        ALL already confirmed capable — capable-to-incapable failover
        must never happen. If NOTHING in the global universe meets the
        bar, that is a controlled failure (empty chain -> the normal
        complete()/stream() loop raises ProviderUnavailableError exactly
        as it already does for an all-disabled or fully-exhausted chain),
        never a silent downgrade to a below-bar model.

        V2 addition — plan quality floor: the bar a candidate must meet
        is max(complexity.required_quality_class, plan_limits.min_quality_class)
        (by providers.QUALITY_CLASS_ORDER rank) — global discovery means
        the router can now see the ENTIRE catalog, so the plan's own
        floor (PlanLimits.min_quality_class) is what stops a trivial
        request on a PAID plan from legitimately routing to the
        cheapest-in-the-whole-catalog model (e.g. groq-llama4-scout,
        ultra_low) purely because it's technically "capable" of a
        trivial task. Complexity can only ever RAISE the bar above this
        floor, never lower it — see limits.py's own docstring for why
        each plan's floor value is not invented from nothing.

        Plan cost ceiling: deliberately NOT re-checked here as a second
        eligibility filter — enforce_request_limits()/_preflight() below
        (unchanged) already rejects/skips any candidate whose estimated
        cost exceeds plan_limits.max_estimated_cost_per_request_usd,
        exactly as it always has, per-attempt. Duplicating that check
        here would be a second financial rule for the same concept the
        mission explicitly says to avoid.
        """
        if not messages:
            return _ChainDecision(chain=legacy_pool)

        try:
            complexity = compute_complexity(capability=capability, messages=messages, tools=tools)
        except Exception:
            logger.warning("router: complexity computation failed, using legacy chain order", exc_info=True)
            _dynamic_chain_used_total.inc(selection_reason="static_fallback_error")
            return _ChainDecision(chain=legacy_pool, selection_reason="static_fallback_error")

        _complexity_score.observe(complexity.score, capability=capability or "unknown", band=complexity.band)

        required_quality_class = max(
            complexity.required_quality_class, plan_limits.min_quality_class,
            key=QUALITY_CLASS_ORDER.index,
        )

        # Entitlement/Quality Gate (this etapa) — plano/entitlement →
        # capability → quality permitida pelo plano → budget → custo →
        # seleção do modelo. Checked BEFORE get_candidate_models()/
        # get_eligible_models() run: a plan is never entitled to consider
        # a tier above its own PlanLimits.max_quality_class ceiling, so a
        # request needing more than that ceiling allows must never reach
        # candidate discovery, cost ranking, or a provider call at all —
        # zero attempts, zero cost, zero wasted retries. This does NOT
        # downgrade required_quality_class to fit the plan (that would be
        # exactly the silent-downgrade this gate exists to prevent) — the
        # task's real requirement is preserved on the raised exception,
        # unchanged, for the caller to report honestly.
        if QUALITY_CLASS_ORDER.index(required_quality_class) > QUALITY_CLASS_ORDER.index(plan_limits.max_quality_class):
            logger.info(
                "router: quality_not_entitled | plan=%s required_quality_class=%s plan_max_quality_class=%s "
                "capability=%s task_type=%s — controlled rejection, no candidate considered",
                plan_limits.plan, required_quality_class, plan_limits.max_quality_class,
                capability or "unknown", task_type.value,
            )
            _dynamic_chain_used_total.inc(selection_reason="quality_not_entitled")
            raise QualityNotEntitledError(
                required_quality_class=required_quality_class,
                plan_max_quality_class=plan_limits.max_quality_class,
                plan=plan_limits.plan,
            )

        global_candidates = get_candidate_models(
            capability=capability, task_type=task_type.value,
            disabled_provider_keys=disabled_provider_keys,
        )
        models = [cfg for _key, cfg in global_candidates]

        eligible = get_eligible_models(
            models,
            requires_tools=bool(tools),
            min_quality_class=required_quality_class,
        )
        _eligible_model_count.observe(
            len(eligible), capability=capability or "unknown", required_quality_class=required_quality_class,
        )

        if not eligible:
            # Nothing in the global technical universe meets the required
            # bar. Mission hardening: never silently use an incapable
            # model just because it's the only one left, and never
            # silently upgrade past the plan's own budget/floor policy
            # either — a controlled failure (empty chain) is the correct,
            # honest outcome, not a masked-as-success degrade.
            logger.warning(
                "router: no candidate in the global catalog meets required_quality_class=%s "
                "(capability=%s, task_type=%s) — controlled failure, chain intentionally empty",
                required_quality_class, capability or "unknown", task_type.value,
            )
            _dynamic_chain_used_total.inc(selection_reason="no_capable_model_controlled_failure")
            return _ChainDecision(
                chain=[],
                complexity=complexity,
                eligible_model_count=0,
                selection_reason="no_capable_model_controlled_failure",
                total_candidate_count=len(global_candidates),
            )

        ranked = rank_by_cost(
            eligible,
            max_output_tokens_estimate=output_tokens_estimate,
            # Lets a model carry a per-capability tiebreak instead of one
            # global priority. Only ever consulted between candidates of
            # EQUAL blended cost, so it cannot make routing more expensive.
            capability=capability,
        )
        # Correlate each ranked ProviderModel back to its catalog KEY by
        # object identity (id()), not by model_id/any field value — two
        # distinct catalog entries could in principle share a model_id
        # (e.g. the same underlying model reached via two different
        # aliases/providers); identity is the one thing guaranteed unique
        # per `global_candidates` entry regardless of catalog contents.
        key_by_identity = {id(cfg): key for key, cfg in global_candidates}
        # No "remainder" of excluded (incapable) candidates is appended —
        # `ranked` already contains every ELIGIBLE candidate (rank_by_cost
        # only sorts, never filters), so the chain below is exactly the
        # capable subset, cheapest-first. An incapable candidate never
        # enters this chain, so it can never be reached by failover.
        ordered_chain = [(key_by_identity[id(cfg)], cfg) for cfg in ranked]

        # Still bounded by the plan's own attempt budget — global
        # discovery finds MORE capable candidates than the old curated
        # chain typically listed, but a plan's max_failover_attempts is a
        # separate, pre-existing economic policy (how many provider
        # calls this request may cost, at most) that global discovery
        # must not silently expand.
        max_attempts = plan_limits.max_failover_attempts + 1
        ordered_chain = ordered_chain[:max_attempts]

        _dynamic_chain_used_total.inc(selection_reason="dynamic_eligible")
        return _ChainDecision(
            chain=ordered_chain,
            complexity=complexity,
            eligible_model_count=len(eligible),
            selection_reason="dynamic_eligible",
            total_candidate_count=len(global_candidates),
        )

    def _log_disabled_provider(self, key: str) -> None:
        """Etapa 5H observability — a candidate that WOULD have been in
        the chain got excluded by an operational kill switch. Logged +
        counted here (the one place this decision is made) rather than at
        every caller. Reuses the exact same counter/outcome-label
        convention _attempts_total already uses for skipped_circuit_open/
        skipped_budget_exceeded (Etapa 5G) — no new metrics system."""
        cfg = PROVIDER_CATALOG[key]
        logger.info("router: provider disabled, excluded from chain | key=%s", key)
        _attempts_total.inc(provider=cfg.provider, model=key, outcome="skipped_disabled")

    def _preflight(
        self,
        plan_limits: PlanLimits,
        cfg: ProviderModel,
        requested_max_tokens: int | None,
        provider_key: str,
        *,
        input_tokens_estimate: int = _MIN_INPUT_TOKENS_ESTIMATE,
    ) -> tuple[int, bool]:
        """Check limits before calling a provider.

        Returns (effective_tokens, skip).
        skip=True means this provider should be bypassed (budget exceeded).
        """
        estimated_cost = estimate_cost_usd(
            cfg.cost_per_1m_input_usd,
            cfg.cost_per_1m_output_usd,
            estimated_input_tokens=input_tokens_estimate,
            max_output_tokens=plan_limits.max_output_tokens_per_request,
        )
        try:
            effective_tokens = enforce_request_limits(
                plan_limits, requested_max_tokens, estimated_cost
            )
        except BudgetExceededError:
            logger.warning("router budget_exceeded | key=%s", provider_key)
            return 0, True
        return effective_tokens, False

    def _has_affordable_successor(
        self,
        chain: list[tuple[str, ProviderModel]],
        index: int,
        plan_limits: PlanLimits,
        spent_usd: float,
        input_tokens_estimate: int,
    ) -> bool:
        """Whether any candidate after `index` could still be called with
        what is left of this request's budget. Pure: no logging, no I/O."""
        remaining = _limits_after_spend(plan_limits, spent_usd)
        for key, cfg in chain[index + 1:]:
            if not self._circuit_breakers.is_available(key):
                continue
            estimate = estimate_cost_usd(
                cfg.cost_per_1m_input_usd,
                cfg.cost_per_1m_output_usd,
                estimated_input_tokens=input_tokens_estimate,
                max_output_tokens=remaining.max_output_tokens_per_request,
            )
            if estimate <= remaining.max_estimated_cost_per_request_usd:
                return True
        return False

    def _build_result(
        self,
        response,
        cfg: ProviderModel,
        failover_used: bool,
        failover_reason: str | None,
        request: RoutingRequest,
        provider_key: str = "",
        logical_request_id: str = "",
        attempt_number: int = 1,
        latency_ms: float = 0.0,
        audit_extra: dict | None = None,
        extra_cost_usd: float = 0.0,
    ) -> RoutingResult:
        """Extract usage, calculate cost/TCU, audit-log, and return RoutingResult.

        `extra_cost_usd` is spend this request already incurred on answers the
        quality gate discarded; it is added to the returned cost and TCU."""
        msg = response.choices[0].message
        usage = response.usage or type("U", (), {"prompt_tokens": 0, "completion_tokens": 0})()

        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or 0
        details = getattr(usage, "prompt_tokens_details", None)
        cached_tokens = (getattr(details, "cached_tokens", 0) or 0) if details else 0

        call_cost_usd = _priced_cost_usd(cfg, input_tokens, output_tokens, cached_tokens)
        _observe_cache(cfg, provider_key=provider_key, input_tokens=input_tokens,
                       cached_tokens=cached_tokens)
        cost_usd = round(call_cost_usd + max(0.0, extra_cost_usd), 8)
        tcu = calculate_tcu(cost_usd)
        _cost_usd.observe(call_cost_usd, provider=cfg.provider, model=provider_key or cfg.model_id)
        _tcu.observe(tcu, provider=cfg.provider, model=provider_key or cfg.model_id)

        event = create_audit_event(
            session_id=request.session_id,
            user_plan=request.user_plan,
            task_type=request.task_type.value,
            provider=cfg.provider,
            model=cfg.model_id,
            status="failover" if failover_used else "success",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            tcu=tcu,
            failover_used=failover_used,
            failover_reason=failover_reason or "",
            logical_request_id=logical_request_id,
            attempt_number=attempt_number,
            latency_ms=latency_ms,
            **(audit_extra or {}),
        )
        log_audit_event(event)

        return RoutingResult(
            content=msg.content,
            tool_calls=getattr(msg, "tool_calls", None),
            provider=cfg.provider,
            model=cfg.model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            cost_usd=cost_usd,
            tcu=tcu,
            failover_used=failover_used,
            failover_reason=failover_reason,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _priced_cost_usd(
    cfg: ProviderModel, input_tokens: int, output_tokens: int, cached_tokens: int,
) -> float:
    """The one place a call's tokens become dollars, for every router path.

    Cached input at its verified rate, then the provider's long-context tier
    when the prompt reaches it (see ProviderModel.long_context_threshold_tokens).
    """
    cost = calculate_actual_cost_usd(
        cfg.cost_per_1m_input_usd, cfg.cost_per_1m_output_usd,
        input_tokens, output_tokens,
        cached_input_tokens=cached_tokens,
        cost_per_1m_cached_input_usd=cfg.cost_per_1m_cached_input_usd,
    )
    threshold = cfg.long_context_threshold_tokens
    if threshold is not None and input_tokens >= threshold:
        cost *= cfg.long_context_price_multiplier
    return round(cost, 8)


def _completion_cost_usd(cfg: ProviderModel, response) -> float:
    """What a non-streaming provider response cost, from its own usage block."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0.0
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    details = getattr(usage, "prompt_tokens_details", None)
    cached_tokens = int((getattr(details, "cached_tokens", 0) or 0) if details else 0)
    return _priced_cost_usd(cfg, input_tokens, output_tokens, cached_tokens)


def _observe_cache(
    cfg: ProviderModel,
    *,
    provider_key: str | None,
    input_tokens: int,
    cached_tokens: int,
) -> None:
    """Record prompt-cache effectiveness for one call.

    Counts only — no prompt text, no user content, no identifiers beyond the
    provider/model labels the other router metrics already carry. A call that
    reports nothing cached still records a 0.0 hit rate deliberately: the
    difference between "cache missed" and "we stopped measuring" has to stay
    visible, and only recording hits would hide the first case entirely.
    """
    if input_tokens <= 0:
        return
    cached = max(0, min(cached_tokens, input_tokens))
    labels = {"provider": cfg.provider, "model": provider_key or cfg.model_id}
    _cached_input_tokens.observe(cached, **labels)
    _uncached_input_tokens.observe(input_tokens - cached, **labels)
    _cache_hit_rate.observe(cached / input_tokens, **labels)


def _sanitize_error(exc: Exception) -> str:
    """Return a safe failover reason string.

    Raw exception messages are NEVER returned — they may contain API keys,
    Authorization headers, or other secret material from provider responses.
    Only the exception class name is used to classify the error.
    """
    name = type(exc).__name__.lower()
    if "auth" in name or "credential" in name or "permission" in name:
        return "provider_auth_error"
    if "connection" in name or "timeout" in name or "connect" in name:
        return "provider_connection_error"
    if "rate" in name or "ratelimit" in name or "throttl" in name:
        return "provider_rate_limited"
    if "overload" in name or "capacity" in name or "unavailable" in name:
        return "provider_overloaded"
    return "provider_error"
