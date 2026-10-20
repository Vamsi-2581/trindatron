"""The xAI adapters speak the Imagine API and price exactly what the call reports.

Every call goes through httpx.MockTransport: nothing leaves the machine and nothing is spent.
"""
from __future__ import annotations

import base64
import io
import json

import httpx
import pytest
from PIL import Image

from src.media.engines import EngineFailed, EngineUnavailable, ImageRequest, VideoRequest
from src.media.settings import media_settings
from src.media.xai import USD_PER_TICK, XaiImageEngine, XaiVideoEngine

MP4 = b"\x00\x00\x00\x18ftypmp42fake"


def _png(width: int = 768, height: int = 1365) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-test")
    monkeypatch.setenv("MEDIA_XAI_POLL_INTERVAL_S", "0")
    media_settings.cache_clear()
    yield
    media_settings.cache_clear()


def factory(handler):
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def image_request(**overrides) -> ImageRequest:
    values = dict(prompt="a lighthouse at dusk", width=576, height=1024, seed=7, steps=8)
    values.update(overrides)
    return ImageRequest(**values)


def video_request(**overrides) -> VideoRequest:
    values = dict(prompt="waves on rocks", width=854, height=480, duration_seconds=5, fps=16, seed=7)
    values.update(overrides)
    return VideoRequest(**values)


async def test_image_generation_asks_for_the_nearest_shape_and_prices_the_reported_cost():
    seen = {}
    png = _png()

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"], seen["body"] = str(request.url), request.headers["authorization"], json.loads(request.content)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png).decode(), "mime_type": "image/png"}],
                                         "usage": {"cost_in_usd_ticks": 200_000_000}})

    output = await XaiImageEngine(factory(handler)).generate(image_request())

    assert seen["url"].endswith("/v1/images/generations") and seen["auth"] == "Bearer xai-test"
    assert seen["body"] == {"model": "grok-imagine-image", "prompt": "a lighthouse at dusk", "n": 1,
                            "aspect_ratio": "9:16", "resolution": "1k", "response_format": "b64_json"}
    assert output.data == png and output.content_type == "image/png"
    assert (output.width, output.height) == (768, 1365)
    assert output.cost_usd == pytest.approx(200_000_000 * USD_PER_TICK) == pytest.approx(0.02)


async def test_image_editing_sends_the_source_as_a_data_uri_to_the_edits_endpoint():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["body"] = str(request.url), json.loads(request.content)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(_png(512, 512)).decode()}]})

    engine = XaiImageEngine(factory(handler))
    request = image_request(width=1024, height=1024, source=b"\x89PNGsource", source_content_type="image/png")
    output = await engine.generate(request)

    assert seen["url"].endswith("/v1/images/edits")
    assert seen["body"]["image"]["url"].startswith("data:image/png;base64,")
    # No usage in the response: the published price, including the input image.
    assert output.cost_usd == pytest.approx(engine.estimate_usd(request)) == pytest.approx(0.022)


def test_image_estimate_follows_resolution():
    engine = XaiImageEngine()
    assert engine.estimate_usd(image_request()) == pytest.approx(0.02)
    assert engine.estimate_usd(image_request(width=2048, height=2048)) == pytest.approx(0.04)


@pytest.mark.parametrize("status,retryable", [(400, False), (429, True), (500, True)])
async def test_image_failures_are_classified_and_never_carry_provider_text(status, retryable):
    engine = XaiImageEngine(factory(lambda request: httpx.Response(status, json={"error": "secret detail"})))
    with pytest.raises(EngineFailed) as caught:
        await engine.generate(image_request())
    assert caught.value.retryable is retryable and "secret" not in caught.value.reason


async def test_without_a_key_the_engine_is_unavailable():
    import os

    os.environ["XAI_API_KEY"] = ""
    media_settings.cache_clear()
    engine = XaiImageEngine(factory(lambda request: httpx.Response(200, json={})))
    assert await engine.is_available() is False
    with pytest.raises(EngineUnavailable):
        await engine.generate(image_request())


async def test_availability_requires_the_configured_model_to_be_listed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": [{"id": "grok-imagine-image"}, {"id": "grok-imagine-video"}]})

    assert await XaiImageEngine(factory(handler)).is_available() is True
    assert await XaiVideoEngine(factory(handler)).is_available(from_image=True) is True
    assert await XaiImageEngine(factory(lambda r: httpx.Response(200, json={"models": []}))).is_available() is False


def test_video_normalizes_to_what_the_api_makes():
    engine = XaiVideoEngine()
    normalized = engine.normalize(video_request(width=1080, height=1920, duration_seconds=40))
    assert normalized.duration_seconds == 15 and (normalized.width, normalized.height) == (720, 1280)
    square = engine.normalize(video_request(width=480, height=480, duration_seconds=0))
    assert square.duration_seconds == 1 and (square.width, square.height) == (480, 480)
    assert engine.estimate_usd(engine.normalize(video_request())) == pytest.approx(0.25)
    assert engine.estimate_usd(engine.normalize(video_request(width=1280, height=720))) == pytest.approx(0.35)


async def test_video_submits_polls_downloads_and_prices_the_reported_cost():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            body = json.loads(request.content)
            assert body == {"model": "grok-imagine-video", "prompt": "waves on rocks", "duration": 5,
                            "aspect_ratio": "16:9", "resolution": "480p"}
            return httpx.Response(200, json={"request_id": "req-1"})
        if request.url.path == "/v1/videos/req-1":
            if sum(1 for method, path in calls if path == "/v1/videos/req-1") == 1:
                return httpx.Response(200, json={"status": "pending", "progress": 40})
            return httpx.Response(200, json={"status": "done", "video": {"url": "https://files.example/v.mp4", "duration": 5,
                                                                          "respect_moderation": True},
                                             "usage": {"cost_in_usd_ticks": 2_500_000_000}})
        return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})

    output = await XaiVideoEngine(factory(handler)).generate(video_request())

    assert output.data == MP4 and output.content_type == "video/mp4" and output.duration_seconds == 5
    assert output.cost_usd == pytest.approx(0.25)
    assert [path for _, path in calls] == ["/v1/videos/generations", "/v1/videos/req-1", "/v1/videos/req-1", "/v.mp4"]


async def test_image_to_video_sends_the_first_frame():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"request_id": "r"})
        if request.url.path == "/v1/videos/r":
            return httpx.Response(200, json={"status": "done", "video": {"url": "https://f.example/v.mp4", "duration": 3,
                                                                          "respect_moderation": True}})
        return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})

    await XaiVideoEngine(factory(handler)).generate(video_request(duration_seconds=3, source=b"jpg", source_content_type="image/jpeg"))
    assert seen["body"]["image"]["url"].startswith("data:image/jpeg;base64,")


@pytest.mark.parametrize("code,retryable", [("service_unavailable", True), ("invalid_argument", False)])
async def test_a_failed_video_is_classified_by_its_error_code(code, retryable):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"request_id": "r"})
        return httpx.Response(200, json={"status": "failed", "error": {"code": code, "message": "secret"}})

    with pytest.raises(EngineFailed) as caught:
        await XaiVideoEngine(factory(handler)).generate(video_request())
    assert caught.value.retryable is retryable and "secret" not in caught.value.reason
