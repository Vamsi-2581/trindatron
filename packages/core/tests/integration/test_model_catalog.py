"""
Tests for the TRINITY Model Router catalog and chain integrity.

Validates:
- All catalog aliases have valid, litellm-known model IDs
- No prices are zero or negative
- All chain aliases exist in the catalog
- Chain structure matches plan limits
- Failover chain lengths respect max_failover_attempts
- Removed legacy models (gemini-1.5-x) are gone
- claude-haiku is in catalog but intentionally not in chains
"""
from __future__ import annotations

import pytest
import litellm

from trinity_router.providers import PROVIDER_CATALOG, ProviderModel
from trinity_router.selector import (
    TaskType,
    _PROVIDER_CHAINS,
    _DEFAULT_CHAIN,
    select_provider_chain,
)
from trinity_router.limits import get_plan_limits


# ---------------------------------------------------------------------------
# Catalog integrity
# ---------------------------------------------------------------------------

class TestCatalogIntegrity:

    def test_all_model_ids_exist_in_litellm(self):
        """Every model_id in the catalog must appear in litellm.model_cost."""
        mc = litellm.model_cost
        missing = [
            (alias, m.model_id)
            for alias, m in PROVIDER_CATALOG.items()
            if m.model_id not in mc
        ]
        assert missing == [], f"Model IDs missing from litellm.model_cost: {missing}"

    def test_no_zero_input_price(self):
        """Input price must be positive for all catalog entries."""
        zero = [
            (alias, m.model_id, m.cost_per_1m_input_usd)
            for alias, m in PROVIDER_CATALOG.items()
            if m.cost_per_1m_input_usd <= 0
        ]
        assert zero == [], f"Catalog entries with zero/negative input price: {zero}"

    def test_no_zero_output_price(self):
        """Output price must be positive for all catalog entries."""
        zero = [
            (alias, m.model_id, m.cost_per_1m_output_usd)
            for alias, m in PROVIDER_CATALOG.items()
            if m.cost_per_1m_output_usd <= 0
        ]
        assert zero == [], f"Catalog entries with zero/negative output price: {zero}"

    def test_no_zero_context_window(self):
        """Context window must be positive for all catalog entries."""
        zero = [
            (alias, m.model_id, m.context_window)
            for alias, m in PROVIDER_CATALOG.items()
            if m.context_window <= 0
        ]
        assert zero == [], f"Catalog entries with zero/negative context_window: {zero}"

    def test_litellm_prices_match_catalog(self):
        """
        For each catalog entry, verify the litellm price matches what we hardcoded.
        Tolerance: ±$0.001/1M to account for rounding in litellm data.
        """
        mc = litellm.model_cost
        mismatches = []
        for alias, m in PROVIDER_CATALOG.items():
            entry = mc.get(m.model_id, {})
            litellm_input = (entry.get("input_cost_per_token") or 0) * 1_000_000
            litellm_output = (entry.get("output_cost_per_token") or 0) * 1_000_000
            if abs(litellm_input - m.cost_per_1m_input_usd) > 0.001:
                mismatches.append(
                    f"{alias}: input expected={m.cost_per_1m_input_usd} litellm={litellm_input:.4f}"
                )
            if abs(litellm_output - m.cost_per_1m_output_usd) > 0.001:
                mismatches.append(
                    f"{alias}: output expected={m.cost_per_1m_output_usd} litellm={litellm_output:.4f}"
                )
        assert mismatches == [], "Price mismatches vs litellm:\n" + "\n".join(mismatches)

    def test_legacy_gemini_15_removed(self):
        """gemini-1.5-pro and gemini-1.5-flash must not exist in the catalog."""
        legacy_ids = {"gemini/gemini-1.5-pro", "gemini/gemini-1.5-flash"}
        found = [
            alias for alias, m in PROVIDER_CATALOG.items()
            if m.model_id in legacy_ids
        ]
        assert found == [], f"Legacy Gemini 1.5 models still in catalog: {found}"

    def test_haiku_id_has_no_date_suffix(self):
        """claude-haiku must use the clean model ID without the date suffix."""
        haiku = PROVIDER_CATALOG.get("claude-haiku")
        assert haiku is not None, "claude-haiku alias missing from catalog"
        assert haiku.model_id == "claude-haiku-4-5", (
            f"Expected 'claude-haiku-4-5', got '{haiku.model_id}'"
        )

    def test_all_providers_correct(self):
        """Provider field must match the litellm_provider from model_cost."""
        mc = litellm.model_cost
        provider_map = {"anthropic": "anthropic", "openai": "openai", "google": "gemini"}
        mismatches = []
        for alias, m in PROVIDER_CATALOG.items():
            entry = mc.get(m.model_id, {})
            litellm_prov = entry.get("litellm_provider", "")
            expected_litellm = provider_map.get(m.provider, m.provider)
            if litellm_prov and litellm_prov != expected_litellm:
                mismatches.append(
                    f"{alias}: catalog.provider={m.provider} litellm_provider={litellm_prov}"
                )
        assert mismatches == [], f"Provider mismatches: {mismatches}"

    def test_all_agent_models_support_tools(self):
        """All models used in agent chains must have supports_tools=True in catalog."""
        agent_aliases = set()
        for (task, _plan), chain in _PROVIDER_CHAINS.items():
            if task == TaskType.AGENT:
                agent_aliases.update(chain)

        no_tools = [
            alias for alias in agent_aliases
            if alias in PROVIDER_CATALOG and not PROVIDER_CATALOG[alias].supports_tools
        ]
        assert no_tools == [], f"Agent chain models without tool support: {no_tools}"

    def test_catalog_has_expected_aliases(self):
        """Catalog must contain exactly the expected aliases."""
        expected = {
            "claude-opus", "claude-sonnet", "claude-haiku",
            "gpt-4o", "gpt-4o-mini",
            # gemini-25-flash removed 2026-09-13 — unreachable by cost rank.
            "gemini-25-flash-lite",
            "groq-llama4-scout",
            "xai-grok", "mistral-small",
        }
        actual = set(PROVIDER_CATALOG.keys())
        assert actual == expected, (
            f"Catalog mismatch.\nExpected: {sorted(expected)}\nActual:   {sorted(actual)}"
        )

    def test_specific_model_ids(self):
        """Key model IDs must be exactly as validated against litellm."""
        expected_ids = {
            "claude-opus": "claude-opus-4-7",
            "claude-sonnet": "claude-sonnet-5",
            "claude-haiku": "claude-haiku-4-5",
            "gpt-4o": "gpt-6.1-sol",       # alias kept; gpt-4o retires 2027-01-14
            "gpt-4o-mini": "gpt-6-luna",   # alias kept; gpt-4o-mini retires 2027-01-14
            # "gemini-25-flash" (gemini/gemini-3.6-flash) was removed from the
            # catalog on 2026-09-13 as unreachable by cost rank. Its expectation
            # is deleted rather than left here: the loop below skips aliases
            # absent from the catalog, so a stale entry would sit dead forever.
            "gemini-25-flash-lite": "gemini/gemini-3.5-flash-lite",
            "groq-llama4-scout": "groq/openai/gpt-oss-20b",
            "xai-grok": "xai/grok-4.20-0309-non-reasoning",
            "mistral-small": "mistral/mistral-small-latest",
        }
        mismatches = [
            f"{alias}: expected '{mid}', got '{PROVIDER_CATALOG[alias].model_id}'"
            for alias, mid in expected_ids.items()
            if alias in PROVIDER_CATALOG and PROVIDER_CATALOG[alias].model_id != mid
        ]
        assert mismatches == [], "Model ID mismatches:\n" + "\n".join(mismatches)

    def test_groq_llama4_scout_model_id_not_reintroduced(self):
        """
        Regression guard for the 2026-09-06 incident: this exact model ID
        returns Groq error code=model_not_found for our account/project
        (confirmed live against GET /v1/models, not merely "deprecated" in
        general) — it must never be reintroduced as any catalog model_id.
        """
        banned_id = "groq/meta-llama/llama-4-scout-17b-16e-instruct"
        offenders = [
            alias for alias, m in PROVIDER_CATALOG.items()
            if m.model_id == banned_id
        ]
        assert offenders == [], (
            f"model_id={banned_id!r} is not accessible to our Groq account/"
            f"project (confirmed via live GET /v1/models) and must not be "
            f"used by any catalog entry. Offending aliases: {offenders}"
        )


# ---------------------------------------------------------------------------
# Chain integrity
# ---------------------------------------------------------------------------

class TestChainIntegrity:

    def test_all_chain_aliases_exist_in_catalog(self):
        """Every alias referenced in a chain must exist in PROVIDER_CATALOG."""
        all_chains = list(_PROVIDER_CHAINS.values()) + [_DEFAULT_CHAIN]
        missing = []
        for chain in all_chains:
            for alias in chain:
                if alias not in PROVIDER_CATALOG:
                    missing.append(alias)
        assert missing == [], f"Chain aliases not in catalog: {set(missing)}"

    def test_claude_haiku_not_in_chains(self):
        """claude-haiku is reserved for classifier use — must not appear in routing chains."""
        all_chains = list(_PROVIDER_CHAINS.values()) + [_DEFAULT_CHAIN]
        in_chains = any("claude-haiku" in chain for chain in all_chains)
        assert not in_chains, "claude-haiku should not be in routing chains (reserved for classifier)"

    def test_legacy_gemini_not_in_chains(self):
        """gemini-pro and gemini-flash (1.5 legacy) must not appear in any chain."""
        legacy = {"gemini-pro", "gemini-flash"}
        all_chains = list(_PROVIDER_CHAINS.values()) + [_DEFAULT_CHAIN]
        found = [alias for chain in all_chains for alias in chain if alias in legacy]
        assert found == [], f"Legacy gemini aliases still in chains: {found}"

    @pytest.mark.parametrize("plan", ["start", "pro", "max"])
    def test_chain_length_vs_failover_limit(self, plan):
        """
        Every chain must have at least 2 entries (primary + 1 fallback).
        The chain length need not equal max_failover_attempts+1; the router
        silently caps — but a chain shorter than 2 is a configuration error.
        """
        limits = get_plan_limits(plan)
        for task in TaskType:
            chain = select_provider_chain(task, plan)
            assert len(chain) >= 2, (
                f"{plan}/{task.value} chain has fewer than 2 entries: {chain}"
            )

    @pytest.mark.parametrize("plan,task,expected_primary", [
        ("start", TaskType.CHAT,  "groq-llama4-scout"),  # cheapest-capable primary; falls over to gemini-25-flash-lite
        ("start", TaskType.AGENT, "claude-sonnet"),
        ("pro",   TaskType.CHAT,  "claude-sonnet"),
        ("pro",   TaskType.AGENT, "claude-sonnet"),
        ("max",   TaskType.CHAT,  "claude-opus"),
        ("max",   TaskType.AGENT, "claude-opus"),
    ])
    def test_primary_provider_per_plan(self, plan, task, expected_primary):
        """Primary provider (first in chain) matches expected alias per plan."""
        chain = select_provider_chain(task, plan)
        assert chain[0] == expected_primary, (
            f"{plan}/{task.value}: expected primary '{expected_primary}', got '{chain[0]}'"
        )

    def test_start_has_cross_provider_fallback(self):
        """START chain must have providers from at least 2 different companies."""
        for task in TaskType:
            chain = select_provider_chain(task, "start")
            providers = {PROVIDER_CATALOG[alias].provider for alias in chain if alias in PROVIDER_CATALOG}
            assert len(providers) >= 2, (
                f"START/{task.value} chain lacks cross-provider diversity: {chain} → {providers}"
            )

    def test_pro_has_gemini_fallback(self):
        """PRO chains must include a Google/Gemini model for third-provider diversity."""
        for task in TaskType:
            chain = select_provider_chain(task, "pro")
            google_aliases = [
                alias for alias in chain
                if alias in PROVIDER_CATALOG and PROVIDER_CATALOG[alias].provider == "google"
            ]
            assert google_aliases, (
                f"PRO/{task.value} chain has no Google provider fallback: {chain}"
            )

    def test_max_has_four_providers(self):
        """MAX chains must have at least 4 aliases for full failover coverage."""
        for task in TaskType:
            chain = select_provider_chain(task, "max")
            assert len(chain) >= 4, (
                f"MAX/{task.value} chain has fewer than 4 entries: {chain}"
            )

    def test_select_unknown_plan_falls_back_to_start(self):
        """Unknown plan name must return the default start chain."""
        start_chat = select_provider_chain(TaskType.CHAT, "start")
        unknown = select_provider_chain(TaskType.CHAT, "enterprise")
        assert unknown == start_chat

    def test_select_provider_chain_returns_copy(self):
        """Mutating returned chain must not affect future calls."""
        chain1 = select_provider_chain(TaskType.CHAT, "start")
        chain1.clear()
        chain2 = select_provider_chain(TaskType.CHAT, "start")
        assert len(chain2) > 0

    def test_free_plan_uses_a_single_reliably_configured_provider(self):
        """FREE plan (max_failover_attempts=0, no recovery on failure) must
        route to a single provider whose API key is always configured — never
        groq-llama4-scout, whose GROQ_API_KEY is optional; a FREE-plan user
        would have zero recovery if it were absent and used as the sole
        provider (see selector.py's own module docstring on this exact
        rationale — FREE and START deliberately diverge here, unlike most
        other plan pairs in this module)."""
        limits = get_plan_limits("free")
        assert limits.max_failover_attempts == 0
        for task in TaskType:
            chain = select_provider_chain(task, "free")
            assert len(chain) == 1, f"free/{task.value} must be a single-provider chain: {chain}"
            assert chain[0] != "groq-llama4-scout", (
                f"free/{task.value} must not rely on the optional GROQ_API_KEY as its only shot: {chain}"
            )

    def test_team_plan_routes_like_max(self):
        """TEAM plan must use the same chain as MAX (premium routing)."""
        for task in TaskType:
            assert select_provider_chain(task, "team") == select_provider_chain(task, "max"), (
                f"team/{task.value} chain differs from max/{task.value}"
            )

    @pytest.mark.parametrize("plan", ["free", "team"])
    def test_new_plans_have_valid_chain_aliases(self, plan):
        """Chains for new plans must only reference catalog aliases."""
        for task in TaskType:
            chain = select_provider_chain(task, plan)
            for alias in chain:
                assert alias in PROVIDER_CATALOG, (
                    f"{plan}/{task.value}: alias '{alias}' not in PROVIDER_CATALOG"
                )


# ---------------------------------------------------------------------------
# Price spot-checks (exact confirmed values from litellm 2026-08-20)
# ---------------------------------------------------------------------------

class TestConfirmedPrices:

    @pytest.mark.parametrize("alias,expected_input,expected_output", [
        ("claude-opus",      5.0,   25.0),
        ("claude-sonnet",    2.0,   10.0),
        ("claude-haiku",     1.0,    5.0),
        ("gpt-4o",           2.0,   10.0),   # gpt-6.1-sol
        ("gpt-4o-mini",      0.10,   0.50),   # gpt-6-luna
        ("gemini-25-flash-lite", 0.30, 2.50),
        ("groq-llama4-scout", 0.075, 0.30),
        ("xai-grok", 1.25, 2.50),
        ("mistral-small", 0.15, 0.60),
    ])
    def test_hardcoded_prices(self, alias, expected_input, expected_output):
        m = PROVIDER_CATALOG[alias]
        assert m.cost_per_1m_input_usd == pytest.approx(expected_input, rel=1e-4), (
            f"{alias} input price: expected {expected_input}, got {m.cost_per_1m_input_usd}"
        )
        assert m.cost_per_1m_output_usd == pytest.approx(expected_output, rel=1e-4), (
            f"{alias} output price: expected {expected_output}, got {m.cost_per_1m_output_usd}"
        )
