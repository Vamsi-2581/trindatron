"""A video on the Modal worker, through the real product path, across a cold start.

The first video in production (2026-09-27) failed: the worker was still cold (~100 s queued), one status
check failed, and the whole job was dropped — "Nothing was charged", but no video either, while the clip
kept generating on the GPU. Here the worker's HTTP API is simulated on a clock (queued, running, completed)
with checks that fail on the way; the runner, route, orchestrator, plans, reservation, settlement, job,
storage and API are real.
"""
from __future__ import annotations

import json

import httpx
import pytest

from src.media.adapters import WAN22_TI2V_5B
from src.media.health import health
from src.media.orchestrator import VideoEngineChain
from src.media.registry import use_engines
from src.media.routes import VideoRoute
from src.media.runners.gpu_worker import ModalRunner
from src.media.settings import media_settings
from src.models.media import Artifact, MediaJob
from src.models.usage import UsageRecord
from src.models.user import User
from src.services import capability_service, media_service
from src.storage.local import LocalObjectStore
from tests.conftest import _TestingSession
from tests.integration.test_multi_engine_media import _mp4

QUEUED_S, RUNNING_S, GPU_S = 100.0, 17.0, 17.0


class Worker:
    """The worker's HTTP API on a clock that moves only while the Core waits between checks."""

    def __init__(self, broken_checks: dict | None = None) -> None:
        self.now = 0.0
        self.broken_checks = broken_checks or {}
        self.calls: list[tuple[str, str]] = []
        self.submitted: dict | None = None

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 3.0)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if request.url.path == "/v1/health":
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST":
            self.submitted = json.loads(request.content)["job"]
            return httpx.Response(200, json={"id": "fc-1"})
        if request.method == "DELETE":
            return httpx.Response(200, json={"status": "cancelled"})
        check = sum(1 for method, path in self.calls if method == "GET" and path.startswith("/v1/jobs/"))
        broken = self.broken_checks.get(check)
        if broken == "timeout":
            raise httpx.ReadTimeout("status took too long", request=request)
        if broken is not None:
            return httpx.Response(broken, json={"detail": "busy"})
        if self.now < QUEUED_S:
            return httpx.Response(200, json={"status": "queued"})
        if self.now < QUEUED_S + RUNNING_S:
            return httpx.Response(200, json={"status": "running"})
        width, height = self.submitted["width"], self.submitted["height"]
        import base64

        return httpx.Response(200, json={"status": "completed", "output": {
            "content_type": "video/mp4", "data_base64": base64.b64encode(_mp4(width, height, 5.0)).decode(),
            "metrics": {"gpu_seconds": GPU_S, "cold_start_seconds": 72.0, "queue_seconds": QUEUED_S}}})

    def runner(self) -> ModalRunner:
        return ModalRunner(lambda: httpx.AsyncClient(transport=httpx.MockTransport(self.handler)),
                           clock=self.clock, sleep=self.sleep)

    @property
    def cancelled(self) -> bool:
        return ("DELETE", "/v1/jobs/fc-1") in self.calls


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_MODAL_URL", "https://worker.test")
    monkeypatch.setenv("MEDIA_MODAL_TOKEN", "modal-test")
    monkeypatch.setenv("MEDIA_MODAL_ENGINES", "wan22_ti2v_5b")
    monkeypatch.setenv("MEDIA_MODAL_TASKS", "text_to_video")
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


def _max(db, user_id: str) -> None:
    db.query(User).filter(User.id == user_id).update({"plan": "max"})
    db.commit()


def _create(client, headers, **body) -> dict:
    response = client.post("/media/videos", headers=headers, json={"prompt": "um barco a vela no pôr do sol", **body})
    assert response.status_code == 202, response.text
    return response.json()


async def _run(job_id: str, store) -> None:
    await media_service.run_job(job_id, session_factory=_TestingSession, store=store)


@pytest.mark.parametrize("broken_checks", [{}, {12: "timeout"}, {20: "timeout", 21: 502, 22: 503}])
async def test_economy_480p_survives_a_cold_start_and_is_billed_its_gpu_seconds(
        db, client, auth_headers, registered_user, store, broken_checks):
    _max(db, registered_user["user_id"])
    worker = Worker(broken_checks)
    with use_engines(video=VideoEngineChain([VideoRoute(WAN22_TI2V_5B, worker.runner())])):
        job = _create(client, auth_headers)
        reserved = db.query(UsageRecord).filter(UsageRecord.status == "pending").one()
        await _run(job["id"], store)

    db.expire_all()
    done = client.get(f"/media/jobs/{job['id']}", headers=auth_headers).json()
    assert done["status"] == "completed" and done["mode"] == "economy"
    assert min(done["artifact"]["width"], done["artifact"]["height"]) == 480  # 480p unless asked for more
    assert worker.now >= QUEUED_S + RUNNING_S and not worker.cancelled
    download = client.get(done["artifact"]["download_path"], headers=auth_headers)
    assert download.status_code == 200 and download.content[4:8] == b"ftyp"

    record = db.query(UsageRecord).filter(UsageRecord.id == reserved.id).one()
    price = media_settings().media_modal_gpu_usd_per_second
    assert record.status == "completed" and float(record.cost_usd) == pytest.approx(GPU_S * price, rel=1e-6)
    assert float(record.tcu) <= float(reserved.tcu)  # settled at its measured cost, never above the reservation
    assert db.query(UsageRecord).filter(UsageRecord.status == "pending").count() == 0


async def test_quality_720p_when_asked_survives_a_cold_start(db, client, auth_headers, registered_user, store):
    _max(db, registered_user["user_id"])
    worker = Worker({15: "timeout"})
    with use_engines(video=VideoEngineChain([VideoRoute(WAN22_TI2V_5B, worker.runner())])):
        job = _create(client, auth_headers, mode="quality")
        await _run(job["id"], store)

    done = client.get(f"/media/jobs/{job['id']}", headers=auth_headers).json()
    assert done["status"] == "completed" and done["mode"] == "quality"
    # The 5B's 720p is 1248x704 (a multiple of 32), the same the real worker delivered on 2026-09-27.
    assert min(done["artifact"]["width"], done["artifact"]["height"]) == 704


async def test_a_warm_worker_delivers_at_once(db, client, auth_headers, registered_user, store, monkeypatch):
    _max(db, registered_user["user_id"])
    monkeypatch.setattr("tests.integration.test_modal_cold_start.QUEUED_S", 0.0)
    worker = Worker()
    with use_engines(video=VideoEngineChain([VideoRoute(WAN22_TI2V_5B, worker.runner())])):
        job = _create(client, auth_headers)
        await _run(job["id"], store)

    assert client.get(f"/media/jobs/{job['id']}", headers=auth_headers).json()["status"] == "completed"
    assert worker.now < 30


async def test_when_the_worker_cannot_be_reached_the_video_fails_bills_nothing_and_the_clip_is_cancelled(
        db, client, auth_headers, registered_user, store):
    _max(db, registered_user["user_id"])
    worker = Worker({check: "timeout" for check in range(5, 500)})
    with use_engines(video=VideoEngineChain([VideoRoute(WAN22_TI2V_5B, worker.runner())])):
        job = _create(client, auth_headers)
        await _run(job["id"], store)

    db.expire_all()
    done = client.get(f"/media/jobs/{job['id']}", headers=auth_headers).json()
    assert done["status"] == "failed" and done["artifact"] is None
    assert worker.cancelled  # the GPU does not keep generating for nobody
    assert db.query(UsageRecord).filter(UsageRecord.status.in_(("pending", "completed"))).count() == 0
    assert db.query(Artifact).count() == 0


async def test_a_video_cancelled_before_it_starts_releases_its_reservation(db, client, auth_headers,
                                                                          registered_user, store):
    _max(db, registered_user["user_id"])
    worker = Worker()
    with use_engines(video=VideoEngineChain([VideoRoute(WAN22_TI2V_5B, worker.runner())])):
        job = _create(client, auth_headers)
        response = client.post(f"/media/jobs/{job['id']}/cancel", headers=auth_headers)

    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert db.query(UsageRecord).filter(UsageRecord.status.in_(("pending", "completed"))).count() == 0
    assert db.query(MediaJob).one().status == "cancelled"
