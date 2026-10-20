"""
CapabilityRouter — dispatch layer between Capability and Executor.

Given a Capability, this module determines which executor type should handle
the request and whether that executor is currently implemented.

This is a pure routing decision layer. It does not call any LLM, provider
API, or executor directly. The actual execution remains delegated to the
appropriate service (ModelRouter for text, specialized routers for multimodal).

Architecture:
    Capability → route() → CapabilityRoute(executor, implemented)

    TEXT capabilities → ExecutorKind.TEXT  → ModelRouter  (implemented)
    IMAGE             → ExecutorKind.IMAGE → ImageRouter  (not implemented)
    VIDEO             → ExecutorKind.VIDEO → VideoRouter  (not implemented)
    AUDIO             → ExecutorKind.AUDIO → AudioRouter  (not implemented)

Services that currently handle the gate logic (sessions.py, agent_service.py)
are not changed — they continue to use UNIMPLEMENTED_CAPABILITIES from
pipelines.py. This module is the architectural foundation for future
consolidation of those checks.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from src.capability.classifier import Capability


class ExecutorKind(str, Enum):
    """The type of executor that should handle a capability."""
    TEXT  = "text"   # ModelRouter — litellm-based text completion (implemented)
    IMAGE = "image"  # ImageRouter — image generation providers    (not yet implemented)
    VIDEO = "video"  # VideoRouter — video generation providers    (not yet implemented)
    AUDIO = "audio"  # AudioRouter — voice synthesis/transcription (not yet implemented)
    # PresentationProvider — a deck engine, not a model call. Its own kind
    # rather than TEXT because the work is a long job that returns a file,
    # and nothing about the text router's request/response shape fits that.
    PRESENTATION = "presentation"


@dataclass(frozen=True)
class CapabilityRoute:
    """Immutable routing decision for a single capability.

    ``implemented`` is True only when a concrete executor exists and is wired
    up. When False, the request should be intercepted with a "coming soon"
    response before reaching any executor.
    """
    capability: Capability
    executor: ExecutorKind
    implemented: bool


# ---------------------------------------------------------------------------
# Routing table — maps each Capability to its ExecutorKind.
# ---------------------------------------------------------------------------
# All text capabilities (CHAT, RESEARCH, CODE, WORK, FILES, AUTOMATION, AGENT)
# go to the existing ModelRouter (TextRouter).
#
# Multimodal capabilities (IMAGE, VIDEO, AUDIO) go to specialized routers that
# are architecturally reserved but have no concrete implementation yet. Their
# routes are defined here so that the dispatch layer is complete — the
# implemented=False flag on those routes signals that execution must be
# intercepted before reaching an executor.
# ---------------------------------------------------------------------------

_CAPABILITY_EXECUTOR: dict[Capability, ExecutorKind] = {
    Capability.CHAT:       ExecutorKind.TEXT,
    Capability.RESEARCH:   ExecutorKind.TEXT,
    Capability.CODE:       ExecutorKind.TEXT,
    Capability.WORK:       ExecutorKind.TEXT,
    Capability.FILES:      ExecutorKind.TEXT,
    Capability.AUTOMATION: ExecutorKind.TEXT,
    Capability.AGENT:      ExecutorKind.TEXT,
    Capability.IMAGE:      ExecutorKind.IMAGE,
    Capability.VIDEO:      ExecutorKind.VIDEO,
    Capability.AUDIO:      ExecutorKind.AUDIO,
    Capability.PRESENTATION: ExecutorKind.PRESENTATION,
}

# Executor kinds that have a concrete, wired implementation today.
# Updating this set is the future commit that activates a new executor.
#
# PRESENTATION is present because a real provider exists and is wired. Note
# that "implemented" is not the same as "available right now": the provider
# also has to be configured and healthy for a given deployment, which
# presentation_service.availability() answers at request time. This flag says
# the code path exists; that check says the environment can run it.
_IMPLEMENTED_EXECUTORS: frozenset[ExecutorKind] = frozenset({
    ExecutorKind.TEXT,
    ExecutorKind.PRESENTATION,
})


def route(capability: Capability) -> CapabilityRoute:
    """Return the routing decision for a capability.

    Never raises — unrecognised capabilities fall back to TEXT (safe default).
    """
    executor = _CAPABILITY_EXECUTOR.get(capability, ExecutorKind.TEXT)
    return CapabilityRoute(
        capability=capability,
        executor=executor,
        implemented=executor in _IMPLEMENTED_EXECUTORS,
    )
