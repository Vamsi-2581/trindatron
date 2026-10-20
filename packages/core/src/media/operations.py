"""The permanent TRANSFORM/COMPOSE operation contract (CP19).

GENERATE (text->image, text->video, image-edit, image-to-video) keeps using
ImageEngine/VideoEngine/ImageRequest/VideoRequest unchanged — that contract already
works, is fully tested, and every existing provider (Higgsfield, fal, xAI, OpenAI,
Veo, ZeroGPU/Modal/RunPod) is built on it. Rewriting it to fit a shape those
providers don't need would touch every one of them for no functional gain.

TRANSFORM and COMPOSE need one thing GENERATE's shape cannot express: more than one
typed input (a character + a driving video; an image + an audio clip). That is the
one real gap, and this module is the permanent, one-time fix for it: a future model
(Animate, S2V, SeedVR2, Qwen-Image-Edit, Real-ESRGAN, USO, a logo/caption compose
step) is a new OperationEngine + a registry entry, never a new field here.

`OperationEngine` is deliberately the same shape as ImageEngine/VideoEngine
(is_available / estimate_usd / a run method) so it plugs into the existing,
unmodified orchestrator ranking (media/orchestrator.py rank()/run_chain() are
already duck-typed — they were never ImageEngine-specific) via OperationEngineChain
below. The model/provider is the engine implementation; the operation is the
contract every implementation of it shares.

Artifact-first: `inputs` carries artifact ids, never bytes. media_service resolves
each id to bytes (from the one ObjectStore every capability already uses) right
before calling an engine, and never earlier — so a request that is refused before
execution never pays to move a blob between layers.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import NamedTuple

from src.media.engines import EngineUnavailable, MediaOutput, ProviderLink
from src.media.orchestrator import run_chain, rank

#: Known operation names. Adding a capability means adding a string here and an
#: engine that implements it — never touching OperationRequest/OperationEngine.
TRANSFORM_OPERATIONS: frozenset[str] = frozenset({"background_remove", "vectorize", "character_animate",
                                                     "video_edit"})

#: role -> the Artifact.kind that role's input must already be, per operation. Checked at
#: creation time for EVERY declared input (not just the primary one) before anything is
#: reserved. A role missing from an operation's map here is refused as unknown; a required role
#: (every key in the map) missing from the request is refused as incomplete. New operation, new
#: entry here — never a change to OperationInput/OperationRequest themselves (CP22).
OPERATION_INPUT_KINDS: dict[str, dict[str, str]] = {
    "background_remove": {"source_image": "image"},
    "vectorize": {"source_image": "image"},
    "character_animate": {"character": "image", "driving_video": "video"},
    "video_edit": {"source_video": "video"},
}

#: operation -> the MediaJob.kind (and therefore output Artifact.kind) its result is stored as.
#: Drives capability gating, the daily quota bucket, and the "one active job per kind" rule —
#: never a second billing/quota mechanism (CP22).
OPERATION_OUTPUT_KIND: dict[str, str] = {
    "background_remove": "image",
    "vectorize": "image",
    "character_animate": "video",
    "video_edit": "video",
}


class ResolvedInput(NamedTuple):
    """One input's bytes and content type, resolved from its artifact right before an engine runs."""

    data: bytes
    content_type: str


@dataclass(frozen=True)
class OperationInput:
    """One artifact playing one role in an operation. `role` is an open string on
    purpose ("source_image", "driving_video", "audio", "character", "style_reference",
    "mask", "first_frame", "last_frame", "logo", "background", ...) — a new role
    never requires a schema or migration change, only an engine that reads it."""

    role: str
    artifact_id: str


@dataclass(frozen=True)
class OperationRequest:
    operation: str
    inputs: tuple[OperationInput, ...]
    parameters: dict = field(default_factory=dict)  # operation-specific knobs, read by the engine that needs them
    quality_tier: str = "standard"
    budget_usd: float | None = None
    # CP38: the job this request belongs to, for an OperationEngine that submits to a provider queue
    # (e.g. HiggsfieldGenjutsuEngine) — the exact same field ImageRequest/VideoRequest already carry
    # (media/engines.py), reused here rather than inventing a second mechanism. None for Adapter x
    # Runner operations (BiRefNet, Wan22Animate), which have no provider queue to report back to.
    link: ProviderLink | None = field(default=None, compare=False, repr=False)

    def input_for(self, role: str) -> OperationInput | None:
        return next((i for i in self.inputs if i.role == role), None)


class OperationEngine(ABC):
    """One operation, on one model/provider. Adapter x Runner (media/adapters.py,
    media/runners/) still decides model vs infrastructure underneath a concrete
    implementation of this — this ABC is only the contract every operation shares."""

    operation: str

    @abstractmethod
    async def is_available(self) -> bool:
        """Configured and reachable. Never raises."""

    @abstractmethod
    async def run(self, request: OperationRequest, resolved: dict[str, ResolvedInput]) -> MediaOutput:
        """`resolved` is role -> that input's (data, content_type), fetched by the caller
        (artifact-first) from the artifact each OperationInput names."""

    def estimate_usd(self, request: OperationRequest) -> float | None:
        return None


class OperationEngineChain(OperationEngine):
    """Many engines for the same operation, ranked and failed-over by the same
    deterministic rules every other media chain uses (media/orchestrator.py)."""

    def __init__(self, engines: list[OperationEngine]) -> None:
        if not engines:
            raise ValueError("an operation chain needs at least one engine")
        self.engines = list(engines)
        self.operation = engines[0].operation

    async def is_available(self) -> bool:
        for engine in self.engines:
            try:
                if await engine.is_available():
                    return True
            except Exception:
                continue
        return False

    def estimate_usd(self, request: OperationRequest) -> float | None:
        ranked, _ = rank(self.engines, estimate=lambda engine: engine.estimate_usd(request),
                         quality_tier=request.quality_tier)
        return ranked[0].estimate_usd(request) if ranked else None

    def headroom_usd(self, request: OperationRequest, affordable) -> float | None:
        """The most any eligible engine in the chain would cost, among those `affordable(usd)` allows."""
        ranked, _ = rank(self.engines, estimate=lambda engine: engine.estimate_usd(request),
                         quality_tier=request.quality_tier)
        costs = [cost for cost in (engine.estimate_usd(request) for engine in ranked)
                 if cost is not None and affordable(cost)]
        return max(costs) if costs else self.estimate_usd(request)

    async def run(self, request: OperationRequest, resolved: dict[str, bytes]) -> MediaOutput:
        if not self.engines:
            raise EngineUnavailable("no engine for operation")
        output, trace = await run_chain(
            self.engines,
            available=lambda engine: engine.is_available(),
            estimate=lambda engine: engine.estimate_usd(request),
            call=_Budgeted(lambda engine: engine.run(request, resolved), request.budget_usd),
            timeout=None, what=f"operation:{request.operation}", quality_tier=request.quality_tier,
        )
        return replace(output, trace=tuple(a.as_dict() for a in trace))


class _Budgeted:
    """Mirrors orchestrator._Budgeted — a call bound to one request, carrying the budget the chain must respect."""

    def __init__(self, fn, budget: float | None) -> None:
        self._fn, self.budget = fn, budget

    def __call__(self, engine):
        return self._fn(engine)
