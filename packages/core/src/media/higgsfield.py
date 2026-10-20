"""Higgsfield: one more hosted route in the media orchestrator — never a path of its own.

TRINDATRON asks the orchestrator for an image or a video; the orchestrator may
pick a Higgsfield route (soul_2@higgsfield, kling_3_std@higgsfield, ...) the same
way it picks fal, xAI or a GPU runner: by task, licence, quality floor, health,
price for this exact request and the reserved budget. Nothing here knows about
users, plans or storage, and nothing here reaches a user: not the provider, the
model, a URL, a price or the provider's own error text.

Server-side only. Credentials come from the environment (HIGGSFIELD_API_KEY_ID +
HIGGSFIELD_API_KEY_SECRET, or HIGGSFIELD_API_KEY="id:secret"); without them every
Higgsfield route answers "unavailable" and the chain behaves as before. The
credentials are sent only to the API host, never to an output URL, and never
logged.

API contract (docs.higgsfield.ai / open.higgsfield.ai, read 2026-09-18)
-----------------------------------------------------------------------
  submit   POST {base}/{model path}, flat JSON body
           -> {status: "queued", request_id, status_url, cancel_url}
  status   GET  {base}/requests/{id}/status
           -> {status, request_id, images: [{url}], video: {url}, error}
           statuses: queued, in_progress, completed, failed, nsfw, canceled
  cancel   POST {cancel_url}: 202 while queued, 400 once it has started
  upload   POST {base}/files/generate-upload-url {content_type}
           -> {public_url, upload_url, upload_headers}; then PUT the bytes
  billing  credits, charged only on success; failed and nsfw are not charged,
           a cancelled job is refunded. Responses carry no cost, so a finished
           job is priced at the published price of exactly what was asked
           (MediaSettings, highest published figure, promotions ignored).
  webhook  `?hf_webhook=URL` exists but is unsigned; not used (polling only).

Which calls may be repeated
---------------------------
  submit    NEVER repeated. No idempotency key exists: a second POST is a second
            paid job. A request that may have reached the provider without an
            answer (read timeout, dropped connection, 502/504, an answer without a
            request id) is failed with `may_have_started` — no retry, no fallback.
            A connection that was never opened is safe to hand to another route.
  status    repeatable (read-only); transient errors are tolerated up to a limit.
  cancel    repeatable; a second cancel is answered 400 and changes nothing.
  upload    repeatable (a new upload URL each time, nothing billed).
  download  repeatable (outputs are kept at least 7 days).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit

import httpx

from src.media import plans
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
from src.media.hosted import ClientFactory, default_client
from src.media.operations import OperationEngine, OperationRequest, ResolvedInput
from src.media.runners.base import assert_public_https_url, image_size, retry_after_seconds, video_metadata
from src.media.settings import media_settings

logger = logging.getLogger("trinity.media.higgsfield")

PROVIDER = "higgsfield"
QUEUED, IN_PROGRESS, COMPLETED, FAILED, NSFW, CANCELED = "queued", "in_progress", "completed", "failed", "nsfw", "canceled"
PENDING = frozenset({QUEUED, IN_PROGRESS})
TERMINAL = frozenset({COMPLETED, FAILED, NSFW, CANCELED})
UPLOAD_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
# CP35: Genjutsu Motion Transfer (media/operations.py character_animate) needs its driving video
# uploaded through this same pre-signed-URL mechanism. Higgsfield's own docs never restrict
# /files/generate-upload-url to images — the image-only set above just predates any video-input
# model in this catalog.
UPLOAD_VIDEO_TYPES = frozenset({"video/mp4", "video/quicktime", "video/webm"})
_DOWNLOAD_ATTEMPTS = 3
# Two shapes count as the same ratio when their log-ratios are this close (1024x768 is 4:3; 3:4 is not).
_RATIO_TOLERANCE = 0.06

# Soul 2 request schema (docs.higgsfield.ai/docs/models/soul-2/generate.md, read 2026-09-18).
# enhance_prompt is not sent: the provider's default (true) applies and rewrites the prompt with the style.
SOUL_2_ASPECT_RATIOS = ("9:16", "16:9", "4:3", "3:4", "1:1", "2:3", "3:2")
SOUL_2_RESOLUTIONS = ("720p", "1080p")
SOUL_2_BATCH_SIZES = (1, 4)
SOUL_2_SEED_RANGE = (1, 1_000_000)


# ── Credentials ──────────────────────────────────────────────────────────────

def credentials() -> tuple[str, str] | None:
    """(key id, key secret) from the environment, or None. The values never leave this module except in a header."""
    settings = media_settings()
    key_id, secret = settings.higgsfield_api_key_id.strip(), settings.higgsfield_api_key_secret.strip()
    if not (key_id and secret):
        combined = settings.higgsfield_api_key.strip()
        key_id, _, secret = combined.partition(":")
        key_id, secret = key_id.strip(), secret.strip()
    return (key_id, secret) if key_id and secret else None


def configured() -> bool:
    return credentials() is not None


# ── Client ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Submitted:
    request_id: str
    status_url: str | None
    cancel_url: str | None


def _classify(status: int, text: str) -> tuple[str, bool]:
    """(FailureKind, may_have_started) for an HTTP error answer."""
    lowered = text.lower()
    if status == 400 and "concurrent" in lowered:
        return FailureKind.RATE_LIMITED, False     # "Maximum number of concurrent requests (4)"
    if status in (400, 422):
        return FailureKind.UNSUPPORTED_FORMAT, False  # validation: this model will not take this request
    if status in (401, 404):
        return FailureKind.UNAVAILABLE, False      # bad key, model path gone
    if status in (402, 403):
        return FailureKind.QUOTA, False            # insufficient credits
    if status == 423:
        return FailureKind.UNAVAILABLE, False      # account blocked
    if status == 429:
        return FailureKind.RATE_LIMITED, False
    if status in (502, 504):
        return FailureKind.TEMPORARY, True         # a gateway may have forwarded the job
    if status == 503:
        return FailureKind.UNAVAILABLE, False
    if status >= 500:
        return FailureKind.TEMPORARY, False
    return FailureKind.REJECTED, False


class HiggsfieldClient:
    """Thin async client over one httpx.AsyncClient. Errors are EngineFailed with status codes only."""

    def __init__(self, client: httpx.AsyncClient, *, base_url: str | None = None) -> None:
        self._client = client
        self._base = (base_url or media_settings().media_higgsfield_base_url).rstrip("/")
        self._host = urlsplit(self._base).netloc

    def _auth(self) -> dict[str, str]:
        keys = credentials()
        if keys is None:
            raise EngineUnavailable("higgsfield: no credentials")
        return {"Authorization": f"Key {keys[0]}:{keys[1]}"}

    def _api_url(self, url: str | None, fallback: str) -> str:
        """A URL the provider handed back, used only if it points at the API host (credentials go nowhere else)."""
        if url and urlsplit(url).netloc == self._host and urlsplit(url).scheme == "https":
            return url
        return fallback

    async def _request(self, method: str, url: str, what: str, *, safe_to_repeat: bool, **kwargs) -> httpx.Response:
        try:
            response = await self._client.request(method, url, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # The connection never opened: the provider cannot have seen the request.
            raise EngineFailed(f"{what}: connection", kind=FailureKind.TEMPORARY, resubmit_safe=False) from exc
        except httpx.TimeoutException as exc:
            raise EngineFailed(f"{what}: timeout", kind=FailureKind.TIMEOUT, retryable=safe_to_repeat,
                               may_have_started=not safe_to_repeat) from exc
        except httpx.HTTPError as exc:
            raise EngineFailed(f"{what}: connection lost", kind=FailureKind.TEMPORARY, retryable=safe_to_repeat,
                               may_have_started=not safe_to_repeat) from exc
        if response.status_code >= 400:
            kind, started = _classify(response.status_code, response.text[:300])
            started = started and not safe_to_repeat
            raise EngineFailed(f"{what}: HTTP {response.status_code}", kind=kind,
                               retryable=False if started else None, may_have_started=started,
                               retry_after=retry_after_seconds(response.headers.get("retry-after")),
                               resubmit_safe=False)
        return response

    @staticmethod
    def _payload(response: httpx.Response, what: str, *, started: bool) -> dict:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise EngineFailed(f"{what}: invalid response", kind=FailureKind.TEMPORARY, retryable=not started,
                               may_have_started=started, resubmit_safe=False)
        return payload

    async def submit(self, model_path: str, body: dict) -> Submitted:
        """Queue one job. Never repeated by this module (see the module docstring)."""
        response = await self._request("POST", f"{self._base}/{model_path.strip('/')}", "higgsfield submit",
                                       safe_to_repeat=False, json=body, headers=self._auth())
        payload = self._payload(response, "higgsfield submit", started=True)
        request_id = str(payload.get("request_id") or "").strip()
        if not request_id:
            raise EngineFailed("higgsfield submit: no request id", kind=FailureKind.TEMPORARY, retryable=False,
                               may_have_started=True, resubmit_safe=False)
        return Submitted(request_id=request_id, status_url=payload.get("status_url"),
                         cancel_url=payload.get("cancel_url"))

    async def status(self, request_id: str, status_url: str | None = None) -> dict:
        url = self._api_url(status_url, f"{self._base}/requests/{request_id}/status")
        response = await self._request("GET", url, "higgsfield status", safe_to_repeat=True, headers=self._auth())
        return self._payload(response, "higgsfield status", started=False)

    async def cancel(self, request_id: str, cancel_url: str | None = None) -> bool:
        """True when the provider accepted the cancel (the job had not started); False when it can no longer be cancelled."""
        url = self._api_url(cancel_url, f"{self._base}/requests/{request_id}/cancel")
        try:
            response = await self._client.request("POST", url, headers=self._auth())
        except httpx.HTTPError:
            return False
        return response.status_code in (200, 202)

    async def upload(self, data: bytes, content_type: str) -> str:
        """Put an input image where a model can read it; returns its public URL (valid about an hour)."""
        content_type = (content_type or "").split(";")[0].strip().lower()
        if content_type not in UPLOAD_TYPES and content_type not in UPLOAD_VIDEO_TYPES:
            raise EngineFailed("higgsfield upload: unsupported type", kind=FailureKind.UNSUPPORTED_FORMAT,
                               resubmit_safe=False)
        response = await self._request("POST", f"{self._base}/files/generate-upload-url", "higgsfield upload url",
                                       safe_to_repeat=True, json={"content_type": content_type}, headers=self._auth())
        payload = self._payload(response, "higgsfield upload url", started=False)
        upload_url, public_url = payload.get("upload_url"), payload.get("public_url")
        if not upload_url or not public_url:
            raise EngineFailed("higgsfield upload url: incomplete", kind=FailureKind.TEMPORARY, resubmit_safe=False)
        headers = payload.get("upload_headers") if isinstance(payload.get("upload_headers"), dict) else {}
        # The upload URL is pre-signed storage: no credentials go with it.
        await self._request("PUT", str(upload_url), "higgsfield upload", safe_to_repeat=True, content=data,
                            headers={"Content-Type": content_type, **{str(k): str(v) for k, v in headers.items()}})
        return str(public_url)

    async def download(self, url: str) -> tuple[bytes, str]:
        """The output file. No credentials are sent to the output host.

        CP18: the output host is not the API host (it is Higgsfield's own CDN, which varies), so
        this cannot be pinned to one known host the way status/cancel URLs are (_api_url). It is
        still checked before fetching: https only, and not a literal private/internal address.
        """
        url = assert_public_https_url(url, "higgsfield download")
        last: EngineFailed | None = None
        for _ in range(_DOWNLOAD_ATTEMPTS):
            try:
                response = await self._request("GET", url, "higgsfield download", safe_to_repeat=True)
            except EngineFailed as exc:
                last = exc
                continue
            if response.content:
                return response.content, response.headers.get("content-type", "").split(";")[0].strip().lower()
            last = EngineFailed("higgsfield download: empty", kind=FailureKind.TEMPORARY)
        # The job completed and was billed: the output is kept for days, so nothing is resubmitted.
        raise EngineFailed(f"{last.reason if last else 'higgsfield download'}", kind=FailureKind.TEMPORARY,
                           retryable=False, may_have_started=True, resubmit_safe=False)

    async def wait(self, handle: ProviderHandle, *, deadline: float, link: ProviderLink | None = None,
                   sleep=asyncio.sleep, clock=time.monotonic) -> dict:
        """Follow a queued job until it completes, within `deadline` (a `clock()` value).

        completed        -> the final status payload
        failed           -> EngineFailed TEMPORARY (not billed: another route may try)
        nsfw             -> EngineFailed FILTERED (not billed: the chain stops)
        canceled         -> JobCancelled (refunded)
        deadline passed  -> cancel if still queued (refunded, another route may try);
                            otherwise EngineFailed with may_have_started (billed or not, never resubmitted)
        """
        settings = media_settings()
        interval = max(0.0, settings.media_higgsfield_poll_interval_s)
        errors = 0
        while True:
            if link is not None and link.cancel_requested:
                if await self.cancel(handle.request_id, handle.cancel_url):
                    raise JobCancelled(handle.request_id)
                link.cancel_requested = False  # already running: it can no longer be cancelled
                logger.info("higgsfield: cancel refused, job already running | request=%s", handle.request_id)
            try:
                state = await self.status(handle.request_id, handle.status_url)
                status = str(state.get("status") or "").lower()
            except EngineFailed as exc:
                if exc.kind in (FailureKind.UNAVAILABLE, FailureKind.QUOTA):
                    errors = settings.media_higgsfield_poll_error_limit  # the key stopped working: stop reading
                state, status = {}, ""
                errors += 1
                logger.info("higgsfield: status read failed | request=%s reason=%s", handle.request_id, exc.reason)
            else:
                if status not in PENDING and status not in TERMINAL:
                    errors += 1
                else:
                    errors = 0
            if status == COMPLETED:
                return state
            if status == FAILED:
                detail = safe_provider_detail(state)
                reason = f"higgsfield: job failed | request={handle.request_id}"
                if detail:
                    reason += f" | {json.dumps(detail, ensure_ascii=False, default=str)}"
                logger.warning("higgsfield: job failed | request=%s detail=%s", handle.request_id, detail)
                raise EngineFailed(reason, kind=FailureKind.TEMPORARY, resubmit_safe=False,
                                   provider_detail=detail or None)
            if status == NSFW:
                detail = safe_provider_detail(state)
                raise EngineFailed(f"higgsfield: nsfw | request={handle.request_id}", kind=FailureKind.FILTERED,
                                   resubmit_safe=False, provider_detail=detail or None)
            if status == CANCELED:
                raise JobCancelled(handle.request_id)
            if errors >= max(1, settings.media_higgsfield_poll_error_limit) or clock() > deadline:
                lost = errors >= max(1, settings.media_higgsfield_poll_error_limit)
                if await self.cancel(handle.request_id, handle.cancel_url):
                    raise EngineFailed("higgsfield: cancelled after " + ("lost status" if lost else "deadline") +
                                       f" | request={handle.request_id}", kind=FailureKind.TIMEOUT,
                                       resubmit_safe=False)
                raise EngineFailed("higgsfield: " + ("status lost" if lost else "deadline passed while running") +
                                   f" | request={handle.request_id}", kind=FailureKind.TIMEOUT, retryable=False,
                                   may_have_started=True,
                                   resubmit_safe=False)
            await sleep(interval)
            interval = min(max(interval, settings.media_higgsfield_poll_max_interval_s), interval * 1.5 or 0.0) \
                if interval else 0.0


# ── Catalog ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class HiggsfieldModel:
    """One Higgsfield model as a route sees it. Built from MediaSettings, so a model or a price is configuration."""

    route: str
    engine_id: str
    kind: str                                   # "image" | "video"
    text_path: str = ""                         # model path without an input image ("" = not offered)
    image_path: str = ""                        # model path with an input image ("" = not offered)
    image_field: str = ""                       # body field carrying the image URL ("" = unverified: not offered)
    aspect_ratios: tuple[str, ...] = ()         # verified values only
    resolutions: tuple[str, ...] = ()           # sent as "resolution"; () = the model has no such field
    usd: dict[str, float] = field(default_factory=dict)  # per image (image) or per second (video), by resolution; "*" = any
    min_seconds: int = 1
    max_seconds: int = 1
    fixed: dict = field(default_factory=dict)   # documented fields sent with a fixed value
    # The provider's own request schema, where documented. Every body is checked against it before sending.
    documented_ratios: tuple[str, ...] = ()     # () = no documented list
    batch_sizes: tuple[int, ...] = ()           # () = the model has no batch_size field
    seed_range: tuple[int, int] | None = None   # None = no seed is sent

    def does_text(self) -> bool:
        return bool(self.text_path and self.aspect_ratios)

    def does_image(self) -> bool:
        return bool(self.image_path and self.image_field and self.aspect_ratios)

    def price(self, resolution: str | None) -> float | None:
        value = self.usd.get(resolution or "*", self.usd.get("*"))
        return value if value is not None and value > 0 else None


def _ratios(value: str) -> tuple[str, ...]:
    out = []
    for part in (value or "").split(","):
        part = part.strip()
        try:
            w, h = (float(x) for x in part.split(":"))
        except ValueError:
            continue
        if w > 0 and h > 0 and part not in out:
            out.append(part)
    return tuple(out)


def catalog() -> dict[str, HiggsfieldModel]:
    s = media_settings()
    models = (
        HiggsfieldModel("soul_2@higgsfield", "higgsfield_soul_2", "image", text_path=s.media_higgsfield_soul_model,
                        aspect_ratios=_ratios(s.media_higgsfield_soul_aspect_ratios), resolutions=SOUL_2_RESOLUTIONS,
                        usd={"720p": s.media_higgsfield_soul_usd_720p, "1080p": s.media_higgsfield_soul_usd_1080p},
                        fixed={"batch_size": 1}, documented_ratios=SOUL_2_ASPECT_RATIOS,
                        batch_sizes=SOUL_2_BATCH_SIZES, seed_range=SOUL_2_SEED_RANGE),
        HiggsfieldModel("image_edit@higgsfield", "higgsfield_image_edit", "image",
                        image_path=s.media_higgsfield_edit_model, image_field=s.media_higgsfield_edit_image_field,
                        aspect_ratios=_ratios(s.media_higgsfield_edit_aspect_ratios),
                        usd={"*": s.media_higgsfield_edit_usd_per_image}),
        HiggsfieldModel("wan_3@higgsfield", "higgsfield_wan_3", "video", text_path=s.media_higgsfield_wan_model,
                        image_path=s.media_higgsfield_wan_i2v_model, image_field=s.media_higgsfield_wan_image_field,
                        aspect_ratios=_ratios(s.media_higgsfield_wan_aspect_ratios), resolutions=("480p", "720p"),
                        usd={"480p": s.media_higgsfield_wan_usd_per_second_480p,
                             "720p": s.media_higgsfield_wan_usd_per_second_720p},
                        min_seconds=2, max_seconds=30, fixed={"generate_audio": False}),
        HiggsfieldModel("kling_3_std@higgsfield", "higgsfield_kling_3_std", "video",
                        text_path=s.media_higgsfield_kling_model, image_path=s.media_higgsfield_kling_i2v_model,
                        image_field=s.media_higgsfield_kling_image_field,
                        aspect_ratios=_ratios(s.media_higgsfield_kling_aspect_ratios),
                        usd={"*": s.media_higgsfield_kling_usd_per_second}, min_seconds=3, max_seconds=15,
                        fixed={"sound": "off"}),
        HiggsfieldModel("grok_imagine_video_1_5@higgsfield", "higgsfield_grok_imagine_video_1_5", "video",
                        text_path=s.media_higgsfield_grok_video_model, image_path=s.media_higgsfield_grok_video_model,
                        image_field=s.media_higgsfield_grok_video_image_field,
                        aspect_ratios=_ratios(s.media_higgsfield_grok_video_aspect_ratios),
                        resolutions=("480p", "720p"),
                        usd={"480p": s.media_higgsfield_grok_video_usd_per_second_480p,
                             "720p": s.media_higgsfield_grok_video_usd_per_second_720p},
                        min_seconds=1, max_seconds=15),
    )
    return {model.route: model for model in models}


def model_for(route: str) -> HiggsfieldModel:
    return catalog()[route]


def matching_ratio(width: int, height: int, ratios: tuple[str, ...]) -> str | None:
    """The verified ratio this shape is, or None: a shape is never bent to fit a model."""
    wanted = math.log(max(1, width) / max(1, height))
    best, distance = None, math.inf
    for label in ratios:
        w, h = (float(part) for part in label.split(":"))
        gap = abs(math.log(w / h) - wanted)
        if gap < distance:
            best, distance = label, gap
    return best if distance <= _RATIO_TOLERANCE else None


def provider_seed(model: HiggsfieldModel, seed: int) -> int | None:
    """The seed this model is sent for a Core seed, or None when the model takes no seed.

    Core seeds are drawn from 0..2**31 for every engine (media_service._seed) and are never chosen by a
    person. A model with a narrower documented range gets the same seed folded into that range — one
    deterministic value per Core seed, so a job stays reproducible — and the value actually sent is
    recorded with the job (metrics["seed"]).
    """
    if model.seed_range is None:
        return None
    low, high = model.seed_range
    if low <= seed <= high:
        return seed
    return low + seed % (high - low + 1)


def validate_body(model: HiggsfieldModel, body: dict) -> None:
    """Refuse — before anything is sent, loudly — a body outside the model's documented schema.

    Normalisation happens before this (provider_seed, matching_ratio); anything still out of range here
    is a bug or a misconfiguration and must not reach the provider as a paid request or a 422.
    """
    problems = []
    if model.documented_ratios and body.get("aspect_ratio") not in model.documented_ratios:
        problems.append("aspect_ratio")
    if model.resolutions and "resolution" in body and body["resolution"] not in model.resolutions:
        problems.append("resolution")
    if model.batch_sizes and body.get("batch_size", 1) not in model.batch_sizes:
        problems.append("batch_size")
    if "seed" in body:
        seed = body["seed"]
        if (model.seed_range is None or isinstance(seed, bool) or not isinstance(seed, int)
                or not model.seed_range[0] <= seed <= model.seed_range[1]):
            problems.append("seed")
    if problems:
        logger.error("higgsfield: request outside the documented schema | route=%s fields=%s", model.route, problems)
        raise EngineFailed(f"higgsfield {model.route}: invalid {', '.join(problems)}",
                           kind=FailureKind.UNSUPPORTED_FORMAT, resubmit_safe=False)


def _image_resolution(model: HiggsfieldModel, width: int, height: int) -> str | None:
    if not model.resolutions:
        return None
    return "1080p" if "1080p" in model.resolutions and min(width, height) > 900 else model.resolutions[0]


def _video_resolution(model: HiggsfieldModel, width: int, height: int) -> str | None:
    if not model.resolutions:
        return None
    return "720p" if "720p" in model.resolutions and min(width, height) > 480 else model.resolutions[0]


def quote(usd: float | None) -> dict:
    """An estimate in both units the product accounts in (TCU = USD x 100)."""
    return {"usd": usd, "tcu": plans.usd_to_tcu(usd) if usd is not None else None}


# ── Engines ──────────────────────────────────────────────────────────────────

class _Base:
    def __init__(self, route: str, client_factory: ClientFactory = default_client) -> None:
        self.route = route
        self._client_factory = client_factory

    @property
    def model(self) -> HiggsfieldModel:
        return model_for(self.route)

    def _deadline(self, kind: str) -> float:
        settings = media_settings()
        return time.monotonic() + (settings.media_image_timeout_s if kind == "image" else settings.media_video_timeout_s)

    async def _execute(self, *, kind: str, path: str, body: dict, request, estimate: float | None,
                       width: int | None, height: int | None,
                       duration: float | None) -> tuple[bytes, str, float, ProviderHandle]:
        """Upload the source (if any), submit once, record the handle, wait, download."""
        if credentials() is None:
            raise EngineUnavailable("higgsfield: no credentials")
        model = self.model
        link: ProviderLink | None = getattr(request, "link", None)
        started = time.monotonic()
        async with self._client_factory() as http:
            client = HiggsfieldClient(http)
            if request.source is not None:
                body = {**body, model.image_field: await client.upload(request.source, request.source_content_type or "")}
            submitted = await client.submit(path, body)
            handle = ProviderHandle(provider=PROVIDER, model=path, route=self.route, request_id=submitted.request_id,
                                    status_url=submitted.status_url, cancel_url=submitted.cancel_url, kind=kind,
                                    estimate_usd=estimate, width=width, height=height, duration_seconds=duration)
            if link is not None:
                link.submitted(handle)
            logger.info("higgsfield: submitted | route=%s request=%s", self.route, handle.request_id)
            state = await client.wait(handle, deadline=self._deadline(kind), link=link)
            data, content_type = await client.download(output_url(state, kind))
        return data, content_type, time.monotonic() - started, handle


def _metrics(handle: ProviderHandle, **extra) -> dict:
    """Internal record of the provider job behind an output (job params, logs). Never shown to a user."""
    return {"provider": PROVIDER, "model": handle.model, "request_id": handle.request_id, **extra}


def output_url(state: dict, kind: str) -> str:
    """The output URL of a completed job. A completed job without one was billed: never resubmitted."""
    url = None
    if kind == "image":
        images = state.get("images")
        if isinstance(images, list) and images and isinstance(images[0], dict):
            url = images[0].get("url")
    else:
        video = state.get("video")
        if isinstance(video, dict):
            url = video.get("url")
    if not url or not str(url).startswith("https://"):
        raise EngineFailed("higgsfield: completed without output", kind=FailureKind.TEMPORARY, retryable=False,
                           may_have_started=True, resubmit_safe=False)
    return str(url)


class HiggsfieldImageEngine(_Base, ImageEngine):
    """Text-to-image (and image editing when a verified edit model is configured)."""

    def _supports(self, editing: bool) -> bool:
        return self.model.does_image() if editing else self.model.does_text()

    async def is_available(self, *, editing: bool = False) -> bool:
        # Higgsfield has no free probe endpoint: configuration decides, the first job proves reachability.
        return configured() and self._supports(editing)

    def estimate_usd(self, request: ImageRequest) -> float | None:
        model = self.model
        return model.price(_image_resolution(model, request.width, request.height))

    def body(self, request: ImageRequest) -> tuple[str, dict]:
        """(model path, request body) for this request. Refuses — before anything is sent — what is not verified."""
        model = self.model
        editing = request.source is not None
        if not self._supports(editing):
            raise EngineUnavailable(f"higgsfield {self.route}: {'edit' if editing else 'text'} not offered")
        ratio = matching_ratio(request.width, request.height, model.aspect_ratios)
        if ratio is None:
            raise EngineFailed(f"higgsfield {self.route}: shape not verified", kind=FailureKind.UNSUPPORTED_FORMAT,
                               resubmit_safe=False)
        body: dict = {"prompt": request.prompt, "aspect_ratio": ratio, **model.fixed}
        resolution = _image_resolution(model, request.width, request.height)
        if resolution:
            body["resolution"] = resolution
        seed = provider_seed(model, request.seed) if not editing else None
        if seed is not None:
            body["seed"] = seed
        validate_body(model, body)
        return (model.image_path if editing else model.text_path), body

    async def generate(self, request: ImageRequest) -> MediaOutput:
        path, body = self.body(request)
        estimate = self.estimate_usd(request)
        if estimate is None:
            raise EngineUnavailable(f"higgsfield {self.route}: no price configured")
        data, content_type, seconds, handle = await self._execute(kind="image", path=path, body=body, request=request,
                                                          estimate=estimate, width=request.width,
                                                          height=request.height, duration=None)
        width, height = image_size(data)
        if not content_type.startswith("image/"):
            content_type = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        return MediaOutput(data=data, content_type=content_type, width=width, height=height, duration_seconds=None,
                           engine_seconds=seconds, cost_usd=estimate,
                           metrics=_metrics(handle, **({"seed": body["seed"]} if "seed" in body else {})))


class HiggsfieldVideoEngine(_Base, VideoEngine):
    """Text-to-video (and image-to-video when the model's image field is verified)."""

    def _supports(self, from_image: bool) -> bool:
        return self.model.does_image() if from_image else self.model.does_text()

    async def is_available(self, *, from_image: bool = False) -> bool:
        return configured() and self._supports(from_image)

    def normalize(self, request: VideoRequest) -> VideoRequest:
        model = self.model
        duration = max(model.min_seconds, min(model.max_seconds, request.duration_seconds))
        return replace(request, duration_seconds=duration, fps=24)

    def estimate_usd(self, request: VideoRequest) -> float | None:
        model = self.model
        request = self.normalize(request)
        price = model.price(_video_resolution(model, request.width, request.height))
        return None if price is None else price * request.duration_seconds

    def body(self, request: VideoRequest) -> tuple[str, dict]:
        model = self.model
        request = self.normalize(request)
        from_image = request.source is not None
        if not self._supports(from_image):
            raise EngineUnavailable(f"higgsfield {self.route}: {'image' if from_image else 'text'} to video not offered")
        ratio = matching_ratio(request.width, request.height, model.aspect_ratios)
        if ratio is None:
            raise EngineFailed(f"higgsfield {self.route}: shape not verified", kind=FailureKind.UNSUPPORTED_FORMAT,
                               resubmit_safe=False)
        body: dict = {"prompt": request.prompt, "duration": request.duration_seconds, "aspect_ratio": ratio,
                      **model.fixed}
        resolution = _video_resolution(model, request.width, request.height)
        if resolution:
            body["resolution"] = resolution
        validate_body(model, body)
        return (model.image_path if from_image else model.text_path), body

    async def generate(self, request: VideoRequest) -> MediaOutput:
        request = self.normalize(request)
        path, body = self.body(request)
        estimate = self.estimate_usd(request)
        if estimate is None:
            raise EngineUnavailable(f"higgsfield {self.route}: no price configured")
        data, content_type, seconds, handle = await self._execute(kind="video", path=path, body=body, request=request,
                                                          estimate=estimate, width=request.width,
                                                          height=request.height,
                                                          duration=float(request.duration_seconds))
        width, height, duration = video_metadata(data)
        return MediaOutput(data=data, content_type=content_type if content_type.startswith("video/") else "video/mp4",
                           width=width or request.width, height=height or request.height,
                           duration_seconds=duration or float(request.duration_seconds), engine_seconds=seconds,
                           cost_usd=estimate, metrics=_metrics(handle))


class HiggsfieldGenjutsuEngine(OperationEngine):
    """higgsfiled/genjutsu/motion-transfer/v1.0: character image + driving video -> animated video
    (media/operations.py character_animate's `character` and `driving_video` roles).

    Two typed inputs instead of GENERATE's single optional source, so this does not fit
    _Base._execute()/HiggsfieldVideoEngine's one-upload contract — it talks to HiggsfieldClient
    directly. Auth, submit/wait/download, and the never-resubmit rule are the exact same
    HiggsfieldClient every other Higgsfield route uses; only the two-upload body-building is new.
    """

    operation = "character_animate"
    route = "genjutsu_motion_transfer@higgsfield"

    def __init__(self, client_factory: ClientFactory = default_client) -> None:
        self._client_factory = client_factory

    async def is_available(self) -> bool:
        return configured() and bool(media_settings().media_higgsfield_genjutsu_motion_transfer_model)

    @staticmethod
    def _resolution(parameters: dict) -> str:
        resolution = str(parameters.get("resolution") or "480p").lower()
        return resolution if resolution in ("480p", "720p") else "480p"

    @staticmethod
    def _billable_duration(parameters: dict, driving_data: bytes | None = None) -> int:
        """Higgsfield bills per second of the DRIVING VIDEO's own real length, rounded up —
        DOCUMENTED (open.higgsfield.ai Genjutsu model page, read 2026-09-21: "Each second of input
        video costs $X ... Input duration is rounded up to the nearest whole second."). Never a
        caller-supplied `duration_seconds` — no `duration` field exists in Genjutsu's documented
        request body, so that number is never actually sent to the provider at all (CP39/CP42
        evidence: a 3s-requested job produced a ~4.7s output from a 4.963s input; TRINDATRON's old
        estimate silently priced the job at 3s while the provider billed nearer 5s).

        `driving_data` (real bytes, available in run() — OBSERVED, authoritative) wins when given.
        Otherwise `parameters["_input_durations"]["driving_video"]` (the input Artifact's own
        stored duration, populated at upload time — media_service.store_source_media(); still
        OBSERVED, just read earlier and once, so estimate_usd() stays synchronous and DB-free like
        every other engine's). `duration_seconds` (the caller's own request) is kept only as a last
        resort floor when neither is known yet — never trusted as the true billing figure."""
        if driving_data:
            try:
                _, _, real_seconds = video_metadata(driving_data)
            except Exception:  # noqa: BLE001
                real_seconds = None
            if real_seconds:
                return max(1, min(30, math.ceil(real_seconds)))
        known = (parameters.get("_input_durations") or {}).get("driving_video")
        if known:
            return max(1, min(30, math.ceil(float(known))))
        try:
            seconds = int(parameters.get("duration_seconds", 3))
        except (TypeError, ValueError):
            seconds = 3
        return max(1, min(30, seconds))

    def _cost_for(self, resolution: str, seconds: int) -> float | None:
        settings = media_settings()
        per_second = (settings.media_higgsfield_genjutsu_usd_per_second_480p if resolution == "480p"
                     else settings.media_higgsfield_genjutsu_usd_per_second_720p)
        return per_second * seconds if per_second > 0 else None

    def estimate_usd(self, request: OperationRequest) -> float | None:
        parameters = request.parameters or {}
        return self._cost_for(self._resolution(parameters), self._billable_duration(parameters))

    def _deadline(self) -> float:
        settings = media_settings()
        # CP41: services/media_service._generate() now sizes the outer wait_for for this operation
        # (job.kind == "video") at media_video_timeout_s + 30 — the same fix that replaced the old
        # flat media_image_timeout_s + 30 CP40 proved too short for a real, healthy Genjutsu job.
        # This internal deadline stays 5s under that outer bound so a real timeout is this engine's
        # own EngineFailed(TIMEOUT) — which attempts a cancel first — not a bare asyncio.TimeoutError.
        return time.monotonic() + settings.media_video_timeout_s + 25

    async def run(self, request: OperationRequest, resolved: dict[str, ResolvedInput]) -> MediaOutput:
        if credentials() is None:
            raise EngineUnavailable("higgsfield: no credentials")
        model_path = media_settings().media_higgsfield_genjutsu_motion_transfer_model
        if not model_path:
            raise EngineUnavailable("higgsfield genjutsu: no model path configured")
        character, driving = resolved.get("character"), resolved.get("driving_video")
        if character is None or driving is None:
            raise EngineFailed("higgsfield genjutsu: missing input", kind=FailureKind.REJECTED, resubmit_safe=False)
        parameters = request.parameters or {}
        link: ProviderLink | None = getattr(request, "link", None)
        resolution = self._resolution(parameters)
        # CP43: the real driving-video bytes are already local — measure the actual billable
        # duration from them (authoritative, video_metadata()) rather than trust whatever was
        # estimated at job-creation time from a possibly-stale or missing Artifact.duration_seconds.
        duration = self._billable_duration(parameters, driving.data)
        estimate = self._cost_for(resolution, duration)
        if request.budget_usd is not None and estimate is not None and estimate > request.budget_usd + 1e-9:
            # The real driving video bills for more than what was actually reserved (job creation's
            # own estimate under-counted it — e.g. an older Artifact row with no stored duration).
            # Refuse here, before any upload or submit: never let a request spend more than its own
            # reservation authorized, and never silently under-bill by proceeding anyway.
            raise EngineFailed(
                f"higgsfield genjutsu: real cost ${estimate:.3f} ({duration}s @ {resolution}) exceeds the "
                f"${request.budget_usd:.3f} reserved for this request", kind=FailureKind.REJECTED,
                resubmit_safe=False)
        body = {"prompt": str(parameters.get("prompt") or ""), "resolution": resolution}
        started = time.monotonic()
        async with self._client_factory() as http:
            client = HiggsfieldClient(http)
            body["image_urls"] = [await client.upload(character.data, character.content_type)]
            body["video_url"] = await client.upload(driving.data, driving.content_type)
            submitted = await client.submit(model_path, body)
            handle = ProviderHandle(provider=PROVIDER, model=model_path, route=self.route,
                                    request_id=submitted.request_id, status_url=submitted.status_url,
                                    cancel_url=submitted.cancel_url, kind="video", estimate_usd=estimate,
                                    width=None, height=None, duration_seconds=float(duration))
            # CP38: the same ProviderLink GENERATE-path engines already use (_Base._execute() above) —
            # persists provider/provider_request_id/status_url on the job the instant they exist,
            # before any polling, via media_service._record_provider(). Previously OperationRequest
            # had no `link` field at all, so this call site had nothing to report to.
            if link is not None:
                link.submitted(handle)
            logger.info("higgsfield: submitted | route=%s request=%s", self.route, handle.request_id)
            state = await client.wait(handle, deadline=self._deadline(), link=link)
            data, content_type = await client.download(output_url(state, "video"))
        width, height, out_duration = video_metadata(data)
        return MediaOutput(
            data=data, content_type=content_type if content_type.startswith("video/") else "video/mp4",
            width=width, height=height, duration_seconds=out_duration or float(duration),
            engine_seconds=time.monotonic() - started, cost_usd=estimate, metrics=_metrics(handle),
        )


# ── After a restart, and on a person's cancel ────────────────────────────────

async def resume(handle: ProviderHandle, *, client_factory: ClientFactory | None = None,
                 link: ProviderLink | None = None) -> MediaOutput:
    """Follow a job submitted before a restart to its end — never submitting it again."""
    client_factory = client_factory or default_client
    settings = media_settings()
    started = time.monotonic()
    limit = settings.media_image_timeout_s if handle.kind == "image" else settings.media_video_timeout_s
    async with client_factory() as http:
        client = HiggsfieldClient(http)
        state = await client.wait(handle, deadline=started + limit, link=link)
        data, content_type = await client.download(output_url(state, handle.kind))
    if handle.kind == "image":
        width, height = image_size(data)
        duration = None
        content_type = content_type if content_type.startswith("image/") else "image/jpeg"
    else:
        width, height, duration = video_metadata(data)
        duration = duration or handle.duration_seconds
        content_type = content_type if content_type.startswith("video/") else "video/mp4"
    return MediaOutput(data=data, content_type=content_type, width=width or handle.width,
                       height=height or handle.height, duration_seconds=duration,
                       engine_seconds=time.monotonic() - started, cost_usd=handle.estimate_usd,
                       metrics=_metrics(handle, resumed=True, route=handle.route))


async def cancel(handle: ProviderHandle, *, client_factory: ClientFactory | None = None) -> bool:
    """Ask the provider to cancel. True only when it accepted (the job had not started, and is refunded)."""
    if credentials() is None:
        return False
    client_factory = client_factory or default_client
    async with client_factory() as http:
        return await HiggsfieldClient(http).cancel(handle.request_id, handle.cancel_url)
