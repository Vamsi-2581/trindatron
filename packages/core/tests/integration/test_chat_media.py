"""Asking for an image or a video in the conversation creates it — or says honestly why not.

No language model is involved in either outcome: the router is patched to fail
if it is ever called. The engines are scripted; routes, plans, the daily count,
reservations, jobs, artifacts and persisted messages are real.
"""
from __future__ import annotations

import io
import json
import re
from unittest.mock import patch

import pytest
from PIL import Image

from src.media.engines import ImageEngine, MediaOutput, VideoEngine
from src.media.registry import use_engines
from src.models.media import MediaJob
from src.models.usage import UsageRecord
from src.models.user import User
from src.services import capability_service, media_service
from src.services.runtime_settings import FREE_IMAGE_DAILY_LIMIT, set_setting
from src.storage.local import LocalObjectStore
from tests.conftest import _TestingSession

PROVIDER_NAMES = re.compile(r"comfy|z-image|wan|veo|openai|gemini|google|anthropic|claude|gpt|fal\b|provider|cost|tcu", re.I)


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (0, 120, 140)).save(buffer, format="PNG")
    return buffer.getvalue()


class ScriptedImage(ImageEngine):
    async def is_available(self, *, editing: bool = False) -> bool:
        return not editing

    async def generate(self, request):
        return MediaOutput(data=_png(), content_type="image/png", width=request.width, height=request.height,
                           duration_seconds=None, engine_seconds=0.5, cost_usd=0.004)


class ScriptedVideo(VideoEngine):
    async def is_available(self, *, from_image: bool = False) -> bool:
        return not from_image

    async def generate(self, request):
        return MediaOutput(data=b"\x00\x00\x00\x18ftypmp42" + b"0" * 64, content_type="video/mp4", width=request.width,
                           height=request.height, duration_seconds=float(request.duration_seconds), engine_seconds=30.0)


async def _never(request):
    raise AssertionError("a creation request must never reach the conversation model")
    yield  # pragma: no cover


@pytest.fixture
def store(tmp_path, monkeypatch):
    local = LocalObjectStore(root=str(tmp_path / "objects"))
    monkeypatch.setattr(media_service, "build_object_store", lambda: local)
    monkeypatch.setattr("src.api.routes.media.build_object_store", lambda: local)
    monkeypatch.setattr(media_service, "schedule", lambda job_id: None)
    capability_service.reset_cache()
    yield local
    capability_service.reset_cache()


def _plan(db, user_id: str, plan: str) -> None:
    db.query(User).filter(User.id == user_id).update({"plan": plan})
    db.commit()


def _events(body: str) -> list[dict]:
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]


def _ask(client, headers, content: str) -> tuple[str, list[dict]]:
    session_id = client.post("/sessions", json={}, headers=headers).json()["id"]
    with patch("src.services.chat_service._router.stream", _never), patch("src.services.agent_service._router.stream", _never):
        response = client.post(f"/sessions/{session_id}/message", json={"content": content}, headers=headers)
    assert response.status_code == 200
    return session_id, _events(response.text)


def _text(events: list[dict]) -> str:
    """The reply, which arrives once, as the done event's answer."""
    assert not [event for event in events if event["type"] == "text"], "the reply must not also stream as text"
    return next(event["answer"] for event in events if event["type"] == "done")


def test_a_creation_request_in_chat_starts_a_real_image_job(db, client, auth_headers, registered_user, store):
    with use_engines(image=ScriptedImage(), video=None):
        session_id, events = _ask(client, auth_headers, "Crie uma imagem de um farol ao entardecer")

    assert _text(events).startswith("Estou criando sua imagem")
    [artifact] = [event["artifact"] for event in events if event["type"] == "artifact"]
    [done] = [event for event in events if event["type"] == "done"]
    assert artifact["kind"] == "image" and artifact["status"] == "queued"
    assert done["media_job_id"] == artifact["id"]

    job = db.query(MediaJob).filter(MediaJob.id == artifact["id"]).one()
    assert job.user_id == registered_user["user_id"] and "farol" in job.prompt
    # The chat turn's reservation was released; only the job's own is pending.
    assert db.query(UsageRecord).filter(UsageRecord.status == "pending").count() == 1

    detail = client.get(f"/sessions/{session_id}", headers=auth_headers).json()
    assert detail["messages"][-1]["role"] == "assistant"
    assert detail["messages"][-1]["media_job_id"] == job.id
    assert not PROVIDER_NAMES.search(json.dumps(events))


async def test_the_job_started_from_chat_runs_and_is_billed_what_it_cost(db, client, auth_headers, store):
    with use_engines(image=ScriptedImage(), video=None):
        _, events = _ask(client, auth_headers, "Crie uma imagem de um gato astronauta")
        job_id = next(event["artifact"]["id"] for event in events if event["type"] == "artifact")
        await media_service.run_job(job_id, session_factory=_TestingSession, store=store)

    db.expire_all()
    job = client.get(f"/media/jobs/{job_id}", headers=auth_headers).json()
    assert job["status"] == "completed" and job["artifact"]["content_type"] == "image/png"
    record = db.query(UsageRecord).filter(UsageRecord.status == "completed").one()
    assert float(record.cost_usd) == pytest.approx(0.004)


def test_when_images_are_not_available_the_reply_says_so_and_nothing_is_created(db, client, auth_headers, store):
    with use_engines(image=None, video=None):
        session_id, events = _ask(client, auth_headers, "Crie uma imagem de um farol")

    assert _text(events) == "Criar imagens não está disponível agora."
    assert not [event for event in events if event["type"] == "artifact"]
    assert db.query(MediaJob).count() == 0
    assert client.get(f"/sessions/{session_id}", headers=auth_headers).json()["messages"][-1]["media_job_id"] is None


def test_free_asking_for_a_video_is_told_its_plan_does_not_include_it(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "free")
    with use_engines(image=ScriptedImage(), video=ScriptedVideo()):
        _, events = _ask(client, auth_headers, "Crie um vídeo de ondas quebrando nas pedras")
    assert _text(events) == "Seu plano atual não inclui criar vídeos."
    assert db.query(MediaJob).count() == 0


def test_max_asking_for_a_video_gets_a_real_video_job(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "max")
    with use_engines(image=ScriptedImage(), video=ScriptedVideo()):
        _, events = _ask(client, auth_headers, "Crie um vídeo de ondas quebrando nas pedras")
    assert _text(events).startswith("Estou criando seu vídeo")
    [artifact] = [event["artifact"] for event in events if event["type"] == "artifact"]
    job = db.query(MediaJob).filter(MediaJob.id == artifact["id"]).one()
    assert job.kind == "video" and job.params["duration_seconds"] >= 1


@pytest.mark.parametrize("request_text,label,requested,portrait", [
    ("Crie uma imagem vertical de um farol para stories", "9:16", "1080x1920", True),
    ("Crie uma imagem 1920x1080 de um farol", "16:9", "1920x1080", False),
    ("Crie uma imagem de um farol em proporção 4:5", "4:5", None, True),
    ("Crie uma imagem de um farol para o banner do site", "3:1", "1500x500", False),
])
def test_the_format_asked_for_in_words_is_the_format_created(db, client, auth_headers, store, request_text, label,
                                                              requested, portrait):
    with use_engines(image=ScriptedImage(), video=None):
        _, events = _ask(client, auth_headers, request_text)
    job_id = next(event["artifact"]["id"] for event in events if event["type"] == "artifact")
    params = db.query(MediaJob).filter(MediaJob.id == job_id).one().params
    assert params["format"] == label
    assert params.get("requested_size") == requested
    assert (params["height"] > params["width"]) is portrait
    # The legacy three-shape name no longer decides anything, so it is not recorded.
    assert "aspect" not in params


@pytest.mark.parametrize("request_text,reply", [
    ("Create an image of a lighthouse at dusk", "I'm creating your image."),
    ("Crea una imagen de un faro al atardecer", "Estoy creando tu imagen."),
    ("Faça uma foto de um farol", "Estou criando sua imagem."),
])
def test_the_reply_follows_the_language_of_the_request(client, auth_headers, store, request_text, reply):
    with use_engines(image=ScriptedImage(), video=None):
        _, events = _ask(client, auth_headers, request_text)
    assert _text(events).startswith(reply)


async def test_after_todays_creations_the_reply_says_when_they_renew(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "free")
    set_setting(db, FREE_IMAGE_DAILY_LIMIT, 1, admin_user_id=registered_user["user_id"])
    with use_engines(image=ScriptedImage(), video=None):
        _, events = _ask(client, auth_headers, "Crie uma imagem de um farol")
        job_id = next(event["artifact"]["id"] for event in events if event["type"] == "artifact")
        await media_service.run_job(job_id, session_factory=_TestingSession, store=store)
        _, again = _ask(client, auth_headers, "Crie uma imagem de uma montanha")
    assert _text(again) == "Você já usou as criações de hoje do seu plano. Elas renovam amanhã."


def test_asking_about_an_image_is_not_a_creation_request(client, auth_headers, store):
    from src.capability import Capability, classify

    assert classify("O que tem nessa foto?") is Capability.CHAT
    assert classify("Descreva a imagem que eu enviei") is Capability.CHAT
    assert classify("Faça uma imagem de um farol") is Capability.IMAGE
    assert classify("make me a picture of a lighthouse") is Capability.IMAGE
    assert classify("Hazme una imagen de un faro") is Capability.IMAGE
