"""Media creation rules: who may create what, how it is paid for, where it lands.

A creation is an asynchronous job. The HTTP request only validates, reserves
and records it (202); the engine runs in the background, the result is stored
in object storage as an Artifact, and clients follow the job until it is done.
No request ever waits on a generation.

Order of checks in create_*_job() — cheapest and most final first, and nothing
is reserved before every refusal that does not cost money has had its chance:

  1. input shape (prompt, duration, aspect)
  2. idempotency: the same key returns the same job, never a second charge
  3. kill switch, engine availability, storage availability
  4. plan: included at all, operation included, fits one request's ceiling
  5. daily count for this kind, in the user's own day
  6. one active job per kind per user (a double tap is not two jobs)
  7. project / source ownership
  8. TCU reservation through entitlement_service — the same windows, per-
     request ceiling and FREE plan-wide budget as chat. A reservation that
     comes back smaller than the estimate is refused rather than run: a
     creation cannot be delivered partially.

Pricing follows the engine that will run: a hosted engine estimates its own
price for the exact request (media/hosted.py); a self-hosted one is estimated
from GPU time (media/plans.py). The daily counts a plan offers are lowered to
what that price allows (entitlement_for).

Accounting: the reservation is finalized with the measured cost — what a hosted
call reported, or the GPU time a self-hosted engine reported — never more than
was reserved, and cancelled when the job fails, so a failed creation costs the
user nothing and does not count against their daily number.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session as DBSession

from src.database import SessionLocal
from src.entitlements import get_plan_entitlement
from src.media import economics, formats, modes, plans, vectorize as vectorize_module
from src.media import higgsfield
from src.media.engines import (
    EngineFailed,
    EngineUnavailable,
    ImageEngine,
    ImageRequest,
    JobCancelled,
    MediaOutput,
    ProviderHandle,
    ProviderLink,
    VideoEngine,
    VideoRequest,
)
from src.config import settings as core_settings
from src.media.runners.base import image_size, video_metadata
from src.media.operations import (
    OPERATION_INPUT_KINDS,
    OPERATION_OUTPUT_KIND,
    OperationEngineChain,
    OperationInput,
    OperationRequest,
    ResolvedInput,
    TRANSFORM_OPERATIONS,
)
from src.media.orchestrator import EngineChainFailed, ImageEngineChain, VideoEngineChain
from src.media.plans import MediaEntitlement, get_media_entitlement
from src.media.registry import image_engine, operation_engine, video_engine
from src.media.settings import media_settings
from src.models.media import MEDIA_TERMINAL_STATUSES, Artifact, MediaJob
from src.models.project import Project
from src.models.user import User
from src.observability.metrics import core_metrics
from src.services import entitlement_service
from src.services.runtime_settings import get_free_image_daily_limit, is_capability_disabled
from src.storage import build_object_store, local_store_is_acceptable
from src.storage.object_store import ObjectStore
from src.timezones import day_window

logger = logging.getLogger("trinity.media")

_jobs_created = core_metrics.counter("media_jobs_created_total", "Media jobs accepted, labeled by kind.")
_jobs_refused = core_metrics.counter("media_jobs_refused_total", "Media requests refused, labeled by reason.")
_jobs_finished = core_metrics.counter("media_jobs_finished_total", "Media jobs finished, labeled by kind and outcome.")

MAX_PROMPT_CHARS = 2000
# Legacy shape names still accepted from older clients; any other format arrives as text or pixels (media/formats.py).
IMAGE_ASPECTS = {"square": (1, 1), "portrait": (3, 4), "landscape": (4, 3)}
VIDEO_ASPECTS = {"landscape": (16, 9), "portrait": (9, 16), "square": (1, 1)}
# The widest spread of shapes a plan's price is checked against: the most one creation can cost is across all of them.
_PRICE_CHECK_FORMATS = (formats.FormatSpec(1, 1), formats.FormatSpec(9, 16), formats.FormatSpec(16, 9),
                        formats.FormatSpec(4, 1), formats.FormatSpec(1, 4))
_MAX_OUTPUT_BYTES = {"image": 25 * 1024 * 1024, "video": 200 * 1024 * 1024}
# CP19: image sources only. CP22 generalizes store_source_image into store_source_media(kind=...)
# for Wan2.2-Animate's driving_video — audio deliberately not added yet (no operation needs it
# until S2V, a separate future checkpoint). image/video limits stay separate settings
# (media_max_source_bytes / media_max_video_source_bytes) — a driving video clip is legitimately
# much larger than a reference image, and raising one shared cap for both would either starve
# video uploads or let image uploads balloon for no reason.
_ALLOWED_SOURCE_TYPES_BY_KIND = {
    "image": {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"},
    "video": {"video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov"},
}
# The bytes a file must actually start with to be what its declared content-type claims — the
# client's Content-Type header is never trusted on its own (CP22).
_MP4_LIKE = {"video/mp4", "video/quicktime"}


def _sniff_ok(data: bytes, content_type: str) -> bool:
    if content_type in _MP4_LIKE:
        return data[4:8] == b"ftyp"  # ISO base media file box, at a fixed offset for every MP4/MOV
    if content_type == "image/webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if content_type == "image/png":
        return data[:8] == b"\x89PNG\r\n\x1a\n"
    if content_type == "image/jpeg":
        return data[:3] == b"\xff\xd8\xff"
    if content_type == "video/webm":
        return data[:4] == b"\x1a\x45\xdf\xa3"
    return False


_OUTPUT_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp",
                      "video/mp4": "mp4", "video/webm": "webm", "image/gif": "gif"}

# Messages a user may read. Short, neutral, and free of anything internal.
MESSAGES = {
    "invalid_request": "Check the request and try again.",
    "media_unavailable": "Creating this is not available right now.",
    "plan_not_included": "Your current plan does not include this.",
    "daily_limit_reached": "You have used today's creations for this plan.",
    "budget_exhausted": "There is not enough capacity left in your current usage window.",
    "job_in_progress": "A creation is already in progress.",
    "not_found": "Not found.",
    "cannot_cancel": "This creation can no longer be cancelled.",
    "capacity_exceeded": "Creation capacity is at its limit right now. Please try again shortly.",
    "confirmation_required": "This mode uses premium capacity. Confirm it to continue.",
    # Job outcomes (not refusals): shown on a failed job.
    "failed": "The creation could not be completed. Nothing was charged.",
}

_background: set[asyncio.Task] = set()
# The provider job behind each creation running in this process (media/engines.py ProviderLink).
_links: dict[str, ProviderLink] = {}
# A provider keeps outputs at least this long; a job it accepted is followed, not failed, within it.
PROVIDER_FOLLOW_DAYS = 6


class MediaRefused(Exception):
    """A request that will not become a job. `code` is stable; `message` is user-safe."""

    STATUS = {"invalid_request": 422, "media_unavailable": 503, "plan_not_included": 403,
              "daily_limit_reached": 429, "budget_exhausted": 429, "job_in_progress": 409, "not_found": 404,
              "cannot_cancel": 409, "capacity_exceeded": 429, "confirmation_required": 409}

    def __init__(self, code: str, *, resets_at: datetime | None = None, details: dict | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = self.STATUS[code]
        self.message = MESSAGES[code]
        self.resets_at = resets_at
        # User-safe facts about the refusal (a premium clip's TCU, its length); never an engine or a vendor.
        self.details = details or {}


@dataclass(frozen=True)
class DailyUsage:
    limit: int
    used: int
    resets_at: datetime

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


# ── Pricing on the configured engine ─────────────────────────────────────────

def _image_usd(engine: ImageEngine | None, request: ImageRequest) -> float:
    estimated = engine.estimate_usd(request) if engine is not None else None
    if estimated is not None:
        return estimated
    return plans.estimate_image_usd(width=request.width, height=request.height, quality_tier=request.quality_tier,
                                    gpu_usd_per_hour=media_settings().media_gpu_usd_per_hour)


def _video_usd(engine: VideoEngine | None, request: VideoRequest, mode: modes.VideoMode | None = None) -> float:
    """The route's own price; else the mode's measured reference; else the documented GPU model."""
    estimated = engine.estimate_usd(request) if engine is not None else None
    if estimated is not None:
        return estimated
    if mode is not None:
        return mode.reference_usd(request.duration_seconds)
    return plans.estimate_video_usd(short_side=max(1, min(request.width, request.height)),
                                    duration_seconds=request.duration_seconds,
                                    gpu_usd_per_hour=media_settings().media_gpu_usd_per_hour)


def image_unit_usd(base: MediaEntitlement, engine: ImageEngine | None) -> float:
    """What one image usually costs on this plan and engine, in its dearest format — the unit a daily offer is sized on.

    A typical request, not the longest prompt accepted: the offer is what a plan
    can pay for day after day, while every single request still reserves its own
    worst case before any engine runs.
    """
    quality = base.image_quality_tier
    worst = 0.0
    for spec in _PRICE_CHECK_FORMATS:
        width, height = image_dimensions_for(spec, base)
        request = ImageRequest(prompt="", width=width, height=height, seed=0,
                               steps=plans.QUALITY_STEPS.get(quality, 8), quality_tier=quality)
        typical = engine.typical_usd(request) if engine is not None else None
        worst = max(worst, typical if typical is not None else _image_usd(engine, request))
    return worst


def _reservation_usd(plan: str, engine, request, primary_usd: float) -> float:
    """What to reserve: enough for the dearest fallback this plan's per-request ceiling allows, never less than the primary."""
    if isinstance(engine, (ImageEngineChain, VideoEngineChain)):
        headroom = engine.headroom_usd(request, lambda usd: plans.fits_request_ceiling(plan, usd))
        if headroom is not None:
            return max(primary_usd, headroom)
    return primary_usd


def video_offer(plan: str, base: MediaEntitlement, engine: VideoEngine | None,
                mode: modes.VideoMode | None = None) -> tuple[int, float] | None:
    """(longest clip in seconds, cost of the mode's default clip) this plan can be offered in `mode`, or None.

    `mode` defaults to the plan's default mode. Only the routes of the mode's own
    class are considered (media/modes.py), at the mode's resolution: the longest
    clip up to the plan's and the mode's ceiling that the engine can make and that
    fits one request's ceiling. The cost returned — what a daily count is sized on —
    is the mode's default clip (5 s), which the media pool must pay for at least
    once a day; a longer clip asked for simply costs its own reservation.
    """
    if base.video_daily_limit <= 0 or base.max_video_resolution <= 0 or base.max_video_duration_seconds <= 0:
        return None
    mode = mode or modes.get(base.default_video_mode)
    longest, short_side = base.max_video_duration_seconds, base.max_video_resolution
    tier, count = base.video_quality_tier, base.video_daily_limit
    lengths = range(longest, 0, -1)
    if mode is not None:
        if mode.id not in base.video_modes:
            return None
        if engine is not None:
            engine = modes.restrict(engine, mode)
            if engine is None:
                return None
        longest, short_side, tier = min(longest, mode.max_seconds), mode.short_side, mode.quality_tier
        count = base.mode_cap(mode.id) or count
        lengths = modes.durations(longest)  # never shorter than the router would shorten a clip
    width, height = video_dimensions_for(formats.from_request(kind="video", aspect="landscape"), base, short_side)
    for duration in lengths:
        request = VideoRequest(prompt="", width=width, height=height, duration_seconds=duration,
                               fps=plans.VIDEO_FPS, seed=0, quality_tier=tier)
        if engine is not None:
            request = engine.normalize(request)
        if request.duration_seconds > longest:
            continue  # the engine's shortest clip is longer than this plan is sold with
        usd = _video_usd(engine, request, mode)
        if not plans.fits_request_ceiling(plan, usd):
            continue
        sizing_usd = usd
        if mode is not None and request.duration_seconds > mode.default_seconds:
            default = replace(request, duration_seconds=mode.default_seconds)
            sizing_usd = _video_usd(engine, engine.normalize(default) if engine is not None else default, mode)
        if plans.affordable_daily_count(plan, count, sizing_usd) >= 1:
            return request.duration_seconds, sizing_usd
    return None


# ── Entitlement and usage ────────────────────────────────────────────────────

def _experimental_allowed() -> bool:
    return media_settings().media_allow_experimental_runners and not core_settings.is_production


def guaranteed_engine(engine):
    """The part of an engine a plan's promise may rest on: routes on commercial infrastructure only.

    A free, shared runner (ZeroGPU) can queue or run out at any moment, so it never
    sizes what a plan is sold with — even when it would cost nothing.
    """
    if engine is None:
        return None
    if isinstance(engine, (ImageEngineChain, VideoEngineChain)):
        return engine.subset(lambda route: getattr(route, "commercial", True))
    return engine if getattr(engine, "commercial", True) else None


async def _ready(engine, quality_tier: str, **kwargs) -> bool:
    """Some route can serve this operation at the plan's quality floor (a chain checks the floor; a lone engine cannot)."""
    if isinstance(engine, (ImageEngineChain, VideoEngineChain)):
        return await engine.is_available(quality_tier=quality_tier, **kwargs)
    return await engine.is_available(**kwargs)


def _priced(engine) -> tuple[object | None, bool]:
    """(engine to price the offer on, whether that offer is guaranteed)."""
    guaranteed = guaranteed_engine(engine)
    if guaranteed is not None:
        return guaranteed, True
    if engine is not None and _experimental_allowed():
        return engine, False
    return None, True


def entitlement_for(db: DBSession, user: User) -> MediaEntitlement:
    """The plan's media entitlement: reference counts lowered to what the media pool pays for on these engines.

    Video: the modes (media/modes.py) this plan may use that a configured route can
    serve within one request's ceiling, each mode's daily cap, and the default mode
    that sizes the daily video count. Premium is offered only as itself — never as a
    default — so it is reached only by a confirmed request.
    """
    base = get_media_entitlement(user.plan, free_image_daily_limit=get_free_image_daily_limit(db))
    image_priced, image_guaranteed = _priced(image_engine())
    image_count = plans.affordable_daily_count(user.plan, base.image_daily_limit, image_unit_usd(base, image_priced))
    base = replace(base, image_guaranteed=image_guaranteed)

    video_priced, video_guaranteed = _priced(video_engine())
    trial = media_settings().media_free_experimental_video_daily_limit
    if not video_guaranteed and base.plan == "free" and trial > 0:
        # FREE experimental video, outside production only: START's economy ceilings, the operator's daily number.
        start = get_media_entitlement("start")
        base = replace(base, video_daily_limit=trial, video_quality_tier=start.video_quality_tier,
                       max_video_duration_seconds=start.max_video_duration_seconds,
                       max_video_resolution=start.max_video_resolution, video_modes=("economy",),
                       default_video_mode="economy", video_mode_caps=(("economy", trial),))
    base = replace(base, video_guaranteed=video_guaranteed)

    offers: dict[str, tuple[int, float]] = {}
    for mode_id in base.video_modes:
        offer = video_offer(user.plan, base, video_priced, modes.MODES[mode_id])
        if offer is not None:
            offers[mode_id] = offer
    if not offers:
        return replace(base, image_daily_limit=image_count, video_daily_limit=0, video_modes=(),
                       default_video_mode=None, video_mode_caps=())
    default = _default_mode(base, offers)
    sizing = default or modes.PREMIUM.id
    unit = offers[sizing][1]
    caps = tuple((mode_id, plans.affordable_daily_count(user.plan, base.mode_cap(mode_id), offer[1]))
                 for mode_id, offer in offers.items())
    # Never more clips a day than the offered modes' caps add up to (MAX with only premium: 2, not 3).
    videos = min(plans.affordable_daily_count(user.plan, base.video_daily_limit, unit), sum(cap for _, cap in caps))
    image_count, video_count = plans.joint_daily_counts(
        user.plan, image_count, image_unit_usd(base, image_priced), videos, unit)
    return replace(base, image_daily_limit=image_count, video_daily_limit=video_count,
                   max_video_duration_seconds=max(offer[0] for offer in offers.values()),
                   max_video_resolution=max(modes.MODES[mode_id].short_side for mode_id in offers),
                   video_quality_tier=modes.MODES[sizing].quality_tier,
                   video_modes=tuple(offers), default_video_mode=default, video_mode_caps=caps)


def _default_mode(base: MediaEntitlement, offers: dict) -> str | None:
    """The plan's default mode if it is offered, else the best offered mode below it, else the cheapest; never premium."""
    wanted = modes.get(base.default_video_mode) or modes.ECONOMY
    usable = [modes.MODES[m] for m in offers if not modes.MODES[m].requires_confirmation]
    below = [mode for mode in usable if mode.rank <= wanted.rank]
    pick = max(below, key=lambda mode: mode.rank) if below else min(usable, key=lambda mode: mode.rank, default=None)
    return pick.id if pick is not None else None


def daily_usage(db: DBSession, user: User, kind: str, entitlement: MediaEntitlement) -> DailyUsage:
    start, end = day_window(_now(), user.timezone)
    used = (
        db.query(MediaJob)
        .filter(MediaJob.user_id == user.id, MediaJob.kind == kind, MediaJob.status != "failed",
                MediaJob.created_at >= start, MediaJob.created_at < end)
        .count()
    )
    limit = entitlement.image_daily_limit if kind == "image" else entitlement.video_daily_limit
    return DailyUsage(limit=limit, used=used, resets_at=end)


def _premium_media_usd_used_today(db: DBSession, user: User, operation: str) -> float:
    """What `user` has already reserved/spent on `operation` today (media/economics.py's daily
    ceiling) — same day window and same "not failed" convention as daily_usage() above. Sums
    cost_usd for settled jobs and estimated_cost_usd for ones still in flight (queued/running):
    the budget is consumed at reservation time, exactly like entitlement_service's own windows,
    not only once a job finishes."""
    start, end = day_window(_now(), user.timezone)
    jobs = (
        db.query(MediaJob)
        .filter(MediaJob.user_id == user.id, MediaJob.operation == operation, MediaJob.status != "failed",
                MediaJob.created_at >= start, MediaJob.created_at < end)
        .all()
    )
    return sum(float(job.cost_usd if job.cost_usd is not None else job.estimated_cost_usd) for job in jobs)


@dataclass(frozen=True)
class MediaPool:
    """One pool for images, videos and operations: each window less the chat/agent floor (media/plans.py)."""
    daily_limit: float | None
    daily_used: float
    weekly_limit: float | None
    weekly_used: float

    @property
    def remaining(self) -> float | None:
        left = [limit - used for limit, used in ((self.daily_limit, self.daily_used),
                                                 (self.weekly_limit, self.weekly_used)) if limit is not None]
        return max(0.0, min(left)) if left else None


def media_pool(db: DBSession, user: User) -> MediaPool:
    """The pool as the reservation path sees it: every media-tagged usage record, pending ones included."""
    daily_used, weekly_used = entitlement_service.media_tcu_used(db, user.id, user.timezone)
    return MediaPool(daily_limit=plans.media_daily_pool_tcu(user.plan), daily_used=daily_used,
                     weekly_limit=plans.media_weekly_pool_tcu(user.plan), weekly_used=weekly_used)


def media_room_tcu(db: DBSession, user: User) -> float | None:
    """What one creation may reserve right now: the media pool and every TCU window, whichever is smaller."""
    left = [room for room in (media_pool(db, user).remaining,
                              entitlement_service.remaining_budget_tcu(db, user.id, user.plan, user.timezone))
            if room is not None]
    return min(left) if left else None


def _video_mode_usage_today(db: DBSession, user: User) -> dict[str, int]:
    start, end = day_window(_now(), user.timezone)
    rows = (
        db.query(MediaJob.params)
        .filter(MediaJob.user_id == user.id, MediaJob.kind == "video", MediaJob.status.notin_(("failed", "cancelled")),
                MediaJob.created_at >= start, MediaJob.created_at < end)
        .all()
    )
    used: dict[str, int] = {}
    for (params,) in rows:
        mode = (params or {}).get("mode")
        if mode:
            used[mode] = used.get(mode, 0) + 1
    return used


def image_dimensions_for(spec: formats.FormatSpec, entitlement: MediaEntitlement) -> tuple[int, int]:
    """The asked-for proportion at the size this plan creates (the long side capped, never above what was asked)."""
    return formats.fit(spec, long_side=min(1024, entitlement.max_image_resolution))


def video_dimensions_for(spec: formats.FormatSpec, entitlement: MediaEntitlement,
                         short_side: int | None = None) -> tuple[int, int]:
    """The asked-for proportion with the short side at a mode's resolution (default: the plan's highest)."""
    return formats.fit_short_side(spec, short_side=short_side or entitlement.max_video_resolution)


def image_dimensions(aspect: str, entitlement: MediaEntitlement) -> tuple[int, int]:
    return image_dimensions_for(formats.from_request(kind="image", aspect=aspect), entitlement)


def video_dimensions(aspect: str, entitlement: MediaEntitlement) -> tuple[int, int]:
    return video_dimensions_for(formats.from_request(kind="video", aspect=aspect), entitlement)


def _format_spec(kind: str, *, aspect: str | None, format_text: str | None, width: int | None,
                 height: int | None) -> formats.FormatSpec:
    """The requested format, or a refusal when a legacy shape name is not one of the known three."""
    if not format_text and not (width and height):
        legacy = IMAGE_ASPECTS if kind == "image" else VIDEO_ASPECTS
        if (aspect or "") not in legacy:
            raise MediaRefused("invalid_request")
    if (width or height) and not (width and height):
        raise MediaRefused("invalid_request")
    return formats.from_request(kind=kind, format_text=format_text, aspect=aspect, width=width, height=height)


def _legacy_shape(aspect: str | None, format_text: str | None, width: int | None, height: int | None) -> dict:
    """The legacy shape name, recorded only when that is what actually decided the format."""
    return {} if (format_text or (width and height)) else {"aspect": aspect}


async def storage_ready(store: ObjectStore | None = None) -> bool:
    store = store or build_object_store()
    if store.backend_name == "local" and not local_store_is_acceptable():
        return False
    return await store.is_available()


# ── Creation ─────────────────────────────────────────────────────────────────

def _clean_prompt(prompt: str) -> str:
    text = " ".join((prompt or "").split())
    if not text or len(text) > MAX_PROMPT_CHARS:
        raise MediaRefused("invalid_request")
    return text


def _existing(db: DBSession, user: User, idempotency_key: str | None) -> MediaJob | None:
    if not idempotency_key:
        return None
    return db.query(MediaJob).filter(MediaJob.user_id == user.id, MediaJob.idempotency_key == idempotency_key).first()


def _refuse(code: str, **kwargs) -> MediaRefused:
    _jobs_refused.inc(reason=code)
    return MediaRefused(code, **kwargs)


def _admission_check(db: DBSession, *, operation: str) -> None:
    """CP19: DB-backed capacity limits — Postgres COUNT queries against MediaJob's own indexed
    `status` column, no new infrastructure. Global and per-operation now; per-provider/per-runner
    is a real future dimension, but that data lives only inside params/provider_ref JSON today —
    adding it needs an indexed column, a deliberate follow-up, not a redesign of this function.
    Per-tenant fairness already exists structurally: one active job per (user, kind) below."""
    settings = media_settings()
    in_flight = db.query(MediaJob).filter(MediaJob.status.in_(("queued", "running")))
    if settings.media_max_global_in_flight and in_flight.count() >= settings.media_max_global_in_flight:
        raise _refuse("capacity_exceeded")
    if settings.media_max_per_operation_in_flight:
        per_operation = in_flight.filter(MediaJob.operation == operation).count()
        if per_operation >= settings.media_max_per_operation_in_flight:
            raise _refuse("capacity_exceeded")


async def _create(db: DBSession, user: User, *, kind: str, operation: str, prompt: str, params: dict,
                  estimated_usd: float, daily: DailyUsage, engine_ready: bool, project_id: str | None,
                  source_artifact_id: str | None, idempotency_key: str | None,
                  reserve_usd: float | None = None, source_kind: str = "image",
                  extra_artifact_ids: tuple[tuple[str, str], ...] = ()) -> MediaJob:
    """`estimated_usd` is what the first engine will cost; `reserve_usd` (≥ it) also covers an affordable
    fallback. `source_kind` (CP22): the Artifact.kind the primary source must already be — "image" by
    default (create_image_job/create_video_job both pass an image source, unchanged), but a multi-input
    operation whose primary role isn't an image (none exist yet, but the check must not assume it never
    will) passes its own. `extra_artifact_ids` (CP19, reshaped CP22): every input of a multi-input
    operation beyond the primary one, as (artifact_id, expected_kind) pairs — each checked against its
    OWN declared media type, not left unchecked the way a bare id tuple would have to be."""
    if is_capability_disabled(db, kind) or not engine_ready or not await storage_ready():
        raise _refuse("media_unavailable")
    if operation in economics.PREMIUM_MEDIA_OPERATIONS:
        # CP36: Media Economics — a separate budget for operations priced far above what
        # max_tcu_per_request allows (media/economics.py's own module docstring has the full
        # reasoning). This replaces the ordinary count-ceiling/fits_request_ceiling pair below
        # for these operations only; every other operation (background_remove, vectorize,
        # image/video GENERATE) keeps using that pair completely unchanged, and
        # entitlement_service.check_and_reserve's session/daily/weekly windows still run,
        # unmodified, right after this block for premium operations exactly like for any other.
        if not economics.operation_allowed(operation, user.plan):
            raise _refuse("plan_not_included")
        if not economics.fits_media_request_ceiling(operation, user.plan, estimated_usd):
            raise _refuse("plan_not_included")
        used_today = _premium_media_usd_used_today(db, user, operation)
        if not economics.fits_media_daily_ceiling(operation, user.plan, used_today, estimated_usd):
            raise _refuse("daily_limit_reached", resets_at=daily.resets_at)
    else:
        if daily.limit <= 0 or not plans.fits_request_ceiling(user.plan, estimated_usd):
            raise _refuse("plan_not_included")
        if daily.used >= daily.limit:
            raise _refuse("daily_limit_reached", resets_at=daily.resets_at)
    _admission_check(db, operation=operation)

    active = (
        db.query(MediaJob)
        .filter(MediaJob.user_id == user.id, MediaJob.kind == kind, MediaJob.status.in_(("queued", "running")))
        .first()
    )
    if active is not None:
        raise _refuse("job_in_progress")

    if project_id and not db.query(Project).filter(Project.id == project_id, Project.user_id == user.id).first():
        raise _refuse("not_found")
    if source_artifact_id and not db.query(Artifact).filter(
        Artifact.id == source_artifact_id, Artifact.user_id == user.id, Artifact.kind == source_kind
    ).first():
        raise _refuse("not_found")
    for artifact_id, expected_kind in extra_artifact_ids:
        if not db.query(Artifact).filter(
            Artifact.id == artifact_id, Artifact.user_id == user.id, Artifact.kind == expected_kind
        ).first():
            raise _refuse("not_found")

    estimated_tcu = plans.usd_to_tcu(estimated_usd)
    # The media pool (media never spends the floor kept for chat and agents) is checked and clamped inside
    # check_and_reserve, under the per-user lock, and the reservation is tagged as media the moment it is
    # written: a concurrent creation sees it before this job row exists.
    try:
        reservation_id = entitlement_service.check_and_reserve(
            db, user.id, None, user.plan, user.timezone,
            estimated_tcu=plans.usd_to_tcu(max(estimated_usd, reserve_usd or 0.0)),
            model=f"{entitlement_service.MEDIA_MODEL_PREFIX}{kind}",
            media_pool_tcu=(plans.media_daily_pool_tcu(user.plan), plans.media_weekly_pool_tcu(user.plan)),
        )
    except entitlement_service.MediaPoolExhausted:
        raise _refuse("budget_exhausted")
    # A window with room for the first engine but not for a fallback still gets the creation; only the fallback is lost.
    if entitlement_service.get_reservation_budget_tcu(db, reservation_id) + 1e-6 < estimated_tcu:
        entitlement_service.cancel_reservation(db, reservation_id)
        raise _refuse("budget_exhausted")

    job = MediaJob(
        user_id=user.id, project_id=project_id, kind=kind, operation=operation, status="queued",
        prompt=prompt, params=params, source_artifact_id=source_artifact_id,
        idempotency_key=idempotency_key, reservation_id=reservation_id,
        estimated_cost_usd=Decimal(str(round(estimated_usd, 8))),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    _jobs_created.inc(kind=kind)
    logger.info("media: job accepted | job=%s kind=%s operation=%s user=%s", job.id, kind, operation, user.id)
    return job


async def create_image_job(db: DBSession, user: User, *, prompt: str, aspect: str = "square",
                           format_text: str | None = None, width: int | None = None, height: int | None = None,
                           project_id: str | None = None, source_artifact_id: str | None = None,
                           idempotency_key: str | None = None) -> MediaJob:
    text = _clean_prompt(prompt)
    spec = _format_spec("image", aspect=aspect, format_text=format_text, width=width, height=height)
    shape = _legacy_shape(aspect, format_text, width, height)
    existing = _existing(db, user, idempotency_key)
    if existing is not None:
        return existing

    entitlement = entitlement_for(db, user)
    width, height = image_dimensions_for(spec, entitlement)
    editing = source_artifact_id is not None
    engine = image_engine()
    engine_ready = engine is not None and await _ready(engine, entitlement.image_quality_tier, editing=editing)
    if editing and not entitlement.image_editing:
        raise _refuse("plan_not_included")
    quality = entitlement.image_quality_tier
    request = ImageRequest(prompt=text, width=width, height=height, seed=_seed(),
                           steps=plans.QUALITY_STEPS[quality], quality_tier=quality)
    params = {"width": width, "height": height, "quality_tier": quality,
              "steps": request.steps, "seed": request.seed, **shape, **spec.as_params()}
    estimated = _image_usd(engine, request)
    return await _create(db, user, kind="image", operation="edit" if editing else "generate", prompt=text,
                         params=params, estimated_usd=estimated,
                         reserve_usd=_reservation_usd(user.plan, engine, request, estimated),
                         daily=daily_usage(db, user, "image", entitlement), engine_ready=engine_ready,
                         project_id=project_id, source_artifact_id=source_artifact_id,
                         idempotency_key=idempotency_key)


async def create_video_job(db: DBSession, user: User, *, prompt: str, aspect: str = "landscape",
                           format_text: str | None = None, width: int | None = None, height: int | None = None,
                           duration_seconds: int | None = None, project_id: str | None = None,
                           source_artifact_id: str | None = None, idempotency_key: str | None = None,
                           mode: str | None = None, confirm_premium: bool = False) -> MediaJob:
    """A video in a mode (media/modes.py), routed within what the person can spend right now.

    The mode asked for (else the plan's default) is tried first. When it does not fit
    one request's ceiling, the media pool, the TCU windows or the mode's daily cap, the
    router moves down in order: a shorter clip, then a lower resolution, then a cheaper
    engine. What it changed is kept in params["adjustments"] for the reply. When nothing
    fits, the request is refused: nothing ever runs above the budget. Premium runs only
    when asked for by name and confirmed (`confirm_premium`); it is never a fallback.
    """
    text = _clean_prompt(prompt)
    spec = _format_spec("video", aspect=aspect, format_text=format_text, width=width, height=height)
    shape = _legacy_shape(aspect, format_text, width, height)
    existing = _existing(db, user, idempotency_key)
    if existing is not None:
        return existing

    entitlement = entitlement_for(db, user)
    # A plan without video (or whose video no route's price fits) is told so before its request shape is judged.
    if entitlement.video_daily_limit <= 0 or not entitlement.video_modes:
        raise _refuse("plan_not_included")
    from_image = source_artifact_id is not None
    if from_image and not entitlement.image_to_video:
        raise _refuse("plan_not_included")
    requested = modes.get(mode or entitlement.default_video_mode or modes.PREMIUM.id)
    if requested is None:
        raise MediaRefused("invalid_request")
    if from_image and requested.engine_class == modes.EFFICIENT:
        # Photo -> Movement: the efficient engines ignore the photo (measured 2026-09-25), so a photo runs in
        # Maximum whatever mode the plan defaults to — not an upgrade the person has to ask for.
        requested = modes.MAXIMUM
    # Nothing asked for: the mode's default clip (5 s), never the longest the plan allows.
    duration = (min(requested.default_seconds, entitlement.max_video_duration_seconds)
                if duration_seconds is None else duration_seconds)
    if duration < 1 or (entitlement.max_video_duration_seconds and duration > entitlement.max_video_duration_seconds):
        raise MediaRefused("invalid_request")
    start = requested if requested.id in entitlement.video_modes else _best_offered_below(requested, entitlement)
    if start is None:
        raise _refuse("plan_not_included")

    engine = video_engine()
    if start.requires_confirmation and not confirm_premium:
        routed = modes.restrict(engine, start)
        probe = _mode_request(text, spec, entitlement, start, min(duration, start.max_seconds))
        probe = routed.normalize(probe) if routed is not None else probe
        raise _refuse("confirmation_required", details={
            "mode": start.id, "duration_seconds": probe.duration_seconds,
            "estimated_tcu": round(plans.usd_to_tcu(_video_usd(routed, probe, start)), 2)})

    daily = daily_usage(db, user, "video", entitlement)
    if daily.used >= daily.limit:
        raise _refuse("daily_limit_reached", resets_at=daily.resets_at)
    used_by_mode = _video_mode_usage_today(db, user)
    room = media_room_tcu(db, user)
    ceiling = get_plan_entitlement(user.plan).max_tcu_per_request
    blocked = {"cap": False, "budget": False, "ceiling": False}
    chosen = None
    for candidate in modes.ladder(start, entitlement.video_modes):
        if from_image and candidate.engine_class == modes.EFFICIENT:
            continue  # never down to an engine that would deliver a clip ignoring the person's photo
        if used_by_mode.get(candidate.id, 0) >= entitlement.mode_cap(candidate.id):
            blocked["cap"] = True
            continue
        routed = modes.restrict(engine, candidate)
        if routed is None or not await _ready(routed, candidate.quality_tier, from_image=from_image):
            continue
        for seconds in modes.durations(min(duration, candidate.max_seconds)):
            request = routed.normalize(_mode_request(text, spec, entitlement, candidate, seconds, from_image))
            if request.duration_seconds > seconds:
                continue  # the engine's shortest clip is longer than this
            usd = _video_usd(routed, request, candidate)
            tcu = plans.usd_to_tcu(usd)
            if tcu > ceiling + 1e-6:
                blocked["ceiling"] = True
                continue
            if room is not None and tcu > room + 1e-6:
                blocked["budget"] = True
                continue
            chosen = (candidate, routed, request, usd)
            break
        if chosen is not None:
            break
    if chosen is None:
        if blocked["budget"]:
            raise _refuse("budget_exhausted")
        if blocked["cap"]:
            raise _refuse("daily_limit_reached", resets_at=daily.resets_at)
        raise _refuse("plan_not_included" if blocked["ceiling"] else "media_unavailable")

    candidate, routed, request, usd = chosen
    # Relative to the mode the plan allows: a mode above the plan is its own adjustment ("plan").
    changed = modes.adjustments(start, candidate, duration, request.duration_seconds,
                                above_plan=start is not requested)
    params = {"width": request.width, "height": request.height,
              "duration_seconds": request.duration_seconds, "fps": request.fps,
              "quality_tier": candidate.quality_tier, "seed": request.seed,
              "mode": candidate.id, "requested_mode": requested.id, "requested_duration_seconds": duration,
              "adjustments": list(changed), **shape, **spec.as_params()}
    return await _create(db, user, kind="video", operation="image_to_video" if from_image else "generate",
                         prompt=text, params=params, estimated_usd=usd,
                         reserve_usd=_reservation_usd(user.plan, routed, request, usd),
                         daily=daily, engine_ready=True,
                         project_id=project_id, source_artifact_id=source_artifact_id,
                         idempotency_key=idempotency_key)


def _best_offered_below(requested: modes.VideoMode, entitlement: MediaEntitlement) -> modes.VideoMode | None:
    """A mode above the plan becomes the best mode the plan offers below it (never premium)."""
    below = [modes.MODES[m] for m in entitlement.video_modes
             if modes.MODES[m].rank < requested.rank and not modes.MODES[m].requires_confirmation]
    return max(below, key=lambda mode: mode.rank) if below else None


def _mode_request(text: str, spec: formats.FormatSpec, entitlement: MediaEntitlement, mode: modes.VideoMode,
                  seconds: int, from_image: bool = False) -> VideoRequest:
    width, height = video_dimensions_for(spec, entitlement, mode.short_side)
    return VideoRequest(prompt=text, width=width, height=height, duration_seconds=seconds,
                        fps=plans.VIDEO_FPS, seed=_seed(), quality_tier=mode.quality_tier, from_image=from_image)


async def create_operation_job(db: DBSession, user: User, *, operation: str, inputs: list[OperationInput],
                               parameters: dict | None = None, quality_tier: str = "standard",
                               project_id: str | None = None, idempotency_key: str | None = None) -> MediaJob:
    """CP19: the permanent TRANSFORM/COMPOSE creation path — background_remove, vectorize, and every
    future operation share this one function, the same way create_image_job/create_video_job already
    share _create(). `inputs[0]` becomes source_artifact_id (kept for the existing single-FK listing/
    ownership convention); the full typed list is `job.params["inputs"]` — no new column, no migration.

    CP22: `kind` is no longer hardcoded to "image" — it is the operation's own declared output kind
    (OPERATION_OUTPUT_KIND), so a video-output operation (character_animate) is billed, quota-gated,
    and concurrency-limited as a video job, not silently miscounted as an image one. Every declared
    input's role is checked against OPERATION_INPUT_KINDS[operation]: an unknown role, a role repeated
    twice, or a required role missing from `inputs` is refused before anything is reserved — and each
    input's artifact must already be the Artifact.kind that role expects (not just "any kind" the way
    extra_artifact_ids used to accept)."""
    if operation not in TRANSFORM_OPERATIONS:
        raise MediaRefused("invalid_request")
    if not inputs:
        raise MediaRefused("invalid_request")
    existing = _existing(db, user, idempotency_key)
    if existing is not None:
        return existing

    expected_kinds = OPERATION_INPUT_KINDS.get(operation, {})
    roles = [i.role for i in inputs]
    if len(set(roles)) != len(roles):  # the same role declared twice
        raise MediaRefused("invalid_request")
    if any(role not in expected_kinds for role in roles):  # a role this operation doesn't take
        raise MediaRefused("invalid_request")
    if not set(expected_kinds).issubset(roles):  # a required role this operation didn't get
        raise MediaRefused("invalid_request")

    # CP43: a per-second-priced provider (Higgsfield Genjutsu) bills the driving VIDEO's own real
    # length, never a caller-supplied duration_seconds knob (no `duration` field exists in its
    # documented request schema — CP39/CP42 evidence: a 3s-requested job produced a ~4.7s output
    # from a 4.963s input). Any video-kind input's already-known real duration (populated at
    # upload time, store_source_media() above) is handed to the engine's estimate_usd() through
    # parameters — the one place OperationRequest already carries operation-specific knobs — so a
    # cost estimate can reflect it before anything is reserved, without widening the permanent
    # OperationRequest/OperationInput contract itself.
    input_durations = {}
    for i in inputs:
        if expected_kinds.get(i.role) == "video":
            artifact = db.query(Artifact).filter(Artifact.id == i.artifact_id, Artifact.user_id == user.id).first()
            if artifact is not None and artifact.duration_seconds:
                input_durations[i.role] = float(artifact.duration_seconds)
    full_parameters = {**(parameters or {}), "_input_durations": input_durations} if input_durations \
        else (parameters or {})

    engine = operation_engine(operation)
    request = OperationRequest(operation=operation, inputs=tuple(inputs), parameters=full_parameters,
                               quality_tier=quality_tier)
    engine_ready = engine is not None and await engine.is_available()
    estimated = engine.estimate_usd(request) if engine is not None else 0.0
    estimated = estimated if estimated is not None else 0.0
    reserve_usd = estimated
    if isinstance(engine, OperationEngineChain):
        headroom = engine.headroom_usd(request, lambda usd: plans.fits_request_ceiling(user.plan, usd))
        if headroom is not None:
            reserve_usd = max(estimated, headroom)

    kind = OPERATION_OUTPUT_KIND.get(operation, "image")
    entitlement = get_media_entitlement(user.plan, free_image_daily_limit=get_free_image_daily_limit(db))
    params = {"inputs": [{"role": i.role, "artifact_id": i.artifact_id} for i in inputs],
              "parameters": full_parameters, "quality_tier": quality_tier}
    return await _create(
        db, user, kind=kind, operation=operation, prompt=operation, params=params, estimated_usd=estimated,
        reserve_usd=reserve_usd, daily=daily_usage(db, user, kind, entitlement), engine_ready=engine_ready,
        project_id=project_id, source_artifact_id=inputs[0].artifact_id, source_kind=expected_kinds[inputs[0].role],
        extra_artifact_ids=tuple((i.artifact_id, expected_kinds[i.role]) for i in inputs[1:]),
        idempotency_key=idempotency_key,
    )


def _seed() -> int:
    import secrets

    return secrets.randbelow(2**31)


async def store_source_media(db: DBSession, user: User, *, data: bytes, content_type: str, kind: str = "image",
                             store: ObjectStore | None = None) -> Artifact:
    """A user-provided source artifact — an image to edit/animate, or (CP22) a video to drive an
    animation. `kind` picks the allow-list and size limit; ownership/storage/retention are
    identical regardless of kind, the same generic Artifact every media capability already uses."""
    allowed = _ALLOWED_SOURCE_TYPES_BY_KIND.get(kind)
    if allowed is None:
        raise MediaRefused("invalid_request")
    normalized = (content_type or "").split(";")[0].lower()
    extension = allowed.get(normalized)
    max_bytes = (media_settings().media_max_video_source_bytes if kind == "video"
                else media_settings().media_max_source_bytes)
    if (extension is None or not data or len(data) > max_bytes or not _sniff_ok(data, normalized)):
        raise MediaRefused("invalid_request")
    if not await storage_ready(store):
        raise MediaRefused("media_unavailable")
    store = store or build_object_store()
    # CP43: a source video's real duration is needed to estimate what a per-second-priced provider
    # (Higgsfield Genjutsu) will actually bill — read once, here, at upload time, the same way an
    # OUTPUT artifact's duration_seconds is already captured in _deliver() below. width/height come
    # along for free from the same read. Best-effort: a value that can't be read (image_size/
    # video_metadata both return None on a malformed or unusual file) leaves the column NULL, same
    # as an artifact created before this existed — callers already treat a missing duration as
    # "unknown", never as "zero".
    width = height = None
    duration_seconds = None
    if kind == "image":
        width, height = image_size(data)
    elif kind == "video":
        width, height, duration_seconds = video_metadata(data)
    artifact = Artifact(user_id=user.id, kind=kind, source="upload", filename=f"source.{extension}",
                        content_type=content_type, size_bytes=len(data), width=width, height=height,
                        duration_seconds=duration_seconds, storage_backend=store.backend_name,
                        storage_key="pending",
                        expires_at=_now() + timedelta(days=media_settings().media_retention_days))
    db.add(artifact)
    db.flush()
    artifact.storage_key = f"media/{user.id}/sources/{artifact.id}.{extension}"
    await store.put(key=artifact.storage_key, data=data, content_type=content_type)
    db.commit()
    db.refresh(artifact)
    return artifact


async def store_source_image(db: DBSession, user: User, *, data: bytes, content_type: str,
                             store: ObjectStore | None = None) -> Artifact:
    """A user-provided image to edit or animate. Unchanged behavior — kept as its own name since
    every existing caller (api/routes/media.py's /media/sources) already uses it by this name."""
    return await store_source_media(db, user, data=data, content_type=content_type, kind="image", store=store)


async def vectorize_source_image(db: DBSession, user: User, *, data: bytes, content_type: str,
                                 store: ObjectStore | None = None) -> Artifact:
    """A user-provided raster image, converted to SVG and stored as an artifact.

    CP19: runs through VTracerEngine (media/vectorize.py), the same OperationEngine contract
    background_remove uses — but called directly, not through create_operation_job/the MediaJob
    queue: deterministic, local, no provider, no network, no cost to reserve, so queuing it would
    only add latency. The cost is a real, measured $0, recorded through the same artifact/storage
    pipeline every media capability delivers through.
    """
    if vectorize_module.extension_for(content_type) is None or not data or len(data) > media_settings().media_max_source_bytes:
        raise MediaRefused("invalid_request")
    try:
        output = await vectorize_module.VTracerEngine().run(
            None, {"source_image": ResolvedInput(data, content_type)})
    except vectorize_module.VectorizeFailed as exc:
        logger.warning("vectorize failed | reason=%s", exc.reason)
        raise MediaRefused("failed") from None
    if not await storage_ready(store):
        raise MediaRefused("media_unavailable")
    store = store or build_object_store()
    svg_bytes = output.data
    artifact = Artifact(user_id=user.id, kind="image", source="vectorize", filename="vector.svg",
                        content_type="image/svg+xml", size_bytes=len(svg_bytes), storage_backend=store.backend_name,
                        storage_key="pending",
                        expires_at=_now() + timedelta(days=media_settings().media_retention_days))
    db.add(artifact)
    db.flush()
    artifact.storage_key = f"media/{user.id}/sources/{artifact.id}.svg"
    await store.put(key=artifact.storage_key, data=svg_bytes, content_type="image/svg+xml")
    db.commit()
    db.refresh(artifact)
    return artifact


# ── Execution ────────────────────────────────────────────────────────────────

def schedule(job_id: str) -> None:
    """Run a job in the background of this process (single-instance deployment)."""
    task = asyncio.get_running_loop().create_task(run_job(job_id))
    _background.add(task)
    task.add_done_callback(_background.discard)


async def run_job(job_id: str, *, session_factory=None, store: ObjectStore | None = None) -> None:
    db: DBSession = (session_factory or SessionLocal)()
    try:
        job = db.query(MediaJob).filter(MediaJob.id == job_id).first()
        if job is None or job.status != "queued":
            return
        job.status, job.started_at, job.progress = "running", _now(), 0.05
        db.commit()
        store = store or build_object_store()
        await _deliver(db, job, store, _generate(db, job, store))
    finally:
        _links.pop(job_id, None)
        db.close()


async def resume_job(job_id: str, *, session_factory=None, store: ObjectStore | None = None,
                     client_factory=None) -> None:
    """Follow a provider job a previous process submitted, to its end — never submitting it again."""
    db: DBSession = (session_factory or SessionLocal)()
    try:
        job = db.query(MediaJob).filter(MediaJob.id == job_id).first()
        if job is None or job.status != "running" or not job.provider_ref or job_id in _links:
            return
        handle = ProviderHandle.from_dict(job.provider_ref)
        if handle.provider != higgsfield.PROVIDER:
            return
        link = _links[job_id] = ProviderLink()
        link.handle = handle
        store = store or build_object_store()
        kwargs = {"client_factory": client_factory} if client_factory is not None else {}
        logger.info("media: resuming provider job | job=%s", job.id)
        await _deliver(db, job, store, higgsfield.resume(handle, link=link, **kwargs))
    finally:
        _links.pop(job_id, None)
        db.close()


async def _deliver(db: DBSession, job: MediaJob, store: ObjectStore, work) -> None:
    """Await the generation, then store its output as an Artifact and settle the reservation."""
    job_id = job.id
    try:
        output = await work
    except JobCancelled:
        _cancelled(db, job)
        return
    except EngineChainFailed as exc:
        logger.warning("media: no engine delivered | job=%s reason=%s", job.id, exc.reason)
        if not _was_cancelled(db, job):
            _fail(db, job, "media_unavailable" if exc.unavailable else "failed",
                  trace=[attempt.as_dict() for attempt in exc.trace])
        return
    except EngineFailed as exc:
        logger.warning("media: engine failed | job=%s reason=%s retryable=%s started=%s", job.id, exc.reason,
                       exc.retryable, exc.may_have_started)
        if not _was_cancelled(db, job):
            _fail(db, job, "failed")
        return
    except EngineUnavailable:
        if not _was_cancelled(db, job):
            _fail(db, job, "media_unavailable")
        return
    except asyncio.TimeoutError:
        if _was_cancelled(db, job):  # already refreshes `job`
            return
        if job.provider_request_id:
            # CP41: our own local deadline elapsed, but the provider already accepted this job
            # (a live request_id exists) — it may still be working, may still deliver, and may
            # still bill on its own side. Failing it now would release the reservation and tell
            # the person "failed" for work that could complete; reclaim_stale_jobs()'s own
            # docstring already states this exact policy for the restart-recovery case, applied
            # here too. Left "running": resume_provider_jobs() (src/main.py, on the next process
            # start) follows it to a real, provider-confirmed terminal state instead of us
            # guessing. Not yet re-attempted from inside this same process — see CP41's report
            # for that as a documented follow-up, not a silent gap.
            logger.warning("media: local timeout with a live provider job, leaving it running | "
                           "job=%s request=%s", job.id, job.provider_request_id)
            return
        _fail(db, job, "failed")
        return
    except Exception:
        logger.exception("media: unexpected failure | job=%s", job.id)
        if not _was_cancelled(db, job):
            _fail(db, job, "failed")
        return
    if _was_cancelled(db, job):
        return

    base = output.content_type.split(";")[0].lower()
    extension = _OUTPUT_EXTENSIONS.get(base)
    if extension is None or not base.startswith(("image/", "video/")) or len(output.data) > _MAX_OUTPUT_BYTES[job.kind]:
        logger.warning("media: unusable output | job=%s content_type=%s size=%d", job.id, base, len(output.data))
        _fail(db, job, "failed")
        return

    artifact = Artifact(
        user_id=job.user_id, project_id=job.project_id, kind=job.kind, source="media",
        filename=f"trindatron-{job.kind}-{job.id[:8]}.{extension}", content_type=base,
        size_bytes=len(output.data), width=output.width, height=output.height,
        duration_seconds=output.duration_seconds, storage_backend=store.backend_name, storage_key="pending",
        expires_at=_now() + timedelta(days=media_settings().media_retention_days),
    )
    db.add(artifact)
    db.flush()
    artifact.storage_key = f"media/{job.user_id}/{job.id}/{artifact.filename}"
    try:
        await store.put(key=artifact.storage_key, data=output.data, content_type=base)
    except Exception:
        logger.exception("media: storage write failed | job=%s", job.id)
        db.rollback()
        job = db.query(MediaJob).filter(MediaJob.id == job_id).first()
        _fail(db, job, "failed")
        return

    _complete(db, job, artifact, output)


def _was_cancelled(db: DBSession, job: MediaJob) -> bool:
    """Whether the person cancelled this job meanwhile (cancel_job): then nothing else is written to it."""
    db.refresh(job)
    return job.status == "cancelled"


async def _generate(db: DBSession, job: MediaJob, store: ObjectStore) -> MediaOutput:
    settings = media_settings()
    source = source_type = None
    if job.source_artifact_id:
        artifact = db.query(Artifact).filter(Artifact.id == job.source_artifact_id, Artifact.user_id == job.user_id).first()
        if artifact is None:
            raise EngineFailed("source missing")
        source, source_type = await store.get(key=artifact.storage_key), artifact.content_type

    p = job.params
    budget = _budget_usd(db, job)
    link = _links[job.id] = ProviderLink(on_submit=lambda handle: _record_provider(db, job, handle))
    if job.operation in TRANSFORM_OPERATIONS:
        # CP19: TRANSFORM/COMPOSE — media/operations.py. Every declared input is resolved to
        # (bytes, content_type) here, right before the engine runs (artifact-first).
        engine = operation_engine(job.operation)
        if engine is None:
            raise EngineUnavailable(f"no engine for operation {job.operation}")
        declared = [OperationInput(**item) for item in p.get("inputs", [])]
        resolved: dict[str, ResolvedInput] = {}
        for item in declared:
            artifact = db.query(Artifact).filter(Artifact.id == item.artifact_id, Artifact.user_id == job.user_id).first()
            if artifact is None:
                raise EngineFailed(f"input {item.role} missing")
            resolved[item.role] = ResolvedInput(await store.get(key=artifact.storage_key), artifact.content_type)
        request = OperationRequest(operation=job.operation, inputs=tuple(declared), parameters=p.get("parameters", {}),
                                   quality_tier=p.get("quality_tier", "standard"), budget_usd=budget, link=link)
        call_fn = lambda: engine.run(request, resolved)  # noqa: E731
        # CP41: sized by the operation's own OUTPUT kind, not assumed to be image — a TRANSFORM
        # operation is exactly as likely to produce video (character_animate) as image
        # (background_remove, vectorize). Reuses the same media_video_timeout_s the `else` branch
        # below already uses for GENERATE-path video (HiggsfieldVideoEngine._deadline() and
        # friends) — a video-output TRANSFORM job runs on the same class of infrastructure and
        # deserves the same budget, not a second, invented number. CP40 proved 210s (the old flat
        # image timeout + 30) was not enough for a real Genjutsu job that was still healthy and
        # in_progress when it was cut off.
        timeout = (settings.media_video_timeout_s if job.kind == "video" else settings.media_image_timeout_s) + 30
    elif job.kind == "image":
        engine = image_engine()
        if engine is None:
            raise EngineUnavailable("no image engine")
        request = ImageRequest(prompt=await _engine_prompt(db, job, source is not None),
                               width=p["width"], height=p["height"], seed=p["seed"],
                               steps=p["steps"], source=source, source_content_type=source_type,
                               quality_tier=p.get("quality_tier", "standard"), budget_usd=budget, link=link)
        call_fn = lambda: engine.generate(request)  # noqa: E731
        timeout = settings.media_image_timeout_s + 30
    else:
        # Only the routes of the job's own mode: a premium API is never reached as a fallback.
        engine = modes.restrict(video_engine(), modes.get(p.get("mode")))
        if engine is None:
            raise EngineUnavailable("no video engine")
        request = VideoRequest(prompt=await _engine_prompt(db, job, source is not None),
                               width=p["width"], height=p["height"],
                               duration_seconds=p["duration_seconds"], fps=p["fps"], seed=p["seed"],
                               source=source, source_content_type=source_type, budget_usd=budget,
                               quality_tier=p.get("quality_tier", "standard"), link=link)
        call_fn = lambda: engine.generate(request)  # noqa: E731
        timeout = settings.media_video_timeout_s + 30
    call = call_fn()
    chained = isinstance(engine, (ImageEngineChain, VideoEngineChain, OperationEngineChain))
    if chained:
        # A chain tries each engine once; the timeout covers every engine it may fall back to.
        timeout *= len(engine.engines)

    attempts = 0
    while True:
        attempts += 1
        job.attempts = attempts
        db.commit()
        try:
            return await asyncio.wait_for(call, timeout=timeout)
        except EngineChainFailed:
            raise
        except EngineFailed as exc:
            # Never a second submission of work a provider may have accepted: queue-based providers have no
            # idempotency key, so asking again would be a second paid job.
            if chained or not exc.retryable or not exc.resubmit_safe or link.handle is not None or attempts >= 2:
                raise
            logger.info("media: retrying once | job=%s", job.id)
            call = call_fn()


async def _engine_prompt(db: DBSession, job: MediaJob, has_source: bool) -> str:
    """The prompt the engine receives: the person's words written as a scene (media/prompt_writer.py).

    Written once per job and kept in its params, so a retry or a fallback engine gets the same scene. An edit
    of an attached image or an animation of one keeps the person's own instruction: it describes a change to
    what is there, not a scene to invent. Premium keeps what is heard (its engine records sound).
    """
    from src.media import prompt_writer

    params = dict(job.params or {})
    if params.get("engine_prompt"):
        return params["engine_prompt"]
    if has_source:
        return job.prompt
    user = db.query(User).filter(User.id == job.user_id).first()
    with_sound = job.kind == "video" and params.get("mode") == modes.PREMIUM.id and modes.wants_audio(job.prompt)
    written = await prompt_writer.rewrite(job.prompt, kind=job.kind, plan=user.plan if user else "free",
                                          with_sound=with_sound)
    if written is None:
        return job.prompt
    job.params = {**params, "engine_prompt": written.prompt, "prompt_cost_usd": round(written.cost_usd, 8)}
    db.commit()
    logger.info("media: prompt written | job=%s chars=%d cost_usd=%.6f", job.id, len(written.prompt),
                written.cost_usd)
    return written.prompt


def _budget_usd(db: DBSession, job: MediaJob) -> float | None:
    """What this job may spend: its reservation's budget, or its estimate when it has none."""
    if job.reservation_id:
        budget_tcu = entitlement_service.get_reservation_budget_tcu(db, job.reservation_id)
        if budget_tcu:
            return plans.tcu_to_usd(budget_tcu)
    return float(job.estimated_cost_usd) if job.estimated_cost_usd is not None else None


def _record_trace(job: MediaJob, trace) -> None:
    if trace:
        job.params = {**(job.params or {}), "engine_trace": list(trace)}


def _record_provider(db: DBSession, job: MediaJob, handle: ProviderHandle) -> None:
    """Persist the provider's request id the moment it exists, before any polling."""
    job.provider_request_id, job.provider_ref = handle.request_id, handle.as_dict()
    db.commit()


def _cancelled(db: DBSession, job: MediaJob) -> None:
    """The job ended without output and without a charge: the reservation is released. Idempotent."""
    db.refresh(job)
    if job.status == "cancelled":
        return
    if job.reservation_id:
        entitlement_service.cancel_reservation(db, job.reservation_id)
    job.status, job.error_code, job.progress, job.completed_at = "cancelled", None, None, _now()
    db.commit()
    _jobs_finished.inc(kind=job.kind, outcome="cancelled")


async def cancel_job(db: DBSession, user: User, job_id: str, *, client_factory=None) -> MediaJob:
    """Cancel a creation the person no longer wants.

    queued   -> cancelled here; nothing ran, the reservation is released.
    running  -> only when the provider job behind it is still queued there and the
                provider accepts the cancel (then it is refunded). Once generation
                has started it cannot be stopped — the provider bills it — so the
                answer is an honest "cannot cancel" and the creation completes.
    finished -> a cancelled job answers as it is; a completed or failed one cannot be cancelled.
    """
    job = owned_job(db, user, job_id)
    if job.status == "cancelled":
        return job
    if job.status in MEDIA_TERMINAL_STATUSES:
        raise MediaRefused("cannot_cancel")
    if job.status == "queued":
        _cancelled(db, job)
        return job
    link = _links.get(job.id)
    handle = link.handle if link is not None and link.handle is not None else (
        ProviderHandle.from_dict(job.provider_ref) if job.provider_ref else None)
    if handle is None or handle.provider != higgsfield.PROVIDER:
        raise MediaRefused("cannot_cancel")  # no provider job to cancel yet, or an engine that cannot cancel
    kwargs = {"client_factory": client_factory} if client_factory is not None else {}
    if not await higgsfield.cancel(handle, **kwargs):
        raise MediaRefused("cannot_cancel")
    _cancelled(db, job)
    return job


def _fail(db: DBSession, job: MediaJob, code: str, *, trace=None) -> None:
    if job.reservation_id:
        entitlement_service.cancel_reservation(db, job.reservation_id)
    _record_trace(job, trace)
    job.status, job.error_code, job.progress, job.completed_at = "failed", code, None, _now()
    db.commit()
    _jobs_finished.inc(kind=job.kind, outcome="failed")


def _complete(db: DBSession, job: MediaJob, artifact: Artifact, output: MediaOutput) -> None:
    if output.cost_usd is not None:
        cost = max(0.0, float(output.cost_usd))  # what the hosted call cost
    else:
        cost = plans.measured_usd(output.engine_seconds, media_settings().media_gpu_usd_per_hour)
    # Writing the scene the engine ran (_engine_prompt) is part of making it.
    cost += float((job.params or {}).get("prompt_cost_usd") or 0.0)
    tcu = plans.usd_to_tcu(cost)
    if job.reservation_id:
        budget = entitlement_service.get_reservation_budget_tcu(db, job.reservation_id)
        tcu = min(tcu, budget)
        entitlement_service.finalize_usage(
            db, job.reservation_id, message_id=None, model=f"media:{job.kind}", input_tokens=0, output_tokens=0,
            cached_tokens=0, cost_usd=round(cost, 8), tcu=tcu, duration_ms=int(output.engine_seconds * 1000),
        )
    _record_trace(job, output.trace)
    if output.metrics:
        # Queue, cold start, inference, GPU and total seconds of the run that delivered. Internal only.
        job.params = {**(job.params or {}), "engine_metrics": dict(output.metrics)}
    job.status, job.progress, job.completed_at = "completed", 1.0, _now()
    job.artifact_id, job.engine_seconds = artifact.id, output.engine_seconds
    job.cost_usd = Decimal(str(round(cost, 8)))
    db.commit()
    _jobs_finished.inc(kind=job.kind, outcome="completed")
    logger.info("media: job completed | job=%s kind=%s engine_seconds=%.2f", job.id, job.kind, output.engine_seconds)


def reclaim_stale_jobs(db: DBSession, *, now: datetime | None = None) -> int:
    """Fail jobs a restart left behind, releasing their reservations.

    A job a provider accepted (provider_request_id) is followed by resume_job
    instead — failing it would refund the person for work the provider may still
    deliver and bill — until the provider no longer keeps its output.
    """
    now = now or _now()
    settings = media_settings()
    reclaimed = 0
    for job in db.query(MediaJob).filter(MediaJob.status.in_(("queued", "running"))).all():
        if job.provider_request_id and _aware(job.created_at) >= now - timedelta(days=PROVIDER_FOLLOW_DAYS):
            continue
        limit = settings.media_image_timeout_s if job.kind == "image" else settings.media_video_timeout_s
        if _aware(job.created_at) < now - timedelta(seconds=limit * 2 + 120):
            _fail(db, job, "failed")
            reclaimed += 1
    return reclaimed


def resume_provider_jobs(*, session_factory=None) -> int:
    """At startup: follow every provider job a previous process left running. Needs a running event loop."""
    db: DBSession = (session_factory or SessionLocal)()
    try:
        ids = [job.id for job in db.query(MediaJob).filter(MediaJob.status == "running",
                                                           MediaJob.provider_request_id.isnot(None)).all()
               if job.id not in _links]
    finally:
        db.close()
    loop = asyncio.get_running_loop()
    for job_id in ids:
        task = loop.create_task(resume_job(job_id, session_factory=session_factory))
        _background.add(task)
        task.add_done_callback(_background.discard)
    return len(ids)


# ── Reading ──────────────────────────────────────────────────────────────────

def owned_job(db: DBSession, user: User, job_id: str) -> MediaJob:
    job = db.query(MediaJob).filter(MediaJob.id == job_id, MediaJob.user_id == user.id).first()
    if job is None:
        raise MediaRefused("not_found")
    return job


def owned_artifact(db: DBSession, user: User, artifact_id: str) -> Artifact:
    artifact = db.query(Artifact).filter(Artifact.id == artifact_id, Artifact.user_id == user.id).first()
    if artifact is None:
        raise MediaRefused("not_found")
    return artifact


def job_view(db: DBSession, job: MediaJob) -> dict:
    artifact = db.query(Artifact).filter(Artifact.id == job.artifact_id).first() if job.artifact_id else None
    return {
        "id": job.id,
        "kind": job.kind,
        "operation": job.operation,
        "status": job.status,
        "progress": job.progress,
        "error": MESSAGES.get(job.error_code) if job.status == "failed" else None,
        "error_code": job.error_code,
        "project_id": job.project_id,
        "created_at": _aware(job.created_at).isoformat(),
        "completed_at": _aware(job.completed_at).isoformat() if job.completed_at else None,
        "artifact": artifact_view(artifact) if artifact is not None else None,
        # Video only: the mode it runs in and what the router changed to fit the budget (never an engine or a cost).
        "mode": (job.params or {}).get("mode") if job.kind == "video" else None,
        "adjustments": list((job.params or {}).get("adjustments") or []) if job.kind == "video" else [],
    }


def artifact_view(artifact: Artifact) -> dict:
    return {
        "id": artifact.id,
        "kind": artifact.kind,
        "content_type": artifact.content_type,
        "size_bytes": artifact.size_bytes,
        "width": artifact.width,
        "height": artifact.height,
        "duration_seconds": artifact.duration_seconds,
        "created_at": _aware(artifact.created_at).isoformat(),
        "expires_at": _aware(artifact.expires_at).isoformat() if artifact.expires_at else None,
        "content_path": f"/media/artifacts/{artifact.id}/content",
        "download_path": f"/media/artifacts/{artifact.id}/download",
    }


def list_creations(db: DBSession, user: User, *, kind: str | None = None, project_id: str | None = None,
                   since: datetime | None = None, until: datetime | None = None, limit: int = 30,
                   before: datetime | None = None) -> tuple[list[MediaJob], datetime | None]:
    query = db.query(MediaJob).filter(MediaJob.user_id == user.id, MediaJob.status.notin_(("failed", "cancelled")))
    if kind:
        query = query.filter(MediaJob.kind == kind)
    if project_id:
        query = query.filter(MediaJob.project_id == project_id)
    if since:
        query = query.filter(MediaJob.created_at >= since)
    if until:
        query = query.filter(MediaJob.created_at < until)
    if before:
        query = query.filter(MediaJob.created_at < before)
    rows = query.order_by(MediaJob.created_at.desc()).limit(limit + 1).all()
    next_before = _aware(rows[limit - 1].created_at) if len(rows) > limit else None
    return rows[:limit], next_before


async def delete_creation(db: DBSession, user: User, job_id: str, *, store: ObjectStore | None = None) -> None:
    job = owned_job(db, user, job_id)
    if job.status not in MEDIA_TERMINAL_STATUSES:
        raise MediaRefused("job_in_progress")
    if job.artifact_id:
        artifact = db.query(Artifact).filter(Artifact.id == job.artifact_id).first()
        if artifact is not None:
            await (store or build_object_store()).delete(key=artifact.storage_key)
            job.artifact_id = None
            db.delete(artifact)
    db.delete(job)
    db.commit()


def refusal_http(exc: MediaRefused) -> HTTPException:
    detail: dict = {"error": exc.code, "message": exc.message}
    if exc.resets_at is not None:
        detail["resets_at"] = exc.resets_at.isoformat()
    if exc.details:
        detail.update(exc.details)
    return HTTPException(status_code=exc.status_code, detail=detail)


def plan_ceiling_tcu(plan: str) -> float:
    return get_plan_entitlement(plan).max_tcu_per_request
