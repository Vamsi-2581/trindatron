"""Self-hosted media engine: a ComfyUI server running open-weight models.

Why this engine (docs/MEDIA_ENGINES.md): the chosen models — Z-Image Turbo and
Qwen-Image for images, Wan2.2 TI2V-5B for video — are Apache-2.0, so a GPU hour
is the whole cost. ComfyUI (GPL-3.0) runs as a separate service reached only
over HTTP; TRINDATRON does not modify or embed it.

Workflows
---------
The deployment exports each workflow from ComfyUI with "Save (API Format)" and
points a MEDIA_COMFYUI_*_WORKFLOW setting at the file. Inside the JSON, any
string value that is exactly one of these placeholders is replaced before
submission (numbers are substituted as numbers):

    "{{prompt}}"  "{{seed}}"  "{{width}}"  "{{height}}"  "{{steps}}"
    "{{frames}}"  "{{fps}}"   "{{input_image}}"

The node that saves the result must be a SaveImage / SaveAnimatedWEBP /
SaveVideo (or any node whose history output lists images, gifs or videos).

Protocol (ComfyUI HTTP API)
---------------------------
  POST /upload/image            multipart, for edit / image-to-video sources
  POST /prompt {prompt, client_id} -> {prompt_id}
  GET  /history/{prompt_id}     until the entry has outputs (or an error status)
  GET  /view?filename&subfolder&type
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from src.media.engines import (
    EngineFailed,
    EngineUnavailable,
    ImageEngine,
    ImageRequest,
    MediaOutput,
    VideoEngine,
    VideoRequest,
)
from src.media.settings import MediaSettings, media_settings

logger = logging.getLogger("trinity.media.comfyui")

_OUTPUT_KEYS = ("images", "gifs", "videos")


def load_workflow(path: str) -> dict | None:
    """The API-format workflow at `path`, or None if unset/unreadable."""
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.error("media: workflow could not be read | path_set=%s", bool(path))
        return None
    return data if isinstance(data, dict) and data else None


def fill_workflow(workflow: dict, values: dict[str, Any]) -> dict:
    """Replace exact-match placeholders anywhere in the workflow tree."""
    tokens = {f"{{{{{name}}}}}": value for name, value in values.items()}

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: walk(value) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(value) for value in node]
        if isinstance(node, str) and node in tokens:
            return tokens[node]
        return node

    return walk(copy.deepcopy(workflow))


class ComfyUIClient:
    def __init__(self, settings: MediaSettings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport

    def _client(self, timeout: float) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {self._settings.media_comfyui_token}"} if self._settings.media_comfyui_token else {}
        return httpx.AsyncClient(
            base_url=self._settings.media_comfyui_url.rstrip("/"), headers=headers,
            timeout=timeout, transport=self._transport,
        )

    @property
    def configured(self) -> bool:
        return bool(self._settings.media_comfyui_url)

    async def reachable(self) -> bool:
        if not self.configured:
            return False
        try:
            async with self._client(5.0) as client:
                return (await client.get("/system_stats")).status_code == 200
        except httpx.HTTPError:
            return False

    async def run(self, workflow: dict, values: dict[str, Any], *, source: bytes | None,
                  source_content_type: str | None, timeout_s: float) -> tuple[bytes, str, float]:
        """Submit, wait, download. Returns (bytes, content_type, engine_seconds)."""
        started = time.monotonic()
        try:
            async with self._client(60.0) as client:
                if source is not None:
                    extension = mimetypes.guess_extension(source_content_type or "image/png") or ".png"
                    upload = await client.post(
                        "/upload/image",
                        files={"image": (f"source-{uuid.uuid4().hex}{extension}", source, source_content_type or "image/png")},
                        data={"overwrite": "true"},
                    )
                    if upload.status_code != 200:
                        raise EngineFailed(f"upload status {upload.status_code}")
                    values = {**values, "input_image": upload.json().get("name")}

                submitted = await client.post("/prompt", json={"prompt": fill_workflow(workflow, values),
                                                               "client_id": uuid.uuid4().hex})
                if submitted.status_code != 200:
                    raise EngineFailed(f"prompt status {submitted.status_code}", retryable=submitted.status_code >= 500)
                prompt_id = submitted.json().get("prompt_id")
                if not prompt_id:
                    raise EngineFailed("no prompt id")

                deadline = started + timeout_s
                while True:
                    history = await client.get(f"/history/{prompt_id}")
                    entry = history.json().get(prompt_id) if history.status_code == 200 else None
                    if entry:
                        status = (entry.get("status") or {}).get("status_str")
                        if status == "error":
                            raise EngineFailed("workflow error")
                        output = _first_output(entry.get("outputs") or {})
                        if output is not None:
                            break
                    if time.monotonic() >= deadline:
                        raise EngineFailed("timeout", retryable=True)
                    await asyncio.sleep(self._settings.media_comfyui_poll_interval_s)

                view = await client.get("/view", params={"filename": output["filename"],
                                                         "subfolder": output.get("subfolder", ""),
                                                         "type": output.get("type", "output")})
                if view.status_code != 200 or not view.content:
                    raise EngineFailed(f"view status {view.status_code}", retryable=True)
                content_type = view.headers.get("content-type", "").split(";")[0] or (
                    mimetypes.guess_type(output["filename"])[0] or "application/octet-stream"
                )
                return view.content, content_type, time.monotonic() - started
        except httpx.HTTPError as exc:
            raise EngineFailed(type(exc).__name__, retryable=True) from exc


def _first_output(outputs: dict) -> dict | None:
    for node_output in outputs.values():
        for key in _OUTPUT_KEYS:
            files = node_output.get(key) if isinstance(node_output, dict) else None
            if files:
                return files[0]
    return None


class ComfyUIImageEngine(ImageEngine):
    def __init__(self, settings: MediaSettings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings or media_settings()
        self._client = ComfyUIClient(self._settings, transport)

    def _workflow(self, editing: bool) -> dict | None:
        s = self._settings
        return load_workflow(s.media_comfyui_image_edit_workflow if editing else s.media_comfyui_image_workflow)

    async def is_available(self, *, editing: bool = False) -> bool:
        return self._workflow(editing) is not None and await self._client.reachable()

    async def generate(self, request: ImageRequest) -> MediaOutput:
        workflow = self._workflow(request.source is not None)
        if workflow is None or not self._client.configured:
            raise EngineUnavailable("image workflow not configured")
        data, content_type, seconds = await self._client.run(
            workflow,
            {"prompt": request.prompt, "seed": request.seed, "width": request.width,
             "height": request.height, "steps": request.steps},
            source=request.source, source_content_type=request.source_content_type,
            timeout_s=self._settings.media_image_timeout_s,
        )
        return MediaOutput(data=data, content_type=content_type, width=request.width, height=request.height,
                           duration_seconds=None, engine_seconds=seconds)


class ComfyUIVideoEngine(VideoEngine):
    def __init__(self, settings: MediaSettings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings or media_settings()
        self._client = ComfyUIClient(self._settings, transport)

    def _workflow(self, from_image: bool) -> dict | None:
        s = self._settings
        return load_workflow(s.media_comfyui_image_to_video_workflow if from_image else s.media_comfyui_video_workflow)

    async def is_available(self, *, from_image: bool = False) -> bool:
        return self._workflow(from_image) is not None and await self._client.reachable()

    async def generate(self, request: VideoRequest) -> MediaOutput:
        workflow = self._workflow(request.source is not None)
        if workflow is None or not self._client.configured:
            raise EngineUnavailable("video workflow not configured")
        data, content_type, seconds = await self._client.run(
            workflow,
            {"prompt": request.prompt, "seed": request.seed, "width": request.width, "height": request.height,
             "frames": request.frames, "fps": request.fps},
            source=request.source, source_content_type=request.source_content_type,
            timeout_s=self._settings.media_video_timeout_s,
        )
        return MediaOutput(data=data, content_type=content_type, width=request.width, height=request.height,
                           duration_seconds=float(request.duration_seconds), engine_seconds=seconds)
