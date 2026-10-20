"""
Per-plan limits for the TRINITY Model Router.

All limits are enforced server-side before any provider call.
Clients cannot observe, bypass, or configure these values.
"""
from __future__ import annotations

from dataclasses import dataclass


class BudgetExceededError(Exception):
    """Request would exceed the user's plan limits.

    The message is intentionally generic — internal limit values
    are not exposed to clients.
    """
    def __init__(self, limit_type: str, user_plan: str) -> None:
        super().__init__(f"Request exceeds the {limit_type} limit for the '{user_plan}' plan")
        self.limit_type = limit_type   # e.g., "cost_per_request", "tokens_per_request"
        self.user_plan = user_plan


@dataclass(frozen=True)
class PlanLimits:
    """Immutable per-plan constraints. All fields are server-controlled.

    min_quality_class (Adaptive Intelligence Router V2): the plan's own
    quality FLOOR — a value from providers.QUALITY_CLASS_ORDER (this
    module doesn't import that constant to avoid a new cross-module
    coupling; router.py, which already imports both, validates/compares
    it). Global candidate discovery (providers.get_candidate_models())
    means the router can now see the ENTIRE catalog, not just a plan's
    hand-curated chain — without this floor, a trivial request on any
    paid plan would legitimately route to the cheapest model in the
    whole catalog (e.g. groq-llama4-scout, ultra_low), which would erase
    each plan's previously-implicit quality guarantee. This value is NOT
    invented from nothing: it is derived from what each plan's own
    pre-V2 curated CHAT chain already tolerated as its worst realistic
    case (see each entry's comment below) — a plan can never be routed
    BELOW what it already, historically, sometimes used. The actual
    per-request bar fed into eligibility is
    max(complexity.required_quality_class, plan_limits.min_quality_class)
    — complexity can only ever raise the bar above this floor, never
    lower it.

    max_quality_class (Entitlement/Quality Gate — this etapa): the plan's
    quality CEILING. NOT a new invented number — derived directly from
    this SAME plan's own existing max_output_tokens_per_request and
    max_estimated_cost_per_request_usd, by asking: "does the cheapest
    candidate at quality tier X, priced at this plan's own token cap,
    fit inside this plan's own cost ceiling?" (using the unchanged
    tcu.estimate_cost_usd() and the unchanged PROVIDER_CATALOG prices —
    see each entry's comment below for the exact figures). A plan is
    never entitled to a tier whose cheapest model it could not actually
    afford at its own limits — this makes explicit, as a POLICY checked
    before candidate discovery, a boundary that previously only emerged
    as an accidental, generic BudgetExceededError deep inside the
    per-provider preflight (root-caused 2026-09-07: 'start' plan reaching
    required_quality_class='premium' silently failed with zero attempts
    and zero audit trail, because claude-opus — the only premium
    candidate — could never pass its own preflight on that plan). The
    router enforces this BEFORE get_candidate_models()/get_eligible_models()
    ever run for a request whose required tier exceeds this ceiling,
    raising QualityNotEntitledError instead of silently downgrading the
    request to a lower tier or silently trying (and failing) a candidate
    the plan cannot afford — see router.py's _apply_complexity_aware_ordering()."""
    plan: str
    max_output_tokens_per_request: int    # hard cap on output tokens
    max_estimated_cost_per_request_usd: float  # pre-flight cost limit (USD)
    max_failover_attempts: int            # how many extra providers to try
    min_quality_class: str = "ultra_low"
    max_quality_class: str = "premium"


# ---------------------------------------------------------------------------
# Plan definitions
# Actual commercial values are set here server-side — clients never see these.
# ---------------------------------------------------------------------------

PLAN_LIMITS: dict[str, PlanLimits] = {
    # FREE — the cheapest, most restricted plan (commercial policy, this
    # etapa): must ALWAYS use the cheapest eligible model for the real
    # capability, and must NEVER reach "standard" or "premium".
    # min_quality_class="ultra_low" (corrected 2026-09-07 — was "low",
    # which wrongly EXCLUDED groq-llama4-scout, the one ultra_low/cheapest
    # candidate, from FREE for every request; this floor was never meant
    # to raise the bar above what complexity itself requires, only
    # min_quality_class did that by accident here). "ultra_low" is the
    # weakest possible floor — capability/task_type gates (get_eligible_
    # models()'s requires_tools + get_candidate_models()'s capability/
    # task_type filters, both untouched) still apply exactly as before:
    # groq only ever wins a request it can mechanically serve, never a
    # capability downgrade.
    # max_quality_class="low" (corrected 2026-09-07 — was "standard",
    # which was mathematically affordable — xai-grok's ~$0.0051 fits
    # FREE's $0.02 ceiling — but the commercial policy for FREE is
    # stricter than "whatever fits the budget": FREE must never reach
    # "standard", only "ultra_low"/"low". A "standard" or "premium"
    # requirement on FREE now raises QualityNotEntitledError immediately
    # — no candidate considered, no provider called, no retry.
    "free": PlanLimits(
        plan="free",
        max_output_tokens_per_request=1_024,
        max_estimated_cost_per_request_usd=0.02,
        max_failover_attempts=0,
        min_quality_class="ultra_low",
        max_quality_class="low",
    ),
    # min_quality_class="ultra_low": START's pre-V2 CHAT primary was
    # already groq-llama4-scout (ultra_low) — this floor changes nothing
    # for START's simple/trivial requests either.
    # max_quality_class="standard": at START's own limits (max_output=4096,
    # cost_ceiling=$0.10), the cheapest "standard" candidate (xai-grok)
    # costs ~$0.0127 — affordable. The cheapest "premium" candidate
    # (claude-opus) costs ~$0.1124 — exceeds the $0.10 ceiling (this is
    # the exact root cause confirmed 2026-09-07: 'start'+premium always
    # failed BudgetExceededError, silently, deep in the per-provider
    # preflight, with zero eligible-candidate attempts ever logged).
    # START is therefore entitled up to "standard", never "premium".
    "start": PlanLimits(
        plan="start",
        max_output_tokens_per_request=4_096,
        max_estimated_cost_per_request_usd=0.10,
        max_failover_attempts=1,
        min_quality_class="ultra_low",
        max_quality_class="standard",
    ),
    # min_quality_class="low": PRO's pre-V2 CHAT chain's own worst-case
    # fallback (gemini-25-flash) was already "low" — this floor is the
    # exact same guarantee PRO already had, just made explicit; it is
    # what keeps a trivial PRO request from dropping all the way to
    # groq-llama4-scout (ultra_low) under global candidate discovery.
    # max_quality_class="premium": at PRO's own limits (max_output=8192,
    # cost_ceiling=$0.50), claude-opus costs ~$0.2148 — comfortably
    # affordable. No new ceiling behavior for PRO; unchanged from today.
    "pro": PlanLimits(
        plan="pro",
        max_output_tokens_per_request=8_192,
        # Lowered 2026-09-11 (session pass) so a single call can never be
        # authorised for more than one fresh 5-hour window holds. It was
        # 0.50 (50 TCU) against a 28 TCU session, so one permitted call
        # could cost ~1.8x the whole window and the session check would
        # only notice on the NEXT request. Lowering is also the safe
        # direction for max_quality_class, which is derived from this
        # value: a smaller ceiling can never grant a band.
        max_estimated_cost_per_request_usd=0.24,
        max_failover_attempts=2,
        min_quality_class="low",
        # max_quality_class="standard" (2026-09-11, economics pass): PRO used
        # to reach "premium", and that single fact dominated its cost. The
        # same simulated week of heavy use measures 684 TCU capped at
        # standard and 3.480 capped at premium — a 5x multiplier on
        # identical behaviour, which is what made PRO impossible to price.
        # Standard still covers claude-sonnet/gpt-4o class work, so PRO
        # loses none of the tasks it is sold for; premium is now what MAX
        # actually buys, which is a cleaner product story than a number
        # nobody can feel. The figures come from the plan ladder simulation
        # in core's tests/unit/test_plan_economics.py.
        max_quality_class="standard",
    ),
    # min_quality_class="low": same reasoning as PRO — MAX's pre-V2 CHAT
    # chain also bottomed out at gemini-25-flash ("low") in its own
    # worst-case failover, so this floor changes nothing for MAX's own
    # historical worst case while still letting global discovery surface
    # a cheaper genuinely-capable model (e.g. claude-haiku) above that floor.
    # max_quality_class="premium": at MAX's own limits (max_output=16384,
    # cost_ceiling=$2.00), claude-opus costs ~$0.4196 — comfortably
    # affordable. No new ceiling behavior for MAX; unchanged from today.
    "max": PlanLimits(
        plan="max",
        max_output_tokens_per_request=16_384,
        # Lowered 2026-09-11 (session pass): 2.00 (200 TCU) exceeded even
        # MAX's enlarged 200 TCU session. 0.90 keeps a fresh window able
        # to afford one maximum call with room left over.
        max_estimated_cost_per_request_usd=0.90,
        max_failover_attempts=3,
        min_quality_class="low",
        max_quality_class="premium",
    ),
    # TEAM — same model limits as MAX; workspace/seat features added in Entitlement layer (Phase F).
    # max_quality_class="premium": identical numbers to MAX — see MAX's comment above.
    "team": PlanLimits(
        plan="team",
        max_output_tokens_per_request=16_384,
        # Lowered 2026-09-11 (session pass): 2.00 (200 TCU) exceeded even
        # MAX's enlarged 200 TCU session. 0.90 keeps a fresh window able
        # to afford one maximum call with room left over.
        max_estimated_cost_per_request_usd=0.90,
        max_failover_attempts=3,
        min_quality_class="low",
        max_quality_class="premium",
    ),
}

_DEFAULT_LIMITS = PLAN_LIMITS["start"]


def get_plan_limits(user_plan: str) -> PlanLimits:
    """Return limits for the given plan. Unknown plans fall back to 'start' (most restrictive)."""
    return PLAN_LIMITS.get(user_plan.lower(), _DEFAULT_LIMITS)


def effective_max_output_tokens(
    plan_limits: PlanLimits,
    requested_max_tokens: int | None,
) -> int:
    """Pure token-cap logic: the plan-capped, request-aware max output
    token estimate. Shared by enforce_request_limits() below (the
    authoritative per-provider preflight check) and the Adaptive
    Intelligence Router's chain-ordering step (router.py), which needs
    this SAME number before any provider is picked — reusing it here
    avoids a second, parallel token-estimation formula for what is really
    one concept: "how many output tokens should this request be priced
    for."""
    return min(
        requested_max_tokens if requested_max_tokens is not None
        else plan_limits.max_output_tokens_per_request,
        plan_limits.max_output_tokens_per_request,
    )


def enforce_request_limits(
    plan_limits: PlanLimits,
    requested_max_tokens: int | None,
    estimated_cost_usd: float,
) -> int:
    """Validate and cap this request against plan limits.

    Returns the effective (possibly reduced) max_output_tokens.
    Raises BudgetExceededError if the estimated cost exceeds the plan limit.

    Token capping is silent (the request is served with a lower cap).
    Cost violations are raised as errors (the request is rejected).
    """
    effective_tokens = effective_max_output_tokens(plan_limits, requested_max_tokens)
    # Reject if estimated cost exceeds per-request budget
    if estimated_cost_usd > plan_limits.max_estimated_cost_per_request_usd:
        raise BudgetExceededError("cost_per_request", plan_limits.plan)
    return effective_tokens
