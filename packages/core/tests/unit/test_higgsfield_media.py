"""Higgsfield as one more hosted route: the client, the engines and their place in the chain.

Every call goes through httpx.MockTransport playing the Higgsfield API: nothing
leaves the machine and nothing is spent. Credentials here are placeholders.
"""
from __future__ import annotations

import io
import json

import httpx
import pytest
from PIL import Image

from src.media import higgsfield, plans
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
    VideoRequest,
)
from src.media.health import health
from src.media.higgsfield import HiggsfieldClient, HiggsfieldImageEngine, HiggsfieldVideoEngine
from src.media.orchestrator import EngineChainFailed, ImageEngineChain
from src.media.registry import HOSTED_IMAGE_ROUTES, HOSTED_VIDEO_ROUTES, build_image_route, build_video_route
from src.media.routes import HostedImageRoute
from src.media.settings import media_settings

API = "https://api.higgsfield.ai"
CDN = "https://cdn.example-output.test"
SOUL = "higgsfield-ai/soul/v2/standard"
MP4 = b"\x00\x00\x00\x18ftypmp42fake"


def _jpeg(width: int = 960, height: int = 720) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (40, 60, 80)).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    # Set, never deleted: an environment value wins over packages/core/.env, so a real key there is never read.
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "")
    monkeypatch.setenv("HIGGSFIELD_API_KEY_ID", "test-id")
    monkeypatch.setenv("HIGGSFIELD_API_KEY_SECRET", "test-secret")
    monkeypatch.setenv("MEDIA_HIGGSFIELD_POLL_INTERVAL_S", "0")
    monkeypatch.setenv("MEDIA_ACCEPTED_LICENSE_CONDITIONS", "higgsfield_api_terms")
    media_settings.cache_clear()
    health.reset()
    yield
    media_settings.cache_clear()
    health.reset()


class FakeHiggsfield:
    """The Higgsfield API as documented: submit, status, cancel, upload, and an output host."""

    def __init__(self, *, statuses=("queued", "in_progress", "completed"), kind="image", output: bytes | None = None,
                 submit: httpx.Response | Exception | None = None, cancel_status: int = 202,
                 status_errors: int = 0, download_errors: int = 0, completed_payload: dict | None = None,
                 failed_detail: dict | None = None) -> None:
        self.statuses = list(statuses)
        self.kind = kind
        self.output = output if output is not None else (_jpeg() if kind == "image" else MP4)
        self.submit = submit
        self.cancel_status = cancel_status
        self.status_errors = status_errors
        self.download_errors = download_errors
        self.completed_payload = completed_payload
        # CP38: extra fields merged into the status body only when status == "failed" — proves
        # HiggsfieldClient.wait() preserves and propagates real provider detail on failure.
        self.failed_detail = failed_detail
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[dict] = []
        self.auth: dict[str, str | None] = {}

    def count(self, method: str, prefix: str) -> int:
        return sum(1 for m, url in self.calls if m == method and url.startswith(prefix))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append((request.method, url))
        self.auth[url] = request.headers.get("authorization")
        if url.startswith(CDN) or url.startswith("https://upload.example.test"):
            if request.method == "PUT":
                return httpx.Response(200)
            if self.download_errors:
                self.download_errors -= 1
                return httpx.Response(500)
            ctype = "image/jpeg" if self.kind == "image" else "video/mp4"
            return httpx.Response(200, content=self.output, headers={"content-type": ctype})
        if url == f"{API}/files/generate-upload-url":
            return httpx.Response(200, json={"public_url": f"{CDN}/in/source.png",
                                             "upload_url": "https://upload.example.test/put/abc",
                                             "upload_headers": {"x-upload": "1"}})
        if url.endswith("/cancel"):
            return httpx.Response(self.cancel_status)
        if url.endswith("/status"):
            if self.status_errors:
                self.status_errors -= 1
                return httpx.Response(500)
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            body = {"status": status, "request_id": "req-1", "status_url": f"{API}/requests/req-1/status",
                    "cancel_url": f"{API}/requests/req-1/cancel"}
            if status == "completed":
                body.update(self.completed_payload if self.completed_payload is not None else (
                    {"images": [{"url": f"{CDN}/out/image.jpg"}]} if self.kind == "image"
                    else {"video": {"url": f"{CDN}/out/video.mp4"}}))
            if status == "failed" and self.failed_detail:
                body.update(self.failed_detail)
            return httpx.Response(200, json=body)
        if request.method == "POST":
            self.bodies.append(json.loads(request.content or b"{}"))
            if isinstance(self.submit, Exception):
                raise self.submit
            if self.submit is not None:
                return self.submit
            return httpx.Response(200, json={"status": "queued", "request_id": "req-1",
                                             "status_url": f"{API}/requests/req-1/status",
                                             "cancel_url": f"{API}/requests/req-1/cancel"})
        return httpx.Response(404)


def factory(server):
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(server), follow_redirects=True)


def image_request(**overrides) -> ImageRequest:
    values = dict(prompt="a lighthouse at dusk", width=1024, height=768, seed=7, steps=8)
    values.update(overrides)
    return ImageRequest(**values)


def video_request(**overrides) -> VideoRequest:
    values = dict(prompt="waves on rocks", width=1280, height=720, duration_seconds=5, fps=24, seed=7)
    values.update(overrides)
    return VideoRequest(**values)


def soul(server) -> HiggsfieldImageEngine:
    return HiggsfieldImageEngine("soul_2@higgsfield", factory(server))


def kling(server) -> HiggsfieldVideoEngine:
    return HiggsfieldVideoEngine("kling_3_std@higgsfield", factory(server))


# ── Configuration: off means as before ───────────────────────────────────────

async def test_without_credentials_every_route_is_unavailable_and_nothing_is_sent(monkeypatch):
    monkeypatch.setenv("HIGGSFIELD_API_KEY_ID", "")
    monkeypatch.setenv("HIGGSFIELD_API_KEY_SECRET", "")
    media_settings.cache_clear()
    server = FakeHiggsfield()
    assert higgsfield.configured() is False
    assert await soul(server).is_available() is False
    assert await kling(server).is_available() is False
    with pytest.raises(EngineUnavailable):
        await soul(server).generate(image_request())
    assert server.calls == []


async def test_the_combined_id_secret_form_is_accepted_and_sent_only_to_the_api_host(monkeypatch):
    monkeypatch.setenv("HIGGSFIELD_API_KEY_ID", "")
    monkeypatch.setenv("HIGGSFIELD_API_KEY_SECRET", "")
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "combined-id:combined-secret")
    media_settings.cache_clear()
    server = FakeHiggsfield()
    await soul(server).generate(image_request())
    assert server.auth[f"{API}/{SOUL}"] == "Key combined-id:combined-secret"
    assert server.auth[f"{CDN}/out/image.jpg"] is None  # no credentials to the output host


def test_routes_are_registered_and_need_the_accepted_api_terms(monkeypatch):
    for key in ("soul_2@higgsfield", "image_edit@higgsfield"):
        assert key in HOSTED_IMAGE_ROUTES and build_image_route(key).runner_name == "higgsfield"
    for key in ("wan_3@higgsfield", "kling_3_std@higgsfield", "grok_imagine_video_1_5@higgsfield"):
        assert key in HOSTED_VIDEO_ROUTES and build_video_route(key).runner_name == "higgsfield"
    assert build_image_route("soul_2@higgsfield").eligible() is True
    monkeypatch.setenv("MEDIA_ACCEPTED_LICENSE_CONDITIONS", "")
    media_settings.cache_clear()
    assert build_image_route("soul_2@higgsfield").eligible() is False


async def test_capabilities_without_verified_parameters_are_not_offered():
    server = FakeHiggsfield()
    assert await soul(server).is_available(editing=False) is True
    assert await HiggsfieldImageEngine("image_edit@higgsfield", factory(server)).is_available(editing=True) is False
    assert await kling(server).is_available(from_image=False) is True
    assert await kling(server).is_available(from_image=True) is False  # image field undocumented
    for route in ("wan_3@higgsfield", "grok_imagine_video_1_5@higgsfield"):
        assert await HiggsfieldVideoEngine(route, factory(server)).is_available() is False  # no verified ratio
    assert server.calls == []


# ── Soul 2: text to image ────────────────────────────────────────────────────

async def test_soul_2_submits_once_follows_the_queue_and_prices_the_published_rate():
    server = FakeHiggsfield()
    order: list[str] = []
    link = ProviderLink(on_submit=lambda handle: order.append(f"recorded {handle.request_id}"))

    output = await soul(server).generate(image_request(link=link))

    assert server.count("POST", f"{API}/{SOUL}") == 1
    assert server.bodies[0] == {"prompt": "a lighthouse at dusk", "aspect_ratio": "4:3", "resolution": "720p",
                                "batch_size": 1, "seed": 7}
    assert server.auth[f"{API}/{SOUL}"] == "Key test-id:test-secret"
    assert order == ["recorded req-1"] and link.handle.request_id == "req-1"
    assert link.handle.cancel_url == f"{API}/requests/req-1/cancel"
    first_status = next(i for i, (m, url) in enumerate(server.calls) if url.endswith("/status"))
    assert first_status > 0  # the id was recorded before any polling
    assert output.content_type == "image/jpeg" and (output.width, output.height) == (960, 720)
    assert output.cost_usd == pytest.approx(0.0032)
    assert output.metrics == {"provider": "higgsfield", "model": SOUL, "request_id": "req-1", "seed": 7}


async def test_1080p_is_asked_and_priced_for_larger_images():
    engine = soul(FakeHiggsfield())
    request = image_request(width=1440, height=1080)
    assert engine.estimate_usd(request) == pytest.approx(0.0057)
    assert engine.body(request)[1]["resolution"] == "1080p"
    assert engine.estimate_usd(image_request()) == pytest.approx(0.0032)


async def test_a_shape_that_is_not_verified_is_refused_before_anything_is_sent():
    server = FakeHiggsfield()
    with pytest.raises(EngineFailed) as refused:
        await soul(server).generate(image_request(width=1024, height=1024))
    assert refused.value.kind == FailureKind.UNSUPPORTED_FORMAT and refused.value.may_have_started is False
    assert server.calls == []


def test_estimates_come_in_usd_and_tcu():
    assert higgsfield.quote(0.0032) == {"usd": 0.0032, "tcu": plans.usd_to_tcu(0.0032)}
    assert higgsfield.quote(0.42)["tcu"] == pytest.approx(42.0)


async def test_prices_and_models_are_configuration(monkeypatch):
    monkeypatch.setenv("MEDIA_HIGGSFIELD_SOUL_USD_720P", "0.004")
    monkeypatch.setenv("MEDIA_HIGGSFIELD_SOUL_MODEL", "higgsfield-ai/soul/v3/standard")
    media_settings.cache_clear()
    server = FakeHiggsfield()
    engine = soul(server)
    assert engine.estimate_usd(image_request()) == pytest.approx(0.004)
    await engine.generate(image_request())
    assert server.count("POST", f"{API}/higgsfield-ai/soul/v3/standard") == 1


# ── Image editing and uploads ────────────────────────────────────────────────

async def test_a_configured_edit_model_uploads_the_source_then_submits_its_url(monkeypatch):
    monkeypatch.setenv("MEDIA_HIGGSFIELD_EDIT_MODEL", "vendor/edit-model")
    monkeypatch.setenv("MEDIA_HIGGSFIELD_EDIT_IMAGE_FIELD", "image_url")
    monkeypatch.setenv("MEDIA_HIGGSFIELD_EDIT_ASPECT_RATIOS", "4:3")
    monkeypatch.setenv("MEDIA_HIGGSFIELD_EDIT_USD_PER_IMAGE", "0.03")
    media_settings.cache_clear()
    server = FakeHiggsfield()
    engine = HiggsfieldImageEngine("image_edit@higgsfield", factory(server))
    assert await engine.is_available(editing=True) is True
    output = await engine.generate(image_request(source=b"\x89PNG....", source_content_type="image/png"))
    assert server.count("POST", f"{API}/files/generate-upload-url") == 1
    assert server.count("PUT", "https://upload.example.test/put/abc") == 1
    assert server.auth["https://upload.example.test/put/abc"] is None
    assert server.bodies[-1]["image_url"] == f"{CDN}/in/source.png"
    assert server.count("POST", f"{API}/vendor/edit-model") == 1
    assert output.cost_usd == pytest.approx(0.03)


async def test_an_upload_of_an_unsupported_type_is_refused_locally():
    server = FakeHiggsfield()
    async with factory(server)() as http:
        with pytest.raises(EngineFailed) as refused:
            await HiggsfieldClient(http).upload(b"%PDF", "application/pdf")
    assert refused.value.kind == FailureKind.UNSUPPORTED_FORMAT and server.calls == []


# ── Submit: never repeated, errors classified ────────────────────────────────

@pytest.mark.parametrize("status, text, kind, started", [
    (400, "Maximum number of concurrent requests (4)", FailureKind.RATE_LIMITED, False),
    (400, "bad field", FailureKind.UNSUPPORTED_FORMAT, False),
    (422, "validation", FailureKind.UNSUPPORTED_FORMAT, False),
    (401, "", FailureKind.UNAVAILABLE, False),
    (403, "Not enough credits", FailureKind.QUOTA, False),
    (404, "", FailureKind.UNAVAILABLE, False),
    (423, "", FailureKind.UNAVAILABLE, False),
    (429, "", FailureKind.RATE_LIMITED, False),
    (500, "", FailureKind.TEMPORARY, False),
    (503, "", FailureKind.UNAVAILABLE, False),
    (502, "", FailureKind.TEMPORARY, True),
    (504, "", FailureKind.TEMPORARY, True),
])
async def test_submit_errors_are_classified_and_never_resubmitted(status, text, kind, started):
    server = FakeHiggsfield(submit=httpx.Response(status, text=text))
    with pytest.raises(EngineFailed) as failed:
        await soul(server).generate(image_request())
    assert failed.value.kind == kind
    assert failed.value.may_have_started is started
    assert failed.value.resubmit_safe is False
    if started:
        assert failed.value.retryable is False  # no fallback either: it may be running and billed
    assert server.count("POST", f"{API}/{SOUL}") == 1
    if text:
        assert text not in failed.value.reason  # the provider's own words stay out of every message


async def test_an_unanswered_submit_may_have_started_and_is_not_retried_or_handed_over():
    server = FakeHiggsfield(submit=httpx.ReadTimeout("slow"))
    with pytest.raises(EngineFailed) as failed:
        await soul(server).generate(image_request())
    assert failed.value.may_have_started is True and failed.value.retryable is False
    assert server.count("POST", f"{API}/{SOUL}") == 1


async def test_a_connection_that_never_opened_can_go_to_another_route_but_not_again_here():
    server = FakeHiggsfield(submit=httpx.ConnectError("refused"))
    with pytest.raises(EngineFailed) as failed:
        await soul(server).generate(image_request())
    assert failed.value.may_have_started is False and failed.value.retryable is True
    assert failed.value.resubmit_safe is False


@pytest.mark.parametrize("response", [httpx.Response(200, text="not json"), httpx.Response(200, json={"status": "queued"})])
async def test_an_accepted_submit_without_a_request_id_is_never_resubmitted(response):
    server = FakeHiggsfield(submit=response)
    with pytest.raises(EngineFailed) as failed:
        await soul(server).generate(image_request())
    assert failed.value.may_have_started is True and failed.value.retryable is False
    assert server.count("POST", f"{API}/{SOUL}") == 1


# ── Polling ──────────────────────────────────────────────────────────────────

async def test_a_failed_job_is_not_billed_and_may_fall_back():
    with pytest.raises(EngineFailed) as failed:
        await soul(FakeHiggsfield(statuses=("queued", "failed"))).generate(image_request())
    assert failed.value.kind == FailureKind.TEMPORARY and failed.value.retryable is True
    assert failed.value.may_have_started is False and failed.value.resubmit_safe is False


# CP38: HiggsfieldClient.wait()'s FAILED branch, hardened after CP37's real generation lost the
# provider's own explanation entirely (a bare "higgsfield: job failed", no request_id, no detail).

async def test_a_failed_job_with_provider_detail_preserves_it(): # CP38-A
    detail = {"error": "CUDA out of memory. Tried to allocate 2.10 GiB", "code": "oom", "title": "Error"}
    with pytest.raises(EngineFailed) as failed:
        await soul(FakeHiggsfield(statuses=("queued", "failed"), failed_detail=detail)).generate(image_request())
    assert failed.value.provider_detail is not None
    assert failed.value.provider_detail["error"] == detail["error"]
    assert failed.value.provider_detail["code"] == "oom"
    assert failed.value.provider_detail["title"] == "Error"
    assert "req-1" in str(failed.value)  # request_id is now part of the message, not lost
    assert "CUDA out of memory" in str(failed.value)
    # Classification is unchanged by carrying more detail — same kind/retryable/resubmit_safe as before.
    assert failed.value.kind == FailureKind.TEMPORARY and failed.value.resubmit_safe is False


async def test_a_failed_job_with_no_extra_detail_still_works(): # CP38-B
    with pytest.raises(EngineFailed) as failed:
        await soul(FakeHiggsfield(statuses=("queued", "failed"))).generate(image_request())
    # Only the bare status the mock always sends — no error/message/code from the provider —
    # and the client still works: no crash, no masked exception, a minimal but real detail.
    assert failed.value.provider_detail == {"status": "failed"}
    assert "req-1" in str(failed.value)
    assert failed.value.kind == FailureKind.TEMPORARY and failed.value.resubmit_safe is False


async def test_a_failed_job_with_a_completely_empty_status_payload_still_works(): # CP38-B (edge)
    """safe_provider_detail's own contract for a payload with nothing recognizable at all —
    promoted to media/engines.py in CP46 so fal shares the same implementation."""
    from src.media.engines import safe_provider_detail

    assert safe_provider_detail({}) == {}
    assert safe_provider_detail(None) == {}


async def test_a_failed_job_never_leaks_a_credential_from_the_provider_payload(): # CP38-D
    """Defensive: even if a provider payload contained something credential-shaped inside a
    recognized field, it never reaches the exception's message or its provider_detail."""
    detail = {"error": "boom", "detail": {"reason": "oom", "authorization": "Bearer should-never-appear",
                                          "api_key": "sk-should-never-appear"}}
    with pytest.raises(EngineFailed) as failed:
        await soul(FakeHiggsfield(statuses=("queued", "failed"), failed_detail=detail)).generate(image_request())
    dump = str(failed.value) + json.dumps(failed.value.provider_detail or {})
    assert "should-never-appear" not in dump
    assert "authorization" not in dump.lower()
    assert "api_key" not in dump.lower()
    assert failed.value.provider_detail["detail"] == {"reason": "oom"}  # the safe field survives


async def test_an_nsfw_result_stops_the_chain():
    with pytest.raises(EngineFailed) as failed:
        await soul(FakeHiggsfield(statuses=("in_progress", "nsfw"))).generate(image_request())
    assert failed.value.kind == FailureKind.FILTERED and failed.value.retryable is False


async def test_a_job_cancelled_at_the_provider_ends_as_cancelled():
    with pytest.raises(JobCancelled):
        await soul(FakeHiggsfield(statuses=("queued", "canceled"))).generate(image_request())


async def test_transient_status_errors_are_tolerated():
    server = FakeHiggsfield(status_errors=2)
    output = await soul(server).generate(image_request())
    assert output.cost_usd == pytest.approx(0.0032) and server.count("POST", f"{API}/{SOUL}") == 1


async def test_a_status_that_keeps_failing_is_cancelled_when_still_queued():
    server = FakeHiggsfield(status_errors=100)
    with pytest.raises(EngineFailed) as failed:
        await soul(server).generate(image_request())
    assert server.count("POST", f"{API}/requests/req-1/cancel") == 1
    assert failed.value.kind == FailureKind.TIMEOUT and failed.value.may_have_started is False


class Clock:
    def __init__(self, step: float) -> None:
        self.now, self.step = 0.0, step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def _handle(**overrides) -> ProviderHandle:
    values = dict(provider="higgsfield", model=SOUL, route="soul_2@higgsfield", request_id="req-1",
                  status_url=f"{API}/requests/req-1/status", cancel_url=f"{API}/requests/req-1/cancel")
    values.update(overrides)
    return ProviderHandle(**values)


async def _no_sleep(_seconds):
    return None


async def test_the_deadline_cancels_a_job_still_queued_so_another_route_may_run():
    server = FakeHiggsfield(statuses=("queued",))
    async with factory(server)() as http:
        with pytest.raises(EngineFailed) as failed:
            await HiggsfieldClient(http).wait(_handle(), deadline=10.0, sleep=_no_sleep, clock=Clock(4.0))
    assert failed.value.kind == FailureKind.TIMEOUT and failed.value.retryable is True
    assert failed.value.may_have_started is False
    assert server.count("POST", f"{API}/requests/req-1/cancel") == 1


async def test_the_deadline_on_a_job_already_running_stops_everything_without_resubmitting():
    server = FakeHiggsfield(statuses=("in_progress",), cancel_status=400)
    async with factory(server)() as http:
        with pytest.raises(EngineFailed) as failed:
            await HiggsfieldClient(http).wait(_handle(), deadline=10.0, sleep=_no_sleep, clock=Clock(4.0))
    assert failed.value.may_have_started is True and failed.value.retryable is False


async def test_a_cancel_asked_while_queued_is_sent_to_the_provider():
    server = FakeHiggsfield(statuses=("queued",))
    link = ProviderLink()
    link.cancel_requested = True
    async with factory(server)() as http:
        with pytest.raises(JobCancelled):
            await HiggsfieldClient(http).wait(_handle(), deadline=1e9, link=link, sleep=_no_sleep)
    assert server.count("POST", f"{API}/requests/req-1/cancel") == 1


async def test_a_cancel_refused_because_the_job_started_lets_it_finish():
    server = FakeHiggsfield(statuses=("in_progress", "completed"), cancel_status=400)
    link = ProviderLink()
    link.cancel_requested = True
    async with factory(server)() as http:
        state = await HiggsfieldClient(http).wait(_handle(), deadline=1e9, link=link, sleep=_no_sleep)
    assert state["status"] == "completed" and link.cancel_requested is False


async def test_status_and_cancel_urls_on_another_host_are_not_followed():
    server = FakeHiggsfield(statuses=("completed",))
    handle = _handle(status_url="https://elsewhere.example.test/steal", cancel_url="https://elsewhere.example.test/c")
    async with factory(server)() as http:
        await HiggsfieldClient(http).wait(handle, deadline=1e9, sleep=_no_sleep)
    assert all("elsewhere" not in url for _, url in server.calls)
    assert server.count("GET", f"{API}/requests/req-1/status") == 1


# ── Output ───────────────────────────────────────────────────────────────────

async def test_downloads_are_retried_without_resubmitting():
    server = FakeHiggsfield(download_errors=2)
    output = await soul(server).generate(image_request())
    assert output.data and server.count("GET", f"{CDN}/out/image.jpg") == 3
    assert server.count("POST", f"{API}/{SOUL}") == 1


async def test_a_download_that_keeps_failing_is_billed_work_and_is_not_resubmitted():
    server = FakeHiggsfield(download_errors=10)
    with pytest.raises(EngineFailed) as failed:
        await soul(server).generate(image_request())
    assert failed.value.may_have_started is True and failed.value.retryable is False


@pytest.mark.parametrize("payload", [{}, {"images": []}, {"images": [{"url": "http://insecure.test/x.jpg"}]}])
async def test_a_completed_job_without_a_usable_output_is_never_resubmitted(payload):
    server = FakeHiggsfield(completed_payload=payload)
    with pytest.raises(EngineFailed) as failed:
        await soul(server).generate(image_request())
    assert failed.value.may_have_started is True and failed.value.retryable is False
    assert server.count("POST", f"{API}/{SOUL}") == 1


# ── Video ────────────────────────────────────────────────────────────────────

async def test_kling_text_to_video_sends_only_documented_fields_and_prices_per_second():
    server = FakeHiggsfield(kind="video")
    engine = kling(server)
    request = video_request(duration_seconds=5)
    assert engine.estimate_usd(request) == pytest.approx(0.084 * 5)
    output = await engine.generate(request)
    assert server.bodies[0] == {"prompt": "waves on rocks", "duration": 5, "aspect_ratio": "16:9", "sound": "off"}
    assert output.content_type == "video/mp4" and output.duration_seconds == 5.0
    assert output.cost_usd == pytest.approx(0.42)


def test_video_durations_are_kept_inside_what_each_model_accepts():
    engine = kling(FakeHiggsfield(kind="video"))
    assert engine.normalize(video_request(duration_seconds=1)).duration_seconds == 3
    assert engine.normalize(video_request(duration_seconds=40)).duration_seconds == 15
    wan = HiggsfieldVideoEngine("wan_3@higgsfield")
    assert wan.normalize(video_request(duration_seconds=1)).duration_seconds == 2


async def test_a_portrait_video_is_not_bent_into_the_only_verified_landscape_shape():
    server = FakeHiggsfield(kind="video")
    with pytest.raises(EngineFailed) as refused:
        await kling(server).generate(video_request(width=720, height=1280))
    assert refused.value.kind == FailureKind.UNSUPPORTED_FORMAT and server.calls == []


async def test_wan_once_its_ratios_are_verified_asks_for_the_resolution_it_prices(monkeypatch):
    monkeypatch.setenv("MEDIA_HIGGSFIELD_WAN_ASPECT_RATIOS", "16:9,9:16")
    media_settings.cache_clear()
    server = FakeHiggsfield(kind="video")
    engine = HiggsfieldVideoEngine("wan_3@higgsfield", factory(server))
    request = video_request(width=854, height=480, duration_seconds=4)
    assert engine.estimate_usd(request) == pytest.approx(0.05 * 4)
    await engine.generate(request)
    assert server.bodies[0] == {"prompt": "waves on rocks", "duration": 4, "aspect_ratio": "16:9",
                                "generate_audio": False, "resolution": "480p"}
    assert engine.estimate_usd(video_request(duration_seconds=4)) == pytest.approx(0.10 * 4)


# ── In the chain: competes on price, falls back once, respects the budget ────

class PricedImage(ImageEngine):
    """Another hosted route with a price, for the chain to compare against."""

    def __init__(self, usd: float, *, available: bool = True) -> None:
        self.usd, self.available, self.calls = usd, available, 0

    async def is_available(self, *, editing: bool = False) -> bool:
        return self.available

    def estimate_usd(self, request):
        return self.usd

    async def generate(self, request):
        self.calls += 1
        return MediaOutput(data=_jpeg(), content_type="image/jpeg", width=960, height=720, duration_seconds=None,
                           engine_seconds=1.0, cost_usd=self.usd)


def _route(engine, name: str, quality: int = 3):
    return HostedImageRoute(engine, engine_id="dev", runner_name=name, quality=quality)


def _soul_route(server):
    return HostedImageRoute(soul(server), engine_id="higgsfield_soul_2", runner_name="higgsfield", quality=3)


async def test_when_cheapest_it_runs_first():
    server, other = FakeHiggsfield(), PricedImage(0.02)
    chain = ImageEngineChain([_route(other, "other"), _soul_route(server)])
    output = await chain.generate(image_request(quality_tier="standard"))
    assert [a["engine"] for a in output.trace] == ["higgsfield_soul_2@higgsfield"] and other.calls == 0


async def test_when_dearer_it_waits_its_turn():
    server, other = FakeHiggsfield(), PricedImage(0.001)
    chain = ImageEngineChain([_soul_route(server), _route(other, "other")])
    output = await chain.generate(image_request(quality_tier="standard"))
    assert output.trace[0]["engine"] == "dev@other" and server.calls == []


async def test_configured_but_unavailable_falls_back_to_the_next_route(monkeypatch):
    monkeypatch.setenv("HIGGSFIELD_API_KEY_ID", "")
    media_settings.cache_clear()
    server, other = FakeHiggsfield(), PricedImage(0.02)
    output = await ImageEngineChain([_soul_route(server), _route(other, "other")]).generate(image_request())
    assert [(a["engine"], a["outcome"]) for a in output.trace] == [
        ("higgsfield_soul_2@higgsfield", "unavailable"), ("dev@other", "completed")]
    assert server.calls == []


async def test_a_failure_before_submission_falls_back_exactly_once_per_route():
    server, other = FakeHiggsfield(submit=httpx.Response(500)), PricedImage(0.02)
    output = await ImageEngineChain([_soul_route(server), _route(other, "other")]).generate(image_request())
    assert [a["outcome"] for a in output.trace] == ["failed", "completed"]
    assert server.count("POST", f"{API}/{SOUL}") == 1 and other.calls == 1


async def test_work_that_may_have_started_stops_the_chain_so_nothing_is_paid_twice():
    server, other = FakeHiggsfield(submit=httpx.ReadTimeout("slow")), PricedImage(0.02)
    with pytest.raises(EngineChainFailed):
        await ImageEngineChain([_soul_route(server), _route(other, "other")]).generate(image_request())
    assert other.calls == 0 and server.count("POST", f"{API}/{SOUL}") == 1


@pytest.mark.parametrize("budget, runs", [(0.0031, False), (0.0032, True), (0.01, True)])
async def test_the_reserved_budget_below_at_and_above_the_estimate(budget, runs):
    server = FakeHiggsfield()
    chain = ImageEngineChain([_soul_route(server)])
    if runs:
        output = await chain.generate(image_request(budget_usd=budget))
        assert output.cost_usd == pytest.approx(0.0032) and output.cost_usd <= budget
    else:
        with pytest.raises(EngineChainFailed) as failed:
            await chain.generate(image_request(budget_usd=budget))
        assert failed.value.trace[0].outcome == "skipped_budget" and server.calls == []


@pytest.mark.parametrize("fallback_usd, delivered", [(0.004, True), (0.02, False)])
async def test_a_fallback_runs_only_inside_the_budget(fallback_usd, delivered):
    server, other = FakeHiggsfield(submit=httpx.Response(503)), PricedImage(fallback_usd)
    chain = ImageEngineChain([_soul_route(server), _route(other, "other")])
    if delivered:
        output = await chain.generate(image_request(budget_usd=0.005))
        assert output.trace[-1]["outcome"] == "completed"
    else:
        with pytest.raises(EngineChainFailed) as failed:
            await chain.generate(image_request(budget_usd=0.005))
        assert [a.outcome for a in failed.value.trace] == ["failed", "skipped_budget"]
        assert other.calls == 0


async def test_below_the_plan_quality_floor_it_is_not_considered():
    server = FakeHiggsfield()
    with pytest.raises(EngineChainFailed) as failed:
        await ImageEngineChain([_soul_route(server)]).generate(image_request(quality_tier="premium"))
    assert failed.value.trace[0].outcome == "skipped_quality" and server.calls == []


# ── Soul 2 request schema (the CP3 422: seed outside 1..1,000,000) ───────────

def _soul_with(monkeypatch, ratios: str | None = None):
    if ratios is not None:
        monkeypatch.setenv("MEDIA_HIGGSFIELD_SOUL_ASPECT_RATIOS", ratios)
        media_settings.cache_clear()
    return higgsfield.model_for("soul_2@higgsfield")


def test_soul_2_documents_its_schema():
    model = higgsfield.model_for("soul_2@higgsfield")
    assert model.seed_range == (1, 1_000_000)
    assert model.batch_sizes == (1, 4)
    assert model.documented_ratios == ("9:16", "16:9", "4:3", "3:4", "1:1", "2:3", "3:2")
    assert model.aspect_ratios == ("4:3",)  # enabled: only 4:3 until the others are enabled deliberately
    assert "enhance_prompt" not in model.fixed  # not sent: the provider default (true) applies


@pytest.mark.parametrize("seed", [1, 500_000, 1_000_000])
def test_a_seed_inside_the_range_is_sent_unchanged(seed):
    engine = soul(FakeHiggsfield())
    assert engine.body(image_request(seed=seed))[1]["seed"] == seed


@pytest.mark.parametrize("seed", [0, 1_000_001, 1_163_474_900, 2**31 - 1])
def test_a_core_seed_outside_the_range_is_folded_into_it_deterministically(seed):
    engine = soul(FakeHiggsfield())
    sent = engine.body(image_request(seed=seed))[1]["seed"]
    assert 1 <= sent <= 1_000_000
    assert sent == engine.body(image_request(seed=seed))[1]["seed"]  # same Core seed, same provider seed


async def test_the_cp3_seed_now_reaches_the_provider_inside_the_range_and_is_recorded():
    server = FakeHiggsfield()
    output = await soul(server).generate(image_request(seed=1_163_474_900))
    assert 1 <= server.bodies[0]["seed"] <= 1_000_000
    assert output.metrics["seed"] == server.bodies[0]["seed"]
    assert server.count("POST", f"{API}/{SOUL}") == 1


@pytest.mark.parametrize("seed", [0, 1_000_001, 1_163_474_900, -5, "7", True, 1.5])
def test_a_body_with_an_invalid_seed_is_refused_before_sending(seed):
    model = higgsfield.model_for("soul_2@higgsfield")
    body = {"prompt": "x", "aspect_ratio": "4:3", "resolution": "720p", "batch_size": 1, "seed": seed}
    with pytest.raises(EngineFailed) as refused:
        higgsfield.validate_body(model, body)
    assert refused.value.kind == FailureKind.UNSUPPORTED_FORMAT and "seed" in refused.value.reason


@pytest.mark.parametrize("batch", [1, 4])
def test_documented_batch_sizes_are_accepted(batch):
    model = higgsfield.model_for("soul_2@higgsfield")
    higgsfield.validate_body(model, {"prompt": "x", "aspect_ratio": "4:3", "batch_size": batch, "seed": 1})


@pytest.mark.parametrize("batch", [0, 2, 3, 5, 8])
def test_other_batch_sizes_are_refused(batch):
    model = higgsfield.model_for("soul_2@higgsfield")
    with pytest.raises(EngineFailed) as refused:
        higgsfield.validate_body(model, {"prompt": "x", "aspect_ratio": "4:3", "batch_size": batch, "seed": 1})
    assert "batch_size" in refused.value.reason


def test_the_engine_always_asks_for_one_image():
    assert soul(FakeHiggsfield()).body(image_request())[1]["batch_size"] == 1


@pytest.mark.parametrize("ratio", ["9:16", "16:9", "4:3", "3:4", "1:1", "2:3", "3:2"])
def test_every_documented_ratio_passes_the_schema(ratio):
    model = higgsfield.model_for("soul_2@higgsfield")
    higgsfield.validate_body(model, {"prompt": "x", "aspect_ratio": ratio, "batch_size": 1, "seed": 1})


@pytest.mark.parametrize("ratio", ["21:9", "4:5", "5:4", "auto", "", None])
def test_an_undocumented_ratio_is_refused_by_the_schema(ratio):
    model = higgsfield.model_for("soul_2@higgsfield")
    with pytest.raises(EngineFailed) as refused:
        higgsfield.validate_body(model, {"prompt": "x", "aspect_ratio": ratio, "batch_size": 1, "seed": 1})
    assert "aspect_ratio" in refused.value.reason


async def test_a_misconfigured_ratio_is_refused_before_anything_is_sent(monkeypatch):
    _soul_with(monkeypatch, "4:3,21:9")
    server = FakeHiggsfield()
    with pytest.raises(EngineFailed) as refused:
        await soul(server).generate(image_request(width=1344, height=576))
    assert "aspect_ratio" in refused.value.reason and server.calls == []


def test_an_invalid_resolution_is_refused_by_the_schema():
    model = higgsfield.model_for("soul_2@higgsfield")
    with pytest.raises(EngineFailed):
        higgsfield.validate_body(model, {"prompt": "x", "aspect_ratio": "4:3", "resolution": "4k",
                                         "batch_size": 1, "seed": 1})


@pytest.mark.parametrize("ratio, spec", [("9:16", (9, 16)), ("16:9", (16, 9)), ("4:3", (4, 3)), ("3:4", (3, 4)),
                                         ("1:1", (1, 1)), ("2:3", (2, 3)), ("3:2", (3, 2))])
def test_each_documented_ratio_through_the_real_pipeline_sizing(monkeypatch, ratio, spec):
    """formats -> image_dimensions_for (plan sizing) -> Soul 2 body: the ratio the person asked for is the one sent."""
    from src.media import formats
    from src.services import media_service

    _soul_with(monkeypatch, ",".join(higgsfield.SOUL_2_ASPECT_RATIOS))
    entitlement = plans.get_media_entitlement("pro")
    width, height = media_service.image_dimensions_for(formats.FormatSpec(*spec), entitlement)
    request = image_request(width=width, height=height, quality_tier=entitlement.image_quality_tier)
    path, body = soul(FakeHiggsfield()).body(request)
    assert body["aspect_ratio"] == ratio
    # The plan caps the long side at 1024. Only a square keeps its short side above 900, so 1:1 is asked
    # (and priced) at 1080p; every other documented ratio at 720p. Recorded here before any is enabled.
    expected = ("1080p", 0.0057) if ratio == "1:1" else ("720p", 0.0032)
    assert (body["resolution"], soul(FakeHiggsfield()).estimate_usd(request)) == (expected[0], pytest.approx(expected[1]))


# ── Other providers keep their own seeds ─────────────────────────────────────

def test_the_core_seed_generator_is_unchanged():
    import inspect

    from src.services import media_service

    assert "secrets.randbelow(2**31)" in inspect.getsource(media_service._seed)
    assert all(0 <= media_service._seed() < 2**31 for _ in range(200))


async def test_other_hosted_engines_still_send_the_core_seed_as_is(monkeypatch):
    from src.media.hosted import FalImageEngine

    monkeypatch.setenv("FAL_KEY", "fal-test")
    media_settings.cache_clear()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"images": [{"url": "https://fal.example.test/o.jpg", "width": 1024,
                                                         "height": 768}]})
        return httpx.Response(200, content=_jpeg(), headers={"content-type": "image/jpeg"})

    await FalImageEngine(factory(handler)).generate(image_request(seed=1_163_474_900))
    assert seen["body"]["seed"] == 1_163_474_900


def test_higgsfield_video_models_send_no_seed():
    for route in ("wan_3@higgsfield", "kling_3_std@higgsfield", "grok_imagine_video_1_5@higgsfield"):
        assert higgsfield.model_for(route).seed_range is None
    assert "seed" not in kling(FakeHiggsfield(kind="video")).body(video_request())[1]
