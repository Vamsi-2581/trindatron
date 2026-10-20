"""Hugging Face ZeroGPU runner — EXPERIMENTAL, never production.

Runs open-weight engines on shared ZeroGPU hardware through public Gradio
Spaces (half or full NVIDIA RTX Pro 6000). There is no money cost inside the
account's daily GPU quota, and no guarantee either: jobs queue, "No GPU was
available after 60s" happens, quota runs out. So this runner is
`experimental` (refused when ENVIRONMENT=production, never used to size a
plan's promise) and not `commercial`.

Protocol — the Gradio queue (sse_v3), the one gradio_client uses. The simpler
`/gradio_api/call/{api}` endpoint hides every error behind `data: null`, which
would turn "quota exhausted" into an anonymous failure; the queue returns the
Space's own message, so the failure is classified and the runner cools down.

  GET  {space}/config                              -> api_prefix, dependencies[{id, api_name}]
  POST {space}{prefix}/upload                      multipart `files` -> [server path]
  POST {space}{prefix}/queue/join                  {data, fn_index, session_hash} -> {event_id}
  GET  {space}{prefix}/queue/data?session_hash=... SSE `data: {"msg": ...}` until process_completed
                                                   {"success": true, "output": {"data": [...]}}
                                                   {"success": false, "output": {"error": "..."}}
  GET  output file url (or {space}{prefix}/file={path})
Space state: GET https://huggingface.co/api/spaces/{owner}/{name} -> runtime.stage.

Each Space has its own inputs, so each engine has a SpaceProfile that maps a
RunnerJob to that Space's argument list. The Space ids are settings, so a Space
that breaks can be replaced without code. HF_TOKEN, when set, attributes usage
to that account (5 min/day free, 40 min PRO) instead of the anonymous 2 min.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Callable

import httpx

from src.media.engines import EngineFailed, EngineUnavailable, FailureKind
from src.media.runners.base import (
    BACKGROUND_REMOVE,
    CHARACTER_ANIMATE,
    IMAGE_TO_VIDEO,
    TEXT_TO_IMAGE,
    TEXT_TO_VIDEO,
    Runner,
    RunnerJob,
    RunnerResult,
    image_size,
    kind_from_message,
    kind_from_status,
    retry_after_seconds,
    video_metadata,
)
from src.media.settings import media_settings

logger = logging.getLogger("trinity.media.zerogpu")

ClientFactory = Callable[[], httpx.AsyncClient]

_STATE_CACHE_SECONDS = 60.0
_space_state: dict[str, tuple[float, bool]] = {}
_CONFIG_CACHE_SECONDS = 600.0
_space_config: dict[str, tuple[float, dict]] = {}
_TRY_AGAIN = re.compile(r"try again in (\d+):(\d{2}):(\d{2})", re.IGNORECASE)

_WAN_NEGATIVE_ZH = ("色调艳丽, 过曝, 静态, 细节模糊不清, 字幕, 风格, 作品, 画作, 画面, 静止, 整体发灰, 最差质量, 低质量, "
                    "JPEG压缩残留, 丑陋的, 残缺的, 多余的手指, 画得不好的手部, 画得不好的脸部, 畸形的, 毁容的, 形态畸形的肢体, "
                    "手指融合, 静止不动的画面, 杂乱的背景, 三条腿, 背景人很多, 倒着走")
_WAN_NEGATIVE_EN = ("Bright tones, overexposed, static, blurred details, subtitles, style, works, paintings, images, "
                    "static, overall gray, worst quality, low quality, JPEG compression residue, ugly, incomplete, "
                    "extra fingers, poorly drawn hands, poorly drawn faces, deformed, disfigured, misshapen limbs, "
                    "fused fingers, still picture, messy background, three legs, many people in the background, "
                    "walking backwards")
_LTX_NEGATIVE = "worst quality, inconsistent motion, blurry, jittery, distorted"
_Z_IMAGE_RESOLUTIONS = (
    "1024x1024 ( 1:1 )", "1152x896 ( 9:7 )", "896x1152 ( 7:9 )", "1152x864 ( 4:3 )", "864x1152 ( 3:4 )",
    "1248x832 ( 3:2 )", "832x1248 ( 2:3 )", "1280x720 ( 16:9 )", "720x1280 ( 9:16 )", "1344x576 ( 21:9 )",
    "576x1344 ( 9:21 )",
)
_QWEN_ASPECTS = {"1:1": 1.0, "16:9": 16 / 9, "9:16": 9 / 16, "4:3": 4 / 3, "3:4": 3 / 4, "3:2": 1.5, "2:3": 2 / 3}
# CP22: the extension a Space's /upload response path is named with — was image-only (every
# input before CP22 was a single image); a multi-input job (e.g. driving_video) can now carry a
# video file too, so this needs entries for those content types as well.
_UPLOAD_EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
                      "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov"}


def space_host(space: str) -> str:
    """"Tongyi-MAI/Z-Image-Turbo" -> "https://tongyi-mai-z-image-turbo.hf.space"."""
    slug = space.replace("/", "-").replace("_", "-").replace(".", "-").lower()
    return f"https://{slug}.hf.space"


def try_again_seconds(message: str) -> float | None:
    """"Try again in 18:38:07" -> 67087.0 (the Space's own quota reset time)."""
    match = _TRY_AGAIN.search(message or "")
    if not match:
        return None
    hours, minutes, seconds = (int(part) for part in match.groups())
    return float(hours * 3600 + minutes * 60 + seconds)


def _file(path: str, content_type: str | None) -> dict:
    return {"path": path, "orig_name": path.rsplit("/", 1)[-1], "mime_type": content_type,
            "meta": {"_type": "gradio.FileData"}}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _wan_i2v(job: RunnerJob, uploaded: dict[str, dict]) -> list:
    return [uploaded.get("source"), job.prompt, job.steps or 6, job.negative_prompt or _WAN_NEGATIVE_ZH,
            _clamp(float(job.duration_seconds or 3), 0.5, 5.0), job.guidance if job.guidance is not None else 1.0,
            job.guidance if job.guidance is not None else 1.0, job.seed, False]


def _wan_ti2v(job: RunnerJob, uploaded: dict[str, dict]) -> list:
    return [uploaded.get("source"), job.prompt, int(_clamp(job.height, 256, 1024)), int(_clamp(job.width, 256, 1024)),
            job.negative_prompt or _WAN_NEGATIVE_EN, _clamp(float(job.duration_seconds or 2), 1.0, 8.0),
            job.guidance if job.guidance is not None else 0.0, int(_clamp(job.steps or 4, 1, 8)), job.seed, False]


def _ltx(job: RunnerJob, uploaded: dict[str, dict]) -> list:
    mode = "image-to-video" if job.task == IMAGE_TO_VIDEO else "text-to-video"
    return [job.prompt, job.negative_prompt or _LTX_NEGATIVE, uploaded.get("source"), None,
            int(_clamp(job.height, 256, 1280)), int(_clamp(job.width, 256, 1280)), mode,
            _clamp(float(job.duration_seconds or 2), 0.3, 8.5), 9, job.seed, False,
            job.guidance if job.guidance is not None else 1.0, True]


def _z_image(job: RunnerJob, uploaded: dict[str, dict]) -> list:
    wanted = f"{job.width}x{job.height} "
    resolution = next((value for value in _Z_IMAGE_RESOLUTIONS if value.startswith(wanted)), None)
    if resolution is None:
        raise EngineFailed(f"zerogpu z-image: size {job.width}x{job.height}", kind=FailureKind.UNSUPPORTED_FORMAT)
    return [job.prompt, resolution, job.seed, job.steps or 8, 3.0, False, []]


def _qwen_image(job: RunnerJob, uploaded: dict[str, dict]) -> list:
    ratio = job.width / job.height
    label = min(_QWEN_ASPECTS, key=lambda key: abs(_QWEN_ASPECTS[key] - ratio))
    return [job.prompt, job.seed, False, label, job.guidance if job.guidance is not None else 4.0,
            job.steps or 30, False]


def _birefnet(job: RunnerJob, uploaded: dict[str, dict]) -> list:
    source = uploaded.get("source")
    # CP20: verified 2026-09-20 against the real ZhengPeng7/BiRefNet_demo (the official author
    # Space) — /gradio_api/info and its public app.py source (predict(images, resolution,
    # weights_file)), not guessed. api_name="image", 3 positional args: [image, resolution,
    # weights]. Still off by default (media_zerogpu_birefnet_space="") until an operator
    # confirms this exact Space is the one they want live and sets it.
    #
    # resolution: Gradio reports no declared default (parameter_has_default=false) — this
    # adapter does not invent one. It forwards request.parameters["resolution"] verbatim when
    # an operation supplies it (RunnerJob.extra, from OperationRequest.parameters); with none
    # supplied it sends "", which is not a silent guess — predict()'s own source shows an
    # empty/unparseable string is caught and mapped, per the chosen weights, to a resolution
    # the Space's authors picked (1024x1024 for "General", 2048x2048 for the *-HR variants,
    # 2560x1440 for General-Lite-2K, 512x512 for General-reso_512, the source image's own size
    # for the *_dynamic variants) — the Space's documented fallback, not this adapter's.
    #
    # weights: Gradio confirms parameter_default="General" for real, so that is this adapter's
    # only invented-looking default, and it is not invented — it is the Space's own. The other
    # 14 values are never hardcoded here; a caller passes any of them via
    # request.parameters["weights"], validated only by the Space itself.
    resolution = str(job.extra.get("resolution", "") or "")
    weights = str(job.extra.get("weights") or "General")
    return [source, resolution, weights]


def _animate(job: RunnerJob, uploaded: dict[str, dict]) -> list:
    # CP24: verified live against the real alexnasa/Wan2.2-Animate-ZEROGPU Space — a community
    # Space vendoring the official Wan-Video/Wan2.2 code and loading the real Wan-AI/Wan2.2-
    # Animate-14B checkpoint (CP23 source audit), not guessed. api_name="animate_scene" (the
    # ONLY entry in the Space's own /gradio_api/info named_endpoints — the sibling
    # "animate_scene_1" wired to the UI button is not part of the public API). Real parameter
    # order: animate_scene(input_video, max_duration_s, edited_frame, rc_str, resolution_choice).
    # rc_str/resolution_choice are rendered by a custom RadioAnimated(gr.HTML) component, but its
    # own source (and /gradio_api/info's type:"string") confirm the wire value is a plain string
    # matching one of its declared choices — no JSON/HTML wrapper. `prompt_state` (a gr.State,
    # only used by an unrelated advanced-masking UI feature) is not part of the public signature
    # at all and is never sent.
    driving_video = uploaded.get("driving_video")
    character = uploaded.get("source")  # OperationAdapter's primary_role="character" -> RunnerJob.source
    max_duration_s = job.extra.get("max_duration_s", 2)
    rc_str = str(job.extra.get("rc_str") or "Character Swap")
    resolution_choice = str(job.extra.get("resolution_choice") or "Low Res")
    return [driving_video, max_duration_s, character, rc_str, resolution_choice]


@dataclass(frozen=True)
class SpaceProfile:
    engine: str
    tasks: frozenset[str]
    space_setting: str
    api_name: Callable[[RunnerJob], str]
    # CP22: `build` receives every uploaded file this job carries, role -> Gradio FileData — never
    # just the one `source`. "source" is always the key for the primary input (job.source), so
    # every build function written before CP22 (`uploaded.get("source")`) is unaffected; a
    # multi-input engine's build function reads its other roles from the same dict.
    build: Callable[[RunnerJob, dict[str, dict]], list]
    output: str  # "video" | "image" | "imageslider" (a 2-item before/after pair — see _imageslider_result)


PROFILES: dict[str, SpaceProfile] = {
    profile.engine: profile for profile in (
        SpaceProfile("wan22_i2v_a14b", frozenset({IMAGE_TO_VIDEO}), "media_zerogpu_wan22_i2v_space",
                     lambda job: "generate_video", _wan_i2v, "video"),
        SpaceProfile("wan22_ti2v_5b", frozenset({TEXT_TO_VIDEO, IMAGE_TO_VIDEO}), "media_zerogpu_wan22_ti2v_space",
                     lambda job: "generate_video", _wan_ti2v, "video"),
        SpaceProfile("ltx_video_13b_distilled", frozenset({TEXT_TO_VIDEO, IMAGE_TO_VIDEO}),
                     "media_zerogpu_ltx_video_space",
                     lambda job: "image_to_video" if job.task == IMAGE_TO_VIDEO else "text_to_video", _ltx, "video"),
        SpaceProfile("z_image_turbo", frozenset({TEXT_TO_IMAGE}), "media_zerogpu_z_image_space",
                     lambda job: "generate", _z_image, "image"),
        SpaceProfile("qwen_image", frozenset({TEXT_TO_IMAGE}), "media_zerogpu_qwen_image_space",
                     lambda job: "infer", _qwen_image, "image"),
        SpaceProfile("birefnet", frozenset({BACKGROUND_REMOVE}), "media_zerogpu_birefnet_space",
                     lambda job: "image", _birefnet, "imageslider"),
        SpaceProfile("wan22_animate", frozenset({CHARACTER_ANIMATE}), "media_zerogpu_wan22_animate_space",
                     lambda job: "animate_scene", _animate, "video"),
    )
}


def _first_file(value) -> dict | None:
    """The first Gradio FileData in an output (a video dict, a gallery list, a bare file)."""
    if isinstance(value, dict):
        if value.get("url") or value.get("path"):
            return value
        for key in ("video", "image"):
            if isinstance(value.get(key), dict):
                return _first_file(value[key])
        for item in value.values():
            found = _first_file(item)
            if found:
                return found
    if isinstance(value, (list, tuple)):
        for item in value:
            found = _first_file(item)
            if found:
                return found
    return None


def _imageslider_result(value, *, index: int = 1) -> dict | None:
    """The `index`-th image of a Gradio ImageSlider output component's (before, after) pair.

    A Gradio ImageSlider always serializes as a 2-item array in the queue response (verified
    against /gradio_api/info's "SliderData" schema: `{"minItems": 2, "maxItems": 2, ...}`), so
    it cannot be found by `_first_file`'s generic recursive search — that would silently return
    item 0 (the *first* image in the pair) as if it were the model's result, which for BiRefNet
    is the ORIGINAL, unprocessed input, not the background-removed output. index=1 is that
    output for BiRefNet specifically (its own source: `return (image_ori, image_masked)`,
    verified 2026-09-20); reusable for any other Space using the same component, with its own
    verified index.
    """
    for item in (value if isinstance(value, (list, tuple)) else [value]):
        if isinstance(item, (list, tuple)) and len(item) == 2:
            candidate = item[index] if 0 <= index < len(item) else None
            if isinstance(candidate, dict) and (candidate.get("url") or candidate.get("path")):
                return candidate
        elif isinstance(item, (list, tuple)):
            found = _imageslider_result(item, index=index)
            if found:
                return found
    return None


def _default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True)


class ZeroGPURunner(Runner):
    name = "zerogpu"
    commercial = False
    experimental = True

    def __init__(self, client_factory: ClientFactory = _default_client, clock: Callable[[], float] = time.monotonic) -> None:
        self._client_factory = client_factory
        self._clock = clock

    @staticmethod
    def _space(engine: str) -> str | None:
        profile = PROFILES.get(engine)
        return getattr(media_settings(), profile.space_setting, "") if profile else None

    @staticmethod
    def _headers() -> dict[str, str]:
        token = media_settings().hf_token
        return {"Authorization": f"Bearer {token}"} if token else {}

    def supports(self, engine: str, task: str) -> bool:
        profile = PROFILES.get(engine)
        return bool(profile and task in profile.tasks and self._space(engine))

    async def is_available(self) -> bool:
        # The runner as a whole: some configured Space is running. Per engine: is_available_for().
        for engine in PROFILES:
            if self._space(engine) and await self.space_running(engine):
                return True
        return False

    async def is_available_for(self, engine: str) -> bool:
        return await self.space_running(engine)

    async def space_running(self, engine: str) -> bool:
        space = self._space(engine)
        if not space:
            return False
        now = self._clock()
        cached = _space_state.get(space)
        if cached and now - cached[0] < _STATE_CACHE_SECONDS:
            return cached[1]
        try:
            async with self._client_factory() as client:
                response = await client.get(f"https://huggingface.co/api/spaces/{space}", timeout=15)
            running = response.status_code == 200 and ((response.json().get("runtime") or {}).get("stage") == "RUNNING")
        except Exception:
            running = False
        _space_state[space] = (now, running)
        return running

    def estimate_usd(self, engine: str, gpu_seconds: float | None) -> float | None:
        return 0.0  # inside the account's included daily quota; beyond it the Space refuses (quota)

    async def run(self, job: RunnerJob) -> RunnerResult:
        profile = PROFILES.get(job.engine)
        if profile is None or job.task not in profile.tasks:
            raise EngineUnavailable(f"zerogpu: {job.engine}/{job.task} not set up")
        space = self._space(job.engine)
        if not space:
            raise EngineUnavailable(f"zerogpu: no space for {job.engine}")
        if not await self.space_running(job.engine):
            raise EngineUnavailable(f"zerogpu: space not running for {job.engine}")
        host = space_host(space)
        deadline_s = media_settings().media_zerogpu_timeout_s
        started = self._clock()
        async with self._client_factory() as client:
            prefix, fn_index = await self._endpoint(client, host, profile.api_name(job))
            uploaded = await self._upload_all(client, host, prefix, job)
            outputs = await self._predict(client, host, prefix, fn_index, profile.build(job, uploaded), started, deadline_s)
            file = _imageslider_result(outputs) if profile.output == "imageslider" else _first_file(outputs)
            if not file:
                raise EngineFailed("zerogpu: no output file", kind=FailureKind.TEMPORARY)
            url = file.get("url") or f"{host}{prefix}/file={file['path']}"
            download = await self._request(client, "GET", url)
            data = download.content
            if not data:
                raise EngineFailed("zerogpu: empty output", kind=FailureKind.TEMPORARY)
        total = self._clock() - started
        content_type = download.headers.get("content-type", "").split(";")[0].strip().lower()
        if profile.output == "video":
            width, height, seconds = video_metadata(data)
            return RunnerResult(data=data, content_type=content_type if content_type.startswith("video/") else "video/mp4",
                                width=width, height=height, duration_seconds=seconds, total_seconds=total, cost_usd=0.0)
        width, height = image_size(data)
        return RunnerResult(data=data, content_type=content_type if content_type.startswith("image/") else "image/webp",
                            width=width, height=height, total_seconds=total, cost_usd=0.0)

    async def _endpoint(self, client: httpx.AsyncClient, host: str, api_name: str) -> tuple[str, int]:
        """(api prefix, fn_index) for a named endpoint, from the Space's own config (cached)."""
        now = self._clock()
        cached = _space_config.get(host)
        if cached and now - cached[0] < _CONFIG_CACHE_SECONDS:
            config = cached[1]
        else:
            config = _safe_json(await self._request(client, "GET", f"{host}/config"))
            _space_config[host] = (now, config)
        prefix = "/" + str(config.get("api_prefix") or "").strip("/") if config.get("api_prefix") else ""
        for index, dependency in enumerate(config.get("dependencies") or []):
            if isinstance(dependency, dict) and dependency.get("api_name") == api_name:
                return prefix, int(dependency.get("id", index))
        raise EngineUnavailable(f"zerogpu: endpoint {api_name} not found")

    async def _upload_one(self, client: httpx.AsyncClient, host: str, prefix: str, *, role: str, data: bytes,
                          content_type: str) -> dict:
        content_type = content_type or "image/png"
        extension = _UPLOAD_EXTENSIONS.get(content_type, "png")
        try:
            response = await self._request(client, "POST", f"{host}{prefix}/upload",
                                           files={"files": (f"{role}.{extension}", data, content_type)})
        except EngineFailed as exc:
            # Which named input failed is real information a batched multi-file error would lose.
            raise EngineFailed(f"zerogpu: upload failed for {role}: {exc.reason}", kind=exc.kind,
                               retry_after=exc.retry_after, may_have_started=exc.may_have_started,
                               resubmit_safe=exc.resubmit_safe) from exc
        paths = _safe_json(response, default=[])
        if not isinstance(paths, list) or not paths:
            raise EngineFailed(f"zerogpu: upload returned nothing for {role}", kind=FailureKind.TEMPORARY)
        return _file(paths[0], content_type)

    async def _upload_all(self, client: httpx.AsyncClient, host: str, prefix: str, job: RunnerJob) -> dict[str, dict]:
        """Every file this job carries, role -> its uploaded Gradio FileData. `source` (if any)
        uploads first, then each of `job.extra_files` in their declared (dict-insertion, i.e.
        deterministic — see OperationAdapter.job()) order. Uploaded one request per file, not
        batched into one multipart POST: Gradio's own /upload DOES accept several files in one
        call, but a single call turns any one file's failure into an ambiguous batch failure,
        and offers no natural place to skip a duplicate — sequential calls give each role its
        own clear success/failure and let identical bytes (e.g. the same artifact referenced
        under two roles) be uploaded once and reused, never sent twice.
        """
        named: list[tuple[str, bytes, str]] = []
        if job.source is not None:
            named.append(("source", job.source, job.source_content_type or "image/png"))
        for role, (data, content_type) in job.extra_files.items():
            named.append((role, data, content_type or "application/octet-stream"))

        uploaded: dict[str, dict] = {}
        by_bytes: dict[bytes, dict] = {}
        for role, data, content_type in named:
            if data in by_bytes:
                uploaded[role] = by_bytes[data]
                continue
            file_data = await self._upload_one(client, host, prefix, role=role, data=data, content_type=content_type)
            uploaded[role] = file_data
            by_bytes[data] = file_data
        return uploaded

    async def _request(self, client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            response = await client.request(method, url, headers=self._headers(), **kwargs)
        except httpx.TimeoutException as exc:
            raise EngineFailed("zerogpu: timeout", kind=FailureKind.TIMEOUT) from exc
        except httpx.HTTPError as exc:
            raise EngineFailed("zerogpu: connection", kind=FailureKind.TEMPORARY) from exc
        if response.status_code >= 400:
            kind = kind_from_status(response.status_code, response.text[:500])
            raise EngineFailed(f"zerogpu: HTTP {response.status_code}", kind=kind,
                               retry_after=retry_after_seconds(response.headers.get("retry-after")))
        return response

    async def _predict(self, client: httpx.AsyncClient, host: str, prefix: str, fn_index: int, data: list,
                       started: float, deadline_s: float):
        session_hash = uuid.uuid4().hex[:12]
        try:
            joined = await client.post(f"{host}{prefix}/queue/join", headers=self._headers(),
                                       json={"data": data, "fn_index": fn_index, "session_hash": session_hash,
                                             "event_data": None, "trigger_id": None})
        except httpx.HTTPError as exc:
            raise EngineFailed("zerogpu: connection", kind=FailureKind.TEMPORARY) from exc
        if joined.status_code == 503:
            raise EngineFailed("zerogpu: queue full", kind=FailureKind.GPU_UNAVAILABLE)
        if joined.status_code >= 400:
            raise EngineFailed(f"zerogpu: HTTP {joined.status_code}",
                               kind=kind_from_status(joined.status_code, joined.text[:500]))
        event_id = _safe_json(joined).get("event_id")
        try:
            async with client.stream("GET", f"{host}{prefix}/queue/data", params={"session_hash": session_hash},
                                     headers=self._headers(),
                                     timeout=httpx.Timeout(deadline_s, connect=15.0, read=90.0)) as response:
                if response.status_code >= 400:
                    raise EngineFailed(f"zerogpu: HTTP {response.status_code}",
                                       kind=kind_from_status(response.status_code))
                async for line in response.aiter_lines():
                    if self._clock() - started > deadline_s:
                        raise EngineFailed("zerogpu: timeout", kind=FailureKind.TIMEOUT)
                    if not line.startswith("data:"):
                        continue
                    message = json.loads(line[5:].strip() or "{}")
                    kind_of_message = message.get("msg")
                    if kind_of_message == "close_stream":
                        break
                    if event_id and message.get("event_id") not in (None, event_id):
                        continue
                    if kind_of_message == "queue_full":
                        raise EngineFailed("zerogpu: queue full", kind=FailureKind.GPU_UNAVAILABLE)
                    if kind_of_message != "process_completed":
                        continue
                    output = message.get("output") or {}
                    if message.get("success", True) and "error" not in output:
                        return output.get("data")
                    error = str(output.get("error") or output.get("title") or "error")
                    raise EngineFailed(f"zerogpu: {error[:160]}", kind=kind_from_message(error, FailureKind.TEMPORARY),
                                       retry_after=try_again_seconds(error))
        except httpx.TimeoutException as exc:
            raise EngineFailed("zerogpu: timeout", kind=FailureKind.TIMEOUT) from exc
        except httpx.HTTPError as exc:
            raise EngineFailed("zerogpu: connection lost", kind=FailureKind.TEMPORARY) from exc
        except ValueError as exc:
            raise EngineFailed("zerogpu: unreadable result", kind=FailureKind.TEMPORARY) from exc
        raise EngineFailed("zerogpu: stream ended without a result", kind=FailureKind.TEMPORARY)


def _safe_json(response: httpx.Response, default=None):
    try:
        return response.json()
    except ValueError:
        return {} if default is None else default
