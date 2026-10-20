"""Continuity Mode — Etapa 5F of the TRINITY SCALE ENGINE.

WHAT CONTINUITY MODE IS (product concept, per the original TRINITY SCALE
ENGINE authorization): when a user's normal premium quota (daily/weekly
TCU) is exhausted, instead of a hard block, the system MAY continue
serving them through a cheap, TEXT-ONLY path. This is a PRODUCT/COST
policy for quota exhaustion — it is explicitly NOT a resilience mechanism:
provider failure, timeout, overload, rate limiting, or connection errors
are Etapa 5E's domain (Provider Resilience) and must never trigger
Continuity Mode.

BINDING RULE (from the Etapa 5F correction, verbatim):
    CONTINUITY MODE = TEXT INPUT + TEXT OUTPUT + TEXT-COMPATIBLE
    PROCESSING ONLY. Any multimodal capability exits Continuity Mode and
    must be rejected in a controlled way. No exceptions.

── What this module DELIVERS (this etapa) ──────────────────────────────────
A complete, tested, fail-closed TEXT-ONLY eligibility gate
(check_continuity_eligible / is_text_only_capability) and a
cheapest-eligible-CAPABLE-model selection hook for Continuity specifically
(select_continuity_model, built on trinity_router.providers.
select_cheapest_capable from Etapa 5E) — plus a pure classifier
(is_premium_quota_exhausted) that recognizes the ONE HTTPException shape
that represents "the user's normal path was blocked by exhausted premium
quota" (entitlement_service.check_and_reserve()'s daily/weekly rejection),
as distinct from every other rejection reason in this codebase (Request
Control's rate/concurrency limits, Agent Scheduler's capacity limits,
provider failures classified by Etapa 5E's retry.classify_failure(),
budget_exceeded, 404s, auth failures). All four are pure functions —
no I/O, no database access, no side effects — proven independently correct
by tests/unit/test_continuity_mode.py.

── What this module DOES NOT do (deliberately, and why) ────────────────────
It is NOT wired into sessions.py's live request flow. Two product
decisions are genuinely absent from this codebase's existing architecture,
and inventing either would risk real, unaccountable provider cost or an
arbitrary product policy:

  1. WHICH PLANS ARE CONTINUITY-ELIGIBLE. `PlanEntitlement`
     (src/entitlements.py) has no such flag today — only TCU quotas and
     unrelated feature flags (supports_agent_mode, supports_trinity_code,
     supports_trinity_work, supports_image_generation, max_projects,
     max_agents). Per explicit instruction, this must not be invented.

  2. WHAT FUNDS A CONTINUITY EXECUTION once daily/weekly TCU quota is
     already exhausted. Re-calling check_and_reserve() unmodified would
     immediately re-raise the SAME 429 (the quota that's exhausted is the
     one it checks) — so Continuity cannot simply "keep going" through the
     existing reservation path. A structurally well-suited EXISTING
     mechanism was found during audit: `CreditService`/`CreditBalance`/
     `CreditTransaction` (src/services/credit_service.py,
     src/models/ledger.py) already tracks a per-user, per-currency ledger
     balance, and "TCU" is explicitly listed among its supported internal
     currency codes — debiting a user's purchased TCU credit balance for a
     Continuity execution would NOT bypass entitlement semantics (it is a
     genuinely different, already-idempotent, already-race-safe accounting
     dimension, not the daily/weekly usage quota). This is flagged as a
     CANDIDATE, not a decision — nothing in the codebase currently
     connects CreditService to Continuity Mode, and assuming that
     connection without confirmation would be exactly the kind of
     invention this etapa was told to avoid. See the Etapa 5F final report
     for the precise gap and the question this raises for a future etapa.

No code path in this module — or anywhere else touched by this etapa —
lets a request that was rejected for exhausted quota proceed to a real
provider call. `PaymentOrder`, `refund_and_record()`, ledger invariants,
and entitlement semantics are all untouched.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sqlalchemy.orm import Session as DBSession
from trinity_router.providers import PROVIDER_CATALOG, ProviderModel, select_cheapest_capable

from src.capability import Capability

# Deliberately independent from capability.pipelines.UNIMPLEMENTED_CAPABILITIES
# even though the two sets are equal in VALUE today ({IMAGE, VIDEO, AUDIO}).
# They are different concepts that only coincide right now:
#   - UNIMPLEMENTED_CAPABILITIES is a BACKEND-CAPABILITY fact — it can
#     shrink the day image/video/audio generation is actually implemented.
#   - MULTIMODAL_CAPABILITIES below is a permanent MODALITY/COST POLICY —
#     it must stay exactly {IMAGE, VIDEO, AUDIO} even after any of them
#     becomes implemented, because Continuity Mode's TEXT-ONLY rule has no
#     "except when it's cheap enough" clause. Keeping these as two
#     separate constants (not one shared alias) means a future change to
#     one can never silently change the other's meaning.
MULTIMODAL_CAPABILITIES: frozenset[Capability] = frozenset({
    Capability.IMAGE, Capability.VIDEO, Capability.AUDIO,
})


class ContinuityRejectionReason(str, Enum):
    MULTIMODAL_CAPABILITY = "multimodal_capability"
    NO_ELIGIBLE_MODEL = "no_eligible_model"


class ContinuityRejected(Exception):
    """Raised for a controlled Continuity Mode rejection — never a signal
    to retry, fail over, or fall back to a different (possibly premium or
    incapable) path. `reason` distinguishes why; `detail` is safe to log
    but intentionally generic (never exposes provider/model internals)."""

    def __init__(self, reason: ContinuityRejectionReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


def is_text_only_capability(capability: Capability) -> bool:
    """True iff `capability` satisfies Continuity Mode's TEXT INPUT + TEXT
    OUTPUT + TEXT-COMPATIBLE PROCESSING ONLY rule.

    `capability` must be the SERVER-DERIVED value from
    src.capability.classify() — never anything a client could set
    directly. As audited for this etapa: ChatRequest and AgentRequest
    (src/schemas/message.py, src/schemas/agent.py) have exactly one
    content field (`content: str`) and no capability/attachment/image/
    audio/video field of any kind — /chat, /agent, and /message have no
    channel today through which a client could supply either a multimodal
    payload or a self-declared capability. This function does not rely on
    that fact, though: it only ever trusts the enum value it is given,
    never anything else about the request — so it stays correct even if a
    future request schema adds a client-facing field.
    """
    return capability not in MULTIMODAL_CAPABILITIES


def check_continuity_eligible(capability: Capability) -> None:
    """Fail-closed gate. Raises ContinuityRejected for any multimodal
    capability; returns None (silently) for a text-only one.

    MUST be called — and must raise cleanly, with nothing having run yet
    that could cost money — before any provider selection or provider
    call is attempted under Continuity Mode. This function performs no
    I/O and touches no database, so calling it has zero side effects to
    undo on rejection.
    """
    if not is_text_only_capability(capability):
        raise ContinuityRejected(
            ContinuityRejectionReason.MULTIMODAL_CAPABILITY,
            f"Continuity Mode does not support the {capability.value!r} capability — text only.",
        )


def select_continuity_model(
    *,
    requires_tools: bool = False,
    requires_streaming: bool = True,
    min_context_window: int = 0,
    catalog: dict[str, ProviderModel] | None = None,
) -> tuple[str, ProviderModel]:
    """Return (catalog_key, model) for the cheapest catalog model that is
    BOTH text-only AND capable of the stated requirements — "cheapest
    eligible capable model", never "cheapest model absolutely" (Etapa 5F
    correction, section 8). The key is what a caller passes as
    RoutingRequest.forced_provider_key (Etapa 5F execution wiring) — the
    router only knows models by their catalog key, not by identity.

    Explicitly filters to `modality == "text"` before ranking by cost —
    trinity_router.providers.select_cheapest_capable() (Etapa 5E) does
    NOT filter by modality on its own (it compares context/tools/
    streaming/quality only), so this extra filter is what actually
    enforces Continuity's output guarantee. Every entry in
    PROVIDER_CATALOG is `modality="text"` today, so this is currently a
    no-op in practice — but it is what keeps the guarantee real rather
    than incidental if a non-text catalog entry (e.g. a cheap
    image-capable model) is ever added; see
    test_continuity_ignores_a_cheaper_multimodal_candidate.

    Raises ContinuityRejected(NO_ELIGIBLE_MODEL) if no candidate
    qualifies — a controlled rejection, never an arbitrary fallback to an
    incapable-but-cheaper model, and never a reason to try a premium
    model instead (that would defeat Continuity's entire cost rationale).
    """
    items = [(k, m) for k, m in (catalog or PROVIDER_CATALOG).items() if m.modality == "text"]
    text_only_models = [m for _, m in items]

    chosen = select_cheapest_capable(
        text_only_models,
        requires_tools=requires_tools,
        requires_streaming=requires_streaming,
        min_context_window=min_context_window,
    )
    if chosen is None:
        raise ContinuityRejected(
            ContinuityRejectionReason.NO_ELIGIBLE_MODEL,
            "No economic, text-only model satisfies this task's requirements under Continuity Mode.",
        )
    key = next(k for k, m in items if m is chosen)
    return key, chosen


# entitlement_service.check_and_reserve()'s three quota checks (daily/
# weekly/monthly) each raise HTTPException(429, detail={"error": <one of
# these>, ...}) — see src/services/entitlement_service.py. Only
# daily/weekly represent the condition Continuity Mode exists for.
# monthly_limit_exceeded is deliberately excluded: monthly_tcu_limit is
# None for every plan today (2026-08-31 product decision recorded in
# entitlements.py — "no operational monthly usage cap"), so that branch is
# currently unreachable; if a real monthly cap is reintroduced later, this
# set should be revisited alongside that change, not silently included now.
_PREMIUM_QUOTA_EXHAUSTED_ERRORS: frozenset[str] = frozenset({
    "daily_limit_exceeded", "weekly_limit_exceeded",
})


def is_premium_quota_exhausted(detail: object) -> bool:
    """True iff `detail` is the specific HTTPException.detail shape
    entitlement_service.check_and_reserve() raises for an exhausted
    daily/weekly TCU quota — the ONLY condition Continuity Mode is meant
    to respond to.

    Deliberately narrow and pure (no I/O): returns False for every other
    rejection shape in this codebase — Request Control's
    "rate_limit_user"/"rate_limit_ip"/"concurrency_user"/
    "concurrency_global" (Etapa 5B), Agent Scheduler's
    "concurrency_global"/"concurrency_user" (Etapa 5D),
    "budget_exceeded"/"circuit_open" (Etapa 5E's ModelRouter), and any
    provider failure (Etapa 5E's retry.FailureClass) — none of those mean
    "the user's normal path was blocked by exhausted premium quota", and
    none of them may trigger Continuity Mode. In particular: a provider
    outage must keep being handled entirely by Etapa 5E's retry/failover/
    circuit-breaker machinery, never by falling through to Continuity.
    """
    if not isinstance(detail, dict):
        return False
    return detail.get("error") in _PREMIUM_QUOTA_EXHAUSTED_ERRORS


class ContinuityNotApplicable(Exception):
    """Raised when the rejection that triggered this check was NOT
    "premium quota exhausted" — e.g. a Request Control/Agent Scheduler
    rejection, or (critically) a provider failure. Continuity Mode must
    NEVER activate for any of these; they stay entirely Etapa 5E's
    (Provider Resilience) responsibility. Distinct from ContinuityRejected
    (which means "quota WAS exhausted, but Continuity itself declined the
    request for some other reason") — this means "Continuity was never
    the right response to begin with"."""

    def __init__(self, detail: object) -> None:
        super().__init__("Rejection reason is not exhausted premium quota — Continuity does not apply")
        self.detail = detail


@dataclass(frozen=True)
class ContinuityActivation:
    """Successful Continuity Mode activation — everything a caller needs
    to actually dispatch the real economic execution (Etapa 5F, second
    round — wired into chat_service.py/agent_service.py via
    forced_provider_key): the selected model's catalog key (what
    RoutingRequest.forced_provider_key expects), the model itself, and
    the financial receipt (already debited, atomically, via
    BillingOrchestrator)."""
    provider_key: str
    model: ProviderModel
    execution_id: str
    estimated_tcu: float
    credit_transaction_id: str


def activate_continuity_or_raise(
    db: DBSession,
    *,
    user_id: str,
    plan: str,
    capability: Capability,
    quota_rejection_detail: object,
    estimated_tcu: float,
    execution_id: str,
    requires_tools: bool = False,
    requires_streaming: bool = True,
    min_context_window: int = 0,
) -> ContinuityActivation:
    """The single entry point implementing the full activation gate from
    the Etapa 5F correction, section 8 — steps 4-9 (steps 1-3, Request
    Control and pipeline eligibility, are the caller's responsibility;
    this function is only ever reached after those already passed):

      4. `quota_rejection_detail` must represent exhausted premium quota
         (is_premium_quota_exhausted) — anything else raises
         ContinuityNotApplicable. In particular, a provider failure/
         timeout/overload/rate-limit/connection error is NEVER treated as
         a Continuity trigger; that stays Etapa 5E's domain entirely.
      7. TEXT-ONLY (check_continuity_eligible) — checked BEFORE any
         financial code runs, so a multimodal request never touches
         Continuity's budget or the credit ledger at all.
      8. an eligible economic model exists (select_continuity_model) —
         also checked before any financial code runs, for the same
         reason: "no capable candidate" must not spend anything either.
      5-6-9. plan.continuity_enabled + Continuity's own daily budget +
         the actual financial debit, atomically, via
         continuity_entitlement_service.check_and_debit_continuity() —
         deliberately LAST, since it is the one step with a real,
         hard-to-undo side effect (a ledger write).

    Note on ordering vs. the correction's literal 5-6-7-8-9 numbering:
    steps 7-8 (cheap, side-effect-free checks) are evaluated before 5-6-9
    (the financial commitment) so that a request failing 7 or 8 costs
    nothing — "multimodalidade não consome orçamento" holds by
    construction, not by convention. This does not change WHICH checks
    exist or what each one enforces, only the order two cheap checks run
    in relative to the one expensive one.

    Raises (never leaves a request able to reach a real provider call on
    any failure): ContinuityNotApplicable, ContinuityRejected
    (MULTIMODAL_CAPABILITY or NO_ELIGIBLE_MODEL), ContinuityNotEnabledError,
    ContinuityBudgetExceededError, InsufficientCreditError.
    """
    from src.services.continuity_entitlement_service import check_and_debit_continuity

    if not is_premium_quota_exhausted(quota_rejection_detail):
        raise ContinuityNotApplicable(quota_rejection_detail)

    check_continuity_eligible(capability)  # step 7 — raises ContinuityRejected if not

    provider_key, model = select_continuity_model(  # step 8 — raises ContinuityRejected if none
        requires_tools=requires_tools,
        requires_streaming=requires_streaming,
        min_context_window=min_context_window,
    )

    result = check_and_debit_continuity(  # steps 5-6-9, atomically
        db, user_id=user_id, plan=plan, estimated_tcu=estimated_tcu, execution_id=execution_id,
    )

    return ContinuityActivation(
        provider_key=provider_key, model=model, execution_id=execution_id,
        estimated_tcu=estimated_tcu, credit_transaction_id=result.transaction.id,
    )
