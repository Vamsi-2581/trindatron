"""
Provider chain selector.

The selection is determined entirely server-side from (task_type, user_plan).
Clients cannot influence which provider or model is selected.

Economic tiers used in chain design:
  ULTRA_LOW  groq-llama4-scout     $0.075/$0.30 CHAT only (GPT-OSS 20B on Groq, 131K ctx)
  LOW        gpt-4o-mini           $0.10/$0.50  CHAT + AGENT (gpt-6-luna), 922K ctx
  LOW        gemini-25-flash-lite  $0.30/$2.50  CHAT + files, 1M ctx, tools OK
  STANDARD   claude-sonnet         $2.00/$10.00 AGENT primary, complex CHAT
  STANDARD   gpt-4o                $2.00/$10.00 PRO fallback (gpt-6.1-sol)
  PREMIUM    claude-opus           $5.00/$25.00 MAX primary

Routing principles:
  - CHAT: prefer cheapest model that produces correct output for the task.
    Simple conversational tasks route to ULTRA_LOW; complex tasks fail over
    to STANDARD as needed. This is automatic via the failover chain.
  - AGENT: reliability > cost. Tool-calling requires models proven to follow
    function-call format correctly. Keep claude-sonnet as primary.
  - FREE plan: single-provider chain (max_failover_attempts=0). Must use a
    reliably-configured provider — gemini-25-flash-lite (GEMINI_API_KEY always set).
    groq-llama4-scout NOT used as FREE primary: if GROQ_API_KEY is absent the
    only chance fails with no recovery.
  - START plan: groq-llama4-scout primary → gemini-25-flash-lite fallback for CHAT.
    If GROQ_API_KEY is not configured, the Groq call fails and the router
    automatically falls over to gemini-25-flash-lite. No user-visible impact.
"""
from __future__ import annotations

from enum import Enum


class TaskType(str, Enum):
    CHAT = "chat"     # conversational exchange without tool use
    AGENT = "agent"   # ReAct loop with tool calls


# ---------------------------------------------------------------------------
# Provider chains: (task_type, plan_key) → ordered list of provider keys.
#
# First key is the primary provider. Subsequent keys are failover candidates.
# The number of keys tried = PlanLimits.max_failover_attempts + 1.
#
# Server-side policy only — not configurable by clients.
# Aliases must exist in PROVIDER_CATALOG (absent aliases are silently skipped).
# claude-haiku: in catalog but reserved for intent-classification (pre-router).
# ---------------------------------------------------------------------------

# 2026-09-13: gemini-25-flash was removed from PROVIDER_CATALOG as unreachable
# (its blended cost ranked 6th; chains hold at most 4). Its slot in the PRO and
# MAX chains below is refilled with gemini-25-flash-lite rather than left empty,
# because these chains carry two deliberate reliability invariants that the
# removal would otherwise have broken silently: PRO must keep a Google model as
# a THIRD-provider fallback (test_pro_has_gemini_fallback) and MAX must keep
# four candidates (test_max_has_four_providers). The -lite entry is not a new
# model — it is already the third slot of the dynamic chain for both plans.
_PROVIDER_CHAINS: dict[tuple[TaskType, str], list[str]] = {
    # ── FREE — max_failover_attempts=0 → single provider, no failover. ────
    # Use gemini-25-flash-lite: reliably configured (GEMINI_API_KEY), ultra-cheap
    # ($0.10/$0.40 per 1M), 1M context, tools OK.
    # NOT groq-llama4-scout: if GROQ_API_KEY is absent the only shot fails.
    (TaskType.CHAT,  "free"):  ["gemini-25-flash-lite"],
    (TaskType.AGENT, "free"):  ["gemini-25-flash-lite"],

    # ── START — max_failover_attempts=1 → 2 providers total. ─────────────
    # CHAT: groq-llama4-scout ($0.075/$0.30) primary — cheapest available.
    #   Fallback: gemini-25-flash-lite ($0.10/$0.40) if Groq fails or key absent.
    # AGENT: claude-sonnet primary — tool-calling reliability > cost.
    (TaskType.CHAT,  "start"): ["groq-llama4-scout", "gemini-25-flash-lite"],
    (TaskType.AGENT, "start"): ["claude-sonnet",  "gpt-4o-mini"],

    # ── PRO — max_failover_attempts=2 → 3 providers total. ───────────────
    (TaskType.CHAT,  "pro"):   ["claude-sonnet", "gpt-4o", "gemini-25-flash-lite"],
    (TaskType.AGENT, "pro"):   ["claude-sonnet", "gpt-4o", "gemini-25-flash-lite"],

    # ── MAX — max_failover_attempts=3 → 4 providers total. ───────────────
    (TaskType.CHAT,  "max"):   ["claude-opus", "claude-sonnet", "gpt-4o", "gemini-25-flash-lite"],
    (TaskType.AGENT, "max"):   ["claude-opus", "claude-sonnet", "gpt-4o", "gemini-25-flash-lite"],
}

_DEFAULT_CHAIN: list[str] = ["gemini-25-flash-lite", "gpt-4o-mini"]

# Maps every valid user plan to the chain key that governs its routing.
# FREE now has its own key (dedicated single-provider chain).
# TEAM routes like MAX (premium chain; workspace features live in Entitlement layer).
_PLAN_CHAIN_MAP: dict[str, str] = {
    "free":  "free",
    "start": "start",
    "pro":   "pro",
    "max":   "max",
    "team":  "max",
}

# ---------------------------------------------------------------------------
# Capability-specific chains: (capability_value, plan_key) → ordered list.
#
# Only capabilities with genuine provider requirements that differ from the
# TaskType chains appear here. Everything else falls back to _PROVIDER_CHAINS.
#
# FILES: short-context fallbacks (gpt-4o-mini: 128K, groq-llama4-scout: 128K)
# are replaced by long-context alternatives. File parsing against a context
# window smaller than the file causes silent truncation or hard errors.
# gemini-25-flash-lite (1M ctx) is correct here.
# Chain lengths are kept equal to max_failover_attempts+1 per plan.
# ---------------------------------------------------------------------------

_CAPABILITY_CHAINS: dict[tuple[str, str], list[str]] = {
    # FILES — replace short-context models with long-context alternatives.
    # free: no explicit entry — falls back to _PROVIDER_CHAINS[(task, "free")]
    #   = ["gemini-25-flash-lite"] (1M ctx, adequate for files on the single-provider plan).
    ("files", "start"): ["claude-sonnet",  "gemini-25-flash-lite"],
    ("files", "pro"):   ["claude-sonnet",  "gemini-25-flash-lite"],
    ("files", "max"):   ["claude-opus",    "claude-sonnet",   "gemini-25-flash-lite"],

    # CODE — groq-llama4-scout is weak at code, so it is not used for start here.
    # free: no explicit entry — falls back to _PROVIDER_CHAINS[(task, "free")]
    #   = ["gemini-25-flash-lite"] (decent code quality for free plan).
    # The second slot held gemini-25-flash until 2026-09-13. It is removed,
    # not replaced: filling the slot with another model would be inventing
    # a routing decision nobody measured. START keeps claude-sonnet alone
    # on this legacy path; the dynamic path (which is what actually runs)
    # serves code/start from groq-llama4-scout -> gpt-4o-mini regardless.
    ("code", "start"): ["claude-sonnet"],
}


def select_provider_chain(
    task_type: TaskType,
    user_plan: str,
    capability: str | None = None,
) -> list[str]:
    """Return an ordered list of provider keys for the given task and plan.

    The selection is purely server-side. User input does not affect it.
    Returns a copy so callers cannot mutate the source list.

    When ``capability`` matches an entry in ``_CAPABILITY_CHAINS``, that chain
    takes priority over the TaskType-based chain. Unknown capabilities and
    ``capability=None`` fall back to ``_PROVIDER_CHAINS`` as before.
    """
    plan_key = _PLAN_CHAIN_MAP.get(user_plan.lower(), "start")

    if capability is not None:
        cap_chain = _CAPABILITY_CHAINS.get((capability, plan_key))
        if cap_chain is not None:
            return list(cap_chain)

    chain = _PROVIDER_CHAINS.get((task_type, plan_key), _DEFAULT_CHAIN)
    return list(chain)
