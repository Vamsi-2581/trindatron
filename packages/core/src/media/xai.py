"""Hosted image and video on the xAI Imagine API.

  XaiImageEngine  text-to-image and image editing (one source image)
  XaiVideoEngine  text-to-video and image-to-video, 1-15 s, 480p/720p

Cost is what the call reports (`usage.cost_in_usd_ticks`, 1 USD = 10^10 ticks);
when a response carries no usage, the published price for exactly what was
asked. Prices and model ids are MediaSettings values, so a price change is a
configuration change. Nothing here reaches a user: EngineFailed reasons are
status codes and short labels, for logs only.
"""
from __future__ import annotations

import asyncio
import base64
import io
import math
import time
from dataclasses import replace

from src.media.engines import (
    EngineFailed,
    EngineUnavailable,
    ImageEngine,
    ImageRequest,
    MediaOutput,
    VideoEngine,
    VideoRequest,
)
from src.media.hosted import ClientFactory, _download, _json, _send, default_client
from src.media.settings import media_settings

USD_PER_TICK = 1e-10

# Ratios the Imagine API accepts (docs.x.ai REST reference, 2026-09-14).
IMAGE_RATIOS = ("1:1", "3:4", "4:3", "9:16", "16:9", "2:3", "3:2", "9:19.5", "19.5:9", "9:20", "20:9",
                "1:2", "2:1", "21:9", "5:2")
VIDEO_RATIOS = ("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3")
_VIDEO_MIN_SECONDS, _VIDEO_MAX_SECONDS = 1, 15
_VIDEO_FPS = 24


def nearest_ratio(width: int, height: int, ratios: tuple[str, ...]) -> str:
    """The supported ratio closest to width:height, compared on a log scale (2:1 and 1:2 are equally far from 1:1)."""
    wanted = math.log(max(1, width) / max(1, height))

    def distance(label: str) -> float:
        w, h = (float(part) for part in label.split(":"))
        return abs(math.log(w / h) - wanted)

    return min(ratios, key=distance)


def _ratio_value(label: str) -> float:
    w, h = (float(part) for part in label.split(":"))
    return w / h


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {media_settings().xai_api_key}"}


def _cost_from_usage(payload: dict) -> float | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict) or usage.get("cost_in_usd_ticks") is None:
        return None
    try:
        return max(0.0, int(usage["cost_in_usd_ticks"]) * USD_PER_TICK)
    except (TypeError, ValueError):
        return None


def _data_uri(data: bytes, content_type: str | None) -> str:
    return f"data:{content_type or 'image/png'};base64,{base64.b64encode(data).decode()}"


def _image_size(data: bytes) -> tuple[int | None, int | None, str | None]:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            fmt = (image.format or "").lower()
            return image.width, image.height, {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(fmt)
    except Exception:
        return None, None, None


class XaiImageEngine(ImageEngine):
    """Flat price per image; editing takes the source image as a data URI."""

    def __init__(self, client_factory: ClientFactory = default_client) -> None:
        self._client_factory = client_factory

    async def is_available(self, *, editing: bool = False) -> bool:
        settings = media_settings()
        if not settings.xai_api_key:
            return False
        try:
            async with self._client_factory() as client:
                response = await client.get(f"{settings.media_xai_base_url}/image-generation-models",
                                            headers=_headers(), timeout=15)
            if response.status_code != 200:
                return False
            items = response.json().get("models") or response.json().get("data") or []
            return any(isinstance(item, dict) and item.get("id") == settings.media_xai_image_model for item in items)
        except Exception:
            return False

    @staticmethod
    def _resolution(request: ImageRequest) -> str:
        return "2k" if max(request.width, request.height) > 1024 else "1k"

    def estimate_usd(self, request: ImageRequest) -> float:
        settings = media_settings()
        price = settings.media_xai_image_usd_per_image
        if self._resolution(request) == "2k":
            price *= settings.media_xai_image_2k_multiplier
        if request.source is not None:
            price += settings.media_xai_image_input_usd
        return price

    async def generate(self, request: ImageRequest) -> MediaOutput:
        settings = media_settings()
        if not settings.xai_api_key:
            raise EngineUnavailable("xai: no key")
        editing = request.source is not None
        body: dict = {
            "model": settings.media_xai_image_model,
            "prompt": request.prompt,
            "n": 1,
            "aspect_ratio": nearest_ratio(request.width, request.height, IMAGE_RATIOS),
            "resolution": self._resolution(request),
            "response_format": "b64_json",
        }
        if editing:
            body["image"] = {"url": _data_uri(request.source, request.source_content_type)}
        path = "images/edits" if editing else "images/generations"
        started = time.monotonic()
        async with self._client_factory() as client:
            response = await _send(client, "POST", f"{settings.media_xai_base_url}/{path}", "xai image",
                                   json=body, headers=_headers(), timeout=settings.media_image_timeout_s)
            payload = _json(response, "xai image")
            items = payload.get("data") or []
            first = items[0] if items and isinstance(items[0], dict) else {}
            if first.get("b64_json"):
                try:
                    data = base64.b64decode(first["b64_json"])
                except (TypeError, ValueError) as exc:
                    raise EngineFailed("xai image: bad output") from exc
                content_type = str(first.get("mime_type") or "")
            elif first.get("url"):
                data, content_type = await _download(client, first["url"], "xai image download")
            else:
                raise EngineFailed("xai image: no output")
        width, height, detected = _image_size(data)
        cost = _cost_from_usage(payload)
        return MediaOutput(data=data, content_type=(content_type or detected or "image/jpeg").lower(),
                           width=width, height=height, duration_seconds=None,
                           engine_seconds=time.monotonic() - started,
                           cost_usd=self.estimate_usd(request) if cost is None else cost)


class XaiVideoEngine(VideoEngine):
    """Priced per output second by resolution; the first frame may be an image."""

    def __init__(self, client_factory: ClientFactory = default_client) -> None:
        self._client_factory = client_factory

    async def is_available(self, *, from_image: bool = False) -> bool:
        settings = media_settings()
        if not settings.xai_api_key:
            return False
        try:
            async with self._client_factory() as client:
                response = await client.get(f"{settings.media_xai_base_url}/video-generation-models",
                                            headers=_headers(), timeout=15)
            if response.status_code != 200:
                return False
            items = response.json().get("models") or response.json().get("data") or []
            return any(isinstance(item, dict) and item.get("id") == settings.media_xai_video_model for item in items)
        except Exception:
            return False

    @staticmethod
    def _tier(short_side: int) -> int:
        return 720 if short_side > 480 else 480

    def normalize(self, request: VideoRequest) -> VideoRequest:
        duration = max(_VIDEO_MIN_SECONDS, min(_VIDEO_MAX_SECONDS, request.duration_seconds))
        tier = self._tier(min(request.width, request.height) or 480)
        ratio = nearest_ratio(request.width or 16, request.height or 9, VIDEO_RATIOS)
        value = _ratio_value(ratio)
        if value >= 1:
            width, height = round(tier * value), tier
        else:
            width, height = tier, round(tier / value)
        return replace(request, width=width - width % 2, height=height - height % 2,
                       duration_seconds=duration, fps=_VIDEO_FPS)

    @staticmethod
    def _price_per_second(short_side: int) -> float:
        settings = media_settings()
        return (settings.media_xai_video_usd_per_second_720p if short_side > 480
                else settings.media_xai_video_usd_per_second_480p)

    def estimate_usd(self, request: VideoRequest) -> float:
        settings = media_settings()
        price = self._price_per_second(min(request.width, request.height)) * request.duration_seconds
        return price + (settings.media_xai_image_input_usd if request.source is not None else 0.0)

    async def generate(self, request: VideoRequest) -> MediaOutput:
        settings = media_settings()
        if not settings.xai_api_key:
            raise EngineUnavailable("xai: no key")
        request = self.normalize(request)
        body: dict = {
            "model": settings.media_xai_video_model,
            "prompt": request.prompt,
            "duration": request.duration_seconds,
            "aspect_ratio": nearest_ratio(request.width, request.height, VIDEO_RATIOS),
            "resolution": f"{self._tier(min(request.width, request.height))}p",
        }
        if request.source is not None:
            body["image"] = {"url": _data_uri(request.source, request.source_content_type)}
        started = time.monotonic()
        deadline = started + settings.media_video_timeout_s
        base = settings.media_xai_base_url
        async with self._client_factory() as client:
            submitted = _json(await _send(client, "POST", f"{base}/videos/generations", "xai video submit",
                                          json=body, headers=_headers()), "xai video submit")
            request_id = submitted.get("request_id")
            if not request_id:
                raise EngineFailed("xai video: no request id")
            while True:
                state = _json(await _send(client, "GET", f"{base}/videos/{request_id}", "xai video status",
                                          headers=_headers()), "xai video status")
                status = str(state.get("status", "")).lower()
                if status == "done":
                    break
                if status == "failed":
                    code = (state.get("error") or {}).get("code") if isinstance(state.get("error"), dict) else None
                    raise EngineFailed(f"xai video: failed {code or ''}".strip(),
                                       retryable=code in ("service_unavailable", "internal_error"))
                if status not in ("pending", "queued", "processing", "in_progress", ""):
                    raise EngineFailed(f"xai video: status {status}")
                if time.monotonic() > deadline:
                    raise EngineFailed("xai video: timeout")
                await asyncio.sleep(settings.media_xai_poll_interval_s)
            video = state.get("video") or {}
            url = video.get("url") if isinstance(video, dict) else None
            if not url:
                raise EngineFailed("xai video: no output (filtered)" if isinstance(video, dict) and video.get("respect_moderation") is False
                                   else "xai video: no output")
            data, content_type = await _download(client, url, "xai video download")
        duration = float(video.get("duration") or request.duration_seconds)
        cost = _cost_from_usage(state)
        return MediaOutput(data=data, content_type=content_type if content_type.startswith("video/") else "video/mp4",
                           width=request.width, height=request.height, duration_seconds=duration,
                           engine_seconds=time.monotonic() - started,
                           cost_usd=self.estimate_usd(request) if cost is None else cost)
