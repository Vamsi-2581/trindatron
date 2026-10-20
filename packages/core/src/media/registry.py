"""Which routes serve each operation in this deployment — decided by configuration.

Preferred: an ordered list of "engine@runner" routes per operation
(MEDIA_VIDEO_ROUTES / MEDIA_IMAGE_ROUTES). Every listed route that can be built
joins one chain (media/orchestrator.py), which ranks them per request and falls
back across engines and runners:

    MEDIA_VIDEO_ROUTES="wan22_ti2v_5b@runpod,wan22_ti2v_5b@modal,ltx_video_13b_distilled@modal,grok_imagine_video@xai"

  engine ids   media/adapters.py (open-weight models) and HOSTED_* below (APIs)
  runner names zerogpu (experimental) | modal | runpod

Higgsfield routes (soul_2@higgsfield, image_edit@higgsfield, wan_3@higgsfield,
kling_3_std@higgsfield, grok_imagine_video_1_5@higgsfield) join a chain only when
listed here, and run only with credentials and the accepted API-terms condition.

Legacy: MEDIA_IMAGE_ENGINE="openai,xai" / MEDIA_VIDEO_ENGINE="xai,veo" still
work; each name becomes its hosted route. Unknown names are ignored rather than
guessed.

Tests (and only tests) replace the engines with `use_engines()`.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Iterator

from src.config import settings
from src.media.adapters import IMAGE_ADAPTERS, OPERATION_ADAPTERS, VIDEO_ADAPTERS
from src.media.comfyui import ComfyUIImageEngine, ComfyUIVideoEngine
from src.media.dev_engine import DevelopmentImageEngine
from src.media.engines import ImageEngine, VideoEngine, VisionEngine
from src.media.higgsfield import HiggsfieldGenjutsuEngine, HiggsfieldImageEngine, HiggsfieldVideoEngine
from src.media.hosted import FalImageEngine, FalVideoEngine, FalWanAnimateEngine, OpenAIImageEngine, VeoVideoEngine
from src.media.operations import OperationEngine, OperationEngineChain
from src.media.orchestrator import ImageEngineChain, VideoEngineChain, VisionEngineChain
from src.media.routes import (
    HostedImageRoute,
    HostedOperationRoute,
    HostedVideoRoute,
    ImageRoute,
    OperationRoute,
    VideoRoute,
)
from src.media.runners.base import Runner
from src.media.runners.gpu_worker import ModalRunner, RunPodRunner
from src.media.runners.zerogpu import ZeroGPURunner
from src.media.settings import media_settings
from src.media.video_edit import FfmpegVideoEditEngine
from src.media.vision import GeminiVisionEngine
from src.media.xai import XaiImageEngine, XaiVideoEngine

_UNSET = object()
_image_override: object = _UNSET
_video_override: object = _UNSET
_vision_override: object = _UNSET
_operation_override: object = _UNSET

RUNNERS: dict[str, Callable[[], Runner]] = {
    "zerogpu": ZeroGPURunner,
    "modal": ModalRunner,
    "runpod": RunPodRunner,
}

# Hosted APIs are model and infrastructure at once. Quality grades: media/adapters.py.
HOSTED_VIDEO_ROUTES: dict[str, Callable[[], VideoEngine]] = {
    "wan22_a14b@fal": lambda: HostedVideoRoute(FalVideoEngine(), engine_id="wan22_a14b", runner_name="fal", quality=4),
    "grok_imagine_video@xai": lambda: HostedVideoRoute(XaiVideoEngine(), engine_id="grok_imagine_video",
                                                       runner_name="xai", quality=4),
    "veo_3_1_lite@gemini": lambda: HostedVideoRoute(VeoVideoEngine(), engine_id="veo_3_1_lite",
                                                    runner_name="gemini", quality=5),
    "comfyui_workflow@comfyui": lambda: HostedVideoRoute(ComfyUIVideoEngine(), engine_id="comfyui_workflow",
                                                         runner_name="comfyui", quality=None),
    # Higgsfield (media/higgsfield.py): unavailable without credentials, and only the verified shapes.
    "wan_3@higgsfield": lambda: HostedVideoRoute(HiggsfieldVideoEngine("wan_3@higgsfield"),
                                                 engine_id="higgsfield_wan_3", runner_name="higgsfield", quality=4),
    "kling_3_std@higgsfield": lambda: HostedVideoRoute(HiggsfieldVideoEngine("kling_3_std@higgsfield"),
                                                       engine_id="higgsfield_kling_3_std", runner_name="higgsfield",
                                                       quality=4),
    "grok_imagine_video_1_5@higgsfield": lambda: HostedVideoRoute(
        HiggsfieldVideoEngine("grok_imagine_video_1_5@higgsfield"), engine_id="higgsfield_grok_imagine_video_1_5",
        runner_name="higgsfield", quality=4),
}
# CP35: character_animate served by a hosted API (Higgsfield Genjutsu) instead of Adapter x Runner —
# build_operation_route() below checks this dict first, exactly like HOSTED_VIDEO_ROUTES/HOSTED_IMAGE_ROUTES.
# CP46: a second character_animate route, fal's Wan 2.2 Animate Move — listing both in
# MEDIA_OPERATION_ROUTES joins them into one OperationEngineChain (registry.operation_engines()
# below), and orchestrator.rank() already orders a chain by estimated cost ascending, so fal (its
# real per-request cost is a fraction of Higgsfield's for the same input — CP46's own cost table)
# is tried first automatically; Higgsfield is only reached if fal is unavailable, over its own
# media budget, or fails in a way explicitly marked retryable (FalWanAnimateEngine.run() marks
# every post-submit failure NOT retryable, specifically to prevent this chain from ever risking a
# second paid job after fal may already have billed one).
HOSTED_OPERATION_ROUTES: dict[str, Callable[[], OperationEngine]] = {
    "genjutsu_motion_transfer@higgsfield": lambda: HostedOperationRoute(
        HiggsfieldGenjutsuEngine(), engine_id="genjutsu_motion_transfer", runner_name="higgsfield", quality=4),
    "wan_animate_move@fal": lambda: HostedOperationRoute(
        FalWanAnimateEngine(), engine_id="wan_animate_move", runner_name="fal", quality=4),
    # Cuts, pacing and burned-in captions with ffmpeg on this machine (media/video_edit.py): $0 per job.
    "video_edit@local": lambda: HostedOperationRoute(
        FfmpegVideoEditEngine(), engine_id="ffmpeg_video_edit", runner_name="local", quality=None),
}
HOSTED_IMAGE_ROUTES: dict[str, Callable[[], ImageEngine]] = {
    "z_image_turbo@fal": lambda: HostedImageRoute(FalImageEngine(), engine_id="z_image_turbo", runner_name="fal",
                                                  quality=3),
    # CP18: an additional text-to-image option, not a replacement for Z-Image Turbo/Qwen-Image/Soul 2.
    # Off until MEDIA_FAL_FLUX_SCHNELL_ENDPOINT is set (media/settings.py).
    "flux1_schnell@fal": lambda: HostedImageRoute(
        FalImageEngine(endpoint=media_settings().media_fal_flux_schnell_endpoint,
                       usd_per_megapixel=media_settings().media_fal_flux_schnell_usd_per_megapixel),
        engine_id="flux1_schnell", runner_name="fal", quality=4),
    "gpt_image_1_mini@openai": lambda: HostedImageRoute(OpenAIImageEngine(), engine_id="gpt_image_1_mini",
                                                        runner_name="openai", quality=3),
    "grok_imagine_image@xai": lambda: HostedImageRoute(XaiImageEngine(), engine_id="grok_imagine_image",
                                                       runner_name="xai", quality=4),
    "comfyui_workflow@comfyui": lambda: HostedImageRoute(ComfyUIImageEngine(), engine_id="comfyui_workflow",
                                                         runner_name="comfyui", quality=None),
    "dev@local": lambda: HostedImageRoute(DevelopmentImageEngine(production=settings.is_production), engine_id="dev",
                                          runner_name="local", quality=None),
    "soul_2@higgsfield": lambda: HostedImageRoute(HiggsfieldImageEngine("soul_2@higgsfield"),
                                                  engine_id="higgsfield_soul_2", runner_name="higgsfield", quality=3),
    "image_edit@higgsfield": lambda: HostedImageRoute(HiggsfieldImageEngine("image_edit@higgsfield"),
                                                      engine_id="higgsfield_image_edit", runner_name="higgsfield",
                                                      quality=3),
}
LEGACY_VIDEO_NAMES = {"fal": "wan22_a14b@fal", "xai": "grok_imagine_video@xai", "veo": "veo_3_1_lite@gemini",
                      "comfyui": "comfyui_workflow@comfyui"}
LEGACY_IMAGE_NAMES = {"fal": "z_image_turbo@fal", "openai": "gpt_image_1_mini@openai", "xai": "grok_imagine_image@xai",
                      "comfyui": "comfyui_workflow@comfyui", "dev": "dev@local"}
VISION_ENGINES: dict[str, Callable[[], VisionEngine]] = {
    "gemini": GeminiVisionEngine,
}


def _names(value: str) -> list[str]:
    seen: list[str] = []
    for name in (part.strip().lower() for part in (value or "").split(",")):
        if name and name not in seen:
            seen.append(name)
    return seen


def build_video_route(key: str) -> VideoEngine | None:
    if key in HOSTED_VIDEO_ROUTES:
        return HOSTED_VIDEO_ROUTES[key]()
    engine, _, runner = key.partition("@")
    if engine in VIDEO_ADAPTERS and runner in RUNNERS:
        return VideoRoute(VIDEO_ADAPTERS[engine], RUNNERS[runner]())
    return None


def build_image_route(key: str) -> ImageEngine | None:
    if key in HOSTED_IMAGE_ROUTES:
        return HOSTED_IMAGE_ROUTES[key]()
    engine, _, runner = key.partition("@")
    if engine in IMAGE_ADAPTERS and runner in RUNNERS:
        return ImageRoute(IMAGE_ADAPTERS[engine], RUNNERS[runner]())
    return None


def build_operation_route(key: str) -> OperationEngine | None:
    """"birefnet@zerogpu" -> OperationRoute; "genjutsu_motion_transfer@higgsfield" -> HOSTED_OPERATION_ROUTES
    (CP35, the hosted-API case HOSTED_IMAGE_ROUTES/HOSTED_VIDEO_ROUTES already support)."""
    if key in HOSTED_OPERATION_ROUTES:
        return HOSTED_OPERATION_ROUTES[key]()
    engine, _, runner = key.partition("@")
    if engine in OPERATION_ADAPTERS and runner in RUNNERS:
        return OperationRoute(OPERATION_ADAPTERS[engine], RUNNERS[runner]())
    return None


def _from_routes(value: str, build, chain_type):
    engines = [engine for engine in (build(key) for key in _names(value)) if engine is not None]
    return chain_type(engines) if engines else None


def _from_legacy(value: str, names: dict[str, str], build, chain_type):
    engines = [engine for engine in (build(names[name]) for name in _names(value) if name in names) if engine is not None]
    if not engines:
        return None
    return engines[0] if len(engines) == 1 else chain_type(engines)


def image_engine() -> ImageEngine | None:
    if _image_override is not _UNSET:
        return _image_override  # type: ignore[return-value]
    s = media_settings()
    if s.media_image_routes.strip():
        return _from_routes(s.media_image_routes, build_image_route, ImageEngineChain)
    return _from_legacy(s.media_image_engine, LEGACY_IMAGE_NAMES, build_image_route, ImageEngineChain)


def video_engine() -> VideoEngine | None:
    if _video_override is not _UNSET:
        return _video_override  # type: ignore[return-value]
    s = media_settings()
    if s.media_video_routes.strip():
        return _from_routes(s.media_video_routes, build_video_route, VideoEngineChain)
    return _from_legacy(s.media_video_engine, LEGACY_VIDEO_NAMES, build_video_route, VideoEngineChain)


def operation_engines() -> dict[str, OperationEngine]:
    """operation name -> its engine (chained if more than one route serves it), built from
    MEDIA_OPERATION_ROUTES — same "engine@runner" list convention as image/video; which
    operation each route serves comes from its own OperationAdapter, not a second field."""
    if _operation_override is not _UNSET:
        return _operation_override or {}  # type: ignore[return-value]
    by_operation: dict[str, list[OperationEngine]] = {}
    for key in _names(media_settings().media_operation_routes):
        route = build_operation_route(key)
        if route is not None:
            by_operation.setdefault(route.operation, []).append(route)
    return {op: (routes[0] if len(routes) == 1 else OperationEngineChain(routes)) for op, routes in by_operation.items()}


def operation_engine(operation: str) -> OperationEngine | None:
    return operation_engines().get(operation)


def vision_engine() -> VisionEngine | None:
    if _vision_override is not _UNSET:
        return _vision_override  # type: ignore[return-value]
    engines = [VISION_ENGINES[name]() for name in _names(media_settings().media_vision_engine) if name in VISION_ENGINES]
    if not engines:
        return None
    return engines[0] if len(engines) == 1 else VisionEngineChain(engines)


@contextmanager
def use_engines(*, image: ImageEngine | None | object = _UNSET, video: VideoEngine | None | object = _UNSET,
                vision: VisionEngine | None | object = _UNSET,
                operations: dict[str, OperationEngine] | None | object = _UNSET) -> Iterator[None]:
    global _image_override, _video_override, _vision_override, _operation_override
    previous = (_image_override, _video_override, _vision_override, _operation_override)
    if image is not _UNSET:
        _image_override = image
    if video is not _UNSET:
        _video_override = video
    if vision is not _UNSET:
        _vision_override = vision
    if operations is not _UNSET:
        _operation_override = operations
    try:
        yield
    finally:
        _image_override, _video_override, _vision_override, _operation_override = previous
