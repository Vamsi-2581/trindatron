"""GET /users/me/capabilities — what this user can do right now, in one answer.

Every client (Mobile, Web, Desktop) decides what to offer from this response
and nothing else, so a button never appears for something the Core would
refuse. A capability is `available` only when every layer agrees: the plan
includes it, what the configured engine charges fits the plan's per-request
ceiling and daily allowance, the operator has not switched it off, an engine is
configured and reachable, and there is somewhere to store the result.

Only abstract names leave this module. Engines, vendors and costs never do.
"""
from __future__ import annotations

import asyncio
import shutil
import time

from sqlalchemy.orm import Session as DBSession

from src.entitlements import get_plan_entitlement
from src.media import modes
from src.media.registry import image_engine, video_engine, vision_engine
from src.models.user import User
from src.services import media_service, vision_service
from src.services.runtime_settings import is_capability_disabled

# Engine reachability is a network round trip; clients ask for capabilities on
# every screen focus. A short cache keeps that from becoming load on the engine.
_ENGINE_CACHE_SECONDS = 30.0
_engine_cache: dict[str, tuple[float, bool]] = {}


async def _cached(key: str, probe) -> bool:
    now = time.monotonic()
    hit = _engine_cache.get(key)
    if hit and now - hit[0] < _ENGINE_CACHE_SECONDS:
        return hit[1]
    value = bool(await probe())
    _engine_cache[key] = (now, value)
    return value


def _probe(engine, quality_tier: str, **kwargs):
    """Availability for this plan: an engine chain only counts routes that meet the plan's quality floor."""
    from src.media.orchestrator import ImageEngineChain, VideoEngineChain

    if isinstance(engine, (ImageEngineChain, VideoEngineChain)):
        return engine.is_available(quality_tier=quality_tier, **kwargs)
    return engine.is_available(**kwargs)


def reset_cache() -> None:
    _engine_cache.clear()
    _matrix_cache.clear()


# Execution sandboxes are probed by shelling out to Docker: slower, and they change rarely.
_SANDBOX_CACHE_SECONDS = 300.0
_sandbox_cache: dict[str, tuple[float, bool]] = {}
# The last full matrix each person received, for the conversation prompt (capability/matrix.py snapshot()).
_MATRIX_PROMPT_MAX_AGE_SECONDS = 600.0
_matrix_cache: dict[str, tuple[float, dict]] = {}


def cached_matrix(user_id: str) -> dict | None:
    hit = _matrix_cache.get(user_id)
    if hit and time.monotonic() - hit[0] < _MATRIX_PROMPT_MAX_AGE_SECONDS:
        return hit[1]
    return None


async def _sandbox_cached(key: str, probe) -> bool:
    now = time.monotonic()
    hit = _sandbox_cache.get(key)
    if hit and now - hit[0] < _SANDBOX_CACHE_SECONDS:
        return hit[1]
    try:
        value = bool(await asyncio.wait_for(probe(), timeout=8))
    except Exception:
        value = False
    _sandbox_cache[key] = (now, value)
    return value


async def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    process = await asyncio.create_subprocess_exec(
        "docker", "info", "--format", "{{.ServerVersion}}",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        return await asyncio.wait_for(process.wait(), timeout=6) == 0
    except asyncio.TimeoutError:
        process.kill()
        return False


async def _desktop_ready(plan: str) -> bool:
    from src.services.agent_service import _desktop_available

    return await _desktop_available(plan)


def _state(available: bool, *, daily=None, limits: dict | None = None) -> dict:
    state: dict = {"available": bool(available)}
    if available and daily is not None:
        state["daily_quota"] = {"limit": daily.limit, "remaining": daily.remaining,
                                "resets_at": daily.resets_at.isoformat()}
    if available and limits:
        state["limits"] = limits
    return state


async def user_capabilities(db: DBSession, user: User) -> dict:
    entitlement = get_plan_entitlement(user.plan)
    # Counts and the longest clip are already lowered to what the configured engines charge.
    media = media_service.entitlement_for(db, user)
    storage = await media_service.storage_ready()

    image = image_engine()
    image_ok = storage and image is not None and not is_capability_disabled(db, "image") and media.image_daily_limit > 0
    image_tier = media.image_quality_tier
    image_generate = image_ok and await _cached(f"image:{image_tier}", lambda: _probe(image, image_tier))
    image_edit = image_ok and media.image_editing and await _cached(
        f"image_edit:{image_tier}", lambda: _probe(image, image_tier, editing=True))
    image_daily = media_service.daily_usage(db, user, "image", media)
    image_limits = {"quality_tier": media.image_quality_tier,
                    "max_resolution": f"{media.max_image_resolution}x{media.max_image_resolution}"}

    # Probed on the routes of the mode a request runs in by default (media/modes.py): premium only when
    # it is the one mode this plan has on these engines, and then every request asks for confirmation.
    video_mode = modes.get(media.default_video_mode) or modes.PREMIUM
    video = modes.restrict(video_engine(), video_mode)
    video_ok = storage and video is not None and not is_capability_disabled(db, "video") and media.video_daily_limit > 0
    video_tier = media.video_quality_tier
    video_generate = video_ok and await _cached(f"video:{video_mode.id}:{video_tier}", lambda: _probe(video, video_tier))
    # A photo is animated only in Maximum (media_service.create_video_job): probed on those routes, not the default's.
    photo_video = modes.restrict(video_engine(), modes.MAXIMUM)
    video_from_image = (video_ok and media.image_to_video and photo_video is not None and await _cached(
        f"video_i2v:{modes.MAXIMUM.id}:{modes.MAXIMUM.quality_tier}",
        lambda: _probe(photo_video, modes.MAXIMUM.quality_tier, from_image=True)))
    video_daily = media_service.daily_usage(db, user, "video", media)
    video_limits = {"quality_tier": media.video_quality_tier, "max_resolution": f"{media.max_video_resolution}p",
                    "max_duration_seconds": media.max_video_duration_seconds,
                    "modes": list(media.video_modes), "default_mode": media.default_video_mode}

    vision = vision_engine()
    vision_limit = vision_service.daily_limit(user)
    vision_ok = (storage and vision is not None and vision_limit > 0 and not is_capability_disabled(db, "vision")
                 and await _cached("vision", lambda: vision.is_available()))
    vision_daily = None
    if vision_ok:
        _, resets_at = vision_service.daily_window(user)
        vision_daily = media_service.DailyUsage(limit=vision_limit, used=vision_service.used_today(db, user),
                                                resets_at=resets_at)

    from src.capability import matrix as capability_matrix

    presentations = await _presentations_available(db, user)
    # Voice input is offered only when some transcriber is configured and the operator has not switched voice off.
    voice_input = capability_matrix.stt_engines() > 0 and not is_capability_disabled(db, "voice")
    legacy = {
            "image_generation": _state(image_generate, daily=image_daily, limits=image_limits),
            "image_editing": _state(image_edit, daily=image_daily, limits=image_limits),
            "video_generation": _state(video_generate, daily=video_daily, limits=video_limits),
            "image_to_video": _state(video_from_image, daily=video_daily, limits=video_limits),
            # No avatar engine is integrated yet (docs/MEDIA_ENGINES.md §4).
            "avatar_generation": _state(False),
            "voice_input": _state(voice_input),
            "agent_mode": _state(entitlement.supports_agent_mode),
            "persistent_agents": _state(entitlement.max_agents != 0),
            "project_files": _state(True),
            # Documents attached to a conversation are read into its context.
            "chat_attachments": _state(True),
            # An attached image is read once and its description enters the context.
            "image_understanding": _state(vision_ok, daily=vision_daily),
            "presentations": _state(presentations),
    }

    # Only probed for plans that include the capability: a Docker round trip for nothing is load, not information.
    agent_plan = bool(entitlement.supports_agent_mode)
    sandbox_ready = agent_plan and await _sandbox_cached("docker", _docker_ready)
    desktop_ready = (bool(getattr(entitlement, "supports_trinity_work", False)) and sandbox_ready
                     and await _sandbox_cached(f"desktop:{user.plan}", lambda: _desktop_ready(user.plan)))
    full = capability_matrix.build(db, user, legacy, desktop_ready=desktop_ready, sandbox_ready=sandbox_ready,
                                   presentations=presentations)
    _matrix_cache[user.id] = (time.monotonic(), full)
    return {"capabilities": legacy, "matrix": full}


async def _presentations_available(db: DBSession, user: User) -> bool:
    from src.services import presentation_service

    if not presentation_service.plan_allows_presentations(user.plan) or is_capability_disabled(db, "presentation"):
        return False
    return await _cached("presentation", lambda: _presentation_probe(presentation_service))


async def _presentation_probe(presentation_service) -> bool:
    return (await presentation_service.availability()).available
