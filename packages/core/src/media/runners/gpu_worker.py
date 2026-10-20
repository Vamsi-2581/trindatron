"""Serverless GPU runners — Modal and RunPod — speaking one TRINDATRON worker contract.

The worker (deployed by the operator on either platform) receives the same
engine-agnostic job (RunnerJob.payload(): engine, task, prompt, size, frames,
fps, steps, seed, optional base64 source) and returns the same output:

  output   {"content_type", "data_base64" | "url", "width", "height", "duration_seconds",
            "metrics": {"queue_seconds", "cold_start_seconds", "inference_seconds", "gpu_seconds"}}
  failure  {"kind": FailureKind value, "message": "..."}

So the same Wan 2.2 or LTX worker image runs on Modal, RunPod or anywhere else,
and moving an engine between clouds is configuration. docs/MEDIA_ENGINES.md §9.

  ModalRunner   HTTPS endpoint of the worker (a Modal web endpoint; infra/modal/video_worker.py):
                POST   {url}/v1/jobs {"job": payload, "limits": {"max_gpu_seconds"}} -> {"id"}
                GET    {url}/v1/jobs/{id} -> {"status": queued|running|completed|failed|cancelled, "output"|"error"}
                DELETE {url}/v1/jobs/{id}   (a job the Core stopped waiting for)
                GET    {url}/v1/health -> 200
                limits.max_gpu_seconds is the job's reservation at this runner's price: the worker
                refuses a clip that would cost more before any GPU second is spent.
  RunPodRunner  RunPod Serverless API (docs.runpod.io/serverless/endpoints/send-requests):
                POST {base}/{endpoint}/run {"input": {"job": payload}, "policy": {"executionTimeout"}}
                GET  {base}/{endpoint}/status/{id} -> IN_QUEUE|IN_PROGRESS|COMPLETED|FAILED|CANCELLED|TIMED_OUT,
                     "output", "error", "delayTime" / "executionTime" (ms)
                POST {base}/{endpoint}/cancel/{id};  GET {base}/{endpoint}/health

Cost = GPU seconds × the configured per-second price of the GPU class the
endpoint runs on. GPU seconds come from the worker's metrics or the platform's
executionTime; when neither exists, the measured wall time is used as an upper
bound and recorded as such (`cost_basis`). Nothing is priced from a guess.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Callable

import httpx

from src.media.engines import EngineFailed, EngineUnavailable, FailureKind
from src.media.runners.base import (
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

logger = logging.getLogger("trinity.media.gpu_worker")

ClientFactory = Callable[[], httpx.AsyncClient]
_HEALTH_CACHE_SECONDS = 30.0
_health_cache: dict[str, tuple[float, bool]] = {}
_KNOWN_KINDS = {value for key, value in vars(FailureKind).items() if key.isupper()}


def _default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True)


def _names(value: str) -> set[str]:
    return {part.strip().lower() for part in (value or "").split(",") if part.strip()}


def _failure(error, default: str = FailureKind.TEMPORARY) -> EngineFailed:
    if isinstance(error, dict):
        kind = str(error.get("kind") or "")
        message = str(error.get("message") or error.get("error") or "")
        return EngineFailed(f"worker: {message[:160]}",
                            kind=kind if kind in _KNOWN_KINDS else kind_from_message(message, default))
    message = str(error or "")
    return EngineFailed(f"worker: {message[:160]}", kind=kind_from_message(message, default))


class _WorkerRunner(Runner):
    """Shared by both platforms: submit, follow, read the worker output, price the GPU seconds."""

    commercial = True
    experimental = False
    engines_setting = ""
    tasks_setting = ""
    price_setting = ""
    engine_prices_setting = ""
    cold_start_timeout_setting = ""
    poll_setting = ""

    def __init__(self, client_factory: ClientFactory = _default_client, clock: Callable[[], float] = time.monotonic,
                 sleep=asyncio.sleep) -> None:
        self._client_factory = client_factory
        self._clock = clock
        self._sleep = sleep

    def _price(self, engine: str | None = None) -> float:
        """US$ per GPU second for `engine`: its own GPU class's price when the endpoint runs engines on
        different GPUs (Modal: the 5B on H100, the A14B on H200), else the endpoint's one price."""
        settings = media_settings()
        if engine and self.engine_prices_setting:
            prices = _prices(getattr(settings, self.engine_prices_setting, ""))
            if engine.lower() in prices:
                return prices[engine.lower()]
        return float(getattr(settings, self.price_setting))

    def supports(self, engine: str, task: str) -> bool:
        settings = media_settings()
        tasks = _names(getattr(settings, self.tasks_setting, "")) if self.tasks_setting else set()
        return (self.configured() and engine.lower() in _names(getattr(settings, self.engines_setting))
                and (not tasks or task.lower() in tasks))

    def configured(self) -> bool:  # pragma: no cover - overridden
        return False

    def estimate_usd(self, engine: str, gpu_seconds: float | None) -> float | None:
        if gpu_seconds is None:
            return None
        allowance = float(media_settings().media_gpu_cold_start_allowance_s)
        return (gpu_seconds + allowance) * self._price(engine)

    async def _health(self, key: str, url: str, headers: dict) -> bool:
        now = self._clock()
        cached = _health_cache.get(key)
        if cached and now - cached[0] < _HEALTH_CACHE_SECONDS:
            return cached[1]
        try:
            async with self._client_factory() as client:
                ok = (await client.get(url, headers=headers, timeout=15)).status_code == 200
        except Exception:
            ok = False
        _health_cache[key] = (now, ok)
        return ok

    async def _send(self, client: httpx.AsyncClient, method: str, url: str, what: str, **kwargs) -> httpx.Response:
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise EngineFailed(f"{what}: timeout", kind=FailureKind.TIMEOUT) from exc
        except httpx.HTTPError as exc:
            raise EngineFailed(f"{what}: connection", kind=FailureKind.TEMPORARY) from exc
        if response.status_code >= 400:
            raise EngineFailed(f"{what}: HTTP {response.status_code}",
                               kind=kind_from_status(response.status_code, response.text[:500]),
                               retry_after=retry_after_seconds(response.headers.get("retry-after")))
        return response

    async def _read_output(self, client: httpx.AsyncClient, output, job: RunnerJob, *, total: float,
                           queue_seconds: float | None, platform_gpu_seconds: float | None) -> RunnerResult:
        if not isinstance(output, dict):
            raise EngineFailed("worker: no output", kind=FailureKind.TEMPORARY)
        if output.get("error"):
            raise _failure(output["error"])
        if output.get("data_base64"):
            try:
                data = base64.b64decode(output["data_base64"])
            except (ValueError, TypeError) as exc:
                raise EngineFailed("worker: unreadable output", kind=FailureKind.TEMPORARY) from exc
            content_type = str(output.get("content_type") or "")
        elif output.get("url"):
            response = await self._send(client, "GET", str(output["url"]), "worker download")
            data = response.content
            content_type = str(output.get("content_type") or response.headers.get("content-type", "")).split(";")[0]
        else:
            raise EngineFailed("worker: no output file", kind=FailureKind.TEMPORARY)
        if not data:
            raise EngineFailed("worker: empty output", kind=FailureKind.TEMPORARY)

        metrics = output.get("metrics") if isinstance(output.get("metrics"), dict) else {}
        gpu_seconds = _number(metrics.get("gpu_seconds")) or platform_gpu_seconds
        basis = "gpu_seconds" if gpu_seconds is not None else "wall_seconds_upper_bound"
        billed = gpu_seconds if gpu_seconds is not None else total
        if content_type.startswith("video/") or job.task.endswith("video"):
            width, height, seconds = video_metadata(data)
            content_type = content_type if content_type.startswith("video/") else "video/mp4"
        else:
            width, height = image_size(data)
            seconds = None
            content_type = content_type if content_type.startswith("image/") else "image/png"
        logger.info("gpu worker: %s delivered | engine=%s cost_basis=%s", self.name, job.engine, basis)
        return RunnerResult(
            data=data, content_type=content_type,
            width=width or _int(output.get("width")), height=height or _int(output.get("height")),
            duration_seconds=seconds or _number(output.get("duration_seconds")),
            total_seconds=total, queue_seconds=_number(metrics.get("queue_seconds")) or queue_seconds,
            cold_start_seconds=_number(metrics.get("cold_start_seconds")),
            inference_seconds=_number(metrics.get("inference_seconds")), gpu_seconds=gpu_seconds,
            cost_usd=round(billed * self._price(job.engine), 8),
        )


class ModalRunner(_WorkerRunner):
    name = "modal"
    engines_setting = "media_modal_engines"
    tasks_setting = "media_modal_tasks"
    price_setting = "media_modal_gpu_usd_per_second"
    engine_prices_setting = "media_modal_gpu_usd_per_second_by_engine"

    def configured(self) -> bool:
        settings = media_settings()
        return bool(settings.media_modal_url and settings.media_modal_token)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {media_settings().media_modal_token}"}

    async def is_available(self) -> bool:
        if not self.configured():
            return False
        return await self._health("modal", f"{media_settings().media_modal_url.rstrip('/')}/v1/health", self._headers())

    async def run(self, job: RunnerJob) -> RunnerResult:
        settings = media_settings()
        if not self.supports(job.engine, job.task):
            raise EngineUnavailable(f"modal: {job.engine} not deployed")
        base = settings.media_modal_url.rstrip("/")
        body: dict = {"job": job.payload()}
        price = self._price(job.engine)
        if job.budget_usd is not None and price > 0:
            body["limits"] = {"max_gpu_seconds": round(job.budget_usd / price, 2)}
        started = self._clock()
        async with self._client_factory() as client:
            submitted = await self._send(client, "POST", f"{base}/v1/jobs", "modal submit",
                                         json=body, headers=self._headers())
            job_id = (submitted.json() or {}).get("id")
            if not job_id:
                raise EngineFailed("modal: no job id", kind=FailureKind.TEMPORARY)
            job_url = f"{base}/v1/jobs/{job_id}"
            # From here on the clip exists on Modal. Every way out that is not the worker's own final answer
            # (completed, failed, cancelled) cancels it: a GPU never keeps generating for nobody.
            finished_on_worker = False
            try:
                first_running = None
                failing_since: float | None = None
                last_status = "queued"
                while True:
                    elapsed = self._clock() - started
                    try:
                        state = (await self._send(client, "GET", job_url, "modal status",
                                                  headers=self._headers())).json() or {}
                    except EngineFailed as exc:
                        # A cold start is ~100 s of "queued" and the clip keeps running while a check fails:
                        # one slow or dropped status check is not a failed video. Only a check the worker
                        # answered for sure (4xx other than 429) ends it, or a run of failures past the grace.
                        # 503 reads as "unavailable" like 401/404, but only it is a passing state of the endpoint.
                        transient = (exc.kind in (FailureKind.TIMEOUT, FailureKind.TEMPORARY, FailureKind.RATE_LIMITED)
                                     or exc.reason.endswith("HTTP 503"))
                        now = self._clock()
                        failing_since = now if failing_since is None else failing_since
                        if not transient or now - failing_since > settings.media_modal_poll_failure_grace_s:
                            logger.warning("modal: status checks failed | job=%s reason=%s for=%.0fs", job_id,
                                           exc.reason, now - failing_since)
                            raise
                        logger.info("modal: status check failed, still waiting | job=%s reason=%s", job_id, exc.reason)
                        state = {"status": last_status}
                    else:
                        failing_since = None
                    status = str(state.get("status", "")).lower()
                    if status == "completed":
                        finished_on_worker = True
                        return await self._read_output(client, state.get("output"), job, total=elapsed,
                                                       queue_seconds=first_running, platform_gpu_seconds=None)
                    if status == "failed":
                        finished_on_worker = True
                        failure = _failure(state.get("error"))
                        logger.warning("modal: worker reported a failure | job=%s kind=%s reason=%s", job_id,
                                       failure.kind, failure.reason)
                        raise failure
                    if status == "cancelled":
                        finished_on_worker = True
                        raise EngineFailed("modal: cancelled", kind=FailureKind.TEMPORARY)
                    if status == "running" and first_running is None:
                        first_running = elapsed
                    last_status = status or last_status
                    stuck_in_queue = status == "queued" and elapsed > settings.media_modal_cold_start_timeout_s
                    if stuck_in_queue or elapsed > settings.media_video_timeout_s:
                        raise EngineFailed("modal: never started" if stuck_in_queue else "modal: timeout",
                                           kind=FailureKind.COLD_START_TIMEOUT if stuck_in_queue else FailureKind.TIMEOUT)
                    if status not in ("queued", "running"):
                        raise EngineFailed(f"modal: status {status or 'unknown'}", kind=FailureKind.TEMPORARY)
                    await self._sleep(settings.media_modal_poll_interval_s)
            except BaseException:
                # Includes the job's own task being cancelled (the person cancelled, the process is stopping).
                if not finished_on_worker:
                    await asyncio.shield(self._cancel(client, job_url))
                raise


    async def _cancel(self, client: httpx.AsyncClient, url: str) -> None:
        try:
            await client.delete(url, headers=self._headers(), timeout=15)
        except httpx.HTTPError:
            logger.warning("modal: cancel failed | url=%s", url.rsplit("/", 1)[-1])


class RunPodRunner(_WorkerRunner):
    name = "runpod"
    engines_setting = "media_runpod_engines"
    price_setting = "media_runpod_gpu_usd_per_second"

    def configured(self) -> bool:
        settings = media_settings()
        return bool(settings.media_runpod_endpoint_id and settings.runpod_api_key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {media_settings().runpod_api_key}"}

    def _base(self) -> str:
        settings = media_settings()
        return f"{settings.media_runpod_base_url.rstrip('/')}/{settings.media_runpod_endpoint_id}"

    async def is_available(self) -> bool:
        if not self.configured():
            return False
        return await self._health("runpod", f"{self._base()}/health", self._headers())

    async def run(self, job: RunnerJob) -> RunnerResult:
        settings = media_settings()
        if not self.supports(job.engine, job.task):
            raise EngineUnavailable(f"runpod: {job.engine} not deployed")
        base = self._base()
        started = self._clock()
        async with self._client_factory() as client:
            submitted = await self._send(
                client, "POST", f"{base}/run", "runpod submit", headers=self._headers(),
                json={"input": {"job": job.payload()},
                      "policy": {"executionTimeout": int(settings.media_video_timeout_s * 1000)}},
            )
            job_id = (submitted.json() or {}).get("id")
            if not job_id:
                raise EngineFailed("runpod: no job id", kind=FailureKind.TEMPORARY)
            while True:
                state = (await self._send(client, "GET", f"{base}/status/{job_id}", "runpod status",
                                          headers=self._headers())).json() or {}
                status = str(state.get("status", "")).upper()
                elapsed = self._clock() - started
                if status == "COMPLETED":
                    execution = _number(state.get("executionTime"))
                    delay = _number(state.get("delayTime"))
                    return await self._read_output(
                        client, state.get("output"), job, total=elapsed,
                        queue_seconds=delay / 1000 if delay is not None else None,
                        platform_gpu_seconds=execution / 1000 if execution is not None else None)
                if status == "FAILED":
                    raise _failure(state.get("error"))
                if status == "TIMED_OUT":
                    raise EngineFailed("runpod: timed out", kind=FailureKind.TIMEOUT)
                if status == "CANCELLED":
                    raise EngineFailed("runpod: cancelled", kind=FailureKind.TEMPORARY)
                stuck_in_queue = status == "IN_QUEUE" and elapsed > settings.media_runpod_cold_start_timeout_s
                if stuck_in_queue or elapsed > settings.media_video_timeout_s:
                    await self._cancel(client, base, job_id)
                    raise EngineFailed("runpod: never started" if stuck_in_queue else "runpod: timeout",
                                       kind=FailureKind.COLD_START_TIMEOUT if stuck_in_queue else FailureKind.TIMEOUT)
                if status not in ("IN_QUEUE", "IN_PROGRESS"):
                    raise EngineFailed(f"runpod: status {status or 'unknown'}", kind=FailureKind.TEMPORARY)
                await self._sleep(settings.media_runpod_poll_interval_s)

    async def _cancel(self, client: httpx.AsyncClient, base: str, job_id: str) -> None:
        try:
            await client.post(f"{base}/cancel/{job_id}", headers=self._headers(), timeout=15)
        except httpx.HTTPError:
            logger.warning("runpod: cancel failed | job=%s", job_id)


def _prices(value: str) -> dict[str, float]:
    """"wan22_t2v_a14b=0.001261, other=0.002" -> {engine: price}; malformed entries are ignored."""
    out: dict[str, float] = {}
    for part in (value or "").split(","):
        engine, _, price = part.partition("=")
        try:
            if engine.strip() and float(price) > 0:
                out[engine.strip().lower()] = float(price)
        except ValueError:
            continue
    return out


def _number(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int(value) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None
