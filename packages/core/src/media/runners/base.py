"""The runner contract: execute one engine job on one infrastructure, and say what it took.

A runner knows nothing about models — which sizes a model makes, how many
frames, which quality it reaches: that is the engine adapter's job
(media/adapters.py). A runner knows how to get a job to a GPU somewhere and the
bytes back: its protocol, its authentication, its failures and its price.

  RunnerJob     what to run: engine id, task, prompt, normalized size, duration,
                frames, fps, steps, seed, optional source image
  RunnerResult  what came back and what it took: bytes, content type, measured
                queue / cold start / inference / GPU / total seconds, and the
                cost when the infrastructure reports or prices it

Cost is never invented: a runner returns `cost_usd` only from a price applied to
measured seconds or from what the infrastructure billed. ZeroGPU's included
quota costs nothing and says so (0.0).

`commercial` marks infrastructure whose availability is bought and can be relied
on for paying users; `experimental` marks free/shared infrastructure that may
queue or run out at any time — it is never used in production and never sizes a
plan's promise (media_service.entitlement_for).
"""
from __future__ import annotations

import base64
import ipaddress
import re
import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from src.media.engines import EngineFailed, FailureKind

TEXT_TO_VIDEO = "text_to_video"
IMAGE_TO_VIDEO = "image_to_video"
TEXT_TO_IMAGE = "text_to_image"
IMAGE_EDIT = "image_edit"
# CP19: the first TRANSFORM task. Single image in, single image out — fits RunnerJob's
# existing `source` field exactly, so no change was needed there for this one.
BACKGROUND_REMOVE = "background_remove"
# CP22: the first TRANSFORM task needing more than one file input (character + driving_video) —
# see RunnerJob.extra_files below.
CHARACTER_ANIMATE = "character_animate"
TASKS = frozenset({TEXT_TO_VIDEO, IMAGE_TO_VIDEO, TEXT_TO_IMAGE, IMAGE_EDIT, BACKGROUND_REMOVE, CHARACTER_ANIMATE})


@dataclass(frozen=True)
class RunnerJob:
    engine: str
    task: str
    prompt: str
    width: int
    height: int
    seed: int
    duration_seconds: float | None = None
    fps: int | None = None
    frames: int | None = None
    steps: int | None = None
    guidance: float | None = None
    negative_prompt: str | None = None
    source: bytes | None = None
    source_content_type: str | None = None
    # CP20: a generic bag for named arguments a Space needs that none of the fields above cover
    # (BiRefNet's "resolution"/"weights" textbox and radio). Reusable by any future engine with
    # its own extra parameters — never engine-specific fields bolted onto this dataclass.
    extra: dict = field(default_factory=dict)
    # CP22: additional named FILE inputs beyond the single well-known `source` (role ->
    # (bytes, content_type)) — e.g. Wan2.2-Animate's driving_video alongside its primary
    # `source` character image. Mirrors `extra` (CP20)'s own shape and purpose — a generic bag,
    # never engine-specific fields — just for binary inputs instead of scalar params. Empty for
    # every existing engine; none of them read it, so single-source Image/Video engines and
    # every current OperationAdapter are unaffected.
    extra_files: dict = field(default_factory=dict)  # dict[str, tuple[bytes, str]]
    # What the job's reservation allows it to spend (VideoRequest.budget_usd). Not part of payload():
    # a GPU worker runner turns it into the worker's own limit (limits.max_gpu_seconds) at its price.
    budget_usd: float | None = None

    def payload(self) -> dict:
        """The engine-agnostic job every GPU worker receives (docs/MEDIA_ENGINES.md §9)."""
        body = {"engine": self.engine, "task": self.task, "prompt": self.prompt, "width": self.width,
                "height": self.height, "seed": self.seed, "duration_seconds": self.duration_seconds,
                "fps": self.fps, "frames": self.frames, "steps": self.steps, "guidance": self.guidance,
                "negative_prompt": self.negative_prompt, "extra": self.extra or None}
        if self.source is not None:
            body["source"] = {"content_type": self.source_content_type or "image/png",
                              "data_base64": base64.b64encode(self.source).decode("ascii")}
        if self.extra_files:
            body["extra_files"] = {role: {"content_type": content_type,
                                          "data_base64": base64.b64encode(data).decode("ascii")}
                                   for role, (data, content_type) in self.extra_files.items()}
        return {key: value for key, value in body.items() if value is not None}


@dataclass(frozen=True)
class RunnerResult:
    data: bytes
    content_type: str
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    total_seconds: float = 0.0
    queue_seconds: float | None = None
    cold_start_seconds: float | None = None
    inference_seconds: float | None = None
    gpu_seconds: float | None = None
    cost_usd: float | None = None

    def metrics(self) -> dict:
        values = {"total_seconds": self.total_seconds, "queue_seconds": self.queue_seconds,
                  "cold_start_seconds": self.cold_start_seconds, "inference_seconds": self.inference_seconds,
                  "gpu_seconds": self.gpu_seconds, "output_bytes": len(self.data)}
        return {key: round(value, 3) if isinstance(value, float) else value
                for key, value in values.items() if value is not None}


class Runner(ABC):
    name: str = "runner"
    commercial: bool = True
    experimental: bool = False

    @abstractmethod
    def supports(self, engine: str, task: str) -> bool:
        """Whether this infrastructure is set up to run `engine` for `task`. No network."""

    @abstractmethod
    async def is_available(self) -> bool:
        """Reachable and accepting work now. Never raises."""

    async def is_available_for(self, engine: str) -> bool:
        """Reachable for this engine in particular (a runner may host each engine separately). Never raises."""
        return await self.is_available()

    @abstractmethod
    async def run(self, job: RunnerJob) -> RunnerResult:
        """Execute the job. Raises EngineUnavailable / EngineFailed with a FailureKind."""

    def estimate_usd(self, engine: str, gpu_seconds: float | None) -> float | None:
        """Most this job can cost here for that many GPU seconds; None when it cannot be priced."""
        return None


# ── Failure classification shared by runners ────────────────────────────────

_MESSAGE_KINDS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"no gpu (was )?available|gpu (is )?(not )?available|no available gpu|gpu unavailable", re.I),
     FailureKind.GPU_UNAVAILABLE),
    (re.compile(r"quota|exceeded your|out of credits|insufficient (credits|balance)|RESOURCE_EXHAUSTED", re.I),
     FailureKind.QUOTA),
    (re.compile(r"rate.?limit|too many requests", re.I), FailureKind.RATE_LIMITED),
    (re.compile(r"out of memory|\boom\b|cuda error: out of memory", re.I), FailureKind.OOM),
    (re.compile(r"cold.?start|never started|worker (did not|failed to) start", re.I), FailureKind.COLD_START_TIMEOUT),
    (re.compile(r"timed?.?out|timeout|deadline", re.I), FailureKind.TIMEOUT),
    (re.compile(r"unsupported (duration|length)|duration .*(not supported|out of range|too long)", re.I),
     FailureKind.UNSUPPORTED_DURATION),
    (re.compile(r"unsupported (resolution|size|dimension|aspect|format)|(resolution|size|dimension)s? .*(not supported|out of range)", re.I),
     FailureKind.UNSUPPORTED_FORMAT),
    (re.compile(r"nsfw|safety|filtered|moderation|content policy", re.I), FailureKind.FILTERED),
    (re.compile(r"sleeping|paused|building|not running|space is in error|unavailable|starting", re.I),
     FailureKind.UNAVAILABLE),
]


def kind_from_message(message: str | None, default: str = FailureKind.TEMPORARY) -> str:
    text = message or ""
    for pattern, kind in _MESSAGE_KINDS:
        if pattern.search(text):
            return kind
    return default


def kind_from_status(status: int, text: str = "") -> str:
    if status == 429:
        return FailureKind.QUOTA if kind_from_message(text, "") == FailureKind.QUOTA else FailureKind.RATE_LIMITED
    if status in (402,):
        return FailureKind.QUOTA
    if status in (502, 503, 504):
        return FailureKind.UNAVAILABLE if status == 503 else FailureKind.TEMPORARY
    if status >= 500:
        return kind_from_message(text, FailureKind.TEMPORARY)
    if status in (401, 403, 404):
        return FailureKind.UNAVAILABLE
    return kind_from_message(text, FailureKind.REJECTED)


def retry_after_seconds(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


def assert_public_https_url(url: str | None, what: str) -> str:
    """A URL a provider handed back is safe to fetch: https, and not a literal private/loopback/
    link-local/reserved address (169.254.169.254 cloud metadata, 127.0.0.1, 10.x/172.16.x/192.168.x, ...).

    The provider's real output host varies (a CDN, not the API host), so — unlike a status/cancel
    URL, which can be pinned to one known API host — this cannot use a domain allowlist; blocking
    literal internal-IP targets is the minimal guard against a compromised or malicious response
    being used to make this server fetch its own internal network. It does not resolve DNS names
    (no synchronous lookup here), so a hostname that only resolves to a private address at connect
    time is not caught — a narrower guarantee than a full SSRF defense, deliberately kept small.
    """
    parts = urlsplit(url or "")
    if parts.scheme != "https" or not parts.hostname:
        raise EngineFailed(f"{what}: not a usable URL", kind=FailureKind.TEMPORARY, resubmit_safe=False)
    try:
        ip = ipaddress.ip_address(parts.hostname)
    except ValueError:
        return url  # a hostname, not a literal IP — left to DNS/httpx, not checked further here
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        raise EngineFailed(f"{what}: refused (internal address)", kind=FailureKind.TEMPORARY, resubmit_safe=False)
    return url


# ── Output inspection (measured, never assumed) ─────────────────────────────

def _boxes(buf: bytes, start: int, end: int):
    index = start
    while index + 8 <= end:
        size, kind = struct.unpack(">I4s", buf[index:index + 8])
        if size == 1 and index + 16 <= end:
            size = struct.unpack(">Q", buf[index + 8:index + 16])[0]
        if size < 8 or index + size > end:
            return
        yield kind, index, size
        index += size


def video_metadata(data: bytes) -> tuple[int | None, int | None, float | None]:
    """(width, height, seconds) read from an MP4's moov box; Nones when it cannot be read."""
    width = height = None
    seconds = None
    try:
        moov = next(((i, s) for k, i, s in _boxes(data, 0, len(data)) if k == b"moov"), None)
        if moov is None:
            return None, None, None
        for kind, index, size in _boxes(data, moov[0] + 8, moov[0] + moov[1]):
            if kind == b"mvhd":
                version = data[index + 8]
                if version == 0:
                    timescale, duration = struct.unpack(">II", data[index + 20:index + 28])
                else:
                    timescale = struct.unpack(">I", data[index + 28:index + 32])[0]
                    duration = struct.unpack(">Q", data[index + 32:index + 40])[0]
                seconds = duration / timescale if timescale else None
            elif kind == b"trak" and width is None:
                for inner, at, length in _boxes(data, index + 8, index + size):
                    if inner == b"tkhd":
                        w = struct.unpack(">I", data[at + length - 8:at + length - 4])[0] >> 16
                        h = struct.unpack(">I", data[at + length - 4:at + length])[0] >> 16
                        if w and h:
                            width, height = w, h
    except (struct.error, IndexError, StopIteration):
        return width, height, seconds
    return width, height, seconds


def _first_child(data: bytes, kind: bytes, start: int, end: int) -> tuple[int, int] | None:
    return next(((i, s) for k, i, s in _boxes(data, start, end) if k == kind), None)


def video_frame_count(data: bytes) -> int | None:
    """Total frame count of an MP4's (first) video track, read directly from its stts (time-to-
    sample) box — the sum of every entry's sample_count. This is the exact value a frame-based
    billing formula (fal.ai's Wan-Animate models: billed video-seconds = total_frames / 16) needs;
    computing it from duration * assumed-fps would only approximate it and can disagree with the
    container's own real value by a frame or more (CP46: observed 119 real frames for a nominal
    "24fps, ~5s" clip, not the 120 duration*fps would suggest). None when it cannot be read —
    never a guessed or rounded substitute.
    """
    try:
        moov = _first_child(data, b"moov", 0, len(data))
        if moov is None:
            return None
        for kind, index, size in _boxes(data, moov[0] + 8, moov[0] + moov[1]):
            if kind != b"trak":
                continue
            mdia = _first_child(data, b"mdia", index + 8, index + size)
            minf = mdia and _first_child(data, b"minf", mdia[0] + 8, mdia[0] + mdia[1])
            stbl = minf and _first_child(data, b"stbl", minf[0] + 8, minf[0] + minf[1])
            stts = stbl and _first_child(data, b"stts", stbl[0] + 8, stbl[0] + stbl[1])
            if stts is None:
                continue
            at, _length = stts
            entry_count = struct.unpack(">I", data[at + 12:at + 16])[0]
            offset = at + 16
            total = 0
            for _ in range(entry_count):
                count, _delta = struct.unpack(">II", data[offset:offset + 8])
                total += count
                offset += 8
            if total:
                return total
    except (struct.error, IndexError, StopIteration):
        return None
    return None


def image_size(data: bytes) -> tuple[int | None, int | None]:
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            return image.size
    except Exception:
        return None, None
