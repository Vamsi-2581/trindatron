"""Media engine orchestrator: many engines on many runners, the best eligible combination first.

A deployment lists candidate routes per operation (MEDIA_VIDEO_ROUTES =
"wan22_ti2v_5b@runpod,wan22_ti2v_5b@modal,ltx_video_13b_distilled@modal,grok_imagine_video@xai",
media/registry.py). The chain is itself an engine, so every caller —
media_service, vision_service, capability_service — keeps calling one engine and
never learns how many stand behind it, which model ran, or where.

How a request is served
-----------------------
Selection happens in layers, per request:

1. Capability and task — the route's engine must do this task (text-to-video,
   image-to-video, text-to-image, editing) and its runner must host it
   (`is_available` answers per task).
2. Licence and policy — a route whose licence does not allow use, or that needs
   experimental infrastructure in production, is never eligible (media/routes.py).
3. Quality floor — a route graded below the plan's floor (QUALITY_FLOOR) is not
   considered. Plans set the floor; they do not buy the dearest engine.
4. Ranking — among the rest: healthy before cooling down, a priced route before
   one that cannot price itself, then the cheapest for *this* request (each
   route's own normalized shape), then the operator's order. The cheapest engine
   that meets the floor and is healthy runs first — for every plan.
5. Budget — a route whose own estimate does not fit what was reserved
   (`request.budget_usd`) is skipped: a fallback never spends money the person
   was not charged for.
6. Execution and fallback — a failure the next route may not have (FALLBACK_KINDS:
   429, quota, unavailable, GPU unavailable, timeout, cold-start timeout,
   unsupported format or duration, OOM, temporary) hands over to the next
   route. A refused input or a filtered output stops the chain: another engine
   would refuse it too, and trying costs money.
7. Health — failures feed media/health.py; a route (or a whole runner, for quota
   and rate limits) that keeps failing cools down and is skipped until it recovers.

Every route is tried at most once per call: engine A on runner A, then engine A
on runner B, then engine B on runner A — never a loop, never a retry storm.

What happened is recorded on the output (`trace`) or on the failure
(`EngineChainFailed.trace`): route labels, outcome, failure kind, seconds,
estimate. It goes to logs and internal job records only, never to a user.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, replace

from src.media.engines import (
    EngineFailed,
    EngineUnavailable,
    FailureKind,
    ImageEngine,
    ImageRequest,
    MediaOutput,
    VideoEngine,
    VideoRequest,
    VisionEngine,
    VisionResult,
)
from src.media.health import RUNNER_WIDE_KINDS, health, runner_scope
from src.media.runners.base import IMAGE_EDIT, IMAGE_TO_VIDEO, TEXT_TO_IMAGE, TEXT_TO_VIDEO

logger = logging.getLogger("trinity.media.orchestrator")

# The lowest internal quality grade (media/adapters.py) a plan's tier accepts.
QUALITY_FLOOR = {"draft": 2, "standard": 3, "premium": 4}


def engine_label(engine: object) -> str:
    """A short internal name for logs and job records: a route's label, else the class ("XaiVideoEngine" -> "xai_video")."""
    label = getattr(engine, "label", None)
    if isinstance(label, str) and label:
        return label
    name = type(engine).__name__
    for suffix in ("Engine",):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    out = []
    for index, char in enumerate(name):
        if char.isupper() and index and not name[index - 1].isupper():
            out.append("_")
        out.append(char.lower())
    return "".join(out)


@dataclass(frozen=True)
class Attempt:
    engine: str
    # "completed" | "unavailable" | "failed" | "timeout" | "skipped_budget" | "skipped_cooldown" | "skipped_quality"
    outcome: str
    reason: str = ""
    seconds: float = 0.0
    estimate_usd: float | None = None
    kind: str = ""

    def as_dict(self) -> dict:
        return {"engine": self.engine, "outcome": self.outcome, "reason": self.reason,
                "seconds": round(self.seconds, 3), "estimate_usd": self.estimate_usd, "kind": self.kind}


_SKIPPED = ("unavailable", "skipped_budget", "skipped_cooldown", "skipped_quality")


class EngineChainFailed(EngineFailed):
    """Every route that could be tried was tried. Carries what happened, for logs only."""

    def __init__(self, reason: str, trace: list[Attempt], *, retryable: bool = False, unavailable: bool = False) -> None:
        super().__init__(reason, retryable=retryable)
        self.trace = trace
        self.unavailable = unavailable


def _fits(estimate: float | None, budget: float | None) -> bool:
    return budget is None or estimate is None or estimate <= budget + 1e-9


def _tracked(engine: object) -> bool:
    """Health is kept for routes (anything with an engine id and runner), not for anonymous engines."""
    return bool(getattr(engine, "engine_id", None) and getattr(engine, "runner_name", None))


def cooling_seconds(engine: object) -> float:
    if not _tracked(engine):
        return 0.0
    return max(health.cooling_seconds(engine_label(engine)),
               health.cooling_seconds(runner_scope(engine.runner_name)))  # type: ignore[attr-defined]


def _record_failure(engine: object, kind: str, retry_after: float | None) -> None:
    if not _tracked(engine):
        return
    scope = runner_scope(engine.runner_name) if kind in RUNNER_WIDE_KINDS else engine_label(engine)  # type: ignore[attr-defined]
    if health.record_failure(scope, kind, retry_after=retry_after):
        logger.warning("media health: %s cooling down after %s", scope, kind)


def _record_success(engine: object) -> None:
    if _tracked(engine):
        health.record_success(engine_label(engine))
        health.record_success(runner_scope(engine.runner_name))  # type: ignore[attr-defined]


def _safe_estimate(estimate, engine) -> float | None:
    try:
        return estimate(engine)
    except Exception:
        return None


def _does(engine: object, task: str | None) -> bool:
    """Task compatibility, without network: a route that cannot do this task is not a candidate at all."""
    check = getattr(engine, "supports_task", None)
    return task is None or check is None or bool(check(task))


def rank(engines: list, *, estimate, quality_tier: str | None = None,
         task: str | None = None) -> tuple[list, list[Attempt]]:
    """Eligible engines best-first for one request, and the ones the quality floor left out."""
    floor = QUALITY_FLOOR.get(quality_tier or "")
    rows, below = [], []
    for index, engine in enumerate(engines):
        if not _does(engine, task):
            continue
        quality = getattr(engine, "quality", None)
        if floor is not None and quality is not None and quality < floor:
            below.append(Attempt(engine_label(engine), "skipped_quality", f"grade {quality} below {floor}"))
            continue
        cost = _safe_estimate(estimate, engine)
        rows.append(((1 if cooling_seconds(engine) > 0 else 0, 0 if cost is not None else 1,
                      cost if cost is not None else math.inf, index), engine))
    rows.sort(key=lambda row: row[0])
    return [engine for _, engine in rows], below


async def run_chain(engines: list, *, available, estimate, call, timeout: float | None, what: str,
                     quality_tier: str | None = None, task: str | None = None):
    ranked, trace = rank(engines, estimate=estimate, quality_tier=quality_tier, task=task)
    last_retryable = False
    for engine in ranked:
        label = engine_label(engine)
        cost = _safe_estimate(estimate, engine)
        cooling = cooling_seconds(engine)
        if cooling > 0:
            trace.append(Attempt(label, "skipped_cooldown", f"{cooling:.0f}s left", estimate_usd=cost))
            continue
        if not _fits(cost, getattr(call, "budget", None)):
            trace.append(Attempt(label, "skipped_budget", estimate_usd=cost))
            continue
        try:
            ready = await available(engine)
        except Exception:
            ready = False
        if not ready:
            trace.append(Attempt(label, "unavailable", estimate_usd=cost, kind=FailureKind.UNAVAILABLE))
            continue
        started = time.monotonic()
        try:
            coroutine = call(engine)
            result = await (asyncio.wait_for(coroutine, timeout=timeout) if timeout else coroutine)
        except EngineUnavailable as exc:
            kind = getattr(exc, "kind", FailureKind.UNAVAILABLE)
            _record_failure(engine, kind, getattr(exc, "retry_after", None))
            trace.append(Attempt(label, "unavailable", str(exc)[:120], time.monotonic() - started, cost, kind))
            continue
        except asyncio.TimeoutError:
            _record_failure(engine, FailureKind.TIMEOUT, None)
            trace.append(Attempt(label, "timeout", "", time.monotonic() - started, cost, FailureKind.TIMEOUT))
            last_retryable = True
            continue
        except EngineFailed as exc:
            _record_failure(engine, exc.kind, exc.retry_after)
            trace.append(Attempt(label, "failed", exc.reason[:120], time.monotonic() - started, cost, exc.kind))
            if not exc.retryable:
                _log(what, trace, delivered=False)
                raise EngineChainFailed(exc.reason, trace, retryable=False) from exc
            last_retryable = True
            continue
        _record_success(engine)
        trace.append(Attempt(label, "completed", "", time.monotonic() - started, cost))
        _log(what, trace, delivered=True)
        return result, trace
    _log(what, trace, delivered=False)
    unavailable = all(attempt.outcome in _SKIPPED for attempt in trace)
    raise EngineChainFailed(f"{what}: no engine delivered", trace, retryable=last_retryable, unavailable=unavailable)


def _log(what: str, trace: list[Attempt], *, delivered: bool) -> None:
    summary = " > ".join(f"{a.engine}:{a.outcome}{'(' + a.kind + ')' if a.kind else ''}" for a in trace) or "none"
    fallback = sum(1 for a in trace if a.outcome != "completed")
    logger.info("media chain: %s | delivered=%s fallbacks=%d path=%s", what, delivered, fallback if delivered else len(trace), summary)


class _Budgeted:
    """A call bound to one request, exposing the budget the chain must respect."""

    def __init__(self, fn, budget: float | None) -> None:
        self._fn, self.budget = fn, budget

    def __call__(self, engine):
        return self._fn(engine)


def _first_ready(ranked: list, fallback):
    """The engine that sizes a request: the best-ranked one that is not cooling down."""
    return next((engine for engine in ranked if cooling_seconds(engine) <= 0), ranked[0] if ranked else fallback)


class ImageEngineChain(ImageEngine):
    def __init__(self, engines: list[ImageEngine]) -> None:
        if not engines:
            raise ValueError("an engine chain needs at least one engine")
        self.engines = list(engines)

    @property
    def primary(self) -> ImageEngine:
        return self.engines[0]

    @staticmethod
    def _task(request: ImageRequest) -> str:
        return IMAGE_EDIT if request.source is not None else TEXT_TO_IMAGE

    def primary_for(self, request: ImageRequest) -> ImageEngine:
        ranked, _ = rank(self.engines, estimate=lambda engine: engine.estimate_usd(request),
                         quality_tier=request.quality_tier, task=self._task(request))
        return _first_ready(ranked, self.primary)

    async def is_available(self, *, editing: bool = False, quality_tier: str | None = None) -> bool:
        ranked, _ = rank(self.engines, estimate=lambda engine: None, quality_tier=quality_tier,
                         task=IMAGE_EDIT if editing else TEXT_TO_IMAGE)
        for engine in ranked:
            if cooling_seconds(engine) > 0:
                continue
            try:
                if await engine.is_available(editing=editing):
                    return True
            except Exception:
                continue
        return False

    def estimate_usd(self, request: ImageRequest) -> float | None:
        return self.primary_for(request).estimate_usd(request)

    def typical_usd(self, request: ImageRequest) -> float | None:
        return self.primary_for(request).typical_usd(request)

    def headroom_usd(self, request: ImageRequest, affordable) -> float | None:
        """The most any eligible engine in the chain would cost for this request, among those `affordable(usd)` allows."""
        ranked, _ = rank(self.engines, estimate=lambda engine: engine.estimate_usd(request),
                         quality_tier=request.quality_tier, task=self._task(request))
        costs = [cost for cost in (engine.estimate_usd(request) for engine in ranked)
                 if cost is not None and affordable(cost)]
        return max(costs) if costs else self.estimate_usd(request)

    def subset(self, predicate) -> "ImageEngineChain | None":
        engines = [engine for engine in self.engines if predicate(engine)]
        return ImageEngineChain(engines) if engines else None

    async def generate(self, request: ImageRequest) -> MediaOutput:
        editing = request.source is not None
        output, trace = await run_chain(
            self.engines,
            available=lambda engine: engine.is_available(editing=editing),
            estimate=lambda engine: engine.estimate_usd(request),
            call=_Budgeted(lambda engine: engine.generate(request), request.budget_usd),
            timeout=None, what="image", quality_tier=request.quality_tier, task=self._task(request),
        )
        return replace(output, trace=tuple(a.as_dict() for a in trace))


class VideoEngineChain(VideoEngine):
    def __init__(self, engines: list[VideoEngine]) -> None:
        if not engines:
            raise ValueError("an engine chain needs at least one engine")
        self.engines = list(engines)

    @property
    def primary(self) -> VideoEngine:
        return self.engines[0]

    @staticmethod
    def _cost(engine: VideoEngine, request: VideoRequest) -> float | None:
        return engine.estimate_usd(engine.normalize(request))

    @staticmethod
    def _task(request: VideoRequest) -> str:
        return IMAGE_TO_VIDEO if request.source is not None or request.from_image else TEXT_TO_VIDEO

    def primary_for(self, request: VideoRequest) -> VideoEngine:
        ranked, _ = rank(self.engines, estimate=lambda engine: self._cost(engine, request),
                         quality_tier=request.quality_tier, task=self._task(request))
        return _first_ready(ranked, self.primary)

    async def is_available(self, *, from_image: bool = False, quality_tier: str | None = None) -> bool:
        ranked, _ = rank(self.engines, estimate=lambda engine: None, quality_tier=quality_tier,
                         task=IMAGE_TO_VIDEO if from_image else TEXT_TO_VIDEO)
        for engine in ranked:
            if cooling_seconds(engine) > 0:
                continue
            try:
                if await engine.is_available(from_image=from_image):
                    return True
            except Exception:
                continue
        return False

    def normalize(self, request: VideoRequest) -> VideoRequest:
        return self.primary_for(request).normalize(request)

    def estimate_usd(self, request: VideoRequest) -> float | None:
        return self.primary_for(request).estimate_usd(request)

    def headroom_usd(self, request: VideoRequest, affordable) -> float | None:
        """The most any eligible engine would cost for this request (in its own shape), among those affordable."""
        ranked, _ = rank(self.engines, estimate=lambda engine: self._cost(engine, request),
                         quality_tier=request.quality_tier, task=self._task(request))
        costs = [cost for cost in (self._cost(engine, request) for engine in ranked)
                 if cost is not None and affordable(cost)]
        return max(costs) if costs else self.estimate_usd(request)

    def subset(self, predicate) -> "VideoEngineChain | None":
        engines = [engine for engine in self.engines if predicate(engine)]
        return VideoEngineChain(engines) if engines else None

    async def generate(self, request: VideoRequest) -> MediaOutput:
        from_image = request.source is not None
        output, trace = await run_chain(
            self.engines,
            available=lambda engine: engine.is_available(from_image=from_image),
            estimate=lambda engine: self._cost(engine, request),
            call=_Budgeted(lambda engine: engine.generate(request), request.budget_usd),
            timeout=None, what="video", quality_tier=request.quality_tier, task=self._task(request),
        )
        return replace(output, trace=tuple(a.as_dict() for a in trace))


class VisionEngineChain(VisionEngine):
    """Readings are cheap and priced before the call: the most expensive engine sizes the reservation."""

    def __init__(self, engines: list[VisionEngine]) -> None:
        if not engines:
            raise ValueError("an engine chain needs at least one engine")
        self.engines = list(engines)
        self.last_trace: tuple[dict, ...] = ()

    async def is_available(self) -> bool:
        for engine in self.engines:
            try:
                if await engine.is_available():
                    return True
            except Exception:
                continue
        return False

    def estimate_usd(self) -> float:
        return max(engine.estimate_usd() for engine in self.engines)

    async def describe(self, data: bytes, content_type: str) -> VisionResult:
        result, trace = await run_chain(
            self.engines,
            available=lambda engine: engine.is_available(),
            estimate=lambda engine: engine.estimate_usd(),
            call=_Budgeted(lambda engine: engine.describe(data, content_type), None),
            timeout=None, what="vision",
        )
        self.last_trace = tuple(a.as_dict() for a in trace)
        return result
