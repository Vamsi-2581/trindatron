"""What each plan may create — the single place media limits live.

Two independent guards, and both must pass:

  counts   image_daily_limit / video_daily_limit: a product promise the user
           can read ("7 of 10 images available today"), counted per local day.
  budget   every job reserves its estimated cost as TCU through the same
           entitlement engine chat and agents use, so a creation spends the
           plan's own session/daily/weekly windows, respects
           max_tcu_per_request, and — on FREE — the plan-wide daily budget
           that stops many free accounts from adding up. No media path can
           bypass the economics that already protect the product.

The table below holds each plan's *commercial reference*: the images and
videos a day it is sold with, in its default video mode, plus the video modes
it may use and how many clips of each mode a day. Real consumption is always
TCU: every creation reserves its estimate before any engine runs and settles at
its real cost.

Media (images, videos, operations) draws from one pool per plan: the weekly
window minus a protected floor kept for chat and agents
(CHAT_AGENT_FLOOR_SHARE), and the same share of the daily window. Media can
never spend the floor, so a person who creates all week can still talk to
TRINDATRON. The weekly ceiling stays absolute: the pool is inside it, never
beside it.

A daily count is the reference number, lowered only when the configured
engine's price would not let the pool pay for it every day
(`affordable_daily_count`). Change the engine or its price and the counts
follow; nothing is hand-tuned.

Video modes and their measured costs live in media/modes.py. The older GPU
model below (docs/MEDIA_ENGINES.md §6) still prices self-hosted images and any
video request made without a mode.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.entitlements import get_plan_entitlement

# ── Cost model for self-hosted engines (estimates; see module docstring) ────
_IMAGE_SECONDS_1024_8_STEPS = 2.3
_VIDEO_SECONDS_720P_PER_CLIP_SECOND = 540 / 5  # 9 min for 5 s of 720p
_UTILISATION_OVERHEAD = 1 / 0.30
_USD_PER_TCU = 0.01

QUALITY_STEPS = {"draft": 6, "standard": 8, "premium": 12}
VIDEO_FPS = 16

# The most of a plan's daily TCU window image readings may take (vision_daily_limit).
CREATION_SHARE_OF_DAILY_WINDOW = 0.25

# The part of each window only chat and agents may spend; media draws from the rest.
# PRO keeps 11 %, not 12 %: 50 images at 0.22 TCU + 15 economy clips at 1.24 TCU is
# 29.6 TCU a day, 207.2 a week, and 12 % would leave a 206.8 TCU pool.
CHAT_AGENT_FLOOR_SHARE = {"free": 0.50, "start": 0.20, "pro": 0.11, "max": 0.10, "team": 0.10}


@dataclass(frozen=True)
class MediaEntitlement:
    plan: str
    image_daily_limit: int
    video_daily_limit: int
    image_quality_tier: str
    video_quality_tier: str
    max_image_resolution: int  # longest side, px
    max_video_duration_seconds: int
    max_video_resolution: int  # shortest side, px (480 = 480p)
    # False when the offer rests only on experimental infrastructure (outside production): a trial, not a promise.
    image_guaranteed: bool = True
    video_guaranteed: bool = True
    # Video modes (media/modes.py) this plan may use, the one used when none is asked for,
    # and the most clips of each mode a day, as (mode, count) pairs.
    video_modes: tuple[str, ...] = ()
    default_video_mode: str | None = None
    video_mode_caps: tuple[tuple[str, int], ...] = ()

    def mode_cap(self, mode: str) -> int:
        return dict(self.video_mode_caps).get(mode, 0)

    @property
    def image_editing(self) -> bool:
        return self.image_daily_limit > 0

    @property
    def image_to_video(self) -> bool:
        """Photo -> Movement runs only in the Maximum mode (the only engine class that animates a photo;
        the efficient 5B ignores the image), so a plan animates photos exactly when it offers Maximum."""
        return self.video_daily_limit > 0 and PHOTO_MOVEMENT_MODE in self.video_modes


#: The one video mode a photo can be animated in (media/modes.py MAXIMUM).
PHOTO_MOVEMENT_MODE = "maximum"
_START_MODES = (("economy", 5), ("quality", 2))
_PRO_MODES = (("economy", 15), ("quality", 8), ("maximum", 2))
# Premium (xAI) only on MAX, and only on a confirmed request: 35 TCU a clip fits MAX's 60 TCU request ceiling.
_MAX_MODES = (("economy", 40), ("quality", 40), ("maximum", 8), ("premium", 2))

MEDIA_ENTITLEMENTS: dict[str, MediaEntitlement] = {
    "free": MediaEntitlement("free", 3, 0, "standard", "draft", 1024, 0, 0),
    # Longest clip 6 s (the 5B's normal maximum); counts are sized on each mode's 5 s default (media/modes.py).
    "start": MediaEntitlement("start", 25, 5, "standard", "draft", 1024, 6, 480,
                              video_modes=tuple(m for m, _ in _START_MODES), default_video_mode="economy",
                              video_mode_caps=_START_MODES),
    "pro": MediaEntitlement("pro", 50, 15, "standard", "draft", 1536, 6, 480,
                            video_modes=tuple(m for m, _ in _PRO_MODES), default_video_mode="economy",
                            video_mode_caps=_PRO_MODES),
    # Images at the "standard" tier like every plan: the economical image route (gpt-image-1-mini, low) is
    # grade 3, and 100 a day at "premium" (medium, ≈ 1.3 TCU each) would not fit MAX's media pool anyway.
    # Every plan's video defaults to economy (480p): a person who wants more asks for it in words ("em alta
    # qualidade", "HD", "720p" — media/modes.py), and MAX then gets quality or maximum. Owner's rule, 2026-09-27.
    "max": MediaEntitlement("max", 100, 40, "standard", "standard", 2048, 6, 720,
                            video_modes=tuple(m for m, _ in _MAX_MODES), default_video_mode="economy",
                            video_mode_caps=_MAX_MODES),
    # Flat TEAM carries MAX's per-member windows.
    "team": MediaEntitlement("team", 100, 40, "standard", "standard", 2048, 6, 720,
                             video_modes=tuple(m for m, _ in _MAX_MODES), default_video_mode="economy",
                             video_mode_caps=_MAX_MODES),
}


def get_media_entitlement(plan: str, *, free_image_daily_limit: int | None = None) -> MediaEntitlement:
    entitlement = MEDIA_ENTITLEMENTS.get((plan or "").lower(), MEDIA_ENTITLEMENTS["free"])
    if entitlement.plan == "free" and free_image_daily_limit is not None:
        from dataclasses import replace

        return replace(entitlement, image_daily_limit=max(0, free_image_daily_limit))
    return entitlement


def estimate_image_usd(*, width: int, height: int, quality_tier: str, gpu_usd_per_hour: float) -> float:
    pixels = (width * height) / (1024 * 1024)
    steps = QUALITY_STEPS.get(quality_tier, 8) / 8
    seconds = _IMAGE_SECONDS_1024_8_STEPS * pixels * steps
    return seconds * gpu_usd_per_hour / 3600 * _UTILISATION_OVERHEAD


def estimate_video_usd(*, short_side: int, duration_seconds: int, gpu_usd_per_hour: float) -> float:
    pixels = (short_side / 720) ** 2
    seconds = _VIDEO_SECONDS_720P_PER_CLIP_SECOND * duration_seconds * pixels
    return seconds * gpu_usd_per_hour / 3600 * _UTILISATION_OVERHEAD


def usd_to_tcu(usd: float) -> float:
    return round(usd / _USD_PER_TCU, 4)


def tcu_to_usd(tcu: float) -> float:
    return round(float(tcu) * _USD_PER_TCU, 8)


def measured_usd(engine_seconds: float, gpu_usd_per_hour: float) -> float:
    """Cost of work that actually ran, from its measured GPU time."""
    return max(0.0, engine_seconds) * gpu_usd_per_hour / 3600


def fits_request_ceiling(plan: str, estimated_usd: float) -> bool:
    """A creation that one request could never afford on this plan is not offered."""
    return usd_to_tcu(estimated_usd) <= get_plan_entitlement(plan).max_tcu_per_request


def sustainable_daily_tcu(plan: str) -> float | None:
    """What a plan can spend every day of a week: its daily window, or a seventh of its weekly one if smaller.

    A daily count built on the daily window alone would promise more than the
    weekly window pays for (FREE: 10 a day but 20 a week), so the promise is
    built on the smaller of the two. None when the plan has neither.
    """
    entitlement = get_plan_entitlement(plan)
    candidates = [window for window in (entitlement.daily_tcu_limit,
                                        entitlement.weekly_tcu_limit / 7 if entitlement.weekly_tcu_limit is not None else None)
                  if window is not None]
    return min(candidates) if candidates else None


def _share_count(plan: str, unit_usd: float) -> int | None:
    """How many image readings of `unit_usd` fit in their share of what the plan can spend a day.

    0 when one operation does not fit a single request's ceiling; None when the plan has no window.
    """
    entitlement = get_plan_entitlement(plan)
    unit_tcu = usd_to_tcu(unit_usd)
    if unit_tcu > entitlement.max_tcu_per_request:
        return 0
    window = sustainable_daily_tcu(plan)
    if window is None:
        return None
    return max(0, int((CREATION_SHARE_OF_DAILY_WINDOW * window + 1e-9) // unit_tcu))


# ── The media pool and the protected chat/agent floor ───────────────────────

def chat_agent_floor_share(plan: str) -> float:
    return CHAT_AGENT_FLOOR_SHARE.get((plan or "").lower(), CHAT_AGENT_FLOOR_SHARE["free"])


def media_weekly_pool_tcu(plan: str) -> float | None:
    """What media may spend in a week: the weekly window less the chat/agent floor. None without a weekly window."""
    weekly = get_plan_entitlement(plan).weekly_tcu_limit
    return None if weekly is None else round(weekly * (1 - chat_agent_floor_share(plan)), 4)


def media_daily_pool_tcu(plan: str) -> float | None:
    """What media may spend in a day: the daily window less the same floor. None without a daily window."""
    daily = get_plan_entitlement(plan).daily_tcu_limit
    return None if daily is None else round(daily * (1 - chat_agent_floor_share(plan)), 4)


def protected_floor_tcu(plan: str) -> tuple[float | None, float | None]:
    """(daily, weekly) TCU only chat and agents may spend."""
    entitlement = get_plan_entitlement(plan)
    share = chat_agent_floor_share(plan)
    daily = None if entitlement.daily_tcu_limit is None else round(entitlement.daily_tcu_limit * share, 4)
    weekly = None if entitlement.weekly_tcu_limit is None else round(entitlement.weekly_tcu_limit * share, 4)
    return daily, weekly


def sustainable_media_daily_tcu(plan: str) -> float | None:
    """What media can spend every day of a week: the daily pool, or a seventh of the weekly pool if smaller."""
    weekly = media_weekly_pool_tcu(plan)
    candidates = [pool for pool in (media_daily_pool_tcu(plan), weekly / 7 if weekly is not None else None)
                  if pool is not None]
    return min(candidates) if candidates else None


def affordable_daily_count(plan: str, table_limit: int, unit_usd: float) -> int:
    """The daily number a plan is offered: its reference count, lowered to what the media pool pays for every day.

    0 when one creation does not fit a single request's ceiling.
    """
    if table_limit <= 0:
        return 0
    if unit_usd <= 0:
        return table_limit
    unit_tcu = usd_to_tcu(unit_usd)
    if unit_tcu > get_plan_entitlement(plan).max_tcu_per_request:
        return 0
    pool = sustainable_media_daily_tcu(plan)
    if pool is None:
        return table_limit
    return min(table_limit, max(0, int((pool + 1e-9) // unit_tcu)))


def joint_daily_counts(plan: str, images: int, image_unit_usd: float, videos: int,
                       video_unit_usd: float) -> tuple[int, int]:
    """Image and video counts a day that the one media pool pays for *together*.

    Each kind alone is already within the pool (affordable_daily_count); when both at
    their reference would not fit side by side, both shrink by the same factor, so the
    numbers a person reads can be reached in the same day.
    """
    pool = sustainable_media_daily_tcu(plan)
    total = images * usd_to_tcu(image_unit_usd) + videos * usd_to_tcu(video_unit_usd)
    if pool is None or total <= pool + 1e-9:
        return images, videos
    factor = pool / total
    return int(images * factor + 1e-9), int(videos * factor + 1e-9)


def vision_daily_limit(plan: str, unit_usd: float) -> int:
    """Image readings a day. No product ceiling beyond the window share: a reading costs a fraction of a cent."""
    if unit_usd <= 0:
        return 0
    share = _share_count(plan, unit_usd)
    return 0 if share is None else share
