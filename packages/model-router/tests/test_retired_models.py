"""The catalog must never point at a model the provider has retired.

This guard exists because the failure has now happened twice, and both times
it was invisible to code review. A retired Google model is still listed by
the models endpoint and still priced by litellm.model_cost — the entry looks
completely healthy — and only an actual inference call reveals a 404 saying
"no longer available to new users".

So there is nothing to derive the truth from automatically, and the honest
mechanism is a hand-maintained denylist (providers.RETIRED_MODEL_IDS) plus
this test. Adding a retired id back to the catalog now fails here rather than
in production.
"""
from __future__ import annotations

import pytest

from trinity_router.limits import get_plan_limits
from trinity_router.providers import (
    PROVIDER_CATALOG,
    RETIRED_MODEL_IDS,
    get_eligible_models,
    rank_by_cost,
)
from trinity_router.router import ModelRouter
from trinity_router.selector import TaskType


def test_no_catalog_entry_uses_a_retired_model_id():
    offenders = {
        key: model.model_id
        for key, model in PROVIDER_CATALOG.items()
        if model.model_id in RETIRED_MODEL_IDS
    }
    assert not offenders, (
        f"These catalog entries point at retired models: {offenders}. "
        "A retired id still appears in the provider's model listing and still "
        "has a price in litellm, so neither is evidence it can be called — "
        "replace the model_id with the successor the provider's own 404 names."
    )


def test_the_gemini_25_flash_alias_no_longer_exists():
    """Removed 2026-09-13. It carried gemini-3.6-flash at $0.75/$3.75, which
    ranked 6th by blended cost while chains hold at most 4 candidates, and its
    "low" class excluded it whenever the floor rose to "standard" — so no
    dynamic chain could contain it. A sweep of 140 chain cells found it in 44,
    all on the legacy select_provider_chain() fallback and none on the path
    that actually runs."""
    assert "gemini-25-flash" not in PROVIDER_CATALOG


def test_removing_the_alias_did_not_weaken_the_retired_guard():
    """The point that is easy to get wrong: an UNREACHABLE alias and a RETIRED
    model_id are different things. Dropping the first must not drop the
    second, or the id Google 404s on could be reintroduced unnoticed."""
    assert "gemini/gemini-2.5-flash" in RETIRED_MODEL_IDS
    assert not any(m.model_id == "gemini/gemini-2.5-flash"
                   for m in PROVIDER_CATALOG.values())


def test_the_lite_entry_survives_and_still_resolves_correctly():
    """The other Google entry is reachable and must not be collateral damage."""
    entry = PROVIDER_CATALOG["gemini-25-flash-lite"]
    assert entry.model_id == "gemini/gemini-3.5-flash-lite"
    assert entry.provider == "google"
    assert entry.quality_class == "low"
    assert entry.context_window == 1_048_576
    assert entry.supports_tools is True


def test_gemini_3_6_flash_is_not_retired_only_unused():
    """It was never broken — it simply lost every race on cost. Recording that
    distinction stops a future reader assuming the successor was faulty."""
    assert "gemini/gemini-3.6-flash" not in RETIRED_MODEL_IDS


def test_retired_list_is_not_silently_emptied():
    """A denylist that someone empties to make a test pass protects nothing.
    Both known retirements must stay recorded."""
    assert "gemini/gemini-2.5-flash" in RETIRED_MODEL_IDS
    assert "gemini/gemini-2.5-flash-lite" in RETIRED_MODEL_IDS


# ── The catalog fix must not become a routing change ─────────────────────
#
# Swapping a model_id is only safe if the entry keeps the same ROLE. These
# pin the parts of that role the router actually reads, so a future edit
# that moves the tier or narrows the capabilities fails here.

def test_no_capability_lost_an_eligible_model_it_still_needs(minimum=2):
    """Removing a catalog entry is only safe if every capability still has
    real choice at its floor. Two is the bar: a primary and somewhere to fail
    over to."""
    for floor in ("ultra_low", "low", "standard"):
        eligible = get_eligible_models(list(PROVIDER_CATALOG.values()),
                                       min_quality_class=floor)
        assert len(eligible) >= minimum, (
            f"floor {floor} has only {len(eligible)} eligible models"
        )


# ── The guarantee that actually matters at runtime ───────────────────────

def _chain(router, *, capability, task_type, plan):
    decision = router._build_chain_decision(
        task_type, plan, get_plan_limits(plan), capability,
        disabled_provider_keys=frozenset(),
        messages=[{"role": "system", "content": "sys"},
                  {"role": "user", "content": "hi"}],
        tools=None,
    )
    return [key for key, _ in decision.chain]


ALL_PLANS = ("free", "start", "pro", "max", "team")
ALL_CAPABILITIES = (("agent", TaskType.AGENT), ("code", TaskType.AGENT),
                    ("research", TaskType.AGENT), ("files", TaskType.AGENT),
                    ("work", TaskType.AGENT), ("automation", TaskType.AGENT),
                    ("chat", TaskType.CHAT))


@pytest.mark.parametrize("plan", ALL_PLANS)
@pytest.mark.parametrize("capability,task_type", ALL_CAPABILITIES)
def test_no_retired_model_can_reach_a_built_chain(plan, capability, task_type):
    """The catalog-level check above proves no ENTRY holds a retired id.
    This proves the stronger, operational thing: across every plan and
    capability, nothing the router would actually call is retired. That is
    the failure the production incident was — a dead model sitting in a
    failover slot, invisible until the two models ahead of it both failed."""
    for key in _chain(ModelRouter(), capability=capability,
                      task_type=task_type, plan=plan):
        assert PROVIDER_CATALOG[key].model_id not in RETIRED_MODEL_IDS, (
            f"{plan}/{capability}: chain contains retired model {key}"
        )


# ── This fix must not disturb the agent-capability routing ───────────────

@pytest.mark.parametrize("plan", ("pro", "max", "team"))
def test_agent_routing_is_untouched_by_the_gemini_swap(plan):
    chain = _chain(ModelRouter(), capability="agent",
                   task_type=TaskType.AGENT, plan=plan)
    # Order since 2026-10-20, when the gpt-4o-mini alias moved to the cheaper
    # gpt-6-luna; the Gemini swap still does not touch it.
    assert chain[0] == "gpt-4o-mini", chain
    assert chain[1] == "mistral-small", chain


@pytest.mark.parametrize("plan", ("pro", "max", "team"))
@pytest.mark.parametrize("capability,task_type",
                         [("chat", TaskType.CHAT), ("code", TaskType.AGENT),
                          ("research", TaskType.AGENT)])
def test_chat_coding_and_research_keep_their_primary(plan, capability, task_type):
    chain = _chain(ModelRouter(), capability=capability,
                   task_type=task_type, plan=plan)
    assert chain[0] == "gpt-4o-mini", f"{plan}/{capability}: {chain}"


def test_the_lite_entry_is_the_only_google_model_left_in_the_ranking():
    """The removed alias ranked behind gemini-25-flash-lite on cost, which is
    what made it unreachable. With it gone the lite entry is Google's single
    representative — asserted so a future re-add has to justify itself against
    this, rather than quietly reappearing below the cut."""
    ranked = rank_by_cost(get_eligible_models(list(PROVIDER_CATALOG.values()),
                                              min_quality_class="low"))
    google = [m.model_id for m in ranked if m.provider == "google"]
    assert google == ["gemini/gemini-3.5-flash-lite"]
