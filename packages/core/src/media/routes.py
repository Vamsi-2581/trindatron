"""Routes: one engine on one runner — the unit the orchestrator chooses between.

    VideoRoute(WAN22_TI2V_5B, RunPodRunner())   "wan22_ti2v_5b@runpod"
    VideoRoute(WAN22_TI2V_5B, ModalRunner())    "wan22_ti2v_5b@modal"
    VideoRoute(LTX_VIDEO_13B_DISTILLED, ...)    "ltx_video_13b_distilled@modal"
    HostedVideoRoute(XaiVideoEngine(), ...)     "grok_imagine_video@xai"

A route is an ImageEngine / VideoEngine, so media_service keeps calling "the
engine" and never learns which model ran where. A route carries what ranking
needs — engine id, runner name, quality grade, whether the infrastructure is
commercial or experimental — and refuses to run when the engine's licence does
not allow it (engine_catalog.license_permits) or when experimental
infrastructure is asked for in production.

Hosted APIs (media/hosted.py, media/xai.py) are model and infrastructure at
once; HostedVideoRoute / HostedImageRoute give them the same identity so they
rank, fall back and cool down exactly like engine@runner pairs.

Labels are internal (logs, job traces, health). Nothing here reaches a user.
"""
from __future__ import annotations

from dataclasses import replace

from src.config import settings as core_settings
from src.media import engine_catalog
from src.media.adapters import ImageAdapter, OperationAdapter, VideoAdapter
from src.media.engines import (
    EngineUnavailable,
    ImageEngine,
    ImageRequest,
    MediaOutput,
    VideoEngine,
    VideoRequest,
)
from src.media.operations import OperationEngine, OperationRequest
from src.media.runners.base import IMAGE_EDIT, IMAGE_TO_VIDEO, TEXT_TO_IMAGE, TEXT_TO_VIDEO, Runner
from src.media.settings import media_settings


def experimental_allowed(experimental: bool) -> bool:
    return not experimental or (media_settings().media_allow_experimental_runners and not core_settings.is_production)


class _Identity:
    engine_id: str
    runner_name: str
    quality: int | None
    commercial: bool
    experimental: bool

    @property
    def label(self) -> str:
        return f"{self.engine_id}@{self.runner_name}"

    def eligible(self) -> bool:
        """Licence and infrastructure policy allow this route at all (no network)."""
        return engine_catalog.license_permits(self.engine_id) and experimental_allowed(self.experimental)

    def supports_task(self, task: str) -> bool:
        """Whether this route can do the task at all (no network). Hosted APIs answer per call."""
        can = getattr(self, "_can", None)
        return can(task) if can is not None else self.eligible()

    def _tag(self, output: MediaOutput) -> MediaOutput:
        return replace(output, metrics={**(output.metrics or {}), "route": self.label})


class VideoRoute(_Identity, VideoEngine):
    def __init__(self, adapter: VideoAdapter, runner: Runner) -> None:
        self.adapter, self.runner = adapter, runner
        self.engine_id, self.runner_name = adapter.id, runner.name
        self.quality, self.commercial, self.experimental = adapter.quality, runner.commercial, runner.experimental

    def _can(self, task: str) -> bool:
        return self.eligible() and self.adapter.supports(task) and self.runner.supports(self.adapter.id, task)

    async def is_available(self, *, from_image: bool = False) -> bool:
        if not self._can(IMAGE_TO_VIDEO if from_image else TEXT_TO_VIDEO):
            return False
        try:
            return await self.runner.is_available_for(self.adapter.id)
        except Exception:
            return False

    def normalize(self, request: VideoRequest) -> VideoRequest:
        return self.adapter.normalize(request)

    def estimate_usd(self, request: VideoRequest) -> float | None:
        normalized = self.adapter.normalize(request)
        return self.runner.estimate_usd(self.adapter.id, self.adapter.estimate_gpu_seconds(normalized))

    async def generate(self, request: VideoRequest) -> MediaOutput:
        normalized = self.adapter.normalize(request)
        task = IMAGE_TO_VIDEO if normalized.source is not None else TEXT_TO_VIDEO
        if not self._can(task):
            raise EngineUnavailable(f"{self.label}: {task} not allowed here")
        result = await self.runner.run(self.adapter.job(normalized))
        return self._tag(MediaOutput(
            data=result.data, content_type=result.content_type, width=result.width or normalized.width,
            height=result.height or normalized.height,
            duration_seconds=result.duration_seconds or float(normalized.duration_seconds),
            engine_seconds=result.gpu_seconds if result.gpu_seconds is not None else result.total_seconds,
            cost_usd=result.cost_usd, metrics=result.metrics(),
        ))


class ImageRoute(_Identity, ImageEngine):
    def __init__(self, adapter: ImageAdapter, runner: Runner) -> None:
        self.adapter, self.runner = adapter, runner
        self.engine_id, self.runner_name = adapter.id, runner.name
        self.quality, self.commercial, self.experimental = adapter.quality, runner.commercial, runner.experimental

    def _can(self, task: str) -> bool:
        return self.eligible() and self.adapter.supports(task) and self.runner.supports(self.adapter.id, task)

    async def is_available(self, *, editing: bool = False) -> bool:
        if not self._can(IMAGE_EDIT if editing else TEXT_TO_IMAGE):
            return False
        try:
            return await self.runner.is_available_for(self.adapter.id)
        except Exception:
            return False

    def estimate_usd(self, request: ImageRequest) -> float | None:
        return self.runner.estimate_usd(self.adapter.id, self.adapter.estimate_gpu_seconds(request))

    async def generate(self, request: ImageRequest) -> MediaOutput:
        normalized = self.adapter.normalize(request)
        task = IMAGE_EDIT if normalized.source is not None else TEXT_TO_IMAGE
        if not self._can(task):
            raise EngineUnavailable(f"{self.label}: {task} not allowed here")
        result = await self.runner.run(self.adapter.job(normalized))
        return self._tag(MediaOutput(
            data=result.data, content_type=result.content_type, width=result.width or normalized.width,
            height=result.height or normalized.height, duration_seconds=None,
            engine_seconds=result.gpu_seconds if result.gpu_seconds is not None else result.total_seconds,
            cost_usd=result.cost_usd, metrics=result.metrics(),
        ))


class HostedVideoRoute(_Identity, VideoEngine):
    def __init__(self, engine: VideoEngine, *, engine_id: str, runner_name: str, quality: int | None,
                 commercial: bool = True, experimental: bool = False) -> None:
        self.engine = engine
        self.engine_id, self.runner_name, self.quality = engine_id, runner_name, quality
        self.commercial, self.experimental = commercial, experimental

    async def is_available(self, *, from_image: bool = False) -> bool:
        return self.eligible() and await self.engine.is_available(from_image=from_image)

    def normalize(self, request: VideoRequest) -> VideoRequest:
        return self.engine.normalize(request)

    def estimate_usd(self, request: VideoRequest) -> float | None:
        return self.engine.estimate_usd(request)

    async def generate(self, request: VideoRequest) -> MediaOutput:
        if not self.eligible():
            raise EngineUnavailable(f"{self.label}: not allowed")
        return self._tag(await self.engine.generate(request))


class HostedImageRoute(_Identity, ImageEngine):
    def __init__(self, engine: ImageEngine, *, engine_id: str, runner_name: str, quality: int | None,
                 commercial: bool = True, experimental: bool = False) -> None:
        self.engine = engine
        self.engine_id, self.runner_name, self.quality = engine_id, runner_name, quality
        self.commercial, self.experimental = commercial, experimental

    async def is_available(self, *, editing: bool = False) -> bool:
        return self.eligible() and await self.engine.is_available(editing=editing)

    def estimate_usd(self, request: ImageRequest) -> float | None:
        return self.engine.estimate_usd(request)

    def typical_usd(self, request: ImageRequest) -> float | None:
        return self.engine.typical_usd(request)

    async def generate(self, request: ImageRequest) -> MediaOutput:
        if not self.eligible():
            raise EngineUnavailable(f"{self.label}: not allowed")
        return self._tag(await self.engine.generate(request))


class OperationRoute(_Identity, OperationEngine):
    """CP19: one operation model (media/adapters.py OperationAdapter) on one runner —
    the TRANSFORM/COMPOSE equivalent of ImageRoute/VideoRoute, same Adapter x Runner split."""

    def __init__(self, adapter: OperationAdapter, runner: Runner) -> None:
        self.adapter, self.runner = adapter, runner
        self.engine_id, self.runner_name = adapter.id, runner.name
        self.quality, self.commercial, self.experimental = adapter.quality, runner.commercial, runner.experimental
        self.operation = adapter.operation

    def _can(self) -> bool:
        return self.eligible() and self.runner.supports(self.adapter.id, self.adapter.operation)

    async def is_available(self) -> bool:
        if not self._can():
            return False
        try:
            return await self.runner.is_available_for(self.adapter.id)
        except Exception:
            return False

    def estimate_usd(self, request: OperationRequest) -> float | None:
        return self.runner.estimate_usd(self.adapter.id, self.adapter.gpu_seconds_per_call)

    async def run(self, request: OperationRequest, resolved: dict) -> MediaOutput:
        if not self._can():
            raise EngineUnavailable(f"{self.label}: {self.adapter.operation} not allowed here")
        result = await self.runner.run(self.adapter.job(request, resolved))
        return self._tag(MediaOutput(
            data=result.data, content_type=result.content_type, width=result.width, height=result.height,
            duration_seconds=None, engine_seconds=result.gpu_seconds if result.gpu_seconds is not None else result.total_seconds,
            cost_usd=result.cost_usd, metrics=result.metrics(),
        ))


class HostedOperationRoute(_Identity, OperationEngine):
    """CP35: a TRANSFORM/COMPOSE operation served by a hosted API instead of Adapter x Runner —
    the OperationRoute equivalent of HostedVideoRoute/HostedImageRoute (registry.py's own
    build_operation_route comment names this exact case: "the day an operation is served by a
    hosted API instead")."""

    def __init__(self, engine: OperationEngine, *, engine_id: str, runner_name: str, quality: int | None,
                 commercial: bool = True, experimental: bool = False) -> None:
        self.engine = engine
        self.engine_id, self.runner_name, self.quality = engine_id, runner_name, quality
        self.commercial, self.experimental = commercial, experimental
        self.operation = engine.operation

    async def is_available(self) -> bool:
        return self.eligible() and await self.engine.is_available()

    def estimate_usd(self, request: OperationRequest) -> float | None:
        return self.engine.estimate_usd(request)

    async def run(self, request: OperationRequest, resolved: dict) -> MediaOutput:
        if not self.eligible():
            raise EngineUnavailable(f"{self.label}: not allowed")
        return self._tag(await self.engine.run(request, resolved))
