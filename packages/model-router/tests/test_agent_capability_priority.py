"""mistral-small is the primary for capability="agent" at the LOW floor.

Why this exists
---------------
gpt-4o-mini and mistral-small are priced identically ($0.15/$0.60) and sit
in the same quality_class ("low"), so rank_by_cost can never separate them
on cost — the tie falls through to `priority`, which is a single global
integer per model. bench/MODEL_COMPARISON.md measured mistral-small ahead
on the agent task class specifically (0.917 vs 0.879, both with the System
Policy) and found no evidence to separate them anywhere else. A global
priority flip would therefore have changed chat, coding, research and
document routing on evidence that does not exist for those classes.

The two things these tests are actually guarding are consequently:
  1. the intended change happens (agent + low -> mistral-small first), and
  2. NOTHING ELSE MOVES — in particular `code` and `research`, which
     core's CAPABILITY_TASK_TYPE maps onto TaskType.AGENT just like
     `agent` does. That shared task type is exactly why the override is
     keyed on the capability string; a task-type-keyed override would pass
     test 1 and silently fail test 2.

Everything here runs against the real PROVIDER_CATALOG through the real
ModelRouter chain builder. No model is called.

2026-10-20: the tie is gone
---------------------------
OpenAI retires gpt-4o-mini on 2027-01-14, and the gpt-4o-mini alias now
carries gpt-6-luna at $0.10/$0.50 — cheaper than mistral-small. Cost is the
first sort key, so luna now leads agent work as well, and mistral-small's
override is dormant until the two are priced the same again. Tests 1, 2 and
the FREE test below assert that new order; everything else (the override
stays scoped to the capability string, never beats cost, never changes chain
length or membership) is unchanged and still guarded.
"""
from __future__ import annotations

import pytest

from trinity_router.limits import get_plan_limits
from trinity_router.providers import PROVIDER_CATALOG, get_eligible_models, rank_by_cost
from trinity_router.router import ModelRouter
from trinity_router.selector import TaskType

#: Capabilities core maps onto TaskType.AGENT alongside "agent" itself.
#: The whole point of the change is that these are NOT affected.
AGENT_TASK_TYPE_SIBLINGS = ("code", "research", "files", "work", "automation")

#: Plans whose min_quality_class is "low" — the floor this change targets.
LOW_FLOOR_PLANS = ("pro", "max", "team")


def _chain(router, *, capability, task_type=TaskType.AGENT, plan="pro", text="hi", tools=None):
    decision = router._build_chain_decision(
        task_type, plan, get_plan_limits(plan), capability,
        disabled_provider_keys=frozenset(),
        messages=[{"role": "system", "content": "sys"},
                  {"role": "user", "content": text}],
        tools=tools,
    )
    return [key for key, _ in decision.chain], decision


# -- 1. The change itself -------------------------------------------------

@pytest.mark.parametrize("plan", LOW_FLOOR_PLANS)
def test_agent_capability_at_low_floor_picks_the_cheaper_luna_first(plan):
    chain, decision = _chain(ModelRouter(), capability="agent", plan=plan)
    assert chain[0] == "gpt-4o-mini", (
        f"plan={plan}: expected gpt-4o-mini (gpt-6-luna) to lead the agent chain, got {chain}"
    )
    assert decision.selection_reason == "dynamic_eligible"


# -- 2. mistral-small is demoted, NOT removed -----------------------------

def test_mistral_small_remains_the_immediate_fallback_for_agent():
    """A cheaper model reorders the chain; it must not drop a candidate. If
    luna is unavailable the request has to land on mistral-small, the next
    cheapest, not skip to something more expensive."""
    chain, _ = _chain(ModelRouter(), capability="agent", plan="pro")
    assert "mistral-small" in chain, f"mistral-small fell out of the chain: {chain}"
    assert chain.index("mistral-small") == chain.index("gpt-4o-mini") + 1, (
        f"mistral-small should sit immediately behind gpt-4o-mini, got {chain}"
    )


# -- 3/4. Chat and coding are untouched -----------------------------------

def test_chat_capability_still_leads_with_gpt_4o_mini():
    chain, _ = _chain(ModelRouter(), capability="chat", task_type=TaskType.CHAT, plan="pro")
    assert chain[0] == "gpt-4o-mini", f"chat routing moved: {chain}"


def test_code_capability_still_leads_with_gpt_4o_mini_despite_agent_task_type():
    """`code` carries TaskType.AGENT. This is the test that fails if anyone
    re-keys the override on task_type instead of capability."""
    chain, _ = _chain(ModelRouter(), capability="code", task_type=TaskType.AGENT, plan="pro")
    assert chain[0] == "gpt-4o-mini", f"coding routing moved: {chain}"


@pytest.mark.parametrize("capability", AGENT_TASK_TYPE_SIBLINGS)
@pytest.mark.parametrize("plan", LOW_FLOOR_PLANS)
def test_no_other_agent_task_type_capability_moved(capability, plan):
    chain, _ = _chain(ModelRouter(), capability=capability, task_type=TaskType.AGENT, plan=plan)
    assert chain[0] == "gpt-4o-mini", (
        f"capability={capability} plan={plan} was not supposed to change: {chain}"
    )


# -- 5. The override is scoped to the capability string alone -------------

def test_priority_for_overrides_only_the_named_capability():
    m = PROVIDER_CATALOG["mistral-small"]
    assert m.priority_for("agent") < PROVIDER_CATALOG["gpt-4o-mini"].priority_for("agent")
    for other in ("chat", "code", "research", "document", None):
        assert m.priority_for(other) == m.priority, (
            f"capability={other!r} should fall back to the global priority"
        )


def test_models_without_an_override_ignore_capability_entirely():
    for key, cfg in PROVIDER_CATALOG.items():
        if cfg.capability_priority:
            continue
        assert cfg.priority_for("agent") == cfg.priority, key
        assert cfg.priority_for(None) == cfg.priority, key


def test_rank_by_cost_without_a_capability_reproduces_the_old_order():
    """The argument is optional, and omitting it must be a no-op — every
    existing caller that does not pass it keeps its previous behaviour."""
    models = list(PROVIDER_CATALOG.values())
    assert rank_by_cost(models) == rank_by_cost(models, capability=None)
    assert [id(m) for m in rank_by_cost(models)] == [
        id(m) for m in sorted(
            models,
            key=lambda c: (
                c.cost_per_1m_input_usd * 2_000 / 1e6
                + c.cost_per_1m_output_usd * 4_096 / 1e6,
                c.priority,
            ),
        )
    ]


# -- 6. The tiebreak cannot reach an ineligible or costlier model ---------

def test_the_override_never_promotes_a_model_past_a_cheaper_one():
    """priority is the SECOND sort key. An override must be unable to beat
    cost, or it would be a licence to make routing more expensive."""
    eligible = get_eligible_models(list(PROVIDER_CATALOG.values()), min_quality_class="ultra_low")
    ranked = rank_by_cost(eligible, capability="agent")
    costs = [m.cost_per_1m_input_usd * 2_000 / 1e6 + m.cost_per_1m_output_usd * 4_096 / 1e6
             for m in ranked]
    assert costs == sorted(costs), "capability override reordered across a cost boundary"


@pytest.mark.parametrize("plan", LOW_FLOOR_PLANS)
def test_agent_chain_contains_no_model_below_the_plan_floor(plan):
    chain, _ = _chain(ModelRouter(), capability="agent", plan=plan)
    floor = get_plan_limits(plan).min_quality_class
    allowed = set(get_eligible_models(list(PROVIDER_CATALOG.values()), min_quality_class=floor))
    for key in chain:
        assert PROVIDER_CATALOG[key] in allowed, f"{key} is below the {plan} floor ({floor})"


def test_groq_is_excluded_from_agent_by_the_quality_floor_not_by_the_tiebreak():
    """groq-llama4-scout IS an agent-capable candidate and is genuinely
    CHEAPER than the tied pair, so it is the one model a mis-built tiebreak
    could actually cost us. It does not appear in any agent chain because
    capability="agent" carries base_capability_score=30 -> a required
    quality class of "low", and groq is "ultra_low". That exclusion is
    pre-existing and has nothing to do with priority — which is what this
    asserts, by showing groq still leads where the floor does admit it."""
    groq = PROVIDER_CATALOG["groq-llama4-scout"]
    assert "agent" in groq.supported_capabilities
    assert groq.quality_class == "ultra_low"
    assert not groq.capability_priority

    for plan in ("free", "start"):
        _, decision = _chain(ModelRouter(), capability="agent", plan=plan)
        assert decision.complexity.required_quality_class == "low"
        chat_chain, _ = _chain(ModelRouter(), capability="chat",
                               task_type=TaskType.CHAT, plan=plan)
        assert chat_chain[0] == "groq-llama4-scout", (
            f"{plan} chat primary moved: {chat_chain}"
        )


def test_the_agent_capability_sits_at_the_low_floor_on_every_plan():
    """Why this change needs no per-plan carve-out: the LOW floor for agent
    work comes from the capability's own complexity score, so it holds even
    on plans whose min_quality_class is ultra_low."""
    router = ModelRouter()
    for plan in ("free", "start", "pro", "max", "team"):
        _, decision = _chain(router, capability="agent", plan=plan)
        assert decision.complexity.required_quality_class == "low", plan


def test_free_plan_agent_chain_is_a_single_model_before_and_after():
    """FREE has max_failover_attempts=0, so its agent chain is one entry
    with no fallback. That was already true when the entry was gpt-4o-mini;
    this change swaps WHICH model occupies the slot and must not be read as
    having removed a fallback that never existed."""
    chain, _ = _chain(ModelRouter(), capability="agent", plan="free")
    assert chain == ["gpt-4o-mini"]
    assert get_plan_limits("free").max_failover_attempts == 0


# -- 7. Failover economics are unchanged ----------------------------------

@pytest.mark.parametrize("plan", ("free", "start", "pro", "max", "team"))
def test_chain_length_still_respects_the_plans_failover_budget(plan):
    chain, _ = _chain(ModelRouter(), capability="agent", plan=plan)
    assert len(chain) <= get_plan_limits(plan).max_failover_attempts + 1
    assert len(chain) == len(set(chain)), f"duplicate entry in chain: {chain}"


def test_agent_chain_is_the_same_length_as_every_sibling_capability():
    """Reordering must not lengthen or shorten the chain — that would change
    how many provider calls a request can cost."""
    router = ModelRouter()
    agent_chain, _ = _chain(router, capability="agent", plan="pro")
    for capability in AGENT_TASK_TYPE_SIBLINGS:
        sibling, _ = _chain(router, capability=capability, plan="pro")
        assert len(agent_chain) == len(sibling), capability
        assert set(agent_chain) == set(sibling), (
            f"agent chain membership differs from {capability}; only ORDER "
            f"should change: {agent_chain} vs {sibling}"
        )
