"""Hosted media engines: a pay-per-use API does the generation, TRINDATRON keeps everything else.

Each adapter turns a validated request into bytes and reports what the call
cost — from the usage the provider returned when it returns one, otherwise from
its published price for exactly what was requested. Prices are MediaSettings
values with their source and date, so a price change is a configuration change.

Nothing here reaches a user: not the provider, the model, a URL, a price or the
provider's own error text (EngineFailed reasons are status codes, for logs).

  FalImageEngine    Z-Image Turbo or FLUX.1-schnell (both Apache-2.0 weights), billed per megapixel
  FalVideoEngine    Wan 2.2 A14B (Apache-2.0 weights), billed per output second
  OpenAIImageEngine Images API, billed per token
  VeoVideoEngine    Veo on the Gemini API, billed per output second
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import replace
from typing import Callable

import httpx

from src.media.engines import (
    EngineFailed,
    EngineUnavailable,
    FailureKind,
    ImageEngine,
    ImageRequest,
    JobCancelled,
    MediaOutput,
    ProviderHandle,
    ProviderLink,
    VideoEngine,
    VideoRequest,
    safe_provider_detail,
)
from src.media.operations import OperationEngine, OperationRequest, ResolvedInput
from src.media.runners.base import (
    assert_public_https_url,
    kind_from_status,
    retry_after_seconds,
    video_frame_count,
    video_metadata,
)
from src.media.settings import media_settings

logger = logging.getLogger("trinity.media.hosted")

ClientFactory = Callable[[], httpx.AsyncClient]


def default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True)


def _raise_for(response: httpx.Response, what: str) -> None:
    if response.status_code < 400:
        return
    # 429 is a quota or a rate limit, 5xx is transient, 401/402/403/404 mean this provider cannot serve now
    # (key, billing, model gone): all let the chain fall back. Any other 4xx refuses the input itself.
    status = response.status_code
    if status in (401, 402, 403, 404, 429) or status >= 500:
        kind = kind_from_status(status, response.text[:500])
    else:
        kind = FailureKind.REJECTED
    raise EngineFailed(f"{what}: HTTP {status}", kind=kind,
                       retry_after=retry_after_seconds(response.headers.get("retry-after")))


async def _send(client: httpx.AsyncClient, method: str, url: str, what: str, **kwargs) -> httpx.Response:
    try:
        response = await client.request(method, url, **kwargs)
    except httpx.TimeoutException as exc:
        raise EngineFailed(f"{what}: timeout", kind=FailureKind.TIMEOUT) from exc
    except httpx.HTTPError as exc:
        raise EngineFailed(f"{what}: connection", kind=FailureKind.TEMPORARY) from exc
    _raise_for(response, what)
    return response


def _json(response: httpx.Response, what: str) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise EngineFailed(f"{what}: not JSON") from exc
    if not isinstance(payload, dict):
        raise EngineFailed(f"{what}: unexpected shape")
    return payload


async def _download(client: httpx.AsyncClient, url: str, what: str, headers: dict | None = None) -> tuple[bytes, str]:
    # CP18: the URL comes from the provider's own response, not from the request we sent — refuse
    # anything that isn't https or that literally names a private/internal address before fetching it.
    url = assert_public_https_url(url, what)
    response = await _send(client, "GET", url, what, headers=headers or {})
    if not response.content:
        raise EngineFailed(f"{what}: empty")
    return response.content, response.headers.get("content-type", "").split(";")[0].strip().lower()


# ── fal: open-weight models, hosted ─────────────────────────────────────────

def _fal_headers() -> dict[str, str]:
    return {"Authorization": f"Key {media_settings().fal_key}"}


class FalImageEngine(ImageEngine):
    """A text-to-image model hosted on fal. Billed per megapixel of output; at most 8 steps.

    Defaults to Z-Image Turbo (the original, single-model shape this class always had). CP18
    generalised it to take an explicit endpoint/price so a second fal-hosted model — FLUX.1-schnell,
    Apache-2.0, unconditional commercial use per its own LICENSE-FLUX1-schnell — can reuse the exact
    same call/error/billing logic as its own route, instead of a near-duplicate class. Z-Image
    Turbo's own route keeps calling FalImageEngine() with no arguments, so its behaviour is unchanged.
    """

    def __init__(self, client_factory: ClientFactory = default_client, *, endpoint: str | None = None,
                usd_per_megapixel: float | None = None) -> None:
        self._client_factory = client_factory
        self._endpoint = endpoint
        self._usd_per_megapixel = usd_per_megapixel

    def _settings(self) -> tuple[str, float]:
        settings = media_settings()
        endpoint = self._endpoint if self._endpoint is not None else settings.media_fal_image_endpoint
        price = self._usd_per_megapixel if self._usd_per_megapixel is not None else settings.media_fal_image_usd_per_megapixel
        return endpoint, price

    async def is_available(self, *, editing: bool = False) -> bool:
        # Editing needs a different workflow than the one configured. fal has no
        # free probe endpoint, so reachability is proven by the first job.
        endpoint, _ = self._settings()
        return not editing and bool(media_settings().fal_key and endpoint)

    def estimate_usd(self, request: ImageRequest) -> float:
        _, price = self._settings()
        megapixels = request.width * request.height / 1_000_000
        return megapixels * price

    async def generate(self, request: ImageRequest) -> MediaOutput:
        settings = media_settings()
        if not settings.fal_key:
            raise EngineUnavailable("fal: no key")
        endpoint, price = self._settings()
        if not endpoint:
            raise EngineUnavailable("fal: no endpoint configured")
        body = {
            "prompt": request.prompt,
            "image_size": {"width": request.width, "height": request.height},
            "num_inference_steps": max(1, min(8, request.steps)),
            "seed": request.seed,
            "num_images": 1,
            "enable_safety_checker": True,
            "output_format": "jpeg",
        }
        started = time.monotonic()
        async with self._client_factory() as client:
            response = await _send(client, "POST", f"https://fal.run/{endpoint}", "fal image",
                                   json=body, headers=_fal_headers(), timeout=settings.media_image_timeout_s)
            images = _json(response, "fal image").get("images") or []
            if not images or not isinstance(images[0], dict) or not images[0].get("url"):
                raise EngineFailed("fal image: no output")
            data, content_type = await _download(client, images[0]["url"], "fal image download")
        width = int(images[0].get("width") or request.width)
        height = int(images[0].get("height") or request.height)
        cost = width * height / 1_000_000 * price
        return MediaOutput(data=data, content_type=content_type or "image/jpeg", width=width, height=height,
                           duration_seconds=None, engine_seconds=time.monotonic() - started, cost_usd=cost)


_FAL_VIDEO_FPS = 16
_FAL_MIN_FRAMES, _FAL_MAX_FRAMES = 17, 161


def _fal_video_tier(short_side: int) -> int:
    return 480 if short_side <= 480 else 580 if short_side <= 580 else 720


class FalVideoEngine(VideoEngine):
    """Wan 2.2 A14B text-to-video. Billed per output second at 16 fps, by resolution."""

    def __init__(self, client_factory: ClientFactory = default_client) -> None:
        self._client_factory = client_factory

    async def is_available(self, *, from_image: bool = False) -> bool:
        return not from_image and bool(media_settings().fal_key and media_settings().media_fal_video_endpoint)

    def normalize(self, request: VideoRequest) -> VideoRequest:
        frames = max(_FAL_MIN_FRAMES, min(_FAL_MAX_FRAMES, request.duration_seconds * _FAL_VIDEO_FPS + 1))
        tier = _fal_video_tier(min(request.width, request.height) or 480)
        if request.width >= request.height:
            width, height = (tier * 16 // 9 if request.width > request.height else tier), tier
        else:
            width, height = tier, tier * 16 // 9
        return replace(request, width=width - width % 16, height=height - height % 16,
                       duration_seconds=(frames - 1) // _FAL_VIDEO_FPS, fps=_FAL_VIDEO_FPS)

    @staticmethod
    def _price_per_second(short_side: int) -> float:
        settings = media_settings()
        return {480: settings.media_fal_video_usd_per_second_480p, 580: settings.media_fal_video_usd_per_second_580p,
                720: settings.media_fal_video_usd_per_second_720p}[_fal_video_tier(short_side)]

    def estimate_usd(self, request: VideoRequest) -> float:
        return self._price_per_second(min(request.width, request.height)) * request.frames / _FAL_VIDEO_FPS

    async def generate(self, request: VideoRequest) -> MediaOutput:
        settings = media_settings()
        if not settings.fal_key:
            raise EngineUnavailable("fal: no key")
        request = self.normalize(request)
        short_side = min(request.width, request.height)
        aspect = "1:1" if request.width == request.height else "16:9" if request.width > request.height else "9:16"
        body = {
            "prompt": request.prompt,
            "num_frames": request.frames,
            "frames_per_second": _FAL_VIDEO_FPS,
            "resolution": f"{_fal_video_tier(short_side)}p",
            "aspect_ratio": aspect,
            "seed": request.seed,
            "enable_safety_checker": True,
            "enable_prompt_expansion": False,
        }
        started = time.monotonic()
        deadline = started + settings.media_video_timeout_s
        async with self._client_factory() as client:
            submitted = _json(await _send(client, "POST", f"https://queue.fal.run/{settings.media_fal_video_endpoint}",
                                          "fal video submit", json=body, headers=_fal_headers()), "fal video submit")
            status_url, response_url = submitted.get("status_url"), submitted.get("response_url")
            if not status_url or not response_url:
                raise EngineFailed("fal video: no queue handle")
            while True:
                state = _json(await _send(client, "GET", status_url, "fal video status", headers=_fal_headers()),
                              "fal video status")
                status = str(state.get("status", "")).upper()
                if status == "COMPLETED":
                    break
                if status not in ("IN_QUEUE", "IN_PROGRESS"):
                    raise EngineFailed(f"fal video: status {status or 'unknown'}")
                if time.monotonic() > deadline:
                    raise EngineFailed("fal video: timeout", retryable=False)
                await asyncio.sleep(settings.media_fal_poll_interval_s)
            result = _json(await _send(client, "GET", response_url, "fal video result", headers=_fal_headers()),
                           "fal video result")
            video = result.get("video") or {}
            if not isinstance(video, dict) or not video.get("url"):
                raise EngineFailed("fal video: no output")
            data, content_type = await _download(client, video["url"], "fal video download")
        return MediaOutput(data=data, content_type=content_type or "video/mp4", width=request.width,
                           height=request.height, duration_seconds=request.frames / _FAL_VIDEO_FPS,
                           engine_seconds=time.monotonic() - started, cost_usd=self.estimate_usd(request))


# ── character_animate on fal: Wan 2.2 Animate Move (CP46) ───────────────────

_FAL_ANIMATE_FRAMES_PER_BILLED_SECOND = 16  # DOCUMENTED, fal.ai model page: 16 frames = 1 billed "video second"
_FAL_ANIMATE_RESOLUTIONS = ("480p", "580p", "720p")


def _fal_data_url(data: bytes, content_type: str) -> str:
    """A data: URI — fal.ai's own documented fallback input format when a file isn't already on
    the fal CDN or another public URL (docs.fal.ai/documentation/model-apis/fal-cdn, read
    2026-09-22: "Models also accept base64-encoded data URIs for inline file content").

    ponytail: fal's real upload-to-CDN flow is SDK-only (fal_client.upload_file) — its raw REST
    shape is not in the documented Platform API reference (only ACL/signing for files already
    uploaded), and this codebase doesn't depend on the fal_client package. Docs call data URIs
    "not recommended for files larger than a few KB" for exactly this case; a character photo and
    a short driving clip (hundreds of KB to a few MB here) still work, just less efficiently than a
    real CDN upload would. Upgrade path: add fal_client (or reverse-engineer its multipart upload
    calls) if/when input sizes grow enough for this to matter.
    """
    return f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"


class FalWanAnimateEngine(OperationEngine):
    """fal-ai/wan/v2.2-14b/animate/move ("Move"): character image + driving video -> animated
    video (character_animate's `character`/`driving_video` roles).

    CP46: chosen as character_animate's low-cost PRIMARY (Higgsfield Genjutsu remains
    fallback/premium) over the sibling fal-ai/wan-motion — both are Wan-Animate variants (their own
    OpenAPI schema titles are "WanAnimateMoveInput"/"WanAnimateSimpleInput"), but wan-motion is
    fixed at 720p ($0.06/billed-second floor, no cheaper tier at all) while this model offers 480p
    at $0.04 — for the real cp40_driving_5s.mp4 asset (119 real frames), Move@480p costs $0.2975
    versus wan-motion@720p's ~$0.45 for the same clip, and both are already far cheaper than
    Higgsfield Genjutsu's $1.908 for the identical file (CP44).

    Reuses fal.ai's own queue lifecycle exactly as FalVideoEngine above already implements it
    (submit -> status_url/response_url -> poll IN_QUEUE/IN_PROGRESS/COMPLETED -> fetch response_url)
    and the same _send/_json/_download/_fal_headers/_raise_for helpers — the two-input body and
    OperationEngine shape are the genuinely new parts, mirroring HiggsfieldGenjutsuEngine's own
    precedent for character_animate's other provider (media/higgsfield.py). Unlike FalVideoEngine,
    this also checks for `error`/`error_type` on a COMPLETED status: fal's own docs state a
    COMPLETED request can still have failed (docs.fal.ai/model-endpoints/queue, read 2026-09-22:
    "error ... present only if the request failed (only when COMPLETED)").
    """

    operation = "character_animate"
    route = "wan_animate_move@fal"

    def __init__(self, client_factory: ClientFactory = default_client) -> None:
        self._client_factory = client_factory

    async def is_available(self) -> bool:
        settings = media_settings()
        return bool(settings.fal_key and settings.media_fal_animate_move_endpoint)

    @staticmethod
    async def _cancel(client: httpx.AsyncClient, cancel_url: str) -> bool:
        """DOCUMENTED (docs.fal.ai/model-endpoints/queue): PUT the cancel_url. 202 = accepted
        (still queued); 400 ALREADY_COMPLETED / 404 NOT_FOUND both mean nothing more to do here —
        neither is an error worth raising, so this bypasses _send's generic raise-on-4xx and
        reports a plain bool, the same convention HiggsfieldClient.cancel() already uses."""
        try:
            response = await client.request("PUT", cancel_url, headers=_fal_headers())
        except httpx.HTTPError:
            return False
        return response.status_code == 202

    @staticmethod
    def _resolution(parameters: dict) -> str:
        resolution = str(parameters.get("resolution") or "480p").lower()
        return resolution if resolution in _FAL_ANIMATE_RESOLUTIONS else "480p"

    @staticmethod
    def _price_per_second(resolution: str) -> float:
        settings = media_settings()
        return {"480p": settings.media_fal_animate_move_usd_per_second_480p,
                "580p": settings.media_fal_animate_move_usd_per_second_580p,
                "720p": settings.media_fal_animate_move_usd_per_second_720p}[resolution]

    @staticmethod
    def _billable_seconds(parameters: dict, driving_data: bytes | None = None) -> float:
        """DOCUMENTED (fal.ai/models/fal-ai/wan/v2.2-14b/animate/move, read 2026-09-22): billed
        video-seconds = total_frames / 16, kept as an exact fraction — NOT rounded (the model
        page's own worked example keeps it: 300 frames / 16 = 18.75, billed at exactly 18.75).

        total_frames is read directly from the driving video's own container
        (media.runners.base.video_frame_count — the real per-track sample count, not
        duration*assumed-fps) whenever real bytes are available (run() — authoritative, CP46
        confirmed a 5.005s/"24fps" clip is really 119 frames, not the 120 duration*fps would
        suggest). Falls back to the input Artifact's own stored duration at an assumed 24fps only
        when neither real bytes nor a real frame count are known yet (estimate_usd() at creation
        time, before bytes are resolved) — a floor, never presented as the true billing figure,
        the same principle CP43 established for Higgsfield's duration-based formula.
        """
        if driving_data:
            frames = video_frame_count(driving_data)
            if frames:
                return frames / _FAL_ANIMATE_FRAMES_PER_BILLED_SECOND
        input_durations = parameters.get("_input_durations") or {}
        known = input_durations.get("driving_video")
        if known:
            return (float(known) * 24.0) / _FAL_ANIMATE_FRAMES_PER_BILLED_SECOND
        try:
            seconds = float(parameters.get("duration_seconds", 3))
        except (TypeError, ValueError):
            seconds = 3.0
        return (seconds * 24.0) / _FAL_ANIMATE_FRAMES_PER_BILLED_SECOND

    def estimate_usd(self, request: OperationRequest) -> float:
        parameters = request.parameters or {}
        return self._billable_seconds(parameters) * self._price_per_second(self._resolution(parameters))

    async def run(self, request: OperationRequest, resolved: dict[str, ResolvedInput]) -> MediaOutput:
        settings = media_settings()
        if not settings.fal_key:
            raise EngineUnavailable("fal: no key")
        endpoint = settings.media_fal_animate_move_endpoint
        if not endpoint:
            raise EngineUnavailable("fal wan animate: no endpoint configured")
        character, driving = resolved.get("character"), resolved.get("driving_video")
        if character is None or driving is None:
            raise EngineFailed("fal wan animate: missing input", kind=FailureKind.REJECTED, resubmit_safe=False)
        parameters = request.parameters or {}
        link: ProviderLink | None = getattr(request, "link", None)
        resolution = self._resolution(parameters)
        billable_seconds = self._billable_seconds(parameters, driving.data)
        estimate = billable_seconds * self._price_per_second(resolution)
        if request.budget_usd is not None and estimate > request.budget_usd + 1e-9:
            # Real frame count bills for more than what was actually reserved (creation-time
            # estimate under-counted it). Refuse here, before any submit: never spend more than
            # this request's own reservation authorized (CP43's same rule, for a second provider).
            raise EngineFailed(
                f"fal wan animate: real cost ${estimate:.4f} ({billable_seconds:.4f} billed video-s @ "
                f"{resolution}) exceeds the ${request.budget_usd:.4f} reserved for this request",
                kind=FailureKind.REJECTED, retryable=True, resubmit_safe=False)
        body = {
            "image_url": _fal_data_url(character.data, character.content_type),
            "video_url": _fal_data_url(driving.data, driving.content_type),
            "resolution": resolution,
        }
        started = time.monotonic()
        deadline = started + settings.media_video_timeout_s
        async with self._client_factory() as client:
            submitted = _json(await _send(client, "POST", f"https://queue.fal.run/{endpoint}",
                                          "fal wan animate submit", json=body, headers=_fal_headers()),
                              "fal wan animate submit")
            request_id = submitted.get("request_id")
            status_url, response_url = submitted.get("status_url"), submitted.get("response_url")
            cancel_url = submitted.get("cancel_url")
            if not request_id or not status_url or not response_url:
                # fal's server already answered 200 to the submit POST — it may have accepted a
                # billable job even though the response body was missing the fields we need to
                # follow it. Conservatively not retryable: never risk a second paid job on another
                # route (Higgsfield) for what might already be a running fal job we can no longer
                # track (no request_id to check or cancel).
                raise EngineFailed("fal wan animate: no queue handle", retryable=False, may_have_started=True,
                                   resubmit_safe=False)
            handle = ProviderHandle(provider="fal", model=endpoint, route=self.route, request_id=str(request_id),
                                    status_url=status_url, cancel_url=cancel_url, kind="video",
                                    estimate_usd=estimate, width=None, height=None,
                                    duration_seconds=billable_seconds * _FAL_ANIMATE_FRAMES_PER_BILLED_SECOND / 24.0)
            # CP38/CP46: the same ProviderLink every OperationEngine reports through — persists
            # provider/provider_request_id/status_url on the job before any polling.
            if link is not None:
                link.submitted(handle)
            logger.info("fal: submitted | route=%s request=%s", self.route, request_id)
            while True:
                if link is not None and link.cancel_requested:
                    if cancel_url and await self._cancel(client, cancel_url):
                        raise JobCancelled(str(request_id))
                    link.cancel_requested = False  # already running/finished: no longer cancellable
                    logger.info("fal: cancel refused, job already running or finished | request=%s", request_id)
                state = _json(await _send(client, "GET", status_url, "fal wan animate status", headers=_fal_headers()),
                              "fal wan animate status")
                status = str(state.get("status", "")).upper()
                if status == "COMPLETED":
                    break
                if status not in ("IN_QUEUE", "IN_PROGRESS"):
                    # Not retryable: the job was already accepted (we have a real request_id) —
                    # never silently try Higgsfield for a job fal may already be billing.
                    detail = safe_provider_detail(state)
                    raise EngineFailed(f"fal wan animate: status {status or 'unknown'} | request={request_id}",
                                       retryable=False, may_have_started=True, resubmit_safe=False,
                                       provider_detail=detail or None)
                if time.monotonic() > deadline:
                    raise EngineFailed(f"fal wan animate: timeout | request={request_id}",
                                       kind=FailureKind.TIMEOUT, retryable=False, may_have_started=True,
                                       resubmit_safe=False)
                await asyncio.sleep(settings.media_fal_poll_interval_s)
            result = _json(await _send(client, "GET", response_url, "fal wan animate result", headers=_fal_headers()),
                           "fal wan animate result")
            # CP46: fal's own docs say a COMPLETED status can still carry `error`/`error_type` —
            # unlike Higgsfield's explicit failed/nsfw status values, fal signals failure THROUGH a
            # nominally-successful status. Checked here, on the result payload, before trusting it.
            if result.get("error") or result.get("error_type"):
                # fal's documented billing-on-failure policy for this specific model was not found
                # (unlike Higgsfield's explicit "failed and nsfw are not charged") — conservatively
                # not retryable until that is confirmed, rather than risk a second paid job.
                detail = safe_provider_detail(result)
                raise EngineFailed(f"fal wan animate: completed with error | request={request_id}",
                                   retryable=False, may_have_started=True, resubmit_safe=False,
                                   provider_detail=detail or None)
            video = result.get("video") or {}
            if not isinstance(video, dict) or not video.get("url"):
                raise EngineFailed(f"fal wan animate: no output | request={request_id}",
                                   retryable=False, may_have_started=True, resubmit_safe=False)
            data, content_type = await _download(client, video["url"], "fal wan animate download")
        width, height, out_duration = video_metadata(data)
        return MediaOutput(
            data=data, content_type=content_type or "video/mp4", width=width, height=height,
            duration_seconds=out_duration, engine_seconds=time.monotonic() - started, cost_usd=estimate,
            metrics={"provider": "fal", "model": endpoint, "request_id": str(request_id), "route": self.route},
        )


# ── OpenAI Images API ───────────────────────────────────────────────────────

# Output image tokens by quality and size, from the provider's image generation
# guide (2026-09-14). Used only to size the reservation; a finished call is
# priced from the usage it returns.
_OPENAI_OUTPUT_TOKENS = {
    ("low", "1024x1024"): 272, ("low", "1024x1536"): 408, ("low", "1536x1024"): 400,
    ("medium", "1024x1024"): 1056, ("medium", "1024x1536"): 1584, ("medium", "1536x1024"): 1568,
    ("high", "1024x1024"): 4160, ("high", "1024x1536"): 6240, ("high", "1536x1024"): 6208,
}
_OPENAI_QUALITY = {"draft": "low", "standard": "low", "premium": "medium"}
# A 2,000-character prompt (the longest accepted) stays below this many tokens.
_OPENAI_PROMPT_TOKEN_CEILING = 800
# A typical creation request ("Crie uma imagem de um farol ao entardecer, vertical") is well under this.
_OPENAI_TYPICAL_PROMPT_TOKENS = 120


class OpenAIImageEngine(ImageEngine):
    def __init__(self, client_factory: ClientFactory = default_client) -> None:
        self._client_factory = client_factory

    @staticmethod
    def _size(width: int, height: int) -> str:
        if width == height:
            return "1024x1024"
        return "1536x1024" if width > height else "1024x1536"

    @staticmethod
    def _headers() -> dict[str, str]:
        return {"Authorization": f"Bearer {media_settings().openai_api_key}"}

    async def is_available(self, *, editing: bool = False) -> bool:
        settings = media_settings()
        if editing or not settings.openai_api_key:
            return False
        try:
            async with self._client_factory() as client:
                response = await client.get(f"{settings.media_openai_base_url}/models/{settings.media_openai_image_model}",
                                            headers=self._headers(), timeout=15)
            return response.status_code == 200
        except Exception:
            return False

    def estimate_usd(self, request: ImageRequest) -> float:
        settings = media_settings()
        quality = _OPENAI_QUALITY.get(request.quality_tier, "low")
        output_tokens = _OPENAI_OUTPUT_TOKENS[(quality, self._size(request.width, request.height))]
        return (_OPENAI_PROMPT_TOKEN_CEILING * settings.media_openai_text_input_usd_per_mtok
                + output_tokens * settings.media_openai_image_output_usd_per_mtok) / 1_000_000

    def typical_usd(self, request: ImageRequest) -> float:
        settings = media_settings()
        quality = _OPENAI_QUALITY.get(request.quality_tier, "low")
        output_tokens = _OPENAI_OUTPUT_TOKENS[(quality, self._size(request.width, request.height))]
        return (_OPENAI_TYPICAL_PROMPT_TOKENS * settings.media_openai_text_input_usd_per_mtok
                + output_tokens * settings.media_openai_image_output_usd_per_mtok) / 1_000_000

    @staticmethod
    def cost_from_usage(usage: dict) -> float:
        settings = media_settings()
        details = usage.get("input_tokens_details") or {}
        text_tokens = int(details.get("text_tokens", usage.get("input_tokens", 0)) or 0)
        image_tokens = int(details.get("image_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        return (text_tokens * settings.media_openai_text_input_usd_per_mtok
                + image_tokens * settings.media_openai_image_input_usd_per_mtok
                + output_tokens * settings.media_openai_image_output_usd_per_mtok) / 1_000_000

    async def generate(self, request: ImageRequest) -> MediaOutput:
        settings = media_settings()
        if not settings.openai_api_key:
            raise EngineUnavailable("openai: no key")
        size = self._size(request.width, request.height)
        body = {
            "model": settings.media_openai_image_model,
            "prompt": request.prompt,
            "size": size,
            "quality": _OPENAI_QUALITY.get(request.quality_tier, "low"),
            "n": 1,
            "output_format": "jpeg",
            "output_compression": 90,
        }
        started = time.monotonic()
        async with self._client_factory() as client:
            response = await _send(client, "POST", f"{settings.media_openai_base_url}/images/generations", "openai image",
                                   json=body, headers=self._headers(), timeout=settings.media_image_timeout_s)
        payload = _json(response, "openai image")
        try:
            data = base64.b64decode(payload["data"][0]["b64_json"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise EngineFailed("openai image: no output") from exc
        usage = payload.get("usage")
        cost = self.cost_from_usage(usage) if isinstance(usage, dict) and usage else self.estimate_usd(request)
        width, height = (int(side) for side in size.split("x"))
        return MediaOutput(data=data, content_type="image/jpeg", width=width, height=height, duration_seconds=None,
                           engine_seconds=time.monotonic() - started, cost_usd=cost)


# ── Veo on the Gemini API ───────────────────────────────────────────────────

_VEO_DURATIONS = (4, 6, 8)


class VeoVideoEngine(VideoEngine):
    """Text-to-video at 720p, 4/6/8 seconds, audio included. Billed per output second."""

    def __init__(self, client_factory: ClientFactory = default_client) -> None:
        self._client_factory = client_factory

    @staticmethod
    def _headers() -> dict[str, str]:
        return {"x-goog-api-key": media_settings().gemini_api_key}

    async def is_available(self, *, from_image: bool = False) -> bool:
        settings = media_settings()
        if from_image or not settings.gemini_api_key:
            return False
        try:
            async with self._client_factory() as client:
                response = await client.get(f"{settings.media_gemini_base_url}/models/{settings.media_veo_model}",
                                            headers=self._headers(), timeout=15)
            return response.status_code == 200
        except Exception:
            return False

    def normalize(self, request: VideoRequest) -> VideoRequest:
        duration = next((option for option in _VEO_DURATIONS if option >= request.duration_seconds), _VEO_DURATIONS[-1])
        width, height = (720, 1280) if request.height > request.width else (1280, 720)
        return replace(request, width=width, height=height, duration_seconds=duration, fps=24)

    def estimate_usd(self, request: VideoRequest) -> float:
        return media_settings().media_veo_usd_per_second_720p * request.duration_seconds

    async def generate(self, request: VideoRequest) -> MediaOutput:
        settings = media_settings()
        if not settings.gemini_api_key:
            raise EngineUnavailable("veo: no key")
        request = self.normalize(request)
        body = {
            "instances": [{"prompt": request.prompt}],
            "parameters": {
                "aspectRatio": "9:16" if request.height > request.width else "16:9",
                "resolution": "720p",
                "durationSeconds": request.duration_seconds,
                "personGeneration": "allow_all",
            },
        }
        started = time.monotonic()
        deadline = started + settings.media_video_timeout_s
        base = settings.media_gemini_base_url
        async with self._client_factory() as client:
            operation = _json(await _send(client, "POST", f"{base}/models/{settings.media_veo_model}:predictLongRunning",
                                          "veo submit", json=body, headers=self._headers()), "veo submit")
            name = operation.get("name")
            if not name:
                raise EngineFailed("veo: no operation")
            while not operation.get("done"):
                if time.monotonic() > deadline:
                    raise EngineFailed("veo: timeout")
                await asyncio.sleep(settings.media_veo_poll_interval_s)
                operation = _json(await _send(client, "GET", f"{base}/{name}", "veo status", headers=self._headers()),
                                  "veo status")
            if operation.get("error"):
                raise EngineFailed("veo: operation error")
            result = (operation.get("response") or {}).get("generateVideoResponse") or {}
            samples = result.get("generatedSamples") or []
            uri = samples[0].get("video", {}).get("uri") if samples and isinstance(samples[0], dict) else None
            if not uri:
                # Filtered by the provider's safety system: nothing was generated, nothing is billed.
                raise EngineFailed("veo: no output (filtered)" if result.get("raiMediaFilteredCount") else "veo: no output")
            data, content_type = await _download(client, uri, "veo download", headers=self._headers())
        return MediaOutput(data=data, content_type=content_type if content_type.startswith("video/") else "video/mp4",
                           width=request.width, height=request.height, duration_seconds=float(request.duration_seconds),
                           engine_seconds=time.monotonic() - started, cost_usd=self.estimate_usd(request))
