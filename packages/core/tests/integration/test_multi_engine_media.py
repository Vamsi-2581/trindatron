"""The multi-engine, multi-runner platform through the real product paths.

Runners are scripted (no network, no spend); adapters, routes, the orchestrator,
health, plans, reservations, jobs, storage, chat and capabilities are real.
What a person or a client can observe is pinned: a creation survives a failing
cloud, is idempotent, is persisted and downloadable, animates the image they
attached, and never reveals which model or infrastructure made it.
"""
from __future__ import annotations

import io
import json
import re
import struct
from unittest.mock import patch

import pytest
from PIL import Image

from src.media import engine_catalog
from src.media.adapters import WAN22_I2V_A14B, WAN22_TI2V_5B
from src.media.engines import EngineFailed, FailureKind
from src.media.health import health
from src.media.orchestrator import VideoEngineChain
from src.media.registry import use_engines
from src.media.routes import VideoRoute
from src.media.runners.base import Runner, RunnerResult
from src.media.settings import media_settings
from src.models.attachment import SessionAttachment
from src.models.media import Artifact, MediaJob
from src.models.usage import UsageRecord
from src.models.user import User
from src.services import capability_service, media_service
from src.storage.local import LocalObjectStore
from tests.conftest import _TestingSession

INTERNALS = re.compile(r"cloud_a|cloud_b|wan22|ltx|zerogpu|modal|runpod|engine_trace|engine_metrics|route|gpu_seconds", re.I)


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _mp4(width: int = 480, height: int = 832, seconds: float = 5.0) -> bytes:
    mvhd = _box(b"mvhd", b"\x00" * 12 + struct.pack(">II", 1000, int(seconds * 1000)) + b"\x00" * 80)
    tkhd = _box(b"tkhd", b"\x00" * 76 + struct.pack(">II", width << 16, height << 16))
    return _box(b"ftyp", b"mp42\x00\x00\x00\x00") + _box(b"moov", mvhd + _box(b"trak", tkhd))


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 96), (200, 120, 40)).save(buffer, format="PNG")
    return buffer.getvalue()


class ScriptedRunner(Runner):
    def __init__(self, name: str, prices: dict[str, float], fail: list[Exception] | None = None,
                 commercial: bool = True, experimental: bool = False) -> None:
        self.name, self.prices, self.fail = name, prices, list(fail or [])
        self.commercial, self.experimental = commercial, experimental
        self.jobs = []

    def supports(self, engine: str, task: str) -> bool:
        return engine in self.prices

    async def is_available(self) -> bool:
        return True

    def estimate_usd(self, engine: str, gpu_seconds: float | None) -> float | None:
        return self.prices[engine] * 10.0

    async def run(self, job):
        self.jobs.append(job)
        if self.fail:
            raise self.fail.pop(0)
        return RunnerResult(data=_mp4(job.width, job.height, float(job.duration_seconds or 1)), content_type="video/mp4",
                            width=job.width, height=job.height, duration_seconds=float(job.duration_seconds or 1),
                            total_seconds=14.0, queue_seconds=3.0, cold_start_seconds=1.0, gpu_seconds=10.0,
                            cost_usd=self.prices[job.engine] * 10.0)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ACCEPTED_LICENSE_CONDITIONS", engine_catalog.LTX_CONDITION)
    media_settings.cache_clear()
    health.reset()
    local = LocalObjectStore(root=str(tmp_path / "objects"))
    monkeypatch.setattr(media_service, "build_object_store", lambda: local)
    monkeypatch.setattr("src.api.routes.media.build_object_store", lambda: local)
    monkeypatch.setattr(media_service, "schedule", lambda job_id: None)
    capability_service.reset_cache()
    yield local
    capability_service.reset_cache()
    health.reset()
    media_settings.cache_clear()


def _plan(db, user_id: str, plan: str) -> None:
    db.query(User).filter(User.id == user_id).update({"plan": plan})
    db.commit()


async def _run(job_id: str, store) -> None:
    await media_service.run_job(job_id, session_factory=_TestingSession, store=store)


def _two_clouds():
    cloud_a = ScriptedRunner("cloud_a", {"wan22_ti2v_5b": 0.001, "wan22_i2v_a14b": 0.001},
                             fail=[EngineFailed("HTTP 429 quota", kind=FailureKind.QUOTA)])
    cloud_b = ScriptedRunner("cloud_b", {"wan22_ti2v_5b": 0.002, "wan22_i2v_a14b": 0.003})
    chain = VideoEngineChain([VideoRoute(WAN22_TI2V_5B, cloud_a), VideoRoute(WAN22_TI2V_5B, cloud_b),
                              VideoRoute(WAN22_I2V_A14B, cloud_a), VideoRoute(WAN22_I2V_A14B, cloud_b)])
    return chain, cloud_a, cloud_b


async def test_a_video_survives_a_cloud_without_quota_and_nothing_internal_reaches_the_client(
        db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "max")
    chain, cloud_a, cloud_b = _two_clouds()
    with use_engines(video=chain):
        headers = {**auth_headers, "Idempotency-Key": "multi-cloud-1"}
        body = {"prompt": "ondas quebrando nas rochas", "format": "vertical 9:16", "duration_seconds": 5}
        first = client.post("/media/videos", headers=headers, json=body)
        again = client.post("/media/videos", headers=headers, json=body)
        assert first.status_code == 202 and again.json()["id"] == first.json()["id"]

        await _run(first.json()["id"], store)

        done = client.get(f"/media/jobs/{first.json()['id']}", headers=auth_headers)
        capabilities = client.get("/users/me/capabilities", headers=auth_headers)
        creations = client.get("/media/creations", headers=auth_headers)

    view = done.json()
    assert view["status"] == "completed" and view["artifact"]["content_type"] == "video/mp4"
    # Every plan, MAX included, runs the economy mode (480p) unless the request asks for more.
    assert (view["artifact"]["width"], view["artifact"]["height"]) == (480, 832)
    download = client.get(view["artifact"]["download_path"], headers=auth_headers)
    assert download.status_code == 200 and download.content[4:8] == b"ftyp"

    job = db.query(MediaJob).filter(MediaJob.id == view["id"]).one()
    path = [(step["engine"], step["outcome"], step["kind"]) for step in job.params["engine_trace"]]
    assert path == [("wan22_ti2v_5b@cloud_a", "failed", "quota"), ("wan22_ti2v_5b@cloud_b", "completed", "")]
    assert job.params["engine_metrics"]["route"] == "wan22_ti2v_5b@cloud_b"
    assert job.params["engine_metrics"]["gpu_seconds"] == 10.0 and job.params["engine_metrics"]["queue_seconds"] == 3.0
    assert float(job.cost_usd) == pytest.approx(0.02)
    assert len(cloud_a.jobs) == 1 and len(cloud_b.jobs) == 1

    for response in (done, capabilities, creations):
        assert not INTERNALS.search(response.text), response.text[:300]


async def test_when_every_combination_fails_the_creation_fails_and_costs_nothing(
        db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "max")
    broken = [ScriptedRunner(name, {"wan22_ti2v_5b": price}, fail=[EngineFailed("no gpu", kind=FailureKind.GPU_UNAVAILABLE)])
              for name, price in (("cloud_a", 0.001), ("cloud_b", 0.002))]
    with use_engines(video=VideoEngineChain([VideoRoute(WAN22_TI2V_5B, runner) for runner in broken])):
        job = client.post("/media/videos", headers=auth_headers, json={"prompt": "ondas"}).json()
        await _run(job["id"], store)
        failed = client.get(f"/media/jobs/{job['id']}", headers=auth_headers).json()

    assert failed["status"] == "failed" and failed["artifact"] is None
    assert db.query(UsageRecord).filter(UsageRecord.status == "pending").count() == 0
    assert not INTERNALS.search(json.dumps(failed))


async def _attach_image(db, store, user_id: str, session_id: str) -> str:
    """An image attached to the conversation, stored the way attachment_service stores it."""
    data = _png()
    artifact = Artifact(user_id=user_id, kind="image", source="upload", filename="farol.png", content_type="image/png",
                        size_bytes=len(data), storage_backend=store.backend_name,
                        storage_key=f"attachments/{user_id}/{session_id}/farol")
    db.add(artifact)
    db.flush()
    await store.put(key=artifact.storage_key, data=data, content_type="image/png")
    db.add(SessionAttachment(user_id=user_id, session_id=session_id, name="farol.png", mime_type="image/png",
                             size_bytes=len(data), kind="image", artifact_id=artifact.id))
    db.commit()
    return artifact.id


def _ask(client, headers, session_id: str, content: str) -> list[dict]:
    async def never(request):
        raise AssertionError("a creation request must never reach the conversation model")
        yield  # pragma: no cover

    with patch("src.services.chat_service._router.stream", never), patch("src.services.agent_service._router.stream", never):
        response = client.post(f"/sessions/{session_id}/message", json={"content": content}, headers=headers)
    assert response.status_code == 200
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]


async def test_animating_the_image_attached_in_chat_is_a_real_image_to_video_job(
        db, client, auth_headers, registered_user, store):
    """Photo -> Movement runs only in Maximum, on the engine that animates the photo — and still survives a cloud
    without quota. The efficient 5B routes (which ignore a photo, measured 2026-09-25) are in the same chain and
    never receive it."""
    _plan(db, registered_user["user_id"], "max")
    cloud_a = ScriptedRunner("cloud_a", {"wan22_ti2v_5b": 0.0001, "wan22_i2v_a14b": 0.0001},
                             fail=[EngineFailed("HTTP 429 quota", kind=FailureKind.QUOTA)])
    cloud_b = ScriptedRunner("cloud_b", {"wan22_ti2v_5b": 0.0001, "wan22_i2v_a14b": 0.0002})
    chain = VideoEngineChain([VideoRoute(WAN22_TI2V_5B, cloud_a), VideoRoute(WAN22_TI2V_5B, cloud_b),
                              VideoRoute(WAN22_I2V_A14B, cloud_a), VideoRoute(WAN22_I2V_A14B, cloud_b)])
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    artifact_id = await _attach_image(db, store, registered_user["user_id"], session_id)

    with use_engines(video=chain):
        events = _ask(client, auth_headers, session_id, "Crie um vídeo vertical 9:16 de 5 segundos usando esta imagem")
        [artifact] = [event["artifact"] for event in events if event["type"] == "artifact"]
        job = db.query(MediaJob).filter(MediaJob.id == artifact["id"]).one()
        assert job.operation == "image_to_video" and job.source_artifact_id == artifact_id
        assert job.params["mode"] == "maximum"  # MAX defaults to economy; a photo runs in Maximum anyway
        assert job.params["format"] == "9:16" and job.params["duration_seconds"] == 5
        await _run(job.id, store)

    db.refresh(job)
    assert job.status == "completed"
    delivered = [step for step in job.params["engine_trace"] if step["outcome"] == "completed"][0]["engine"]
    assert delivered == "wan22_i2v_a14b@cloud_b"  # cloud_a had no quota; the 5B was never an option
    assert cloud_b.jobs[-1].task == "image_to_video" and cloud_b.jobs[-1].source == _png()
    assert all(job.engine != "wan22_ti2v_5b" for runner in (cloud_a, cloud_b) for job in runner.jobs)
    answer = next(event["answer"] for event in events if event["type"] == "done")
    assert not INTERNALS.search(answer)


def test_asking_to_animate_an_image_without_one_attached_is_answered_honestly(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "max")
    chain, _, _ = _two_clouds()
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    with use_engines(video=chain):
        events = _ask(client, auth_headers, session_id, "Anime esta imagem por 4 segundos")
    assert not [event for event in events if event["type"] == "artifact"]
    assert next(event["answer"] for event in events if event["type"] == "done").startswith("Anexe a imagem")


class _ScriptedImage:
    """A hosted image API graded below MAX's premium floor."""

    async def is_available(self, *, editing: bool = False) -> bool:
        return not editing

    def estimate_usd(self, request):
        return 0.004

    def typical_usd(self, request):
        return 0.004

    async def generate(self, request):  # pragma: no cover - must never run
        raise AssertionError("a route below the quality floor must never run")


def test_a_creation_no_route_can_make_at_the_plans_quality_is_refused_before_it_is_accepted(
        db, client, auth_headers, registered_user, store):
    from src.media.orchestrator import ImageEngineChain
    from src.media.routes import HostedImageRoute

    _plan(db, registered_user["user_id"], "max")  # MAX creates images at the standard tier (grade 3 floor)
    below = ImageEngineChain([HostedImageRoute(_ScriptedImage(), engine_id="gpt_image_1_mini", runner_name="openai",
                                               quality=2)])
    with use_engines(image=below):
        refused = client.post("/media/images", headers=auth_headers, json={"prompt": "um farol"})
        capabilities = client.get("/users/me/capabilities", headers=auth_headers).json()["capabilities"]
    assert refused.status_code == 503 and refused.json()["detail"]["error"] == "media_unavailable"
    assert db.query(MediaJob).count() == 0
    assert capabilities["image_generation"]["available"] is False


def test_free_never_gets_video_from_a_free_runner_unless_the_operator_opens_a_trial(
        db, registered_user, store, monkeypatch):
    user = db.query(User).filter(User.id == registered_user["user_id"]).one()
    _plan(db, user.id, "free")
    shared = ScriptedRunner("zerogpu", {"wan22_ti2v_5b": 0.0}, commercial=False, experimental=True)
    paid = ScriptedRunner("runpod", {"wan22_ti2v_5b": 0.001})

    with use_engines(video=VideoEngineChain([VideoRoute(WAN22_TI2V_5B, shared)])):
        closed = media_service.entitlement_for(db, user)
        monkeypatch.setenv("MEDIA_FREE_EXPERIMENTAL_VIDEO_DAILY_LIMIT", "1")
        media_settings.cache_clear()
        trial = media_service.entitlement_for(db, user)
    with use_engines(video=VideoEngineChain([VideoRoute(WAN22_TI2V_5B, shared), VideoRoute(WAN22_TI2V_5B, paid)])):
        guaranteed = media_service.entitlement_for(db, user)

    assert closed.video_daily_limit == 0
    assert trial.video_daily_limit == 1 and trial.video_guaranteed is False
    # With commercial infrastructure the promise is the plan's, and FREE's plan has no video.
    assert guaranteed.video_daily_limit == 0 and guaranteed.video_guaranteed is True
