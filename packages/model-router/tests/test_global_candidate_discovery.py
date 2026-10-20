"""Adaptive Intelligence Router V2 — global candidate discovery.

Proves the specific difference between V1 (candidate universe bounded by
selector.select_provider_chain()'s curated lists) and V2 (candidate
universe is the full PROVIDER_CATALOG, filtered only by objective
capability/task_type/disabled-key facts — see providers.get_candidate_models()).

The mission's own CRITICAL test lives here: a model present in
PROVIDER_CATALOG, absent from EVERY entry of selector.py's curated chains,
but technically eligible, must be selectable by the real router.
"""
from __future__ import annotations

from trinity_router.limits import get_plan_limits
from trinity_router.providers import PROVIDER_CATALOG, get_eligible_models
from trinity_router.router import ModelRouter
from trinity_router.selector import _CAPABILITY_CHAINS, _PROVIDER_CHAINS, TaskType


def _messages(text: str) -> list[dict]:
    return [{"role": "system", "content": "sys"}, {"role": "user", "content": text}]


# ── CRITICAL: a catalog model absent from every curated chain is reachable ──

def test_claude_haiku_is_absent_from_every_curated_chain():
    """Sanity precondition for the critical test below — if this ever
    stops being true (claude-haiku gets added to a curated chain), the
    critical test's claim would be trivially true for the wrong reason.
    Confirms claude-haiku appears in NEITHER selector.py table today."""
    for keys in _PROVIDER_CHAINS.values():
        assert "claude-haiku" not in keys
    for keys in _CAPABILITY_CHAINS.values():
        assert "claude-haiku" not in keys


def test_model_absent_from_curated_chains_but_technically_eligible_is_selected():
    """THE critical test (mission requirement 13): xai-grok is a real
    PROVIDER_CATALOG entry (provider="xai", "standard" quality,
    validated/working — see providers.py's own comment: in catalog only,
    not yet in any selector.py chain) that NEVER appears in ANY curated
    chain — under V1, it could never have been selected, no matter how
    cheap or capable it was. Under V2's global discovery, a request whose
    required quality is "standard" makes it eligible, and it is CHEAPER
    than every other "standard"-tier catalog model (including
    claude-haiku, itself also absent from every curated chain) — so it
    must win the cost ranking and become the actual top of the chain the
    real router would try first."""
    router = ModelRouter()
    text = "Why does this function behave this way, and how would you compare it to an alternative?"
    decision = router._build_chain_decision(
        TaskType.CHAT, "pro", get_plan_limits("pro"), "code",
        messages=_messages(text),
    )
    assert decision.chain, "expected a non-empty chain"
    assert decision.chain[0][0] == "xai-grok"
    assert decision.selection_reason == "dynamic_eligible"


def test_global_candidate_count_exceeds_the_old_curated_chain_length():
    """The GLOBAL eligible count must be able to exceed what
    select_provider_chain()'s own curated list ever listed for this
    (task_type, plan, capability) — direct proof discovery is no longer
    bounded by that list's length."""
    from trinity_router.selector import select_provider_chain

    router = ModelRouter()
    text = "Why does this function behave this way, and how would you compare it to an alternative?"
    decision = router._build_chain_decision(
        TaskType.CHAT, "pro", get_plan_limits("pro"), "code",
        messages=_messages(text),
    )
    old_curated_length = len(select_provider_chain(TaskType.CHAT, "pro", "code"))
    assert decision.eligible_model_count > old_curated_length


# ── E. tools required (real router-level wiring, not just the pure filter) ──

def test_tools_required_end_to_end_only_returns_tool_capable_candidates():
    """Every current catalog entry happens to support tools (see
    providers.py), so this cannot exercise an EXCLUSION with real catalog
    data — that exact exclusion is already proven at the pure-function
    level in test_model_eligibility.py::test_tools_required_model_without_tools_rejected.
    This test instead proves the real router-level WIRING: a tools-bearing
    request's resulting chain is built with requires_tools=True actually
    threaded through get_eligible_models(), by confirming every candidate
    in the real chain does support tools (never silently dropped/ignored)."""
    router = ModelRouter()
    decision = router._build_chain_decision(
        TaskType.CHAT, "pro", get_plan_limits("pro"), "code",
        messages=_messages("hi"), tools=[{"name": "shell"}],
    )
    assert decision.chain
    assert all(cfg.supports_tools for _key, cfg in decision.chain)


# ── F. context too large (pure-filter level — no real catalog model is ─────
# small enough to exclude with realistic request text; router.py has never
# computed a real min_context_window value in either V1 or V2 — see the
# V2 FINAL REPORT's "remaining limitations" section) ────────────────────────

def test_context_too_large_model_excluded_from_eligibility():
    """Confirms get_eligible_models()'s min_context_window filter — reused
    unchanged from V1 — still works when given a real requirement,
    against the REAL catalog's actual context windows (gpt-4o/gpt-4o-mini
    at 128K are the smallest; everything else is >= 200K)."""
    models = list(PROVIDER_CATALOG.values())
    eligible = get_eligible_models(models, min_context_window=200_000)
    assert all(m.context_window >= 200_000 for m in eligible)
    assert any(m.context_window < 200_000 for m in models)  # precondition: some model IS excluded
    excluded_ids = {m.model_id for m in models if m.context_window < 200_000}
    eligible_ids = {m.model_id for m in eligible}
    assert not (excluded_ids & eligible_ids)
