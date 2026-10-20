"""Reading attached images, inside the plan's economics.

One description per attached image, paid through the same reservation as any
other spend: the plan's per-request ceiling, its session/daily/weekly windows
and, on FREE, the plan-wide daily budget. The daily number of readings follows
from what one reading can cost (plans.vision_daily_limit).

An image that cannot be read — no engine, limit reached, window spent, engine
failure — is still attached. Its content is then marked unreadable and
TRINDATRON says it cannot see it instead of guessing.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session as DBSession

from src.media import plans
from src.media.engines import EngineFailed, EngineUnavailable
from src.media.registry import vision_engine
from src.media.settings import media_settings
from src.media.vision import VISION_TYPES
from src.models.attachment import SessionAttachment
from src.models.user import User
from src.services import entitlement_service
from src.services.runtime_settings import is_capability_disabled
from src.timezones import day_window

logger = logging.getLogger("trinity.vision")

DESCRIPTION_PREFIX = "What this image shows (read by TRINDATRON from the image itself):\n"
RETRY_DELAY_SECONDS = 2.0


def daily_limit(user: User) -> int:
    engine = vision_engine()
    return 0 if engine is None else plans.vision_daily_limit(user.plan, engine.estimate_usd())


def daily_window(user: User) -> tuple[datetime, datetime]:
    return day_window(datetime.now(timezone.utc), user.timezone)


def used_today(db: DBSession, user: User) -> int:
    start, end = daily_window(user)
    return (
        db.query(SessionAttachment)
        .filter(SessionAttachment.user_id == user.id, SessionAttachment.extraction_status == "vision",
                SessionAttachment.created_at >= start, SessionAttachment.created_at < end)
        .count()
    )


async def read_image(db: DBSession, user: User, data: bytes, content_type: str) -> tuple[str | None, str]:
    """(description, "vision") when the image was read; (None, "unsupported") otherwise. Never raises."""
    engine = vision_engine()
    if engine is None or content_type not in VISION_TYPES or is_capability_disabled(db, "vision"):
        return None, "unsupported"
    limit = daily_limit(user)
    if limit <= 0 or used_today(db, user) >= limit:
        return None, "unsupported"

    estimated_tcu = plans.usd_to_tcu(engine.estimate_usd())
    try:
        reservation_id = entitlement_service.check_and_reserve(
            db, user.id, None, user.plan, user.timezone, estimated_tcu=estimated_tcu,
        )
    except HTTPException:
        return None, "unsupported"
    budget = entitlement_service.get_reservation_budget_tcu(db, reservation_id)
    if budget + 1e-6 < estimated_tcu:
        entitlement_service.cancel_reservation(db, reservation_id)
        return None, "unsupported"

    result = None
    # A transient refusal (provider overloaded, rate limited, timeout) is tried once more
    # inside the same reservation; a definitive one is not.
    for attempt in (1, 2):
        try:
            result = await asyncio.wait_for(engine.describe(data, content_type),
                                            timeout=media_settings().media_vision_timeout_s + 5)
            break
        except EngineFailed as exc:
            logger.info("vision: image not read | attempt=%d reason=%s retryable=%s", attempt, exc.reason, exc.retryable)
            if not exc.retryable or attempt == 2:
                break
            await asyncio.sleep(RETRY_DELAY_SECONDS)
        except asyncio.TimeoutError:
            logger.info("vision: image not read | attempt=%d reason=timeout", attempt)
            if attempt == 2:
                break
        except EngineUnavailable as exc:
            logger.info("vision: image not read | reason=%s", exc)
            break
        except Exception:
            logger.exception("vision: unexpected failure")
            break
    if result is None:
        entitlement_service.cancel_reservation(db, reservation_id)
        return None, "unsupported"

    entitlement_service.finalize_usage(
        db, reservation_id, message_id=None, model="media:vision", input_tokens=result.input_tokens,
        output_tokens=result.output_tokens, cached_tokens=0, cost_usd=round(result.cost_usd, 8),
        tcu=min(plans.usd_to_tcu(result.cost_usd), budget), duration_ms=int(result.engine_seconds * 1000),
    )
    logger.info("vision: image read | user=%s engine_seconds=%.2f", user.id, result.engine_seconds)
    return DESCRIPTION_PREFIX + result.text, "vision"
