"""The self-hosted engine adapter, against a scripted ComfyUI server.

No network and no GPU: httpx.MockTransport plays ComfyUI's HTTP API, so these
tests pin the protocol (upload → prompt → history → view) and the rules around
it (placeholders, errors, retryability, timeouts).
"""
from __future__ import annotations

import json

import httpx
import pytest

from src.media.comfyui import ComfyUIImageEngine, ComfyUIVideoEngine, fill_workflow
from src.media.engines import EngineFailed, EngineUnavailable, ImageRequest, VideoRequest
from src.media.settings import MediaSettings

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64

WORKFLOW = {
    "3": {"class_type": "KSampler", "inputs": {"seed": "{{seed}}", "steps": "{{steps}}"}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}"}},
    "5": {"class_type": "EmptyLatentImage", "inputs": {"width": "{{width}}", "height": "{{height}}"}},
    "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "trindatron"}},
}


def _settings(tmp_path, **extra) -> MediaSettings:
    image = tmp_path / "image.json"
    image.write_text(json.dumps(WORKFLOW), encoding="utf-8")
    video = tmp_path / "video.json"
    video.write_text(json.dumps({"1": {"inputs": {"frames": "{{frames}}", "image": "{{input_image}}", "text": "{{prompt}}"}}}),
                     encoding="utf-8")
    values = dict(media_comfyui_url="http://engine.internal:8188", media_comfyui_image_workflow=str(image),
                  media_comfyui_image_edit_workflow=str(image), media_comfyui_video_workflow=str(video),
                  media_comfyui_image_to_video_workflow=str(video), media_comfyui_poll_interval_s=0.0,
                  media_image_timeout_s=5, media_video_timeout_s=5)
    values.update(extra)
    return MediaSettings(_env_file=None, **values)


class Engine:
    """A scripted ComfyUI. Records what TRINDATRON sent."""

    def __init__(self, *, history_rounds: int = 1, prompt_status: int = 200, error: bool = False,
                 output_key: str = "images", content_type: str = "image/png") -> None:
        self.submitted: dict | None = None
        self.uploaded: bytes | None = None
        self.history_calls = 0
        self.history_rounds = history_rounds
        self.prompt_status = prompt_status
        self.error = error
        self.output_key = output_key
        self.content_type = content_type
        self.auth: str | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.auth = request.headers.get("authorization")
        path = request.url.path
        if path == "/system_stats":
            return httpx.Response(200, json={"system": {}})
        if path == "/upload/image":
            self.uploaded = request.content
            return httpx.Response(200, json={"name": "source-upload.png"})
        if path == "/prompt":
            self.submitted = json.loads(request.content)["prompt"]
            return httpx.Response(self.prompt_status, json={"prompt_id": "p-1"})
        if path == "/history/p-1":
            self.history_calls += 1
            if self.history_calls < self.history_rounds:
                return httpx.Response(200, json={})
            if self.error:
                return httpx.Response(200, json={"p-1": {"status": {"status_str": "error"}, "outputs": {}}})
            return httpx.Response(200, json={"p-1": {"status": {"status_str": "success"}, "outputs": {
                "9": {self.output_key: [{"filename": "trindatron_0001.png", "subfolder": "", "type": "output"}]}}}})
        if path == "/view":
            return httpx.Response(200, content=PNG, headers={"content-type": self.content_type})
        return httpx.Response(404)


def test_placeholders_are_replaced_as_values_not_strings():
    filled = fill_workflow(WORKFLOW, {"seed": 7, "steps": 8, "prompt": "a lighthouse", "width": 1024, "height": 768})
    assert filled["3"]["inputs"] == {"seed": 7, "steps": 8}
    assert filled["6"]["inputs"]["text"] == "a lighthouse"
    assert filled["5"]["inputs"] == {"width": 1024, "height": 768}
    # The template itself is never mutated.
    assert WORKFLOW["3"]["inputs"]["seed"] == "{{seed}}"


async def test_generates_an_image_through_the_comfyui_protocol(tmp_path):
    server = Engine(history_rounds=3)
    engine = ComfyUIImageEngine(_settings(tmp_path, media_comfyui_token="t0k"), transport=httpx.MockTransport(server))

    assert await engine.is_available() is True
    output = await engine.generate(ImageRequest(prompt="a lighthouse at dusk", width=1024, height=1024, seed=42, steps=8))

    assert output.data == PNG
    assert output.content_type == "image/png"
    assert output.engine_seconds >= 0
    assert server.submitted["6"]["inputs"]["text"] == "a lighthouse at dusk"
    assert server.submitted["3"]["inputs"]["seed"] == 42
    assert server.history_calls == 3
    assert server.auth == "Bearer t0k"


async def test_editing_uploads_the_source_first(tmp_path):
    server = Engine()
    engine = ComfyUIImageEngine(_settings(tmp_path), transport=httpx.MockTransport(server))
    await engine.generate(ImageRequest(prompt="make it night", width=512, height=512, seed=1, steps=8,
                                       source=PNG, source_content_type="image/png"))
    assert server.uploaded is not None and PNG in server.uploaded


async def test_video_uses_frames_and_the_uploaded_first_frame(tmp_path):
    server = Engine(output_key="videos", content_type="video/mp4")
    engine = ComfyUIVideoEngine(_settings(tmp_path), transport=httpx.MockTransport(server))
    output = await engine.generate(VideoRequest(prompt="waves", width=832, height=480, duration_seconds=3, fps=16,
                                                seed=3, source=PNG, source_content_type="image/png"))
    assert output.content_type == "video/mp4"
    assert server.submitted["1"]["inputs"]["frames"] == 49
    assert server.submitted["1"]["inputs"]["image"] == "source-upload.png"


async def test_a_workflow_error_is_a_definitive_failure(tmp_path):
    engine = ComfyUIImageEngine(_settings(tmp_path), transport=httpx.MockTransport(Engine(error=True)))
    with pytest.raises(EngineFailed) as raised:
        await engine.generate(ImageRequest(prompt="x", width=512, height=512, seed=1, steps=8))
    assert raised.value.retryable is False


async def test_a_server_error_on_submit_is_retryable(tmp_path):
    engine = ComfyUIImageEngine(_settings(tmp_path), transport=httpx.MockTransport(Engine(prompt_status=503)))
    with pytest.raises(EngineFailed) as raised:
        await engine.generate(ImageRequest(prompt="x", width=512, height=512, seed=1, steps=8))
    assert raised.value.retryable is True


async def test_a_generation_that_never_finishes_times_out(tmp_path):
    settings = _settings(tmp_path, media_image_timeout_s=0)
    engine = ComfyUIImageEngine(settings, transport=httpx.MockTransport(Engine(history_rounds=10_000)))
    with pytest.raises(EngineFailed) as raised:
        await engine.generate(ImageRequest(prompt="x", width=512, height=512, seed=1, steps=8))
    assert raised.value.reason == "timeout" and raised.value.retryable is True


async def test_without_a_url_or_workflow_the_engine_is_unavailable(tmp_path):
    unconfigured = ComfyUIImageEngine(MediaSettings(_env_file=None), transport=httpx.MockTransport(Engine()))
    assert await unconfigured.is_available() is False
    with pytest.raises(EngineUnavailable):
        await unconfigured.generate(ImageRequest(prompt="x", width=512, height=512, seed=1, steps=8))


async def test_an_unreachable_engine_is_unavailable_not_an_error(tmp_path):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    engine = ComfyUIImageEngine(_settings(tmp_path), transport=httpx.MockTransport(down))
    assert await engine.is_available() is False
