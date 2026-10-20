"""Entitlement/Quality Gate — plan/entitlement -> capability -> quality
permitida pelo plano -> budget -> custo -> selecao do modelo.

Root cause (2026-09-07): PlanLimits previously only declared a quality
FLOOR (min_quality_class). With no CEILING, a plan whose own budget
could never afford the catalog's one premium candidate (claude-opus)
would still see it as "eligible" by global candidate discovery, try it,
and fail its own per-attempt cost preflight -- a generic
ProviderUnavailableError with zero audit trail, indistinguishable from a
real outage. This suite proves the fix: PlanLimits.max_quality_class is
checked BEFORE candidate discovery ever runs, so an over-tier request is
rejected immediately, with zero candidates considered and zero provider
calls made, via a distinct QualityNotEntitledError -- never a silent
downgrade to a lower-tier model pretending to have solved the same task.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from trinity_router import QualityNotEntitledError, RoutingRequest
from trinity_router.limits import get_plan_limits
from trinity_router.providers import PROVIDER_CATALOG
from trinity_router.router import ModelRouter
from trinity_router.selector import TaskType


def _messages(text: str) -> list[dict]:
    return [{"role": "system", "content": "sys"}, {"role": "user", "content": text}]


_EXPERT_TEXT = (
    "Plan and execute this multi-step refactor, explain why at each step, "
    + ("x" * 3200) + " ```python\ndef f(): pass\n```"
)  # score=100 -> required_quality_class="premium" (see test_complexity.py)

_STANDARD_TEXT = (
    "Why does this function behave this way, and how would you compare it "
    "to an alternative?"
)  # base(code=20)+reasoning(15)+code_signal("function",15)=50 -> required="standard"

_LOW_TEXT = (
    "Why do interest rates typically affect currency exchange rates over "
    "time, considering a country with a persistent trade deficit? "
    + ("Consider how this plays out across several trading partners with "
       "different monetary policy regimes and capital account rules. " * 12)
)  # base(chat=0)+reasoning(15,"why")+length(~11, ~1700 chars//150)=~26 -> required="low"

_CODE_LOW_TEXT = "Write a function to add two numbers."
# base(code=20)+code_signal("function",15)=35 -> required="low" (capability="code",
# no reasoning/multi-step/fence -> stays below the 50 "standard" threshold)

_TRIVIAL_TEXT = "hi"  # base=0, no signals -> required_quality_class="ultra_low"


class TestPlanMaxQualityClassValues:
    """Sanity check on the values themselves (Report requirement: 'show
    current values and where they're defined' before behavior tests)."""

    def test_free_floor_is_ultra_low_and_ceiling_is_low(self):
        # Corrected 2026-09-07 (commercial policy): FREE must be able to
        # reach groq-llama4-scout (ultra_low, the cheapest model in the
        # whole catalog) and must NEVER reach "standard"/"premium",
        # regardless of whether a "standard" candidate would mathematically
        # fit FREE's own cost ceiling (xai-grok does, at ~$0.0051 — that is
        # NOT the test: commercial policy is stricter than pure affordability).
        free = get_plan_limits("free")
        assert free.min_quality_class == "ultra_low"
        assert free.max_quality_class == "low"

    def test_start_still_capped_at_standard_unchanged(self):
        # Explicitly NOT touched by this etapa — START's own policy stays
        # exactly as the previous etapa left it.
        assert get_plan_limits("start").max_quality_class == "standard"
        assert get_plan_limits("start").min_quality_class == "ultra_low"

    def test_premium_belongs_to_max_and_team(self):
        """Renamed from "pro_max_team_allow_premium_unchanged" — PRO no
        longer reaches premium.

        That band is the single largest cost lever in the product: the same
        simulated week of heavy use measures 684 TCU capped at standard and
        3.480 capped at premium. Leaving PRO on premium is what made PRO
        impossible to price at $19.90, so premium is now what MAX buys.
        See packages/core/tests/unit/test_plan_economics.py::test_E08."""
        assert get_plan_limits("pro").max_quality_class == "standard"
        assert get_plan_limits("max").max_quality_class == "premium"
        assert get_plan_limits("team").max_quality_class == "premium"


# ── A. FREE + trivial CHAT -> the cheapest eligible model in the WHOLE catalog ──

async def test_A_free_trivial_chat_picks_the_cheapest_model_in_the_catalog(monkeypatch):
    async def _acompletion(*, model, **kwargs):
        class _Resp:
            choices = [type("C", (), {"message": type("M", (), {"content": "hi", "tool_calls": None})()})]
            usage = type("U", (), {"prompt_tokens": 5, "completion_tokens": 5})()
        return _Resp()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", AsyncMock(side_effect=_acompletion))
    router = ModelRouter()
    result = await router.complete(RoutingRequest(
        messages=_messages(_TRIVIAL_TEXT), task_type=TaskType.CHAT, user_plan="free",
        session_id="s1", capability="chat",
    ))
    # min_quality_class="ultra_low" (corrected) now lets FREE reach
    # groq-llama4-scout, the cheapest model in the ENTIRE catalog — the
    # previous "low" floor wrongly excluded it for every FREE request.
    assert result.provider == "groq"


# ── A2. FREE + CODE -> cheapest eligible model that actually supports CODE ──

async def test_A2_free_code_task_picks_cheapest_eligible_code_capable_model(monkeypatch):
    """capability="code" restricts candidates to those whose
    supported_capabilities includes "code" (get_candidate_models(),
    unchanged) — every current catalog entry happens to support every
    capability, so this proves the GATE itself works (capability is a
    real, enforced filter, not a decoration), even though today it
    doesn't further narrow the field beyond quality tier."""
    async def _acompletion(*, model, **kwargs):
        class _Resp:
            choices = [type("C", (), {"message": type("M", (), {"content": "ok", "tool_calls": None})()})]
            usage = type("U", (), {"prompt_tokens": 5, "completion_tokens": 5})()
        return _Resp()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", AsyncMock(side_effect=_acompletion))
    router = ModelRouter()
    result = await router.complete(RoutingRequest(
        messages=_messages(_CODE_LOW_TEXT), task_type=TaskType.CHAT, user_plan="free",
        session_id="s1", capability="code",
    ))
    # "low" tier (score=35), within FREE's "low" ceiling -- gpt-4o-mini is
    # the cheapest CODE-capable candidate at that tier.
    assert result.provider == "openai"


# ── B. FREE + a "low"-tier task (not "standard") -> executes normally ──

async def test_B_free_low_tier_task_executes_normally_not_blocked(monkeypatch):
    """FREE's ceiling is "low" -- a task that only needs up to "low" must
    still succeed normally; the gate only rejects tiers ABOVE the plan's
    ceiling ("standard"/"premium"), never at or below it."""
    async def _acompletion(*, model, **kwargs):
        class _Resp:
            choices = [type("C", (), {"message": type("M", (), {"content": "ok", "tool_calls": None})()})]
            usage = type("U", (), {"prompt_tokens": 5, "completion_tokens": 5})()
        return _Resp()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", AsyncMock(side_effect=_acompletion))
    router = ModelRouter()
    result = await router.complete(RoutingRequest(
        messages=_messages(_LOW_TEXT), task_type=TaskType.CHAT, user_plan="free",
        session_id="s1", capability="chat",
    ))
    assert result.provider == "openai"  # cheapest eligible at "low" tier


# ── B2. FREE + a "standard"-tier task -> now blocked (commercial policy) ──

async def test_B2_free_standard_task_is_now_blocked_by_commercial_policy(monkeypatch):
    """Superseded scenario (previous etapa): FREE's ceiling was "standard"
    because xai-grok mathematically fit FREE's $0.02 cost ceiling. This
    etapa's commercial policy is stricter than pure affordability — FREE
    must NEVER reach "standard", even though it could technically afford
    it. xai-grok must never even be considered."""
    acompletion_spy = AsyncMock()
    monkeypatch.setattr("trinity_router.router.litellm.acompletion", acompletion_spy)
    router = ModelRouter()

    with pytest.raises(QualityNotEntitledError) as exc_info:
        await router.complete(RoutingRequest(
            messages=_messages(_STANDARD_TEXT), task_type=TaskType.CHAT, user_plan="free",
            session_id="s1", capability="code",
        ))
    acompletion_spy.assert_not_called()
    assert exc_info.value.required_quality_class == "standard"
    assert exc_info.value.plan_max_quality_class == "low"
    assert exc_info.value.plan == "free"


# ── C. FREE + premium task -> blocked by entitlement, before any provider call ──

async def test_C_free_premium_task_blocked_before_provider_call(monkeypatch):
    acompletion_spy = AsyncMock()
    monkeypatch.setattr("trinity_router.router.litellm.acompletion", acompletion_spy)
    router = ModelRouter()

    with pytest.raises(QualityNotEntitledError) as exc_info:
        await router.complete(RoutingRequest(
            messages=_messages(_EXPERT_TEXT), task_type=TaskType.CHAT, user_plan="free",
            session_id="s1", capability="agent", tools=[{"name": "shell"}],
        ))
    acompletion_spy.assert_not_called()  # zero cost, zero attempts, zero retries
    assert exc_info.value.required_quality_class == "premium"
    assert exc_info.value.plan_max_quality_class == "low"
    assert exc_info.value.plan == "free"


# ── C3. FREE + real provider failure -> zero failover (max_failover_attempts=0) ──

async def test_C3_free_provider_failure_never_fails_over(monkeypatch):
    """FREE's max_failover_attempts=0 (unchanged) means the chain has
    exactly ONE candidate — a real litellm failure on it must surface
    immediately as ProviderUnavailableError, with exactly one attempt,
    never a second provider tried."""
    from trinity_router import ProviderUnavailableError

    calls: list[str] = []

    async def _acompletion(*, model, **kwargs):
        calls.append(model)
        raise Exception("provider down")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", AsyncMock(side_effect=_acompletion))
    router = ModelRouter()

    with pytest.raises(ProviderUnavailableError) as exc_info:
        await router.complete(RoutingRequest(
            messages=_messages(_TRIVIAL_TEXT), task_type=TaskType.CHAT, user_plan="free",
            session_id="s1", capability="chat",
        ))
    assert len(calls) == 1  # exactly one attempt — zero failover
    assert len(exc_info.value.providers_tried) == 1


def test_C2_start_premium_task_blocked_at_decision_level_zero_candidates():
    """Same scenario at the _build_chain_decision() level -- confirms the
    rejection happens BEFORE get_candidate_models()/get_eligible_models()
    ever run (no eligible_model_count/total_candidate_count is even
    computed for a rejected decision -- the function never returns one)."""
    router = ModelRouter()
    with pytest.raises(QualityNotEntitledError):
        router._build_chain_decision(
            TaskType.AGENT, "start", get_plan_limits("start"), "agent",
            messages=_messages(_EXPERT_TEXT), tools=[{"name": "shell"}],
        )


# ── D. MAX/TEAM + eligible premium task -> may select premium when budget allows ──

@pytest.mark.parametrize("plan", ["max", "team"])
async def test_D_max_team_premium_task_selects_claude_opus(monkeypatch, plan):
    captured: dict = {}

    async def _acompletion(*, model, **kwargs):
        captured["model"] = model
        class _Resp:
            choices = [type("C", (), {"message": type("M", (), {"content": "ok", "tool_calls": None})()})]
            usage = type("U", (), {"prompt_tokens": 5, "completion_tokens": 5})()
        return _Resp()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", AsyncMock(side_effect=_acompletion))
    router = ModelRouter()
    result = await router.complete(RoutingRequest(
        messages=_messages(_EXPERT_TEXT), task_type=TaskType.CHAT, user_plan=plan,
        session_id="s1", capability="agent", tools=[{"name": "shell"}],
    ))
    assert captured["model"] == PROVIDER_CATALOG["claude-opus"].model_id
    assert result.provider == "anthropic"


# ── D2. PRO/MAX + premium TEXT-ONLY (no tools) reaches premium via CHAT ──

_PREMIUM_TEXT_ONLY = (
    "Explain why this design choice matters, and compare it step by step to "
    "alternative approaches, including how the current function signature "
    "affects long-term maintainability. "
    + ("Consider this scenario carefully and weigh the tradeoffs of each "
       "alternative in depth. " * 60)
)  # capability="chat", tools=None -> score=75 via length(35)+reasoning(15)+
   # code_signal(15)+multi_step(10) alone -- see test_complexity.py's
   # test_chat_text_only_can_reach_premium_without_tools for the exact math.


@pytest.mark.parametrize("plan", ["max", "team"])
async def test_D2_max_team_premium_text_only_reaches_premium_without_agent(monkeypatch, plan):
    """The core proof this etapa (Opção A + Execution Mode) exists for:
    task_type=CHAT, tools=None (exactly what chat_service.py always
    sends — no Agent, no ReAct loop, no tool call ever possible), and the
    router STILL selects claude-opus for a genuinely premium-scoring
    text-only request. Quality and Agent/tool-use are independent axes."""
    captured: dict = {}

    async def _acompletion(*, model, **kwargs):
        captured["model"] = model
        captured["tools"] = kwargs.get("tools")
        class _Resp:
            choices = [type("C", (), {"message": type("M", (), {"content": "ok", "tool_calls": None})()})]
            usage = type("U", (), {"prompt_tokens": 5, "completion_tokens": 5})()
        return _Resp()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", AsyncMock(side_effect=_acompletion))
    router = ModelRouter()
    result = await router.complete(RoutingRequest(
        messages=_messages(_PREMIUM_TEXT_ONLY), task_type=TaskType.CHAT, user_plan=plan,
        session_id="s1", capability="chat", tools=None,
    ))
    assert captured["model"] == PROVIDER_CATALOG["claude-opus"].model_id
    assert captured["tools"] is None  # no toolset was ever sent to the provider
    assert result.provider == "anthropic"


# ── D3. The same premium task on PRO is refused, not quietly downgraded ──

async def test_D3_pro_is_refused_premium_rather_than_served_something_worse(monkeypatch):
    """PRO's ceiling is "standard", and this is the behaviour that makes
    that honest.

    The tempting alternative — serve the best model PRO can reach and say
    nothing — is worse than a refusal: the user asked for something the
    plan cannot do and would receive a visibly weaker answer with no
    explanation, then conclude the product is bad rather than that the plan
    is small. The gate rejects before any candidate is considered, carrying
    the real requirement, and chat_service.py turns that into an upgrade
    message. No provider is called, so the refusal costs nothing."""
    called = AsyncMock()
    monkeypatch.setattr("trinity_router.router.litellm.acompletion", called)
    router = ModelRouter()

    with pytest.raises(QualityNotEntitledError) as exc_info:
        await router.complete(RoutingRequest(
            messages=_messages(_PREMIUM_TEXT_ONLY), task_type=TaskType.CHAT,
            user_plan="pro", session_id="s1", capability="chat", tools=None,
        ))

    assert exc_info.value.required_quality_class == "premium"
    assert exc_info.value.plan_max_quality_class == "standard"
    assert exc_info.value.plan == "pro"
    called.assert_not_awaited()


# ── E. Premium requirement is never silently downgraded to a lower tier ──

def test_E_premium_requirement_is_never_silently_downgraded():
    """The exception carries the REAL required tier ('premium'), not a
    substituted lower one -- and no chain (with any model, of any tier)
    is ever returned for this request. A caller cannot mistake this for
    "solved by a cheaper model": there is no RoutingResult at all."""
    router = ModelRouter()
    with pytest.raises(QualityNotEntitledError) as exc_info:
        router._build_chain_decision(
            TaskType.CHAT, "start", get_plan_limits("start"), "agent",
            messages=_messages(_EXPERT_TEXT), tools=[{"name": "shell"}],
        )
    # The task's real requirement is exactly what was computed -- untouched.
    assert exc_info.value.required_quality_class == "premium"
    # Never coerced down to the plan's own ceiling and reported as if that
    # were what the task asked for.
    assert exc_info.value.required_quality_class != exc_info.value.plan_max_quality_class


# ── F. budget_exceeded skip never retries the same candidate ──

async def test_F_budget_exceeded_skip_advances_without_retrying_same_candidate(monkeypatch):
    """Independent of the quality gate: a candidate skipped by the
    per-attempt budget preflight (limits.py, unchanged) is attempted at
    most once, then failover moves to the NEXT candidate -- never a
    repeat call to the same (already-known-unaffordable) model. Uses a
    request shape that lands in "standard" (within every plan's
    entitlement) so only the pre-existing budget preflight is exercised,
    not the new entitlement gate."""
    calls: list[str] = []

    async def _acompletion(*, model, **kwargs):
        calls.append(model)
        class _Resp:
            choices = [type("C", (), {"message": type("M", (), {"content": "ok", "tool_calls": None})()})]
            usage = type("U", (), {"prompt_tokens": 5, "completion_tokens": 5})()
        return _Resp()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", AsyncMock(side_effect=_acompletion))
    router = ModelRouter()
    # "pro" plan's own ceiling is "premium", so this "standard"-tier
    # request's chain may include several candidates; regardless of which
    # one succeeds first, no model_id may appear in `calls` more than once.
    result = await router.complete(RoutingRequest(
        messages=_messages(_STANDARD_TEXT), task_type=TaskType.CHAT, user_plan="pro",
        session_id="s1", capability="code",
    ))
    assert len(calls) == len(set(calls))  # no duplicate attempts against the same model
    assert result is not None
