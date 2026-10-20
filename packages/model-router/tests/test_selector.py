"""
Tests for select_provider_chain — capability plumbing and backward compatibility.

Verifies that:
1. Adding the capability parameter does not change existing provider chains.
2. capability=None is identical to omitting the parameter (backward compat).
3. Unknown capability values fall back to the TaskType-based chain.
4. The function returns a copy, not a reference to the internal list.
"""
import pytest

from trinity_router.selector import TaskType, select_provider_chain


# ── Backward compatibility — existing chains must be unchanged ────────────────

class TestSelectProviderChainBackwardCompat:
    def test_start_chat_chain(self):
        # CHAT start: groq-llama4-scout primary (ultra-cheap), gemini-25-flash-lite fallback.
        assert select_provider_chain(TaskType.CHAT, "start") == [
            "groq-llama4-scout", "gemini-25-flash-lite"
        ]

    def test_start_agent_chain_unchanged(self):
        assert select_provider_chain(TaskType.AGENT, "start") == [
            "claude-sonnet", "gpt-4o-mini"
        ]

    def test_pro_chat_chain_keeps_three_providers(self):
        """The third slot held gemini-25-flash until 2026-09-13 and now holds
        gemini-25-flash-lite. The MODEL changed; the invariant did not — PRO
        keeps a Google fallback behind Anthropic and OpenAI."""
        assert select_provider_chain(TaskType.CHAT, "pro") == [
            "claude-sonnet", "gpt-4o", "gemini-25-flash-lite"
        ]

    def test_pro_agent_chain_keeps_three_providers(self):
        assert select_provider_chain(TaskType.AGENT, "pro") == [
            "claude-sonnet", "gpt-4o", "gemini-25-flash-lite"
        ]

    def test_max_chat_chain_keeps_four_candidates(self):
        assert select_provider_chain(TaskType.CHAT, "max") == [
            "claude-opus", "claude-sonnet", "gpt-4o", "gemini-25-flash-lite"
        ]

    def test_max_agent_chain_keeps_four_candidates(self):
        assert select_provider_chain(TaskType.AGENT, "max") == [
            "claude-opus", "claude-sonnet", "gpt-4o", "gemini-25-flash-lite"
        ]


# ── capability=None is identical to omitting the parameter ───────────────────

class TestCapabilityNoneIsBackwardCompat:
    @pytest.mark.parametrize("task_type,plan", [
        (TaskType.CHAT,  "start"),
        (TaskType.AGENT, "start"),
        (TaskType.CHAT,  "pro"),
        (TaskType.AGENT, "pro"),
        (TaskType.CHAT,  "max"),
        (TaskType.AGENT, "max"),
    ])
    def test_none_equals_omitted(self, task_type, plan):
        without = select_provider_chain(task_type, plan)
        with_none = select_provider_chain(task_type, plan, capability=None)
        assert without == with_none


# ── Known capability values do not alter the chain (no _CAPABILITY_CHAINS yet) ─

class TestCapabilityStringDoesNotChangeChain:
    @pytest.mark.parametrize("capability", [
        # "files" and "code" intentionally excluded: they have _CAPABILITY_CHAINS entries
        # for the start plan that deliberately differ from the task-type chain.
        "chat", "research", "work", "automation", "agent",
    ])
    def test_text_capability_start_agent(self, capability):
        base = select_provider_chain(TaskType.AGENT, "start")
        with_cap = select_provider_chain(TaskType.AGENT, "start", capability=capability)
        assert with_cap == base

    @pytest.mark.parametrize("capability", [
        # "files" intentionally excluded: it has its own _CAPABILITY_CHAINS entry.
        "chat", "research", "code", "work", "automation", "agent",
    ])
    def test_text_capability_max_agent(self, capability):
        base = select_provider_chain(TaskType.AGENT, "max")
        with_cap = select_provider_chain(TaskType.AGENT, "max", capability=capability)
        assert with_cap == base

    def test_chat_capability_chat_task(self):
        base = select_provider_chain(TaskType.CHAT, "pro")
        assert select_provider_chain(TaskType.CHAT, "pro", capability="chat") == base

    def test_unknown_future_capability_falls_back(self):
        base = select_provider_chain(TaskType.AGENT, "max")
        assert select_provider_chain(TaskType.AGENT, "max", capability="future_xyz") == base


# ── Return value is always a copy ─────────────────────────────────────────────

class TestReturnValueIsCopy:
    def test_mutation_does_not_affect_subsequent_call(self):
        chain = select_provider_chain(TaskType.CHAT, "start", capability="code")
        chain.append("injected")
        fresh = select_provider_chain(TaskType.CHAT, "start", capability="code")
        assert "injected" not in fresh

    def test_two_calls_return_equal_but_distinct_lists(self):
        a = select_provider_chain(TaskType.AGENT, "pro", capability="research")
        b = select_provider_chain(TaskType.AGENT, "pro", capability="research")
        assert a == b
        assert a is not b


# ── _CAPABILITY_CHAINS: FILES-specific routing ────────────────────────────────

class TestCapabilityChains:

    # 1. FILES + start uses gemini-25-flash-lite as fallback (not gpt-4o-mini)
    def test_files_start_uses_long_context_fallback(self):
        chain = select_provider_chain(TaskType.AGENT, "start", capability="files")
        assert "gemini-25-flash-lite" in chain
        assert "gpt-4o-mini" not in chain

    # 2. FILES + pro does not contain gpt-4o (128K context)
    def test_files_pro_excludes_short_context_model(self):
        chain = select_provider_chain(TaskType.AGENT, "pro", capability="files")
        assert "gpt-4o" not in chain

    # 3. FILES + max does not contain gpt-4o (128K context)
    def test_files_max_excludes_short_context_model(self):
        chain = select_provider_chain(TaskType.AGENT, "max", capability="files")
        assert "gpt-4o" not in chain

    # 4. Chain length of each FILES chain fits within the plan's failover limit
    #
    # These were equalities until 2026-09-13. Removing the unreachable
    # gemini-25-flash alias shortened the curated FILES chains for pro and
    # max by one, and the slot was deliberately NOT refilled — putting
    # another model there would be inventing a routing decision nobody
    # measured. The bound is what matters (a chain must never exceed what
    # the plan pays for); the exact lengths are pinned separately below so
    # the shortening stays visible rather than being absorbed by a <=.
    @pytest.mark.parametrize("plan,budget,actual", [
        ("start", 2, 2),
        ("pro",   3, 2),
        ("max",   4, 3),
    ])
    def test_files_chain_length_fits_plan_failover(self, plan, budget, actual):
        from trinity_router.limits import get_plan_limits
        limits = get_plan_limits(plan)
        chain = select_provider_chain(TaskType.AGENT, plan, capability="files")
        assert budget == limits.max_failover_attempts + 1
        assert len(chain) <= budget
        assert len(chain) == actual

    # 5. CHAT, RESEARCH, CODE (pro/max only), WORK, AUTOMATION, AGENT continue with current chains.
    # code/start is excluded: it has its own _CAPABILITY_CHAINS entry.
    @pytest.mark.parametrize("capability,task_type,plan", [
        ("chat",       TaskType.CHAT,  "start"),
        ("chat",       TaskType.CHAT,  "pro"),
        ("chat",       TaskType.CHAT,  "max"),
        ("research",   TaskType.AGENT, "start"),
        ("research",   TaskType.AGENT, "pro"),
        ("research",   TaskType.AGENT, "max"),
        ("code",       TaskType.AGENT, "pro"),
        ("code",       TaskType.AGENT, "max"),
        ("work",       TaskType.AGENT, "start"),
        ("automation", TaskType.AGENT, "start"),
        ("agent",      TaskType.AGENT, "start"),
    ])
    def test_non_files_capabilities_unchanged(self, capability, task_type, plan):
        without = select_provider_chain(task_type, plan)
        with_cap = select_provider_chain(task_type, plan, capability=capability)
        assert with_cap == without

    # 6. FILES capability takes priority over TaskType chain
    def test_files_capability_takes_priority_over_task_chain(self):
        agent_start = select_provider_chain(TaskType.AGENT, "start")
        files_start = select_provider_chain(TaskType.AGENT, "start", capability="files")
        assert files_start != agent_start
        assert "gpt-4o-mini" in agent_start
        assert "gpt-4o-mini" not in files_start
        assert "gemini-25-flash-lite" not in agent_start
        assert "gemini-25-flash-lite" in files_start

    # 7. free + FILES falls back to the free base chain (gemini-25-flash-lite).
    #    free has its own plan key; no files entry in _CAPABILITY_CHAINS for "free"
    #    so the selector falls through to _PROVIDER_CHAINS[(AGENT, "free")].
    def test_files_free_plan_resolves_to_free_base_chain(self):
        free_chain = select_provider_chain(TaskType.AGENT, "free", capability="files")
        free_base = select_provider_chain(TaskType.AGENT, "free")
        assert free_chain == free_base
        assert free_chain == ["gemini-25-flash-lite"]

    # 8. team + FILES resolves to max chain (via _PLAN_CHAIN_MAP)
    def test_files_team_plan_resolves_to_max_chain(self):
        team_chain = select_provider_chain(TaskType.AGENT, "team", capability="files")
        max_chain = select_provider_chain(TaskType.AGENT, "max", capability="files")
        assert team_chain == max_chain

    # 9. Unknown capability falls back to TaskType chain
    def test_unknown_capability_falls_back_to_task_chain(self):
        base = select_provider_chain(TaskType.AGENT, "start")
        unknown = select_provider_chain(TaskType.AGENT, "start", capability="future_unknown_xyz")
        assert unknown == base

    # 10. Defensive copy returned for capability chains
    def test_files_capability_chain_is_defensive_copy(self):
        chain1 = select_provider_chain(TaskType.AGENT, "start", capability="files")
        chain1.append("injected")
        chain2 = select_provider_chain(TaskType.AGENT, "start", capability="files")
        assert "injected" not in chain2


# ── _CAPABILITY_CHAINS: CODE/start routing ────────────────────────────────────

class TestCodeCapabilityChain:

    # 1. CODE + start returns exactly ["claude-sonnet"] - the second slot
    #    held gemini-25-flash until it was removed on 2026-09-13, and was
    #    deliberately NOT refilled.
    def test_code_start_chain_exact(self):
        chain = select_provider_chain(TaskType.AGENT, "start", capability="code")
        assert chain == ["claude-sonnet"]

    # 2. CODE + start does not contain gpt-4o-mini
    def test_code_start_excludes_gpt4o_mini(self):
        chain = select_provider_chain(TaskType.AGENT, "start", capability="code")
        assert "gpt-4o-mini" not in chain

    # 3. CODE + pro unchanged from task chain
    def test_code_pro_unchanged(self):
        base = select_provider_chain(TaskType.AGENT, "pro")
        with_cap = select_provider_chain(TaskType.AGENT, "pro", capability="code")
        assert with_cap == base

    # 4. CODE + max unchanged from task chain
    def test_code_max_unchanged(self):
        base = select_provider_chain(TaskType.AGENT, "max")
        with_cap = select_provider_chain(TaskType.AGENT, "max", capability="code")
        assert with_cap == base

    # 5. Other capabilities (chat, research, work, automation, agent) unchanged
    @pytest.mark.parametrize("capability,task_type,plan", [
        ("chat",       TaskType.CHAT,  "start"),
        ("research",   TaskType.AGENT, "start"),
        ("work",       TaskType.AGENT, "start"),
        ("automation", TaskType.AGENT, "start"),
        ("agent",      TaskType.AGENT, "start"),
    ])
    def test_other_capabilities_unaffected(self, capability, task_type, plan):
        base = select_provider_chain(task_type, plan)
        with_cap = select_provider_chain(task_type, plan, capability=capability)
        assert with_cap == base

    # 6. free + CODE falls back to the free base chain (gemini-25-flash-lite).
    #    No code entry in _CAPABILITY_CHAINS for "free", so falls through to
    #    _PROVIDER_CHAINS[(AGENT, "free")] = ["gemini-25-flash-lite"].
    def test_code_free_resolves_to_free_base_chain(self):
        free_chain = select_provider_chain(TaskType.AGENT, "free", capability="code")
        free_base = select_provider_chain(TaskType.AGENT, "free")
        assert free_chain == free_base
        assert free_chain == ["gemini-25-flash-lite"]

    # 7. team + CODE continues using the max task chain (no code entry for max)
    def test_code_team_uses_max_task_chain(self):
        team_chain = select_provider_chain(TaskType.AGENT, "team", capability="code")
        max_chain = select_provider_chain(TaskType.AGENT, "max")
        assert team_chain == max_chain

    # 8. CODE/start chain is a defensive copy
    def test_code_start_chain_is_defensive_copy(self):
        chain1 = select_provider_chain(TaskType.AGENT, "start", capability="code")
        chain1.append("injected")
        chain2 = select_provider_chain(TaskType.AGENT, "start", capability="code")
        assert "injected" not in chain2

    # 9. CODE/start chain length matches max_failover_attempts + 1
    def test_code_start_chain_is_now_a_single_candidate(self):
        """The honest record of what removing gemini-25-flash cost: this
        curated chain had two candidates and now has one, so on the legacy
        fallback path START/code has nothing to fail over to. It is bounded
        by the plan budget, not filled to it. This is only the legacy path —
        the dynamic path that actually runs serves START/code from
        groq-llama4-scout -> gpt-4o-mini and was unaffected."""
        from trinity_router.limits import get_plan_limits
        limits = get_plan_limits("start")
        chain = select_provider_chain(TaskType.AGENT, "start", capability="code")
        assert chain == ["claude-sonnet"]
        assert len(chain) <= limits.max_failover_attempts + 1
