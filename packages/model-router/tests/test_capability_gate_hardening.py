"""Adaptive Intelligence Router — post-implementation hardening
(capability gate). Proves the central principle literally: a model
rejected for insufficient capability must NEVER enter the normal
failover chain, so failover can only ever move between candidates
already confirmed capable — never from capable to incapable, and never
degrade a below-bar model into "good enough" just because it's cheap or
because it's the only one left. Uses the REAL PROVIDER_CATALOG/selector
chains (test_dynamic_routing_decisions.py's own style), not synthetic
models, so these tests exercise the real quality tiers currently in the
catalog (see providers.py's module docstring for exact tiers/prices).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from trinity_router import ProviderUnavailableError, RoutingRequest
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


# ── 1. premium required + premium available + premium fails -> NEVER standard ──

async def test_premium_required_and_available_but_fails_never_falls_to_standard(monkeypatch):
    """MAX/AGENT pool = [claude-opus(premium), claude-sonnet(standard), ...].
    An expert request only makes claude-opus eligible — claude-sonnet is
    never even in the chain, so a real litellm failure on claude-opus
    must surface as ProviderUnavailableError, never a silent try of
    claude-sonnet."""
    calls: list[str] = []

    async def _acompletion(*, model, **kwargs):
        calls.append(model)
        raise Exception("provider down")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", AsyncMock(side_effect=_acompletion))
    router = ModelRouter()

    with pytest.raises(ProviderUnavailableError):
        await router.complete(RoutingRequest(
            messages=_messages(_EXPERT_TEXT), task_type=TaskType.AGENT, user_plan="max",
            session_id="s1", capability="agent", tools=[{"name": "shell"}],
        ))
    assert calls == [PROVIDER_CATALOG["claude-opus"].model_id]
    assert PROVIDER_CATALOG["claude-sonnet"].model_id not in calls


# ── 2. premium required + no premium candidate available -> controlled failure ──

def test_premium_required_and_none_available_is_a_controlled_failure():
    """claude-opus disabled -> only claude-sonnet(standard) remains, which
    does not meet "premium". The chain must be built EMPTY (controlled
    failure), not populated with the standard-tier survivor."""
    router = ModelRouter()
    decision = router._build_chain_decision(
        TaskType.AGENT, "max", get_plan_limits("max"), "agent",
        disabled_provider_keys=frozenset({"claude-opus"}),
        messages=_messages(_EXPERT_TEXT), tools=[{"name": "shell"}],
    )
    assert decision.chain == []
    assert decision.selection_reason == "no_capable_model_controlled_failure"


async def test_premium_required_and_none_available_raises_via_complete(monkeypatch):
    """Same setup as above, through the real complete() path — proves the
    empty chain actually results in a controlled ProviderUnavailableError
    and litellm is never called at all."""
    acompletion_spy = AsyncMock()
    monkeypatch.setattr("trinity_router.router.litellm.acompletion", acompletion_spy)
    router = ModelRouter()

    with pytest.raises(ProviderUnavailableError):
        await router.complete(RoutingRequest(
            messages=_messages(_EXPERT_TEXT), task_type=TaskType.AGENT, user_plan="max",
            session_id="s1", capability="agent", tools=[{"name": "shell"}],
            disabled_provider_keys=frozenset({"claude-opus"}),
        ))
    acompletion_spy.assert_not_called()


# ── 3. standard required + standard fails + low available -> NEVER low ─────────

async def test_standard_required_and_available_but_fails_never_falls_to_low(monkeypatch):
    """V2: global discovery makes claude-haiku ($1.00/$5.00, "standard")
    and xai-grok ($1.25/$2.50, "standard") eligible too, alongside
    claude-sonnet (also "standard") — PRO's own attempt budget
    (max_failover_attempts=2 -> 3 total) fits the three cheapest-first:
    [xai-grok, claude-haiku, claude-sonnet]. gpt-4o and claude-opus
    (also eligible) are truncated out by that same attempt-budget cap —
    a separate, unrelated policy, not an ineligibility. A
    "standard"-requiring request must never fall over to
    gemini-25-flash-lite ("low") even after every "standard"-tier candidate
    in the chain fails."""
    calls: list[str] = []

    async def _acompletion(*, model, **kwargs):
        calls.append(model)
        raise Exception("provider down")

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", AsyncMock(side_effect=_acompletion))
    router = ModelRouter()

    with pytest.raises(ProviderUnavailableError):
        await router.complete(RoutingRequest(
            messages=_messages(_STANDARD_TEXT), task_type=TaskType.CHAT, user_plan="pro",
            session_id="s1", capability="code",
        ))
    assert set(calls) == {
        PROVIDER_CATALOG["xai-grok"].model_id,
        PROVIDER_CATALOG["claude-haiku"].model_id,
        PROVIDER_CATALOG["claude-sonnet"].model_id,
    }
    # gemini-25-flash was the "low"-tier model this assertion guarded against;
    # it was removed from the catalog on 2026-09-13, so the remaining "low"
    # entry carries the same guarantee.
    assert PROVIDER_CATALOG["gemini-25-flash-lite"].model_id not in calls
    assert PROVIDER_CATALOG["claude-opus"].model_id not in calls
    assert PROVIDER_CATALOG["gpt-4o"].model_id not in calls


# ── 4. two capable models -> cheapest capable first ─────────────────────────────

def test_two_capable_models_cheapest_tried_first():
    router = ModelRouter()
    decision = router._build_chain_decision(
        TaskType.CHAT, "pro", get_plan_limits("pro"), "code",
        messages=_messages(_STANDARD_TEXT),
    )
    # Cheapest-first among the globally-eligible "standard"+ candidates,
    # truncated to PRO's own 3-attempt budget (gpt-4o and claude-opus,
    # also eligible, are truncated out — not excluded for incapability).
    assert [k for k, _ in decision.chain] == ["xai-grok", "claude-haiku", "claude-sonnet"]


# ── 5. cheapest capable fails -> next capable is tried ──────────────────────────

async def test_cheapest_capable_fails_next_capable_succeeds(monkeypatch):
    calls: list[str] = []

    async def _acompletion(*, model, **kwargs):
        calls.append(model)
        if model == PROVIDER_CATALOG["xai-grok"].model_id:
            raise Exception("provider down")
        msg_obj = type("M", (), {"content": "ok", "tool_calls": None})()
        usage_obj = type("U", (), {"prompt_tokens": 10, "completion_tokens": 5, "prompt_tokens_details": None})()
        choice_obj = type("C", (), {"message": msg_obj})()
        return type("R", (), {"choices": [choice_obj], "usage": usage_obj})()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", AsyncMock(side_effect=_acompletion))
    router = ModelRouter()

    result = await router.complete(RoutingRequest(
        messages=_messages(_STANDARD_TEXT), task_type=TaskType.CHAT, user_plan="pro",
        session_id="s1", capability="code",
    ))
    assert calls == [
        PROVIDER_CATALOG["xai-grok"].model_id,
        PROVIDER_CATALOG["claude-haiku"].model_id,
    ]
    assert result.failover_used is True
    assert result.model == PROVIDER_CATALOG["claude-haiku"].model_id


# ── 6. an incapable candidate never enters the chain at all ────────────────────

def test_incapable_candidate_excluded_from_chain_entirely():
    router = ModelRouter()
    decision = router._build_chain_decision(
        TaskType.CHAT, "pro", get_plan_limits("pro"), "code",
        messages=_messages(_STANDARD_TEXT),
    )
    chain_keys = [k for k, _ in decision.chain]
    assert "gemini-25-flash-lite" not in chain_keys  # "low" tier, required is "standard" — incapable
    # 5 globally-eligible candidates (xai-grok/haiku/sonnet/gpt-4o/opus,
    # all "standard"+), but the CHAIN is truncated to PRO's own 3-attempt
    # budget — a separate policy from eligibility, so eligible_model_count
    # and chain length legitimately differ here.
    assert decision.eligible_model_count == 5
    assert len(chain_keys) == 3


# ── 7. real request cost estimate affects ranking when supplied ────────────────

def test_real_request_output_token_estimate_is_threaded_into_ranking(monkeypatch):
    """rank_by_cost's max_output_tokens_estimate must reflect the REAL
    request/plan-derived value, not always the historical hardcoded 4096
    default — proven by spying on rank_by_cost's call kwargs directly
    (no synthetic models needed: the wiring is what's under test, not the
    catalog's own price ordering)."""
    import trinity_router.router as _router_module

    captured: dict = {}
    original_rank_by_cost = _router_module.rank_by_cost

    def _spy_rank_by_cost(candidates, **kwargs):
        captured.update(kwargs)
        return original_rank_by_cost(candidates, **kwargs)

    monkeypatch.setattr(_router_module, "rank_by_cost", _spy_rank_by_cost)
    router = ModelRouter()

    # (a) No explicit request.max_output_tokens on the PRO plan -> falls
    # back to the PLAN's own ceiling (8192), not the old hardcoded 4096.
    router._build_chain_decision(
        TaskType.CHAT, "pro", get_plan_limits("pro"), "code",
        messages=_messages(_STANDARD_TEXT),
    )
    assert captured["max_output_tokens_estimate"] == 8_192

    # (b) An explicit, real per-request value (below the plan ceiling) is
    # threaded straight through.
    captured.clear()
    router._build_chain_decision(
        TaskType.CHAT, "pro", get_plan_limits("pro"), "code",
        messages=_messages(_STANDARD_TEXT), requested_max_output_tokens=1_500,
    )
    assert captured["max_output_tokens_estimate"] == 1_500

    # (c) A real per-request value ABOVE the plan ceiling is still capped
    # by the plan (same cap enforce_request_limits() itself applies) —
    # never a second, divergent financial rule.
    captured.clear()
    router._build_chain_decision(
        TaskType.CHAT, "pro", get_plan_limits("pro"), "code",
        messages=_messages(_STANDARD_TEXT), requested_max_output_tokens=999_999,
    )
    assert captured["max_output_tokens_estimate"] == 8_192


# ── 8. legacy/default behavior intact where adaptive routing cannot run ────────

def test_no_messages_skips_adaptive_routing_entirely_legacy_chain_preserved():
    """The one legitimate case adaptive routing cannot run: no messages
    supplied at all. Chain must be returned completely unfiltered/
    unreordered — the exact static pool, no complexity computed."""
    router = ModelRouter()
    decision = router._build_chain_decision(
        TaskType.CHAT, "pro", get_plan_limits("pro"), "code",
        messages=None,
    )
    # Held gemini-25-flash until the alias was removed on 2026-09-13; the
    # slot carries gemini-25-flash-lite now, so PRO keeps the Google
    # third-provider fallback this legacy pool has always guaranteed.
    assert [k for k, _ in decision.chain] == [
        "claude-sonnet", "gpt-4o", "gemini-25-flash-lite"
    ]
    assert decision.complexity is None
    assert decision.selection_reason == ""


def test_forced_provider_key_still_bypasses_adaptive_routing_entirely():
    """Continuity Mode's forced_provider_key path must remain completely
    untouched by this hardening pass — single pinned candidate, no
    eligibility/complexity check at all."""
    router = ModelRouter()
    decision = router._build_chain_decision(
        TaskType.CHAT, "pro", get_plan_limits("pro"), None,
        forced_provider_key="gemini-25-flash-lite",
        messages=_messages(_EXPERT_TEXT),  # even an expert-level prompt is irrelevant here
    )
    assert decision.chain == [("gemini-25-flash-lite", PROVIDER_CATALOG["gemini-25-flash-lite"])]
    assert decision.complexity is None
