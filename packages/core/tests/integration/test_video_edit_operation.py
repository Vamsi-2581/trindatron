"""video_edit end to end: upload a real clip, ask for the edit, get a shorter captioned MP4 back.

Same path as every TRANSFORM (POST /media/operations -> MediaJob -> run_job -> Artifact), with the
real ffmpeg engine (media/video_edit.py) and a scripted transcriber in place of whisper.cpp.
"""
from __future__ import annotations

import pytest

from src.capability.multimodal import TranscriptionResult, TranscriptSegment
from src.media import video_edit
from src.media.registry import use_engines
from src.media.video_edit import FfmpegVideoEditEngine
from src.models.usage import UsageRecord
from src.services import media_service
from src.storage.local import LocalObjectStore
from tests.conftest import _TestingSession
from tests.unit.test_video_edit import make_clip

pytestmark = pytest.mark.skipif(video_edit.ffmpeg_path() is None, reason="ffmpeg is not installed")


async def _transcribe(audio: bytes, content_type: str, language):
    return TranscriptionResult(text="", segments=(TranscriptSegment(0.1, 0.9, "olá pessoal"),
                                                  TranscriptSegment(3.2, 4.8, "vamos editar")))


@pytest.fixture
def store(tmp_path, monkeypatch):
    local = LocalObjectStore(root=str(tmp_path / "objects"))
    monkeypatch.setattr(media_service, "build_object_store", lambda: local)
    monkeypatch.setattr("src.api.routes.media.build_object_store", lambda: local)
    monkeypatch.setattr(media_service, "schedule", lambda job_id: None)
    yield local


@pytest.fixture
def max_headers(db, registered_user):
    from src.models.user import User

    db.query(User).filter(User.id == registered_user["user_id"]).one().plan = "max"
    db.commit()
    return {"Authorization": f"Bearer {registered_user['token']}"}


async def test_an_uploaded_clip_comes_back_cut_and_captioned_at_no_cost(db, client, max_headers, store, tmp_path):
    source = make_clip(tmp_path / "clip.mp4")
    uploaded = client.post("/media/sources/video", headers=max_headers,
                           files={"file": ("clip.mp4", source, "video/mp4")})
    assert uploaded.status_code == 201

    with use_engines(operations={"video_edit": FfmpegVideoEditEngine(_transcribe)}):
        accepted = client.post("/media/operations", headers=max_headers, json={
            "operation": "video_edit", "inputs": [{"role": "source_video", "artifact_id": uploaded.json()["id"]}],
            "parameters": {"pace": "fast", "language": "pt"}})
        assert accepted.status_code == 202, accepted.text
        job = accepted.json()
        assert job["kind"] == "video"
        await media_service.run_job(job["id"], session_factory=_TestingSession, store=store)

    done = client.get(f"/media/jobs/{job['id']}", headers=max_headers).json()
    assert done["status"] == "completed", done
    assert done["artifact"]["content_type"] == "video/mp4"
    assert done["artifact"]["duration_seconds"] < 4.0  # 5 s source, 2 s pause cut at a fast pace
    assert float(db.query(UsageRecord).filter(UsageRecord.status == "completed").one().cost_usd) == 0.0


def test_an_image_is_not_a_source_video(client, max_headers, store):
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buffer, format="PNG")
    image = client.post("/media/sources", headers=max_headers, files={"file": ("a.png", buffer.getvalue(), "image/png")})
    with use_engines(operations={"video_edit": FfmpegVideoEditEngine(_transcribe)}):
        refused = client.post("/media/operations", headers=max_headers, json={
            "operation": "video_edit", "inputs": [{"role": "source_video", "artifact_id": image.json()["id"]}]})
    assert refused.status_code == 404


def test_video_editing_is_reported_integrated_only_once_the_route_is_configured(client, max_headers, store):
    from src.services import capability_service

    def status() -> str:
        capability_service._matrix_cache.clear()
        return client.get("/users/me/capabilities", headers=max_headers).json()["matrix"]["video_editing"]["status"]

    with use_engines(operations={}):
        assert status() == "not_integrated"
    with use_engines(operations={"video_edit": FfmpegVideoEditEngine(_transcribe)}):
        assert status() == "available"
