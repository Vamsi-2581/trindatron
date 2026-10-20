"""What the TRINDATRON video worker decides before and after a GPU runs — no Modal, no GPU.

Kept apart from video_worker.py so the Core test suite can import and test it
(packages/core/tests/unit/test_modal_video_worker.py) without Modal installed.

Three engines (packages/core/src/media/modes.py):

  wan22_ti2v_5b             Economy / Quality — Wan 2.2 TI2V-5B, FastVideo's DMD-distilled checkpoint
                            (FastWan2.2-TI2V-5B-FullAttn-Diffusers): 3 steps, 24 fps, H100. Text to video.
  wan22_t2v_a14b            Maximum — Wan 2.2 T2V-A14B (Diffusers) + lightx2v Lightning 4-step LoRA
                            (250928): 4 steps, 16 fps, 1280x720, H200 without CPU offload. Text to video.
  wan22_i2v_a14b_lightning  Photo → Movement (Maximum) — Wan 2.2 I2V-A14B (Diffusers) + lightx2v Lightning
                            I2V 4-step LoRA (Seko V1): the person's photo is the first frame; H200.

The 5B's image-to-video path was measured to ignore the image (2026-09-25), so it stays text to
video only: a photo goes to the I2V engine or nowhere, never to a clip that ignored it.
"""
from __future__ import annotations

import math
from typing import NamedTuple

ENGINE = "wan22_ti2v_5b"
MODEL = "FastVideo/FastWan2.2-TI2V-5B-FullAttn-Diffusers"
STEPS = 3                      # the DMD-distilled checkpoint's own schedule
FPS = 24
MULTIPLE = 32                  # VAE 16x spatial x patch 2
MAX_SHORT_SIDE, MAX_LONG_SIDE = 704, 1280   # 720p class (1280x704 measured)
MIN_SHORT_SIDE = 256
MIN_SECONDS, MAX_SECONDS = 1, 6             # beyond ~5-6 s this checkpoint froze its second half (8 s run)
MAX_PROMPT_CHARS = 2000

# Warm H100 seconds for one clip ≈ BASE + PER_MPF x (width x height x frames / 1e6), from the runs of
# 2026-09-25: 832x480x121 in 8.7 s and 1280x704x121 in 16.2 s (3 steps, inference + encode).
BASE_SECONDS = 2.74
SECONDS_PER_MEGA_PIXEL_FRAME = 0.1234


class Profile(NamedTuple):
    """What one engine makes and how long it takes, measured warm."""

    engine: str
    model: str
    gpu: str
    steps: int
    fps: int
    multiple: int
    max_short_side: int
    max_long_side: int
    min_seconds: int
    max_seconds: int
    base_seconds: float
    seconds_per_mega_pixel_frame: float
    guidance: float = 0.0
    task: str = "text_to_video"


FIVE_B = Profile(ENGINE, MODEL, "H100", STEPS, FPS, MULTIPLE, MAX_SHORT_SIDE, MAX_LONG_SIDE, MIN_SECONDS, MAX_SECONDS,
                 BASE_SECONDS, SECONDS_PER_MEGA_PIXEL_FRAME)

# Maximum. Measured on Modal H200 on 2026-09-25 (scratch benchmark, two prompts): 56.7 s and 56.8 s for
# 81 frames of 1280x720 (5 s at 16 fps), 4 steps, warm, without offload. One size was measured, so the
# fixed part is a small allowance and the rest scales with pixel-frames; the Core still settles real seconds.
A14B_ENGINE = "wan22_t2v_a14b"
A14B_MODEL = "Wan-AI/Wan2.2-T2V-A14B-Diffusers"
A14B_LORA = "lightx2v/Wan2.2-Lightning"
A14B_LORA_DIR = "Wan2.2-T2V-A14B-4steps-lora-250928"
A14B_SCHEDULER_SHIFT = 5.0
A14B = Profile(A14B_ENGINE, A14B_MODEL, "H200", steps=4, fps=16, multiple=16, max_short_side=720, max_long_side=1280,
               min_seconds=1, max_seconds=5, base_seconds=3.0,
               seconds_per_mega_pixel_frame=round((56.8 - 3.0) / (1280 * 720 * 81 / 1e6), 4), guidance=1.0)

# Photo → Movement: the I2V sibling of A14B, same size and steps. Measured on Modal H200 on 2026-09-29
# (smoke_i2v, a 832x480 photo): 62.5 GPU s for 81 frames of 1280x720, warm, without offload.
I2V_ENGINE = "wan22_i2v_a14b_lightning"
I2V_MODEL = "Wan-AI/Wan2.2-I2V-A14B-Diffusers"
I2V_LORA_DIR = "Wan2.2-I2V-A14B-4steps-lora-rank64-Seko-V1"
I2V = A14B._replace(engine=I2V_ENGINE, model=I2V_MODEL, task="image_to_video",
                    seconds_per_mega_pixel_frame=round((62.5 - 3.0) / (1280 * 720 * 81 / 1e6), 4))

PROFILES = {profile.engine: profile for profile in (FIVE_B, A14B, I2V)}

#: The photo a Photo → Movement job starts from: its declared type and its own signature must agree.
SOURCE_TYPES = {"image/png": b"\x89PNG", "image/jpeg": b"\xff\xd8\xff", "image/webp": b"RIFF"}
MAX_SOURCE_BYTES = 10 * 1024 * 1024

# Failure kinds the Core understands (src/media/engines.py FailureKind).
REJECTED, UNSUPPORTED_FORMAT, UNSUPPORTED_DURATION = "rejected", "unsupported_format", "unsupported_duration"
QUOTA, TIMEOUT, OOM, TEMPORARY = "quota", "timeout", "oom", "temporary"


class JobRefused(ValueError):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind, self.message = kind, message

    def as_error(self) -> dict:
        return {"kind": self.kind, "message": self.message}


def _down(value: float, multiple: int = MULTIPLE) -> int:
    return max(multiple, int(value) // multiple * multiple)


def frames_for(seconds: int, fps: int = FPS) -> int:
    """Wan's frame count for a clip: 4k + 1 (121 for 5 s, 145 for 6 s at 24 fps)."""
    frames = seconds * fps + 1
    return frames if (frames - 1) % 4 == 0 else ((frames - 1) // 4) * 4 + 1


def size_for(width: int, height: int, profile: Profile = FIVE_B) -> tuple[int, int]:
    """The asked-for proportion inside the engine's 720p class, both sides on its grid."""
    if width <= 0 or height <= 0:
        raise JobRefused(UNSUPPORTED_FORMAT, "width and height must be positive")
    short, long = min(width, height), max(width, height)
    scale = min(1.0, profile.max_short_side / short, profile.max_long_side / long)
    short_out, long_out = _down(short * scale, profile.multiple), _down(long * scale, profile.multiple)
    if short_out < MIN_SHORT_SIDE:
        raise JobRefused(UNSUPPORTED_FORMAT, f"short side {short_out}px below {MIN_SHORT_SIDE}px")
    return (long_out, short_out) if width >= height else (short_out, long_out)


def predicted_seconds(width: int, height: int, frames: int, profile: Profile = FIVE_B) -> float:
    return profile.base_seconds + profile.seconds_per_mega_pixel_frame * width * height * frames / 1e6


def _source(job: dict) -> bytes:
    """The photo a Photo → Movement job starts from, decoded and checked: required, bounded, and really the
    image type it says it is. A job without one is refused, never run as text to video."""
    import base64
    import binascii

    source = job.get("source")
    if not isinstance(source, dict) or not source.get("data_base64"):
        raise JobRefused(REJECTED, "image_to_video needs the source image")
    signature = SOURCE_TYPES.get(str(source.get("content_type") or "").split(";")[0].strip().lower())
    if signature is None:
        raise JobRefused(UNSUPPORTED_FORMAT, "source image must be PNG, JPEG or WebP")
    try:
        data = base64.b64decode(source["data_base64"], validate=True)
    except (binascii.Error, ValueError):
        raise JobRefused(REJECTED, "source image is not valid base64") from None
    if not data or len(data) > MAX_SOURCE_BYTES:
        raise JobRefused(REJECTED, f"source image must be 1 byte to {MAX_SOURCE_BYTES // (1024 * 1024)} MB")
    if not data.startswith(signature) or (signature == b"RIFF" and data[8:12] != b"WEBP"):
        raise JobRefused(UNSUPPORTED_FORMAT, "source image content does not match its declared type")
    return data


def fit_image(data: bytes, width: int, height: int):
    """The photo as the first frame: centre-cropped to the clip's proportion, then resized — never stretched.
    The Core already sized the clip from the photo's own proportion, so the crop is only the rounding to the grid."""
    import io

    from PIL import Image, ImageOps

    image = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    return ImageOps.fit(image, (width, height), method=Image.Resampling.LANCZOS, centering=(0.5, 0.5))


def validate(job: dict, limits: dict | None = None) -> dict:
    """The job this worker will run, or JobRefused. Nothing reaches the GPU without passing here."""
    if not isinstance(job, dict):
        raise JobRefused(REJECTED, "job must be an object")
    profile = PROFILES.get(job.get("engine"))
    if profile is None:
        raise JobRefused(REJECTED, f"engine {job.get('engine')!r} is not served here")
    if job.get("task") != profile.task:
        raise JobRefused(REJECTED, f"task {job.get('task')!r} is not served here ({profile.task} only)")
    source = _source(job) if profile.task == "image_to_video" else None
    prompt = str(job.get("prompt") or "").strip()
    if not prompt or len(prompt) > MAX_PROMPT_CHARS:
        raise JobRefused(REJECTED, "prompt must be 1-2000 characters")
    seconds = job.get("duration_seconds")
    try:
        seconds = int(math.ceil(float(seconds)))
    except (TypeError, ValueError):
        raise JobRefused(UNSUPPORTED_DURATION, "duration_seconds is required") from None
    if not profile.min_seconds <= seconds <= profile.max_seconds:
        raise JobRefused(UNSUPPORTED_DURATION, f"{seconds} s is outside {profile.min_seconds}-{profile.max_seconds} s")
    width, height = size_for(int(job.get("width") or 0), int(job.get("height") or 0), profile)
    frames = frames_for(seconds, profile.fps)
    try:
        seed = int(job.get("seed") or 0) % (2 ** 31)
    except (TypeError, ValueError):
        seed = 0
    plan = {"engine": profile.engine, "prompt": prompt, "width": width, "height": height, "frames": frames,
            "fps": profile.fps, "seconds": seconds, "seed": seed, "steps": profile.steps, "guidance": profile.guidance,
            "predicted_seconds": round(predicted_seconds(width, height, frames, profile), 2)}
    if source is not None:
        plan["source"] = source
    budget = (limits or {}).get("max_gpu_seconds")
    if budget is not None:
        try:
            budget = float(budget)
        except (TypeError, ValueError):
            raise JobRefused(REJECTED, "limits.max_gpu_seconds must be a number") from None
        if plan["predicted_seconds"] > budget:
            # The reservation cannot pay for this clip: refuse before any GPU second is spent.
            raise JobRefused(REJECTED, f"needs ~{plan['predicted_seconds']} GPU s, reservation allows {budget:.1f}")
        plan["max_gpu_seconds"] = round(budget, 2)
    return plan


def motion_by_second(frames, fps: int = FPS) -> list[float]:
    """Mean absolute change between consecutive frames, per second of clip (0-255 scale).

    A clip whose later seconds fall near zero while its first ones move has frozen —
    what an 8 s request did on this checkpoint.
    """
    import numpy as np

    diffs = []
    previous = None
    for frame in frames:
        small = np.asarray(frame, dtype=np.float32)[::8, ::8]
        if previous is not None:
            diffs.append(float(np.abs(small - previous).mean()))
        previous = small
    return [round(sum(chunk) / len(chunk), 2) for chunk in
            (diffs[i:i + fps] for i in range(0, len(diffs), fps)) if chunk]
