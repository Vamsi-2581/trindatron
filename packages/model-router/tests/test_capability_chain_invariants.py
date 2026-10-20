"""
Structural invariants for _CAPABILITY_CHAINS.

These tests iterate the dict directly — any new entry added to _CAPABILITY_CHAINS
is automatically covered without requiring parametrize updates.

Invariants guarded here:
  1. Every alias in _CAPABILITY_CHAINS exists in PROVIDER_CATALOG.
  2. Every chain has exactly max_failover_attempts+1 providers for its plan.
  3. Every provider in an AGENT-capability chain has supports_tools=True.
  4. _PLAN_CHAIN_MAP maps every expected user plan to a valid plan key.

Invariants already covered by test_selector.py (not duplicated):
  - Context exclusions (gpt-4o-mini/gpt-4o) — specific negative tests per capability.
  - Defensive copy — TestReturnValueIsCopy + per-capability tests.
  - Unknown capability falls back to _PROVIDER_CHAINS — TestCapabilityStringDoesNotChangeChain.
  - capability=None identical to omitted — TestCapabilityNoneIsBackwardCompat.

No live API calls. All assertions are against static catalog metadata.
"""
from __future__ import annotations

import pytest

from trinity_router.limits import get_plan_limits
from trinity_router.providers import PROVIDER_CATALOG
from trinity_router.selector import _CAPABILITY_CHAINS, _PLAN_CHAIN_MAP

# Capabilities whose pipeline is TaskType.AGENT — every provider in those chains
# must declare supports_tools=True because the agent loop issues tool calls on
# every hop, including fallbacks.
_AGENT_CAPABILITIES: frozenset[str] = frozenset({"files", "code"})


# ── Invariant 1: every alias in _CAPABILITY_CHAINS exists in PROVIDER_CATALOG ─

class TestCapabilityChainCatalogValidity:
    @pytest.mark.parametrize("key,chain", list(_CAPABILITY_CHAINS.items()))
    def test_all_aliases_in_catalog(self, key, chain):
        capability, plan_key = key
        missing = [alias for alias in chain if alias not in PROVIDER_CATALOG]
        assert not missing, (
            f"_CAPABILITY_CHAINS[({capability!r}, {plan_key!r})] references aliases "
            f"not in PROVIDER_CATALOG: {missing}"
        )


# ── Invariant 2: no chain exceeds max_failover_attempts+1 entries ─────────────
#
# This was an equality until 2026-09-13. Removing the unreachable
# gemini-25-flash alias left three curated chains shorter than their plan's
# budget, and the freed slots were deliberately NOT refilled — dropping
# another model in would be inventing a routing decision nobody measured.
#
# The bound is the invariant that protects money: a chain longer than the
# budget would authorise more provider calls than the plan pays for. Being
# SHORTER costs nothing and only means fewer fallbacks on the legacy path.
# The exact lengths are pinned below so that shortfall stays visible instead
# of being quietly absorbed by the relaxed comparison.

_EXPECTED_CHAIN_LENGTHS: dict[tuple[str, str], int] = {
    ("files", "start"): 2,   # saturates its budget
    ("files", "pro"):   2,   # budget 3 — lost gemini-25-flash
    ("files", "max"):   3,   # budget 4 — lost gemini-25-flash
    ("code",  "start"): 1,   # budget 2 — lost gemini-25-flash, no fallback left
}


class TestCapabilityChainLengths:
    @pytest.mark.parametrize("key,chain", list(_CAPABILITY_CHAINS.items()))
    def test_chain_never_exceeds_plan_limits(self, key, chain):
        capability, plan_key = key
        limits = get_plan_limits(plan_key)
        budget = limits.max_failover_attempts + 1
        assert len(chain) <= budget, (
            f"_CAPABILITY_CHAINS[({capability!r}, {plan_key!r})] has {len(chain)} providers "
            f"but plan '{plan_key}' pays for at most {budget}"
        )

    @pytest.mark.parametrize("key,chain", list(_CAPABILITY_CHAINS.items()))
    def test_chain_length_is_exactly_what_was_recorded(self, key, chain):
        """Pins the shortfall. A chain getting shorter again — or silently
        refilled — has to come through this table first."""
        assert key in _EXPECTED_CHAIN_LENGTHS, f"new curated chain {key!r} is unrecorded"
        assert len(chain) == _EXPECTED_CHAIN_LENGTHS[key]


# ── Invariant 3: AGENT capability chains require supports_tools=True on all providers

class TestAgentCapabilityChainToolsSupport:
    @pytest.mark.parametrize("key,chain", [
        (k, v) for k, v in _CAPABILITY_CHAINS.items() if k[0] in _AGENT_CAPABILITIES
    ])
    def test_all_providers_support_tools(self, key, chain):
        capability, plan_key = key
        no_tools = [
            alias for alias in chain
            if alias in PROVIDER_CATALOG and not PROVIDER_CATALOG[alias].supports_tools
        ]
        assert not no_tools, (
            f"_CAPABILITY_CHAINS[({capability!r}, {plan_key!r})] includes providers "
            f"without tool support: {no_tools}. AGENT capabilities require tools on "
            f"every failover hop."
        )


# ── Invariant 4 (plan aliases): _PLAN_CHAIN_MAP is complete and maps to valid keys

class TestPlanChainMapCompleteness:
    _EXPECTED_USER_PLANS = {"free", "start", "pro", "max", "team"}
    # "free" is its own dedicated chain key (single-provider, ultra-cheap).
    _VALID_PLAN_KEYS = {"free", "start", "pro", "max"}

    def test_all_expected_user_plans_have_a_mapping(self):
        missing = self._EXPECTED_USER_PLANS - set(_PLAN_CHAIN_MAP.keys())
        assert not missing, f"Plans missing from _PLAN_CHAIN_MAP: {missing}"

    def test_all_mappings_target_a_valid_plan_key(self):
        bad = {
            user_plan: plan_key
            for user_plan, plan_key in _PLAN_CHAIN_MAP.items()
            if plan_key not in self._VALID_PLAN_KEYS
        }
        assert not bad, (
            f"_PLAN_CHAIN_MAP entries with invalid target plan key: {bad}. "
            f"Valid targets: {self._VALID_PLAN_KEYS}"
        )

    def test_free_resolves_to_free(self):
        # FREE has its own dedicated chain (single-provider, no failover risk).
        assert _PLAN_CHAIN_MAP["free"] == "free"

    def test_start_resolves_to_start(self):
        assert _PLAN_CHAIN_MAP["start"] == "start"

    def test_pro_resolves_to_pro(self):
        assert _PLAN_CHAIN_MAP["pro"] == "pro"

    def test_max_resolves_to_max(self):
        assert _PLAN_CHAIN_MAP["max"] == "max"

    def test_team_resolves_to_max(self):
        assert _PLAN_CHAIN_MAP["team"] == "max"
