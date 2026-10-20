"""What a provider actually bills, as distinct from the list price that sizes TCU.

TCU is the product's unit of capacity: it is priced from each model's published
list price (trinity_router.providers) so a plan's windows mean the same thing
whichever model served a request. What TRINDATRON actually *pays* can be lower:
a provider account on a free tier bills nothing for calls inside its limits.

Recording list price as spend would overstate real cost (spend_guard,
admin economics); charging the person nothing would change what a plan's
windows are worth depending on an internal account setting. So both are kept:
UsageRecord.cost_usd / tcu stay at list price, and UsageRecord.provider_cost_usd
holds the billed amount when it differs.

Configuration is explicit, not inferred at runtime: GROQ_BILLING_TIER=free when
the Groq organization is on the Free plan (verified from its rate-limit headers:
1K requests/day, 8K tokens/minute on gpt-oss-20b), "developer" (the default,
which never under-reports) once it pays per token.
"""
from __future__ import annotations

from src.config import settings

FREE, DEVELOPER = "free", "developer"


def _is_groq(model: str) -> bool:
    name = (model or "").strip().lower()
    return name.startswith("groq/") or name.startswith("groq:")


def billed_cost_usd(model: str, list_cost_usd: float) -> float:
    """The amount the provider bills for a call recorded under `model` at `list_cost_usd`."""
    if _is_groq(model) and (settings.groq_billing_tier or DEVELOPER).strip().lower() == FREE:
        return 0.0
    return list_cost_usd


def provider_cost_or_none(model: str, list_cost_usd: float) -> float | None:
    """The value for UsageRecord.provider_cost_usd: None when billing equals list price."""
    billed = billed_cost_usd(model, list_cost_usd)
    return None if abs(billed - list_cost_usd) < 1e-12 else billed
