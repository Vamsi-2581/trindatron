"""Hosted engines speak each provider's contract and price exactly what they ran.

Every call goes through httpx.MockTransport: no request leaves the machine and
nothing is spent. What is pinned here is what the product depends on — the
request each provider receives, how the returned usage becomes cost, which
failures are retried, and what an engine promises before it runs.
"""
from __future__ import annotations

import base64
import json

import httpx
import pytest

from src.media.engines import EngineFailed, EngineUnavailable, ImageRequest, VideoRequest
from src.media.hosted import FalImageEngine, FalVideoEngine, OpenAIImageEngine, VeoVideoEngine
from src.media.settings import media_settings
from src.media.vision import GeminiVisionEngine

JPEG = b"\xff\xd8\xff\xe0fake-jpeg"
MP4 = b"\x00\x00\x00\x18ftypmp42fake"


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "fal-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GEMINI_API_KEY", "gm-test")
    monkeypatch.setenv("MEDIA_VEO_POLL_INTERVAL_S", "0")
    monkeypatch.setenv("MEDIA_FAL_POLL_INTERVAL_S", "0")
    media_settings.cache_clear()
    yield
    media_settings.cache_clear()


def factory(handler):
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def image_request(**overrides) -> ImageRequest:
    values = dict(prompt="a lighthouse at dusk", width=1024, height=1024, seed=7, steps=8, quality_tier="standard")
    values.update(overrides)
    return ImageRequest(**values)


def video_request(**overrides) -> VideoRequest:
    values = dict(prompt="waves on rocks", width=854, height=480, duration_seconds=3, fps=16, seed=7)
    values.update(overrides)
    return VideoRequest(**values)


# ── OpenAI Images ───────────────────────────────────────────────────────────

async def test_openai_image_sends_the_request_and_prices_the_returned_usage():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "data": [{"b64_json": base64.b64encode(JPEG).decode()}],
            "usage": {"input_tokens": 20, "output_tokens": 272,
                      "input_tokens_details": {"text_tokens": 20, "image_tokens": 0}},
        })

    output = await OpenAIImageEngine(factory(handler)).generate(image_request(width=1024, height=768))

    assert seen["url"].endswith("/images/generations") and seen["auth"] == "Bearer sk-test"
    assert seen["body"] == {"model": "gpt-image-1-mini", "prompt": "a lighthouse at dusk", "size": "1536x1024",
                            "quality": "low", "n": 1, "output_format": "jpeg", "output_compression": 90}
    assert output.data == JPEG and (output.width, output.height) == (1536, 1024)
    assert output.cost_usd == pytest.approx((20 * 2.00 + 272 * 8.00) / 1_000_000)


def test_openai_image_estimate_covers_the_longest_prompt_and_follows_quality():
    engine = OpenAIImageEngine()
    low = engine.estimate_usd(image_request())
    assert low == pytest.approx((800 * 2.00 + 272 * 8.00) / 1_000_000)
    assert engine.estimate_usd(image_request(quality_tier="premium")) == pytest.approx((800 * 2.00 + 1056 * 8.00) / 1_000_000)


@pytest.mark.parametrize("status,retryable", [(400, False), (429, True), (500, True), (503, True)])
async def test_openai_image_failures_are_classified_for_the_retry_policy(status, retryable):
    engine = OpenAIImageEngine(factory(lambda request: httpx.Response(status, json={"error": {"message": "secret detail"}})))
    with pytest.raises(EngineFailed) as caught:
        await engine.generate(image_request())
    assert caught.value.retryable is retryable
    assert "secret detail" not in caught.value.reason


async def test_openai_image_is_unavailable_without_a_key_or_for_editing(monkeypatch):
    engine = OpenAIImageEngine(factory(lambda request: httpx.Response(200, json={"id": "gpt-image-1-mini"})))
    assert await engine.is_available() is True
    assert await engine.is_available(editing=True) is False
    monkeypatch.setenv("OPENAI_API_KEY", "")
    media_settings.cache_clear()
    assert await engine.is_available() is False
    with pytest.raises(EngineUnavailable):
        await engine.generate(image_request())


# ── fal: Z-Image Turbo ──────────────────────────────────────────────────────

async def test_fal_image_runs_z_image_and_bills_per_megapixel():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "fal.run":
            seen["body"] = json.loads(request.content)
            seen["auth"] = request.headers["authorization"]
            return httpx.Response(200, json={"images": [{"url": "https://cdn.test/out.jpg", "width": 1024, "height": 768}]})
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    engine = FalImageEngine(factory(handler))
    output = await engine.generate(image_request(width=1024, height=768, steps=12))

    assert seen["auth"] == "Key fal-test"
    assert seen["body"]["image_size"] == {"width": 1024, "height": 768}
    assert seen["body"]["num_inference_steps"] == 8  # the model's maximum
    assert seen["body"]["enable_safety_checker"] is True
    assert output.data == JPEG
    assert output.cost_usd == pytest.approx(1024 * 768 / 1_000_000 * 0.005)
    assert engine.estimate_usd(image_request(width=1024, height=1024)) == pytest.approx(1.048576 * 0.005)


# ── fal: FLUX.1-schnell (CP18, same FalImageEngine, its own endpoint/price) ─

async def test_fal_image_with_an_explicit_endpoint_and_price_runs_a_different_model():
    """FLUX.1-schnell@fal reuses FalImageEngine with its own endpoint/price — Z-Image Turbo's own
    route (no arguments) must keep using media_settings()'s defaults, proven by the other test above
    still passing unmodified."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "fal.run":
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"images": [{"url": "https://cdn.test/out.jpg", "width": 1024, "height": 1024}]})
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    engine = FalImageEngine(factory(handler), endpoint="fal-ai/flux-1/schnell", usd_per_megapixel=0.003)
    output = await engine.generate(image_request(width=1024, height=1024))

    assert seen["url"].endswith("fal-ai/flux-1/schnell")
    assert output.cost_usd == pytest.approx(1024 * 1024 / 1_000_000 * 0.003)
    assert engine.estimate_usd(image_request(width=1024, height=1024)) == pytest.approx(1.048576 * 0.003)


async def test_fal_image_without_an_endpoint_configured_is_unavailable_and_refuses():
    engine = FalImageEngine(endpoint="")  # mirrors MEDIA_FAL_FLUX_SCHNELL_ENDPOINT unset
    assert await engine.is_available() is False
    with pytest.raises(EngineUnavailable):
        await engine.generate(image_request())


def test_flux_schnell_route_is_additional_and_z_image_turbo_route_is_unchanged(monkeypatch):
    """CP18: registering flux1_schnell@fal must not alter what z_image_turbo@fal resolves to."""
    from src.media.registry import HOSTED_IMAGE_ROUTES, build_image_route

    assert "flux1_schnell@fal" in HOSTED_IMAGE_ROUTES and "z_image_turbo@fal" in HOSTED_IMAGE_ROUTES

    z_image = build_image_route("z_image_turbo@fal")
    assert z_image.engine_id == "z_image_turbo" and z_image.runner_name == "fal" and z_image.quality == 3

    monkeypatch.setenv("MEDIA_FAL_FLUX_SCHNELL_ENDPOINT", "fal-ai/flux-1/schnell")
    media_settings.cache_clear()
    flux = build_image_route("flux1_schnell@fal")
    assert flux.engine_id == "flux1_schnell" and flux.runner_name == "fal" and flux.quality == 4
    # Same engine, disjoint fal endpoint from Z-Image Turbo's own default.
    assert flux.engine.estimate_usd(image_request(width=1024, height=1024)) != z_image.engine.estimate_usd(
        image_request(width=1024, height=1024))


# ── fal: Wan 2.2 ────────────────────────────────────────────────────────────

def test_fal_video_normalizes_to_frames_at_16fps_and_prices_by_resolution():
    engine = FalVideoEngine()
    three = engine.normalize(video_request(duration_seconds=3))
    assert (three.fps, three.frames, three.duration_seconds) == (16, 49, 3)
    assert engine.estimate_usd(three) == pytest.approx(0.04 * 49 / 16)
    hd = engine.normalize(video_request(width=1280, height=720, duration_seconds=5))
    assert engine.estimate_usd(hd) == pytest.approx(0.08 * 81 / 16)
    # Never below the model's minimum clip or above its maximum.
    assert engine.normalize(video_request(duration_seconds=0)).frames == 17
    assert engine.normalize(video_request(duration_seconds=60)).frames == 161


async def test_fal_video_submits_to_the_queue_polls_and_downloads():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["num_frames"] == 49 and body["resolution"] == "480p" and body["aspect_ratio"] == "16:9"
            return httpx.Response(200, json={"request_id": "r1", "status_url": "https://queue.fal.run/s/r1",
                                             "response_url": "https://queue.fal.run/r/r1"})
        if request.url.path == "/s/r1":
            done = sum(1 for call in calls if call[1] == "/s/r1") > 1
            return httpx.Response(200, json={"status": "COMPLETED" if done else "IN_PROGRESS"})
        if request.url.path == "/r/r1":
            return httpx.Response(200, json={"video": {"url": "https://cdn.test/clip.mp4"}})
        return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})

    output = await FalVideoEngine(factory(handler)).generate(video_request())
    assert output.data == MP4 and output.content_type == "video/mp4"
    assert output.cost_usd == pytest.approx(0.04 * 49 / 16)


# ── fal: Wan 2.2 Animate Move (character_animate, CP46) ──────────────────────

from src.media.engines import JobCancelled, ProviderLink  # noqa: E402
from src.media.hosted import FalWanAnimateEngine  # noqa: E402
from src.media.operations import OperationInput, OperationRequest as _OpReq, ResolvedInput  # noqa: E402

CHARACTER_JPEG = b"\xff\xd8\xff\xe0fake-character-jpeg"


def animate_request(**overrides) -> _OpReq:
    values = {"resolution": "480p"}
    values.update(overrides.pop("parameters", {}))
    kwargs = dict(operation="character_animate",
                 inputs=(OperationInput(role="character", artifact_id="a1"),
                         OperationInput(role="driving_video", artifact_id="a2")),
                 parameters=values)
    kwargs.update(overrides)
    return _OpReq(**kwargs)


def animate_resolved(driving_data: bytes = MP4) -> dict:
    return {"character": ResolvedInput(data=CHARACTER_JPEG, content_type="image/jpeg"),
            "driving_video": ResolvedInput(data=driving_data, content_type="video/mp4")}


def _fal_animate_engine(client_factory) -> FalWanAnimateEngine:
    return FalWanAnimateEngine(client_factory)


def test_fal_animate_billable_seconds_uses_the_real_frame_count_not_duration_times_fps():
    engine = FalWanAnimateEngine()
    # tiny_mp4() (test_media_runners.py) encodes only mvhd+tkhd, no stts sample table, so
    # video_frame_count() correctly returns None on it and _billable_seconds() falls to its
    # documented last-resort floor (3s @ 24fps) — exercised precisely by the next test. What this
    # test pins is the DOCUMENTED formula itself against CP40's real, independently-measured
    # frame count (119, media/runners/base.video_frame_count(), confirmed in CP46's own report):
    # billed video-seconds = total_frames / 16, kept as an exact fraction (fal's own worked
    # example: 300/16 = 18.75, not rounded).
    assert 119 / 16 == pytest.approx(7.4375)
    assert engine._price_per_second("480p") * (119 / 16) == pytest.approx(0.2975)


def test_fal_animate_billable_seconds_prefers_stored_duration_over_the_bare_default():
    engine = FalWanAnimateEngine()
    assert engine._billable_seconds({"_input_durations": {"driving_video": 4.963}}) == pytest.approx(4.963 * 24 / 16)
    assert engine._billable_seconds({}) == pytest.approx(3 * 24 / 16)  # the documented last-resort floor


async def test_fal_animate_success_sends_data_url_inputs_and_downloads_the_video():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith("/animate/move"):
            body = json.loads(request.content)
            assert body["image_url"].startswith("data:image/jpeg;base64,")
            assert body["video_url"].startswith("data:video/mp4;base64,")
            assert base64.b64decode(body["image_url"].split(",", 1)[1]) == CHARACTER_JPEG
            assert body["resolution"] == "480p"
            return httpx.Response(200, json={"request_id": "r1", "status_url": "https://queue.fal.run/s/r1",
                                             "response_url": "https://queue.fal.run/r/r1",
                                             "cancel_url": "https://queue.fal.run/c/r1"})
        if request.url.path == "/s/r1":
            done = sum(1 for c in calls if c[1] == "/s/r1") > 1
            return httpx.Response(200, json={"status": "COMPLETED" if done else "IN_PROGRESS"})
        if request.url.path == "/r/r1":
            return httpx.Response(200, json={"video": {"url": "https://cdn.test/animate-out.mp4"}}, )
        return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})

    output = await _fal_animate_engine(factory(handler)).run(animate_request(), animate_resolved())
    assert output.data == MP4 and output.content_type == "video/mp4"
    assert output.cost_usd == pytest.approx(0.04 * 3 * 24 / 16)  # no real frame table in MP4 -> 3s/24fps floor
    assert output.metrics["provider"] == "fal" and output.metrics["request_id"] == "r1"


async def test_fal_animate_persists_request_id_via_the_provider_link_before_polling():
    submitted_handles = []
    link = ProviderLink(on_submit=submitted_handles.append)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"request_id": "r2", "status_url": "https://queue.fal.run/s/r2",
                                             "response_url": "https://queue.fal.run/r/r2"})
        if request.url.path == "/s/r2":
            return httpx.Response(200, json={"status": "COMPLETED"})
        if request.url.path == "/r/r2":
            return httpx.Response(200, json={"video": {"url": "https://cdn.test/out.mp4"}})
        return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})

    await _fal_animate_engine(factory(handler)).run(animate_request(link=link), animate_resolved())
    assert link.handle is not None and link.handle.request_id == "r2" and link.handle.provider == "fal"
    assert len(submitted_handles) == 1


async def test_fal_animate_a_completed_status_with_an_error_field_is_treated_as_failure():
    """fal's own documented quirk: COMPLETED does not always mean success."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"request_id": "r3", "status_url": "https://queue.fal.run/s/r3",
                                             "response_url": "https://queue.fal.run/r/r3"})
        if request.url.path == "/s/r3":
            return httpx.Response(200, json={"status": "COMPLETED"})
        if request.url.path == "/r/r3":
            return httpx.Response(200, json={"error": "content policy violation", "error_type": "CONTENT_POLICY",
                                             "api_key": "sk-should-never-appear"})
        return httpx.Response(404)

    with pytest.raises(EngineFailed) as caught:
        await _fal_animate_engine(factory(handler)).run(animate_request(), animate_resolved())
    assert "r3" in str(caught.value)
    assert caught.value.retryable is False  # never silently falls back to Higgsfield after this
    assert caught.value.provider_detail["error"] == "content policy violation"
    assert caught.value.provider_detail["error_type"] == "CONTENT_POLICY"
    assert "sk-should-never-appear" not in str(caught.value)
    assert "sk-should-never-appear" not in json.dumps(caught.value.provider_detail)


async def test_fal_animate_an_unrecognized_status_is_not_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"request_id": "r4", "status_url": "https://queue.fal.run/s/r4",
                                             "response_url": "https://queue.fal.run/r/r4"})
        return httpx.Response(200, json={"status": "SOMETHING_UNEXPECTED"})

    with pytest.raises(EngineFailed) as caught:
        await _fal_animate_engine(factory(handler)).run(animate_request(), animate_resolved())
    assert caught.value.retryable is False
    assert caught.value.may_have_started is True


async def test_fal_animate_cancellation_during_polling():
    link = ProviderLink()
    link.cancel_requested = True
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith("/animate/move"):
            return httpx.Response(200, json={"request_id": "r5", "status_url": "https://queue.fal.run/s/r5",
                                             "response_url": "https://queue.fal.run/r/r5",
                                             "cancel_url": "https://queue.fal.run/c/r5"})
        if request.method == "PUT" and request.url.path == "/c/r5":
            return httpx.Response(202, json={"status": "CANCELLATION_REQUESTED"})
        return httpx.Response(500)  # status/result should never be reached once cancelled

    with pytest.raises(JobCancelled):
        await _fal_animate_engine(factory(handler)).run(animate_request(link=link), animate_resolved())
    assert ("PUT", "/c/r5") in calls
    assert ("GET", "/s/r5") not in calls


async def test_fal_animate_real_cost_exceeding_the_reservation_is_refused_before_any_network_call():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json={})

    engine = _fal_animate_engine(factory(handler))
    request = animate_request(budget_usd=0.01)  # far under even the cheapest real estimate
    with pytest.raises(EngineFailed) as caught:
        await engine.run(request, animate_resolved())
    assert "exceeds" in str(caught.value)
    assert caught.value.retryable is True  # nothing was sent yet — safe to let the chain try Higgsfield
    assert calls == []  # confirms it really is before any network call


async def test_fal_animate_is_available_false_without_a_key(monkeypatch):
    monkeypatch.setenv("FAL_KEY", "")
    media_settings.cache_clear()
    try:
        assert await FalWanAnimateEngine().is_available() is False
    finally:
        media_settings.cache_clear()


# ── Veo ─────────────────────────────────────────────────────────────────────

def test_veo_uses_its_own_durations_and_720p():
    engine = VeoVideoEngine()
    assert engine.normalize(video_request(duration_seconds=3)).duration_seconds == 4
    assert engine.normalize(video_request(duration_seconds=5)).duration_seconds == 6
    assert engine.normalize(video_request(duration_seconds=30)).duration_seconds == 8
    portrait = engine.normalize(video_request(width=480, height=854))
    assert (portrait.width, portrait.height) == (720, 1280)
    assert engine.estimate_usd(engine.normalize(video_request(duration_seconds=4))) == pytest.approx(0.20)


async def test_veo_runs_a_long_operation_and_downloads_with_the_key():
    polls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "gm-test"
        if request.method == "POST":
            body = json.loads(request.content)
            assert request.url.path.endswith(":predictLongRunning")
            assert body["parameters"] == {"aspectRatio": "16:9", "resolution": "720p", "durationSeconds": 4,
                                          "personGeneration": "allow_all"}
            return httpx.Response(200, json={"name": "models/veo/operations/op1"})
        if request.url.path.endswith("/operations/op1"):
            polls["count"] += 1
            if polls["count"] < 2:
                return httpx.Response(200, json={"name": "op1", "done": False})
            return httpx.Response(200, json={"done": True, "response": {"generateVideoResponse": {
                "generatedSamples": [{"video": {"uri": "https://generativelanguage.googleapis.com/v1beta/files/v1:download"}}]}}})
        return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})

    output = await VeoVideoEngine(factory(handler)).generate(video_request(duration_seconds=3))
    assert output.data == MP4 and output.duration_seconds == 4.0
    assert output.cost_usd == pytest.approx(0.20)


async def test_veo_filtered_output_is_a_definitive_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"name": "models/veo/operations/op2", "done": True,
                                             "response": {"generateVideoResponse": {"raiMediaFilteredCount": 1}}})
        return httpx.Response(404)

    with pytest.raises(EngineFailed) as caught:
        await VeoVideoEngine(factory(handler)).generate(video_request())
    assert caught.value.retryable is False


# ── Gemini vision ───────────────────────────────────────────────────────────

async def test_vision_sends_the_image_inline_and_prices_thinking_as_output():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["url"] = str(request.url)
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "A photo of a red bicycle leaning on a white wall."}]}}],
            "usageMetadata": {"promptTokenCount": 1300, "candidatesTokenCount": 40, "thoughtsTokenCount": 10},
        })

    result = await GeminiVisionEngine(factory(handler)).describe(JPEG, "image/jpeg")

    part = seen["body"]["contents"][0]["parts"][0]["inline_data"]
    assert seen["url"].endswith("/models/gemini-3.1-flash-lite:generateContent")
    assert part["mime_type"] == "image/jpeg" and base64.b64decode(part["data"]) == JPEG
    assert result.text.startswith("A photo of a red bicycle")
    assert result.cost_usd == pytest.approx((1300 * 0.25 + 50 * 1.50) / 1_000_000)
    assert GeminiVisionEngine().estimate_usd() == pytest.approx(((1120 + 300) * 0.25 + 900 * 1.50) / 1_000_000)


async def test_vision_refuses_what_it_cannot_read_and_an_empty_answer():
    engine = GeminiVisionEngine(factory(lambda request: httpx.Response(200, json={"candidates": []})))
    with pytest.raises(EngineFailed):
        await engine.describe(b"%PDF-1.4", "application/pdf")
    with pytest.raises(EngineFailed):
        await engine.describe(JPEG, "image/jpeg")
