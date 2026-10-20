"""The presentation capability, as the rest of TRINDATRON sees it.

Everything above this module deals in "make me a deck". Below it there is a
provider, an engine, a model and a storage bucket, and none of that reaches
the user or the router.

Three rules this module exists to hold:

  1. The capability is offered only when it can actually run. "Implemented"
     and "available" are different questions — the code path exists in every
     deployment, the engine does not. A capability advertised while its
     engine is unreachable is worse than one that is absent, because the
     user finds out after asking.

  2. A deck is estimated before it starts. It is the first capability whose
     cost is both knowable in advance and much larger than one model call —
     roughly 17 TCU against a per-request reservation of 4 (START) or 8
     (PRO). Reserving the flat figure would recreate the overshoot the
     ADMIT+CLAMP pass removed.

  3. A refusal on quota is final for that attempt. The free Gemini tier was
     measured at 20 requests per DAY and one deck needs more than that;
     retrying harder spends the remainder faster and lengthens the outage.
"""
from __future__ import annotations

import logging

from src.config import settings
from src.entitlements import get_plan_entitlement
from src.observability.metrics import core_metrics
from src.presentation.cost import CostEstimate, estimate, max_slides_within
from src.presentation.presenton import PresentonProvider
from src.presentation.image_policy import ImageDecision
from src.presentation.image_policy import plan_ceiling as image_policy_ceiling
from src.presentation.image_policy import decide as decide_images
from src.presentation.provider import (
    ImageStrategy,
    PresentationProvider,
    ProviderAvailability,
)
from src.storage import build_object_store, local_store_is_acceptable
from src.storage.object_store import ObjectStore

logger = logging.getLogger("trinity.presentation")

_availability_checks = core_metrics.counter(
    "presentation_availability_total",
    "Availability probes for the presentation capability, labeled by outcome.",
)
_estimates = core_metrics.histogram(
    "presentation_estimated_tcu",
    "Pre-flight TCU estimate per presentation request, labeled by plan.",
)


def build_provider() -> PresentationProvider:
    """The configured engine. One provider today, by design — the abstraction
    exists so the engine can be replaced, not so alternatives can be invented
    before anyone needs them."""
    return PresentonProvider(
        base_url=settings.presenton_base_url,
        username=settings.presenton_username,
        password=settings.presenton_password,
        api_key=settings.presenton_api_key,
        timeout_s=settings.presenton_timeout_s,
        wake_timeout_s=settings.presenton_wake_timeout_s,
        wake_poll_interval_s=settings.presenton_wake_poll_interval_s,
    )


async def availability(
    provider: PresentationProvider | None = None,
    *,
    store: ObjectStore | None = None,
) -> ProviderAvailability:
    """Can this deployment produce AND deliver a deck right now?

    Both halves are required, and storage is checked first because it is the
    cheap one: a deck that generates successfully and then has nowhere to be
    stored has spent real money to produce nothing the user can open. Finding
    that out before waking the engine costs a directory check; finding it out
    afterwards costs a full generation.

    Never raises. An unconfigured or unreachable engine is an ordinary answer
    that the capability layer acts on, not an error to propagate.
    """
    store = store or build_object_store()
    # An ephemeral local directory is not a substitute for object storage in
    # production: the deck would be generated, paid for, and then dropped by
    # the next deploy. Declining to offer the capability is the recoverable
    # failure; silently losing a paid-for artifact is not.
    if store.backend_name == "local" and not local_store_is_acceptable():
        _availability_checks.inc(outcome="unavailable")
        logger.error(
            "presentation: unavailable | object storage is not configured in production"
        )
        return ProviderAvailability(False, "object storage not configured")
    if not await store.is_available():
        _availability_checks.inc(outcome="unavailable")
        logger.info("presentation: unavailable | no usable object storage")
        return ProviderAvailability(False, "object storage unavailable")

    provider = provider or build_provider()
    result = await provider.availability()
    _availability_checks.inc(outcome="available" if result.available else "unavailable")
    if not result.available:
        # Operator-facing only. The reason names infrastructure and never
        # reaches a user, who gets an offer of something else instead.
        logger.info("presentation: unavailable | provider=%s reason=%s",
                    provider.name, result.reason)
    return result


def image_strategy_for_plan(plan: str) -> ImageStrategy:
    """The most expensive imagery a plan's economics can carry.

    The plan ceiling alone — what the BRIEF needs is the other half of the
    decision and lives in image_policy.decide(), which takes the stricter of
    the two. Kept as its own function because preflight() below needs the
    ceiling when no brief is in hand yet (a capacity question, not a
    content one).
    """
    return image_policy_ceiling(plan)


def image_decision(*, plan: str, content: str,
                   requested: ImageStrategy | None = None) -> ImageDecision:
    """The full rule, brief included — see src/presentation/image_policy.py."""
    return decide_images(plan=plan, content=content, requested=requested)


def plan_allows_presentations(plan: str) -> bool:
    """Presentations ride on agent mode.

    Not a new entitlement field: a deck is delegated work that runs without
    the user watching, which is precisely what supports_agent_mode already
    gates. FREE has it False, so FREE does not get presentations — and FREE
    could not pay for one anyway, its whole day being 10 TCU against a deck's
    8 at the smallest useful size.
    """
    return get_plan_entitlement(plan).supports_agent_mode


def preflight(
    *,
    plan: str,
    n_slides: int,
    web_search: bool = False,
) -> CostEstimate:
    """Estimate a deck for this plan, before anything is reserved."""
    est = estimate(
        n_slides=n_slides,
        image_strategy=image_strategy_for_plan(plan),
        web_search=web_search,
    )
    _estimates.observe(est.tcu, plan=plan.lower())
    return est


def largest_affordable_deck(*, plan: str, remaining_tcu: float,
                            web_search: bool = False) -> int:
    """How many slides the remaining window can pay for.

    Lets a request be answered with "there is room for six slides" before it
    starts, rather than failing at slide fourteen with the money spent.
    """
    return max_slides_within(
        remaining_tcu,
        image_strategy=image_strategy_for_plan(plan),
        web_search=web_search,
    )
