"""Provider failure classification + bounded exponential backoff with jitter.

Etapa 5E — Provider Resilience.

This module adds a CONTROL-FLOW dimension the router did not previously
have. Before this etapa, router.py's `_sanitize_error()` already turned
every exception into a safe, human-readable string (used for audit/
failover_reason) — but every exception, regardless of what it actually
meant, drove the exact same behavior: move to the next chain candidate,
immediately, with no delay. `classify_failure()` below adds the missing
decision layer on top of that existing sanitizer (never replacing it —
`_sanitize_error()` still produces the string shown in
RouterAuditEvent.failover_reason).

Three-way classification, not a simple retryable/non-retryable boolean:

  RETRYABLE               Transient — rate limit (429), overload/capacity,
                           connection/timeout/network errors, and any
                           unrecognized error (the pre-5E default: try the
                           next candidate). Backoff is applied before the
                           next attempt.
  NON_RETRYABLE_PROVIDER   This SPECIFIC provider's credentials/config are
                           broken — retrying the identical provider would
                           fail identically, but a DIFFERENT provider has
                           independent credentials and is not implicated by
                           this failure at all, so the chain still moves on
                           to the next candidate. No backoff (there is no
                           reason to believe waiting helps this provider,
                           and we are not retrying it anyway).
  NON_RETRYABLE_REQUEST    The failure is about the REQUEST CONTENT itself
                           (malformed request, a capability the provider
                           flatly does not support, a policy/safety
                           refusal) — trying a DIFFERENT provider with the
                           identical content would very likely fail the
                           same way (or, for a safety refusal, produce an
                           inconsistent policy outcome across providers).
                           The entire chain attempt is aborted immediately
                           via RequestRejectedByProviderError — no further
                           candidates are tried, no cost is wasted chasing
                           a failure that is not provider-specific.

Backoff (`backoff_delay`) is bounded, exponential, jittered, and purely
`await asyncio.sleep(...)`-based — it never blocks the event loop. It only
ever runs between chain candidates (this router already never retries the
same candidate more than once — see router.py's docstring — so "same
provider/model repeated indiscriminately" cannot happen structurally, not
just by convention).
"""
from __future__ import annotations

import asyncio
import random
from enum import Enum
from typing import Awaitable, Callable


class FailureClass(str, Enum):
    RETRYABLE = "retryable"
    NON_RETRYABLE_PROVIDER = "non_retryable_provider"
    NON_RETRYABLE_REQUEST = "non_retryable_request"


def classify_failure(exc: BaseException) -> FailureClass:
    """Classify a provider-call exception for retry/failover control flow.

    Checked in this order deliberately: request-content failures first (so
    e.g. an "InvalidRequestError" is never miscategorized by a looser
    substring match further down), then provider-credential failures, then
    everything else defaults to RETRYABLE — preserving this router's
    pre-5E behavior of always trying the next candidate for any
    unrecognized exception.
    """
    name = type(exc).__name__.lower()
    message = str(exc).lower()

    if any(t in name for t in ("invalidrequest", "badrequest", "unsupportedcapab", "validation")):
        return FailureClass.NON_RETRYABLE_REQUEST
    if "content" in name and any(t in name for t in ("polic", "safety", "filter")):
        return FailureClass.NON_RETRYABLE_REQUEST
    if ("polic" in message or "safety" in message) and ("refus" in message or "block" in message or "violat" in message):
        return FailureClass.NON_RETRYABLE_REQUEST

    if any(t in name for t in ("auth", "credential", "permission", "forbidden")):
        return FailureClass.NON_RETRYABLE_PROVIDER

    return FailureClass.RETRYABLE


async def backoff_delay(
    attempt_index: int,
    *,
    base_s: float = 0.1,
    cap_s: float = 1.0,
    jitter_ratio: float = 0.5,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> float:
    """Bounded exponential backoff with jitter before the next failover
    attempt. `attempt_index` is 0 for the delay before the SECOND overall
    attempt, 1 before the third, etc.

    Delegates the actual wait to `sleep_fn` (asyncio.sleep by default) —
    always `await`ed, never a blocking call, and injectable so callers/tests
    can substitute a fast/instrumented fake without real wall-clock delay.

    Returns the total delay actually awaited (base + jitter), useful for
    tests asserting the backoff grows exponentially and includes jitter.
    """
    delay = min(cap_s, base_s * (2 ** attempt_index))
    jitter = random.uniform(0, delay * jitter_ratio)
    total = delay + jitter
    await sleep_fn(total)
    return total
