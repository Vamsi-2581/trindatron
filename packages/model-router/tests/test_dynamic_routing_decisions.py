"""Adaptive Intelligence Router — real routing decision scenarios (mission
requirement 17), exercised against the REAL PROVIDER_CATALOG/selector
chains through ModelRouter._build_chain_decision(), not synthetic models.

Each scenario constructs messages/capability/plan/task_type chosen to
land in a specific complexity band, then asserts on the TOP of the
resulting chain (decision.chain[0]) — the model that would actually be
tried first. Scores are computed from complexity.py's own real formula
(not hand-picked) — see each test's inline arithmetic comment for why the
chosen inputs land where they do.
"""
from __future__ import annotations

import pytest

from trinity_router.limits import get_plan_limits
from trinity_router.providers import PROVIDER_CATALOG
from trinity_router.router import ModelRouter, QualityNotEntitledError
from trinity_router.selector import TaskType


def _messages(text: str) -> list[dict]:
    return [{"role": "system", "content": "sys"}, {"role": "user", "content": text}]


def _decision_for(router: ModelRouter, *, task_type, plan, capability=None, text="hi", tools=None, disabled=frozenset()):
    return router._build_chain_decision(
        task_type, plan, get_plan_limits(plan), capability,
        disabled_provider_keys=disabled, messages=_messages(text), tools=tools,
    )


def _top_model_id(router: ModelRouter, *, task_type, plan, capability=None, text="hi", tools=None, disabled=frozenset()):
    decision = _decision_for(
        router, task_type=task_type, plan=plan, capability=capability, text=text, tools=tools, disabled=disabled,
    )
    assert decision.chain, "expected a non-empty chain"
    return decision.chain[0][0], decision


# ── Scenario A: simple chat -> cheapest model ────────────────────────────────

def test_scenario_A_simple_chat_picks_cheapest_model():
    router = ModelRouter()
    # capability=None/chat -> base=0, plain "hi" -> no other signal fires.
    # score=0 -> required_quality_class="ultra_low", floored by PRO's own
    # min_quality_class="low" (limits.py) -> effective bar = "low".
    # V2: candidate discovery is GLOBAL (providers.get_candidate_models()),
    # not just select_provider_chain()'s 3-entry PRO/CHAT list — every
    # catalog model at "low" tier or above is eligible (groq-llama4-scout,
    # "ultra_low", is the only one excluded by the plan's own floor).
    # gpt-4o-mini ($0.15/$0.60) is the cheapest of those — NOT
    # gemini-25-flash, which was the answer under V1's curated-pool-only
    # discovery (gpt-4o-mini was never in PRO's curated CHAT chain at all).
    top, decision = _top_model_id(router, task_type=TaskType.CHAT, plan="pro", text="hi")
    assert top == "gpt-4o-mini"
    assert decision.selection_reason == "dynamic_eligible"


# ── Scenario B: moderate explanation -> cheapest medium-capability model ────

def test_scenario_B_moderate_explanation_picks_cheapest_standard_model():
    # capability=code (base=20) + reasoning keyword "why"/"compare" (15) = 35
    # -> band=simple, required_quality_class="low" (score 25-49). Use a
    # slightly longer prompt to push past 50 into "standard" territory:
    # base(20) + reasoning(15) + code_signal via keyword "function" (15) = 50 -> standard.
    # V2: global discovery surfaces claude-haiku ($1.00/$5.00, "standard")
    # and xai-grok ($1.25/$2.50, "standard") as ALSO eligible — both
    # cheaper than claude-sonnet ($2.00/$10.00), which was V1's answer
    # (neither was ever in ANY curated chain — see providers.py's own
    # comments). xai-grok's lower output price makes it the cheapest
    # overall for this request shape.
    text = "Why does this function behave this way, and how would you compare it to an alternative?"
    router = ModelRouter()
    top, decision = _top_model_id(router, task_type=TaskType.CHAT, plan="pro", capability="code", text=text)
    assert top == "xai-grok"  # cheapest of the globally-eligible "standard"+ candidates
    assert decision.selection_reason == "dynamic_eligible"


# ── Scenario C: complex coding -> cheapest high-capability coding model ─────

def test_scenario_C_complex_coding_picks_cheapest_capable_not_the_best():
    # base(code=20) + code_signal(fence, 15) + reasoning("debug", not matched by
    # our pattern) ... use explicit signals: code fence + "step by step" (multi_step=10)
    # + a code keyword ("traceback", 15 via _CODE_KEYWORD_PATTERN) = 20+15+10=45 (simple/moderate)
    # Push further with reasoning keyword "explain" (15) -> 60 -> moderate/complex boundary,
    # required_quality_class="standard" at 60. Global discovery (V2) makes
    # claude-opus (premium), claude-haiku (standard), AND xai-grok
    # (standard, cheapest of all for this request shape) eligible —
    # xai-grok must win: "cheap capable beats better", matching the
    # mission's own worked example, now proven across the WHOLE catalog
    # rather than just MAX's old curated pool.
    text = "```python\ndef f(): pass\n```\nExplain the traceback step by step."
    router = ModelRouter()
    top, decision = _top_model_id(router, task_type=TaskType.CHAT, plan="max", capability="code", text=text)
    assert top == "xai-grok"
    assert decision.selection_reason == "dynamic_eligible"


# ── Scenario D: expert multi-step agent task -> cheapest expert-capable ─────

def test_scenario_D_expert_agent_task_picks_the_only_premium_candidate():
    long_text = (
        "Plan and execute this multi-step refactor, explain why at each step, "
        + ("x" * 3200) + " ```python\ndef f(): pass\n```"
    )
    router = ModelRouter()
    top, decision = _top_model_id(
        router, task_type=TaskType.AGENT, plan="max", capability="agent", text=long_text, tools=[{"name": "shell"}],
    )
    assert top == "claude-opus"  # the only "premium" candidate in the MAX pool
    assert decision.selection_reason == "dynamic_eligible"


# ── Scenario E: expert task + no premium candidate left -> controlled failure ─

def test_scenario_E_expert_task_with_premium_disabled_is_a_controlled_failure():
    """Hardening: a below-bar model must NEVER be silently run just
    because it's the best one left. claude-opus (the only premium
    candidate in the MAX/AGENT pool) is operationally disabled — mirrors
    a real provider kill switch / Continuity's own disabled-key set —
    leaving only claude-sonnet (standard), which does not meet this
    request's "premium" bar. The correct outcome is a controlled failure
    (empty chain), never a silent downgrade to claude-sonnet."""
    long_text = (
        "Plan and execute this multi-step refactor, explain why at each step, "
        + ("x" * 3200) + " ```python\ndef f(): pass\n```"
    )
    router = ModelRouter()
    decision = _decision_for(
        router, task_type=TaskType.AGENT, plan="max", capability="agent", text=long_text,
        tools=[{"name": "shell"}], disabled={"claude-opus"},
    )
    assert decision.chain == []
    assert decision.eligible_model_count == 0
    assert decision.complexity.required_quality_class == "premium"
    assert decision.selection_reason == "no_capable_model_controlled_failure"


async def test_scenario_E2_premium_available_but_call_fails_never_falls_over_to_standard(monkeypatch):
    """Complement to Scenario E: this time claude-opus is NOT disabled —
    it's eligible, attempted, and its litellm call fails. Since
    claude-sonnet (standard) never met the "premium" bar, it was never
    placed in the chain at all — the failure must surface as
    ProviderUnavailableError, never a silent failover to claude-sonnet."""
    from unittest.mock import AsyncMock

    from trinity_router import ProviderUnavailableError, RoutingRequest

    long_text = (
        "Plan and execute this multi-step refactor, explain why at each step, "
        + ("x" * 3200) + " ```python\ndef f(): pass\n```"
    )
    calls: list[str] = []

    async def _acompletion(*, model, **kwargs):
        calls.append(model)
        raise Exception("provider down")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", AsyncMock(side_effect=_acompletion))
    router = ModelRouter()

    with pytest.raises(ProviderUnavailableError):
        await router.complete(RoutingRequest(
            messages=_messages(long_text), task_type=TaskType.AGENT, user_plan="max",
            session_id="s1", capability="agent", tools=[{"name": "shell"}],
        ))
    assert calls == ["claude-opus-4-7"]  # only the premium candidate was ever tried


# ── Scenario F: simple task + elite model available -> elite NOT selected ──

def test_scenario_F_simple_task_never_selects_the_elite_model():
    router = ModelRouter()
    # MAX plan's OLD curated pool listed claude-opus (premium) as its
    # FIRST static candidate — a trivial task must still pick the
    # cheapest eligible model, not the plan's own premium primary. V2:
    # required="ultra_low", floored by MAX's min_quality_class="low" ->
    # effective bar="low" -> global discovery makes gpt-4o-mini
    # ($0.15/$0.60, "low") eligible and cheapest — NOT gemini-25-flash
    # (V1's answer; still eligible here too, just pricier).
    top, decision = _top_model_id(router, task_type=TaskType.CHAT, plan="max", text="hi")
    assert top != "claude-opus"
    assert top == "gpt-4o-mini"


# ── Scenario G: expert task on the cheapest plan -> entitlement-gated, controlled failure ─

def test_scenario_G_expert_task_on_cheap_plan_is_rejected_by_the_quality_gate():
    """V1 note (superseded): START plan's OLD curated CHAT pool topped
    out at "low" quality_class, so an expert/"premium" requirement could
    never be met and the chain came back empty at the ELIGIBILITY stage.

    V2 note (superseded 2026-09-07 — root-caused the same day): candidate
    discovery went GLOBAL, so claude-opus (the only "premium" model in
    the catalog) became a genuinely ELIGIBLE candidate for ANY plan,
    including START. The request was still correctly refused, but only
    by ACCIDENT of a different, already-existing mechanism: START's cost
    ceiling ($0.10/request) could not afford claude-opus's real price, so
    the per-attempt budget preflight silently skipped it — zero audit
    trail, indistinguishable from a real provider outage
    (ProviderUnavailableError, "AI service temporarily unavailable").

    Entitlement/Quality Gate (this etapa, the actual fix): a plan is
    never entitled to consider a tier above its own
    PlanLimits.max_quality_class ceiling (START="standard", derived from
    START's own existing token/cost limits — see limits.py). This is now
    checked BEFORE get_candidate_models()/get_eligible_models() run at
    all, so the request is rejected immediately via QualityNotEntitledError
    — a distinct, semantically correct exception carrying the real
    required tier ("premium") and the plan's ceiling ("standard"), never
    a chain containing claude-opus that later fails its own preflight."""
    long_text = (
        "Plan and execute this multi-step refactor, explain why at each step, "
        + ("x" * 3200) + " ```python\ndef f(): pass\n```"
    )
    router = ModelRouter()
    with pytest.raises(QualityNotEntitledError) as exc_info:
        _decision_for(
            router, task_type=TaskType.CHAT, plan="start", capability="agent", text=long_text, tools=[{"name": "shell"}],
        )
    assert exc_info.value.required_quality_class == "premium"
    assert exc_info.value.plan_max_quality_class == "standard"
    assert exc_info.value.plan == "start"


async def test_scenario_G2_expert_task_on_cheap_plan_end_to_end_is_entitlement_rejected(monkeypatch):
    """Complement to Scenario G: proves the REAL end-to-end complete()
    call for that same request raises QualityNotEntitledError — litellm
    is never called at all, not even once for claude-opus (unlike the
    superseded budget-preflight path, which DID attempt/skip claude-opus
    before failing)."""
    from unittest.mock import AsyncMock

    from trinity_router import QualityNotEntitledError, RoutingRequest

    long_text = (
        "Plan and execute this multi-step refactor, explain why at each step, "
        + ("x" * 3200) + " ```python\ndef f(): pass\n```"
    )
    acompletion_spy = AsyncMock()
    monkeypatch.setattr("trinity_router.router.litellm.acompletion", acompletion_spy)
    router = ModelRouter()

    with pytest.raises(QualityNotEntitledError) as exc_info:
        await router.complete(RoutingRequest(
            messages=_messages(long_text), task_type=TaskType.CHAT, user_plan="start",
            session_id="s1", capability="agent", tools=[{"name": "shell"}],
        ))
    acompletion_spy.assert_not_called()  # rejected before candidate discovery — never attempted
    assert exc_info.value.required_quality_class == "premium"
    assert exc_info.value.plan_max_quality_class == "standard"


# ── Scenario H: plan budget too low -> obey plan limit, controlled failure ──

async def test_scenario_H_plan_budget_ceiling_still_enforced_after_reordering():
    """The complexity-aware reordering must never bypass the PRE-EXISTING
    per-plan cost ceiling (limits.py, untouched) — this is enforced in
    _preflight(), downstream of chain-building, and must still reject a
    request whose estimated cost exceeds the plan's own budget regardless
    of which model complexity-aware ordering put first."""
    from trinity_router import ProviderUnavailableError, RoutingRequest

    router = ModelRouter()
    # FREE plan: max_estimated_cost_per_request_usd=0.02, max_output_tokens=1024.
    # gemini-25-flash-lite's own preflight estimate uses the PLAN's own
    # max_output_tokens_per_request (1024) — cheap enough to normally pass;
    # request a huge max_output_tokens to push the estimate over budget
    # instead (enforce_request_limits caps effective tokens to the plan's
    # own ceiling for the CALL, but the preflight estimate itself is
    # computed from plan_limits.max_output_tokens_per_request, not the
    # request's own value — so this test forces the failure via the
    # single-candidate FREE chain's own pricing being non-zero at all).
    import trinity_router.limits as limits_module
    from trinity_router.limits import PlanLimits
    original = limits_module.PLAN_LIMITS["free"]
    forced = PlanLimits(
        plan="free", max_output_tokens_per_request=original.max_output_tokens_per_request,
        max_estimated_cost_per_request_usd=0.0,  # impossible to satisfy — any nonzero cost fails
        max_failover_attempts=0,
    )
    import unittest.mock as mock
    with mock.patch.dict(limits_module.PLAN_LIMITS, {"free": forced}):
        with pytest.raises(ProviderUnavailableError):
            await router.complete(RoutingRequest(
                messages=_messages("hi"), task_type=TaskType.CHAT, user_plan="free", session_id="s1",
            ))
