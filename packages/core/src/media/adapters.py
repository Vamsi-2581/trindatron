"""Engine adapters: what a model is — never where it runs.

An adapter knows a model's tasks (text-to-video, image-to-video,
text-to-image), the sizes and proportions it makes, its durations and frame
rate, its default steps, the quality it reaches and which catalog entry holds
its licence. It turns a validated request into the RunnerJob any runner
executes (media/runners/). It knows nothing about URLs, credentials, queues or
prices: that is the runner's job, and the route (media/routes.py) joins them.

Quality is a 1–5 internal grade used only for the plan's quality floor
(media/orchestrator.py QUALITY_FLOOR): 2 acceptable draft, 3 good, 4 very good,
5 best available. It is the operator's judgement from the model cards and the
real runs recorded in docs/MEDIA_ENGINES.md, never shown to anyone.

`gpu_seconds_per_output_second` is a *measured* reference used to size a
reservation on GPU-priced runners; None when no run has been measured, in which
case the route cannot price itself and media/plans.py's documented GPU model is
used instead.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, replace

from src.media.engines import ImageRequest, VideoRequest
from src.media.runners.base import IMAGE_EDIT, IMAGE_TO_VIDEO, TEXT_TO_IMAGE, TEXT_TO_VIDEO, RunnerJob


def _down(value: float, multiple: int) -> int:
    return max(multiple, int(value) - int(value) % multiple)


@dataclass(frozen=True)
class VideoAdapter:
    id: str
    catalog_name: str
    tasks: frozenset[str]
    quality: int
    multiple: int
    short_side: int
    max_long_side: int
    min_seconds: int
    max_seconds: int
    fps: int
    steps: int | None = None
    guidance: float | None = None
    gpu_seconds_per_output_second: float | None = None
    kind: str = "video"

    def supports(self, task: str) -> bool:
        return task in self.tasks

    def normalize(self, request: VideoRequest) -> VideoRequest:
        """The asked-for proportion at this model's resolution, duration held to what it makes, its own fps."""
        ratio = (request.width / request.height) if request.width and request.height else 16 / 9
        asked_short = min(request.width, request.height) if request.width and request.height else self.short_side
        short = min(self.short_side, max(self.multiple * 8, asked_short))
        if ratio >= 1:
            width, height = short * ratio, short
            if width > self.max_long_side:
                width, height = self.max_long_side, self.max_long_side / ratio
        else:
            width, height = short, short / ratio
            if height > self.max_long_side:
                width, height = self.max_long_side * ratio, self.max_long_side
        duration = int(max(self.min_seconds, min(self.max_seconds, request.duration_seconds or self.min_seconds)))
        return replace(request, width=_down(width, self.multiple), height=_down(height, self.multiple),
                       duration_seconds=duration, fps=self.fps)

    def estimate_gpu_seconds(self, request: VideoRequest) -> float | None:
        if self.gpu_seconds_per_output_second is None:
            return None
        return self.gpu_seconds_per_output_second * request.duration_seconds

    def job(self, request: VideoRequest) -> RunnerJob:
        task = IMAGE_TO_VIDEO if request.source is not None else TEXT_TO_VIDEO
        return RunnerJob(engine=self.id, task=task, prompt=request.prompt, width=request.width, height=request.height,
                         seed=request.seed, duration_seconds=request.duration_seconds, fps=request.fps,
                         frames=request.duration_seconds * request.fps + 1, steps=self.steps, guidance=self.guidance,
                         source=request.source, source_content_type=request.source_content_type,
                         budget_usd=request.budget_usd)


@dataclass(frozen=True)
class ImageAdapter:
    id: str
    catalog_name: str
    tasks: frozenset[str]
    quality: int
    sizes: tuple[tuple[int, int], ...]
    steps: int
    guidance: float | None = None
    gpu_seconds_per_image: float | None = None
    kind: str = "image"

    def supports(self, task: str) -> bool:
        return task in self.tasks

    def normalize(self, request: ImageRequest) -> ImageRequest:
        """The size this model makes with the proportion closest to the one asked for."""
        wanted = math.log((request.width or 1) / (request.height or 1))
        width, height = min(self.sizes, key=lambda size: (abs(math.log(size[0] / size[1]) - wanted),
                                                          abs(size[0] * size[1] - request.width * request.height)))
        return replace(request, width=width, height=height, steps=self.steps)

    def estimate_gpu_seconds(self, request: ImageRequest) -> float | None:
        return self.gpu_seconds_per_image

    def job(self, request: ImageRequest) -> RunnerJob:
        task = IMAGE_EDIT if request.source is not None else TEXT_TO_IMAGE
        return RunnerJob(engine=self.id, task=task, prompt=request.prompt, width=request.width, height=request.height,
                         seed=request.seed, steps=request.steps, guidance=self.guidance, source=request.source,
                         source_content_type=request.source_content_type)


# ── Video ────────────────────────────────────────────────────────────────────

WAN22_I2V_A14B = VideoAdapter(
    id="wan22_i2v_a14b", catalog_name="Wan2.2-I2V-A14B", tasks=frozenset({IMAGE_TO_VIDEO}), quality=4,
    multiple=16, short_side=480, max_long_side=832, min_seconds=1, max_seconds=5, fps=16, steps=6, guidance=1.0,
    # Measured 2026-09-15: 50.5 s wall for a 3.56 s clip on ZeroGPU (half RTX Pro 6000, FP8 + AoT, 6 steps).
    gpu_seconds_per_output_second=14.2,
)
WAN22_TI2V_5B = VideoAdapter(
    id="wan22_ti2v_5b", catalog_name="Wan2.2-TI2V-5B", tasks=frozenset({TEXT_TO_VIDEO, IMAGE_TO_VIDEO}), quality=3,
    # 704 short side: the quality mode's 720p (1280x704, the size measured on 2026-09-25); 480p asks keep 480.
    multiple=32, short_side=704, max_long_side=1280, min_seconds=1, max_seconds=6, fps=24, steps=4, guidance=0.0,
)
# Maximum: Wan 2.2 T2V-A14B + lightx2v Lightning 4-step LoRA on the Modal worker's H200 class
# (infra/modal/video_worker.py WanA14B). 1280x720 at 16 fps, up to 5 s. No per-second estimate on purpose,
# like the 5B: the reservation comes from the measured reference (modes.MAXIMUM, 56.8 H200 s per 5 s) and
# the job settles at the GPU seconds the worker reports, priced at the H200 rate.
WAN22_T2V_A14B = VideoAdapter(
    id="wan22_t2v_a14b", catalog_name="Wan2.2-T2V-A14B + Lightning", tasks=frozenset({TEXT_TO_VIDEO}), quality=4,
    multiple=16, short_side=720, max_long_side=1280, min_seconds=1, max_seconds=5, fps=16, steps=4, guidance=1.0,
)
# Photo -> Movement: Wan 2.2 I2V-A14B + lightx2v Lightning I2V 4-step LoRA on the Modal worker's H200 class
# (infra/modal/video_worker.py WanI2V). The person's photo is the first frame; the clip takes the photo's own
# proportion at 720p, 16 fps, up to 5 s. Priced like WAN22_T2V_A14B: from the Maximum reference, settled on the
# worker's real H200 seconds. Separate from WAN22_I2V_A14B, which is the ZeroGPU Space's 480p/6-step shape.
WAN22_I2V_A14B_LIGHTNING = VideoAdapter(
    id="wan22_i2v_a14b_lightning", catalog_name="Wan2.2-I2V-A14B + Lightning", tasks=frozenset({IMAGE_TO_VIDEO}),
    quality=4, multiple=16, short_side=720, max_long_side=1280, min_seconds=1, max_seconds=5, fps=16, steps=4,
    guidance=1.0,
)
LTX_VIDEO_13B_DISTILLED = VideoAdapter(
    id="ltx_video_13b_distilled", catalog_name="LTX-Video 0.9.8 13B distilled",
    tasks=frozenset({TEXT_TO_VIDEO, IMAGE_TO_VIDEO}), quality=3, multiple=32, short_side=512, max_long_side=1216,
    min_seconds=1, max_seconds=8, fps=30, guidance=1.0,
)

# ── Image ────────────────────────────────────────────────────────────────────

Z_IMAGE_TURBO = ImageAdapter(
    id="z_image_turbo", catalog_name="Z-Image-Turbo", tasks=frozenset({TEXT_TO_IMAGE}), quality=3,
    sizes=((1024, 1024), (1152, 896), (896, 1152), (1152, 864), (864, 1152), (1248, 832), (832, 1248),
           (1280, 720), (720, 1280), (1344, 576), (576, 1344)),
    steps=8,
)
QWEN_IMAGE = ImageAdapter(
    id="qwen_image", catalog_name="Qwen-Image", tasks=frozenset({TEXT_TO_IMAGE}), quality=4,
    sizes=((1328, 1328), (1664, 928), (928, 1664), (1472, 1140), (1140, 1472), (1584, 1056), (1056, 1584)),
    steps=30, guidance=4.0,
)

VIDEO_ADAPTERS: dict[str, VideoAdapter] = {a.id: a for a in (WAN22_I2V_A14B, WAN22_TI2V_5B, WAN22_T2V_A14B,
                                                                 WAN22_I2V_A14B_LIGHTNING, LTX_VIDEO_13B_DISTILLED)}
IMAGE_ADAPTERS: dict[str, ImageAdapter] = {a.id: a for a in (Z_IMAGE_TURBO, QWEN_IMAGE)}


# ── Operations (CP19: TRANSFORM/COMPOSE — see media/operations.py) ────────────

@dataclass(frozen=True)
class OperationAdapter:
    """What an operation model is: which operation it performs, which input role is its
    primary input, which OTHER roles it also needs (CP22 — e.g. Animate's driving_video
    alongside its primary character image), and its quality grade. Same job()-building
    contract as ImageAdapter/VideoAdapter, so it runs on the exact same Runner implementations.

    `extra_roles` defaults to empty: every operation before CP22 (BiRefNet, VTracer) has exactly
    one input and is completely unaffected — `job()` below builds an empty `extra_files={}` for
    them, exactly like before this field existed.
    """

    id: str
    catalog_name: str
    operation: str
    quality: int
    primary_role: str = "source_image"
    extra_roles: tuple[str, ...] = ()
    gpu_seconds_per_call: float | None = None
    kind: str = "operation"

    def job(self, request, resolved: dict) -> RunnerJob:
        primary = resolved.get(self.primary_role)
        data, content_type = primary if primary is not None else (None, None)
        extra_files = {role: (found.data, found.content_type)
                       for role in self.extra_roles if (found := resolved.get(role)) is not None}
        return RunnerJob(engine=self.id, task=self.operation, prompt="", width=0, height=0, seed=0,
                         source=data, source_content_type=content_type or "image/png",
                         extra=dict(request.parameters or {}) if request is not None else {},
                         extra_files=extra_files)


BIREFNET = OperationAdapter(
    id="birefnet", catalog_name="BiRefNet", operation="background_remove", quality=4,
    # Not measured yet (no verified Space to run it on) — GPU-priced runners fall back to
    # media/plans.py's documented model when this is None, same convention as Qwen-Image.
    gpu_seconds_per_call=None,
)

# CP22: character + driving_video, the real inputs of the official inference script
# (Wan-Video/Wan2.2's generate.py/preprocess_data.py — --refer_path, --video_path; no audio).
# No SpaceProfile is registered for it yet (media/runners/zerogpu.py) — see that file's comment
# on `wan22_animate` for why: no official Wan-AI Space exists (the linked one 404s), and the one
# real community candidate found wasn't wired without its own source confirming its output
# semantics. This adapter alone is already complete and correct regardless of that — `job()`
# only needs to know the operation's roles, never which Space (if any) will run it.
WAN22_ANIMATE = OperationAdapter(
    id="wan22_animate", catalog_name="Wan2.2-Animate-14B", operation="character_animate", quality=4,
    primary_role="character", extra_roles=("driving_video",),
    gpu_seconds_per_call=None,
)

OPERATION_ADAPTERS: dict[str, OperationAdapter] = {a.id: a for a in (BIREFNET, WAN22_ANIMATE)}
