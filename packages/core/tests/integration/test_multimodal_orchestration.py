"""The multimodal pieces working together through the real routes.

Engines are scripted at the registry; routes, reservations, storage, jobs,
capabilities and prompt assembly are real. These tests check behaviour a person
or a client can observe — not the internals the unit tests already pin.
"""
from __future__ import annotations

import io
import json
import re
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image
from trinity_router import RouterStreamChunk

from src.capability.matrix import ORDER
from src.media.engines import EngineFailed, ImageEngine, MediaOutput
from src.media.orchestrator import ImageEngineChain
from src.media.registry import use_engines
from src.models.media import MediaJob
from src.models.message import Message
from src.models.usage import UsageRecord
from src.models.user import User
from src.services import capability_service, media_service
from src.storage.local import LocalObjectStore
from tests.conftest import _TestingSession

VENDORS = re.compile(r"openai|gemini|google|xai|grok|groq|elevenlabs|anthropic|claude|veo|fal\b|whisper|docker|engine_trace|"
                     r"cost_usd|scripted", re.I)


def _png(width: int = 64, height: int = 64) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 90, 160)).save(buffer, format="PNG")
    return buffer.getvalue()


class PricedImage(ImageEngine):
    def __init__(self, *, cost: float, fail: EngineFailed | None = None, available: bool = True) -> None:
        self.cost, self.fail, self.available, self.calls = cost, fail, available, 0

    async def is_available(self, *, editing: bool = False) -> bool:
        return self.available

    def estimate_usd(self, request) -> float:
        return self.cost

    async def generate(self, request):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return MediaOutput(data=_png(request.width, request.height), content_type="image/png", width=request.width,
                           height=request.height, duration_seconds=None, engine_seconds=1.0, cost_usd=self.cost)


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


# ── Capability matrix ──────────────────────────────────────────────────────

def test_the_capability_matrix_follows_the_engines_really_configured(client, auth_headers, registered_user, db, store):
    _plan(db, registered_user["user_id"], "pro")
    chain = ImageEngineChain([PricedImage(cost=0.004), PricedImage(cost=0.02)])
    with use_engines(image=chain, video=None, vision=None):
        body = client.get("/users/me/capabilities", headers=auth_headers).json()

    matrix = body["matrix"]
    assert list(matrix) == list(ORDER)
    assert matrix["image_generation"]["available"] is True and matrix["image_generation"]["fallback"] is True
    assert matrix["image_generation"]["limits"]["daily_quota"]["limit"] == body["capabilities"]["image_generation"]["daily_quota"]["limit"]
    assert matrix["video_generation"]["status"] == "unavailable" and matrix["video_generation"]["available"] is False
    assert matrix["vision"]["available"] is False
    assert matrix["avatar_generation"]["status"] == "not_integrated"
    assert matrix["computer_control"]["scope"] == "sandbox"
    # The legacy view and the matrix never disagree.
    assert body["capabilities"]["image_generation"]["available"] == matrix["image_generation"]["available"]
    assert body["capabilities"]["voice_input"]["available"] == matrix["audio_input"]["available"]
    assert not VENDORS.search(json.dumps(body))


# ── Orchestrator through a real job ────────────────────────────────────────

async def test_a_creation_falls_back_to_the_next_engine_and_is_charged_what_that_engine_cost(
        client, auth_headers, registered_user, db, store):
    _plan(db, registered_user["user_id"], "pro")
    blocked = PricedImage(cost=0.004, fail=EngineFailed("HTTP 429", retryable=True))
    backup = PricedImage(cost=0.02)
    with use_engines(image=ImageEngineChain([blocked, backup])):
        created = client.post("/media/images", headers=auth_headers,
                              json={"prompt": "um farol", "format": "Instagram Story"})
        assert created.status_code == 202, created.text
        job_id = created.json()["id"]
        reservation = db.query(UsageRecord).filter(UsageRecord.id == db.get(MediaJob, job_id).reservation_id).one()
        # The reservation covered the fallback, not only the first engine.
        assert float(reservation.tcu) == pytest.approx(2.0)
        await media_service.run_job(job_id, session_factory=_TestingSession, store=store)

    db.expire_all()
    job = db.get(MediaJob, job_id)
    assert job.status == "completed" and blocked.calls == 1 and backup.calls == 1
    assert [step["outcome"] for step in job.params["engine_trace"]] == ["failed", "completed"]
    assert job.params["format"] == "9:16" and job.params["requested_size"] == "1080x1920" and "aspect" not in job.params
    assert job.params["height"] > job.params["width"]
    record = db.query(UsageRecord).filter(UsageRecord.id == job.reservation_id).one()
    assert record.status == "completed" and float(record.cost_usd) == pytest.approx(0.02)

    view = client.get(f"/media/jobs/{job_id}", headers=auth_headers)
    assert view.status_code == 200 and not VENDORS.search(view.text)


async def test_when_no_engine_can_deliver_the_person_is_not_charged(client, auth_headers, registered_user, db, store):
    _plan(db, registered_user["user_id"], "pro")
    chain = ImageEngineChain([PricedImage(cost=0.004, fail=EngineFailed("HTTP 503", retryable=True)),
                              PricedImage(cost=0.02, fail=EngineFailed("HTTP 503", retryable=True))])
    with use_engines(image=chain):
        job_id = client.post("/media/images", headers=auth_headers, json={"prompt": "um farol"}).json()["id"]
        await media_service.run_job(job_id, session_factory=_TestingSession, store=store)

    db.expire_all()
    job = db.get(MediaJob, job_id)
    assert job.status == "failed" and len(job.params["engine_trace"]) == 2
    assert db.query(UsageRecord).filter(UsageRecord.status == "completed").count() == 0
    failed = client.get(f"/media/jobs/{job_id}", headers=auth_headers).json()
    assert failed["error"] and not VENDORS.search(json.dumps(failed))


# ── The conversation knows what the product can do ─────────────────────────

def _capturing_router(calls: list):
    async def _stream(request):
        calls.append(request)
        yield RouterStreamChunk(text="Posso sim.")
        yield RouterStreamChunk(is_final=True, provider="p", model="m", input_tokens=10, output_tokens=5,
                                cached_tokens=0, cost_usd=0.0001, tcu=0.01)
    return _stream


def test_a_question_about_capabilities_reaches_the_model_with_the_real_matrix(client, auth_headers, registered_user, db, store):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    calls: list = []
    with use_engines(image=PricedImage(cost=0.004), video=None, vision=None), \
            patch("src.services.chat_service._router.stream", _capturing_router(calls)):
        response = client.post(f"/sessions/{session_id}/message", headers=auth_headers,
                               json={"content": "Você consegue acessar meu computador?"})

    assert response.status_code == 200 and calls
    system = calls[0].messages[0]["content"]
    assert "<capabilities>" in system
    available = system.split("Available:")[1].split("Not available right now:")[0]
    assert "image_generation" in available and "video_generation" not in available
    assert "video_generation" in system.split("Not available right now:")[1]
    assert "you have no tools yourself" in system
    assert "Never claim to control the person's own device" in system


# ── Agent mode creates instead of promising ────────────────────────────────

def test_an_image_asked_for_in_agent_mode_becomes_a_real_job(client, auth_headers, registered_user, db, store):
    _plan(db, registered_user["user_id"], "pro")
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    model = AsyncMock()
    with use_engines(image=PricedImage(cost=0.004)), patch("src.services.agent_service._router.complete", new=model):
        response = client.post(f"/sessions/{session_id}/agent", headers=auth_headers,
                               json={"content": "Crie uma imagem de um farol ao entardecer em 16:9"})

    assert response.status_code == 200
    events = [json.loads(line[6:]) for line in response.text.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    done = next(event for event in events if event.get("type") == "done")
    artifact = next(event for event in events if event.get("type") == "artifact")
    assert "breve" not in done["answer"].lower() and done["media_job_id"] == artifact["artifact"]["id"]
    model.assert_not_called()
    job = db.get(MediaJob, done["media_job_id"])
    assert job is not None and job.params["format"] == "16:9"
    reply = db.query(Message).filter(Message.session_id == session_id, Message.role == "assistant").one()
    assert reply.media_job_id == job.id
    # The agent execution's own reservation was released; only the job's is pending.
    pending = db.query(UsageRecord).filter(UsageRecord.status == "pending").all()
    assert [record.id for record in pending] == [job.reservation_id]
