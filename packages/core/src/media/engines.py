"""The contract a media engine fulfils.

An engine turns a validated request into bytes (or, for vision, into a
description). It does not know about users, plans, quotas or storage —
media_service and attachment_service decide all of that before an engine is
ever called, and store what it returns. That keeps every engine small and lets
a deployment swap one for another by configuration alone.

An engine here is what media_service calls. Since the engine × runner split
(media/routes.py) it is usually a *route*: one model (media/adapters.py) run on
one infrastructure (media/runners/). Hosted APIs that are model and
infrastructure at once (media/hosted.py, media/xai.py) are routes too.

Cost is always measured, never assumed:
  * a self-hosted engine reports how long the work took on the GPU
    (`engine_seconds`), priced with the configured GPU rate;
  * a hosted engine reports what the call cost (`cost_usd`), from the usage
    the provider returned or its published price for exactly what was asked.

Before a job is accepted, `estimate_usd()` gives the most the request can cost
on this engine, so the reservation is sized to the engine that will really run.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable


class FailureKind:
    """Why an engine could not deliver. Decides fallback and health (media/orchestrator.py, media/health.py)."""

    RATE_LIMITED = "rate_limited"            # 429 without a quota statement
    QUOTA = "quota"                          # the account's allowance is spent
    UNAVAILABLE = "unavailable"              # not configured, down, or not running
    GPU_UNAVAILABLE = "gpu_unavailable"      # the infrastructure had no GPU for the job
    TIMEOUT = "timeout"                      # the job ran past its deadline
    COLD_START_TIMEOUT = "cold_start_timeout"  # the job never left the queue / never started
    UNSUPPORTED_FORMAT = "unsupported_format"
    UNSUPPORTED_DURATION = "unsupported_duration"
    OOM = "oom"                              # out of GPU memory for this request
    TEMPORARY = "temporary"                  # 5xx, lost connection, transient error
    REJECTED = "rejected"                    # the input itself was refused
    FILTERED = "filtered"                    # a safety filter removed the output


# Failures another engine or runner may well not have: the chain moves on.
# REJECTED and FILTERED stop it — another engine would refuse the same input, and trying costs money.
FALLBACK_KINDS = frozenset({
    FailureKind.RATE_LIMITED, FailureKind.QUOTA, FailureKind.UNAVAILABLE, FailureKind.GPU_UNAVAILABLE,
    FailureKind.TIMEOUT, FailureKind.COLD_START_TIMEOUT, FailureKind.UNSUPPORTED_FORMAT,
    FailureKind.UNSUPPORTED_DURATION, FailureKind.OOM, FailureKind.TEMPORARY,
})


class EngineUnavailable(Exception):
    """The engine is not configured or cannot be reached right now."""

    def __init__(self, reason: str = "", *, kind: str = FailureKind.UNAVAILABLE,
                 retry_after: float | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.kind = kind
        self.retry_after = retry_after


class EngineFailed(Exception):
    """The engine accepted the work and could not finish it.

    `kind` (FailureKind) says why; `retryable` — whether another attempt, on this
    engine or another, can succeed — follows from it unless given explicitly.
    Without a kind, `retryable=True` means a transient failure and False a
    definitive one. `reason` is for logs only and is never shown to a user.
    """

    def __init__(self, reason: str, *, retryable: bool | None = None, kind: str | None = None,
                 retry_after: float | None = None, may_have_started: bool = False,
                 resubmit_safe: bool | None = None, provider_detail: dict | None = None) -> None:
        super().__init__(reason)
        if kind is None:
            kind = FailureKind.TEMPORARY if retryable else FailureKind.REJECTED
        if retryable is None:
            retryable = kind in FALLBACK_KINDS
        self.reason = reason
        self.kind = kind
        self.retryable = retryable
        self.retry_after = retry_after
        # The provider may have accepted (and may bill) the work: it must never be submitted again.
        self.may_have_started = may_have_started
        # Whether the same engine may be asked again for the same job. Queue-based providers without an
        # idempotency key say no for every failure; the chain may still hand over to another route.
        self.resubmit_safe = (not may_have_started) if resubmit_safe is None else (resubmit_safe and not may_have_started)
        # CP38: a sanitized, shallow subset of whatever detail the provider itself returned (status,
        # error, message, code, ...) when it explains a failure — never a credential (the caller that
        # builds this is responsible for stripping those before it reaches here). None when the
        # provider gave nothing beyond a bare status, or for failures with no provider payload at all
        # (a lost connection, a timeout). Internal only — same "never shown to a user" rule as `reason`.
        self.provider_detail = provider_detail


class JobCancelled(Exception):
    """The provider confirmed the job was cancelled before it ran; nothing is billed."""


@dataclass(frozen=True)
class ProviderHandle:
    """A job a hosted provider accepted: enough to follow, cancel or resume it after a restart. Internal only."""

    provider: str
    model: str
    route: str
    request_id: str
    status_url: str | None = None
    cancel_url: str | None = None
    kind: str = "image"             # "image" | "video"
    estimate_usd: float | None = None
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None

    def as_dict(self) -> dict:
        return {"provider": self.provider, "model": self.model, "route": self.route, "request_id": self.request_id,
                "status_url": self.status_url, "cancel_url": self.cancel_url, "kind": self.kind,
                "estimate_usd": self.estimate_usd, "width": self.width, "height": self.height,
                "duration_seconds": self.duration_seconds}

    @classmethod
    def from_dict(cls, data: dict) -> "ProviderHandle":
        names = cls.__dataclass_fields__.keys()
        return cls(**{key: value for key, value in data.items() if key in names})


class ProviderLink:
    """What media_service and a queue-based hosted engine share about the provider job behind one creation.

    The engine calls `submitted()` the moment the provider returns a request id,
    so the id is persisted before any polling; media_service sets
    `cancel_requested` when the person cancels. A request carries at most one
    live handle: a fallback route replaces it only after the previous job ended
    without a charge.
    """

    def __init__(self, on_submit: Callable[[ProviderHandle], None] | None = None) -> None:
        self._on_submit = on_submit
        self.handle: ProviderHandle | None = None
        self.cancel_requested = False

    def submitted(self, handle: ProviderHandle) -> None:
        self.handle = handle
        if self._on_submit is not None:
            self._on_submit(handle)


@dataclass(frozen=True)
class ImageRequest:
    prompt: str
    width: int
    height: int
    seed: int
    steps: int
    # For editing: the source image bytes and its content type.
    source: bytes | None = None
    source_content_type: str | None = None
    # "draft" | "standard" | "premium" — the plan's tier; each engine maps it to its own settings.
    quality_tier: str = "standard"
    # The most this request may cost, as reserved. A fallback engine that would cost more is not tried.
    budget_usd: float | None = None
    # The job this request belongs to, for engines that submit to a provider queue. Never compared.
    link: ProviderLink | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class VideoRequest:
    prompt: str
    width: int
    height: int
    duration_seconds: int
    fps: int
    seed: int
    # For image-to-video: the first frame.
    source: bytes | None = None
    source_content_type: str | None = None
    # The most this request may cost, as reserved. A fallback engine that would cost more is not tried.
    budget_usd: float | None = None
    # The plan's video tier; it sets the quality floor an engine must meet (media/orchestrator.py).
    quality_tier: str = "standard"
    # A request planned from a photo before its bytes are loaded (a job is priced at creation; the photo is only
    # read right before an engine runs). Makes the chain choose and price image-to-video routes, not text ones.
    from_image: bool = False
    # The job this request belongs to, for engines that submit to a provider queue. Never compared.
    link: ProviderLink | None = field(default=None, compare=False, repr=False)

    @property
    def frames(self) -> int:
        return max(1, self.duration_seconds * self.fps + 1)


@dataclass(frozen=True)
class MediaOutput:
    data: bytes
    content_type: str
    width: int | None
    height: int | None
    duration_seconds: float | None
    engine_seconds: float
    # What a hosted call cost. None for self-hosted work, priced from engine_seconds.
    cost_usd: float | None = None
    # Which engines were tried and how each ended (media/orchestrator.py). Internal only.
    trace: tuple[dict, ...] = ()
    # Timings of the run that delivered: queue, cold start, inference, GPU seconds, total. Internal only.
    metrics: dict | None = None


class ImageEngine(ABC):
    @abstractmethod
    async def is_available(self, *, editing: bool = False) -> bool:
        """Configured and reachable for this operation. Never raises."""

    @abstractmethod
    async def generate(self, request: ImageRequest) -> MediaOutput:
        """Create an image (or edit `request.source` when present)."""

    def estimate_usd(self, request: ImageRequest) -> float | None:
        """The most this request can cost on this engine. None: priced from GPU time."""
        return None

    def typical_usd(self, request: ImageRequest) -> float | None:
        """What such a request usually costs — used to size daily offers, never a reservation."""
        return self.estimate_usd(request)


class VideoEngine(ABC):
    @abstractmethod
    async def is_available(self, *, from_image: bool = False) -> bool:
        """Configured and reachable for this operation. Never raises."""

    @abstractmethod
    async def generate(self, request: VideoRequest) -> MediaOutput:
        """Create a video (from `request.source` as first frame when present)."""

    def normalize(self, request: VideoRequest) -> VideoRequest:
        """The request this engine will actually run (its own durations, sizes, fps)."""
        return request

    def estimate_usd(self, request: VideoRequest) -> float | None:
        """The most this (normalized) request can cost. None: priced from GPU time."""
        return None


@dataclass(frozen=True)
class VisionResult:
    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    engine_seconds: float


class VisionEngine(ABC):
    """Reads an image and describes it in words the conversation model can use."""

    @abstractmethod
    async def is_available(self) -> bool:
        """Configured and reachable. Never raises."""

    @abstractmethod
    def estimate_usd(self) -> float:
        """The most one description can cost."""

    @abstractmethod
    async def describe(self, data: bytes, content_type: str) -> VisionResult:
        """A factual description of the image, including any text in it."""


# ── Provider error detail, sanitized (CP38, promoted here in CP46 so every hosted provider ──────
# shares one tested implementation instead of each keeping its own copy) ─────────────────────────

# Fields worth keeping from a provider status/error payload when it explains a failure — different
# real providers this codebase talks to (Higgsfield, fal) use different names for the same idea;
# status/error/message/code/reason/title/detail covers what has actually been observed. Anything
# else is dropped, not guessed at.
PROVIDER_DETAIL_FIELDS = ("status", "error", "error_type", "message", "code", "reason", "title", "detail")
# Defensive only: no provider response has been observed to echo a credential back, but a key whose
# name matches one of these is stripped before anything is logged or raised, on the assumption that
# a provider payload is never fully trusted.
SENSITIVE_KEY_MARKERS = ("key", "secret", "token", "authorization", "password", "credential", "cookie")


def safe_scalar(value):
    """A JSON scalar, truncated if it is an unexpectedly long string; None for anything else
    (nested structures beyond one level, bytes, ...) — dropped rather than guessed at."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > 500:
            return value[:500] + "...(truncated)"
        return value
    return None


def safe_provider_detail(state: dict) -> dict:
    """A sanitized, shallow view of a provider status/error payload — safe to log or attach to an
    EngineFailed. Keeps only the known-useful fields (PROVIDER_DETAIL_FIELDS) as JSON scalars (one
    level of dict-of-scalars for a nested field), and drops any key whose name suggests a
    credential. {} for a missing/malformed/empty payload — never raises."""
    if not isinstance(state, dict):
        return {}
    safe: dict = {}
    for key in PROVIDER_DETAIL_FIELDS:
        if key not in state or any(marker in key.lower() for marker in SENSITIVE_KEY_MARKERS):
            continue
        value = state[key]
        scalar = safe_scalar(value)
        if scalar is not None or value is None:
            safe[key] = scalar
        elif isinstance(value, dict):
            nested = {k: safe_scalar(v) for k, v in value.items()
                     if not any(marker in k.lower() for marker in SENSITIVE_KEY_MARKERS)}
            nested = {k: v for k, v in nested.items() if v is not None}
            if nested:
                safe[key] = nested
    return safe
