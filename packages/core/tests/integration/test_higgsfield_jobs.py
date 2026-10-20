"""A creation served by a Higgsfield route, end to end, with the Higgsfield API played by httpx.MockTransport.

Everything else is real: routes, reservation and settlement, the job record,
object storage (a local directory) and artifacts. Nothing leaves the machine,
nothing is spent, and the credentials are placeholders set in the environment
(which wins over packages/core/.env, so a real key there is never read).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.media import plans
from src.media.health import health
from src.media.higgsfield import HiggsfieldImageEngine
from src.media.registry import use_engines
from src.media.routes import HostedImageRoute
from src.media.settings import media_settings
from src.models.media import Artifact, MediaJob
from src.models.usage import UsageRecord
from src.services import capability_service, media_service
from src.storage.local import LocalObjectStore
from tests.conftest import _TestingSession
from tests.unit.test_higgsfield_media import API, SOUL, FakeHiggsfield, factory

PROVIDER_WORDS = ("higgsfield", "soul", "req-1", "kling", "wan")


@pytest.fixture(autouse=True)
def env(monkeypatch):
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
    media_service._links.clear()


@pytest.fixture
def store(tmp_path, monkeypatch):
    local = LocalObjectStore(root=str(tmp_path / "objects"))
    monkeypatch.setattr(media_service, "build_object_store", lambda: local)
    monkeypatch.setattr("src.api.routes.media.build_object_store", lambda: local)
    monkeypatch.setattr(media_service, "schedule", lambda job_id: None)
    capability_service.reset_cache()
    yield local
    capability_service.reset_cache()


def soul_route(server) -> HostedImageRoute:
    return HostedImageRoute(HiggsfieldImageEngine("soul_2@higgsfield", factory(server)),
                            engine_id="higgsfield_soul_2", runner_name="higgsfield", quality=3)


def _create(client, headers):
    return client.post("/media/images", headers=headers, json={"prompt": "a lighthouse at dusk", "aspect": "landscape"})


async def _run(job_id: str, store) -> None:
    await media_service.run_job(job_id, session_factory=_TestingSession, store=store)


def _running_with_handle(db, job_id: str) -> MediaJob:
    """A job as a previous process left it: running, its provider request id recorded."""
    row = db.query(MediaJob).filter(MediaJob.id == job_id).one()
    row.status, row.provider_request_id = "running", "req-1"
    row.provider_ref = {"provider": "higgsfield", "model": SOUL, "route": "soul_2@higgsfield", "request_id": "req-1",
                        "status_url": f"{API}/requests/req-1/status", "cancel_url": f"{API}/requests/req-1/cancel",
                        "kind": "image", "estimate_usd": 0.0032, "width": 1024, "height": 768}
    db.commit()
    return row


# ── A creation on Soul 2 ─────────────────────────────────────────────────────

async def test_a_creation_is_submitted_once_recorded_before_polling_stored_and_billed_its_price(
        db, client, auth_headers, store):
    server = FakeHiggsfield()
    seen_before_first_status: list[str | None] = []
    original = server.__call__

    def watching(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/status") and not seen_before_first_status:
            with _TestingSession() as other:
                seen_before_first_status.append(other.query(MediaJob).one().provider_request_id)
        return original(request)

    with use_engines(image=HostedImageRoute(HiggsfieldImageEngine("soul_2@higgsfield", factory(watching)),
                                            engine_id="higgsfield_soul_2", runner_name="higgsfield", quality=3),
                     video=None):
        accepted = _create(client, auth_headers)
        assert accepted.status_code == 202
        reservation = db.query(UsageRecord).filter(UsageRecord.status == "pending").one()
        assert float(reservation.tcu) >= plans.usd_to_tcu(0.0032)
        await _run(accepted.json()["id"], store)

    db.expire_all()
    assert seen_before_first_status == ["req-1"]
    assert server.count("POST", f"{API}/{SOUL}") == 1
    job = db.query(MediaJob).one()
    assert job.status == "completed" and job.provider_request_id == "req-1"
    assert job.provider_ref["route"] == "soul_2@higgsfield" and job.provider_ref["model"] == SOUL
    metrics = job.params["engine_metrics"]
    assert metrics["provider"] == "higgsfield" and metrics["request_id"] == "req-1"
    assert metrics["route"] == "higgsfield_soul_2@higgsfield"
    assert float(job.cost_usd) == pytest.approx(0.0032)

    artifact = db.query(Artifact).one()
    assert artifact.content_type == "image/jpeg" and (artifact.width, artifact.height) == (960, 720)
    assert artifact.size_bytes == len(server.output) and artifact.expires_at is not None
    record = db.query(UsageRecord).filter(UsageRecord.id == reservation.id).one()
    assert record.status == "completed"
    assert float(record.tcu) == pytest.approx(plans.usd_to_tcu(0.0032))
    assert float(record.tcu) <= float(reservation.tcu) + 1e-9

    view = client.get(f"/media/jobs/{job.id}", headers=auth_headers).json()
    assert not any(word in str(view).lower() for word in PROVIDER_WORDS)


async def test_a_single_higgsfield_route_is_never_asked_twice_for_the_same_creation(db, client, auth_headers, store):
    server = FakeHiggsfield(submit=httpx.Response(500))
    with use_engines(image=soul_route(server), video=None):
        job = _create(client, auth_headers).json()
        await _run(job["id"], store)
    db.expire_all()
    assert server.count("POST", f"{API}/{SOUL}") == 1  # a generic engine would have been retried once
    assert db.query(MediaJob).one().status == "failed"
    assert db.query(UsageRecord).one().status == "failed"


async def test_a_filtered_result_fails_the_creation_without_charge(db, client, auth_headers, store):
    server = FakeHiggsfield(statuses=("queued", "nsfw"))
    with use_engines(image=soul_route(server), video=None):
        job = _create(client, auth_headers).json()
        await _run(job["id"], store)
    db.expire_all()
    assert db.query(MediaJob).one().status == "failed"
    assert db.query(UsageRecord).one().status == "failed"


# ── Cancelling ───────────────────────────────────────────────────────────────

async def test_a_queued_creation_is_cancelled_and_released_and_never_runs(db, client, auth_headers, store):
    server = FakeHiggsfield()
    with use_engines(image=soul_route(server), video=None):
        job = _create(client, auth_headers).json()
        cancelled = client.post(f"/media/jobs/{job['id']}/cancel", headers=auth_headers)
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["error"] is None
        await _run(job["id"], store)
    db.expire_all()
    assert server.calls == []
    assert db.query(UsageRecord).one().status == "failed"  # the reservation was released
    assert client.get("/media/creations", headers=auth_headers).json()["creations"] == []
    again = client.post(f"/media/jobs/{job['id']}/cancel", headers=auth_headers)
    assert again.status_code == 200 and again.json()["status"] == "cancelled"


async def test_a_running_creation_still_queued_at_the_provider_is_cancelled_there(db, client, auth_headers, store,
                                                                                  monkeypatch):
    server = FakeHiggsfield(statuses=("queued",))
    monkeypatch.setattr("src.media.higgsfield.default_client", factory(server))
    with use_engines(image=soul_route(server), video=None):
        job = _create(client, auth_headers).json()
    _running_with_handle(db, job["id"])
    cancelled = client.post(f"/media/jobs/{job['id']}/cancel", headers=auth_headers)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert server.count("POST", f"{API}/requests/req-1/cancel") == 1
    db.expire_all()
    assert db.query(UsageRecord).one().status == "failed"


async def test_a_creation_already_generating_cannot_be_cancelled_and_says_so(db, client, auth_headers, store,
                                                                             monkeypatch):
    server = FakeHiggsfield(statuses=("in_progress",), cancel_status=400)
    monkeypatch.setattr("src.media.higgsfield.default_client", factory(server))
    with use_engines(image=soul_route(server), video=None):
        job = _create(client, auth_headers).json()
    _running_with_handle(db, job["id"])
    refused = client.post(f"/media/jobs/{job['id']}/cancel", headers=auth_headers)
    assert refused.status_code == 409 and refused.json()["detail"]["error"] == "cannot_cancel"
    db.expire_all()
    assert db.query(MediaJob).one().status == "running"
    assert db.query(UsageRecord).one().status == "pending"


async def test_without_a_provider_job_a_running_creation_cannot_be_cancelled(db, client, auth_headers, store):
    with use_engines(image=soul_route(FakeHiggsfield()), video=None):
        job = _create(client, auth_headers).json()
    row = db.query(MediaJob).filter(MediaJob.id == job["id"]).one()
    row.status = "running"
    db.commit()
    refused = client.post(f"/media/jobs/{job['id']}/cancel", headers=auth_headers)
    assert refused.status_code == 409


async def test_a_finished_creation_cannot_be_cancelled(db, client, auth_headers, store):
    server = FakeHiggsfield()
    with use_engines(image=soul_route(server), video=None):
        job = _create(client, auth_headers).json()
        await _run(job["id"], store)
    refused = client.post(f"/media/jobs/{job['id']}/cancel", headers=auth_headers)
    assert refused.status_code == 409


async def test_cancelling_while_the_job_is_being_followed_ends_it_as_cancelled_once(db, client, auth_headers, store,
                                                                                    monkeypatch):
    server = FakeHiggsfield(statuses=("queued",))
    monkeypatch.setattr("src.media.higgsfield.default_client", factory(server))
    with use_engines(image=soul_route(server), video=None):
        job = _create(client, auth_headers).json()
        task = asyncio.create_task(_run(job["id"], store))
        for _ in range(200):
            await asyncio.sleep(0)
            if server.count("GET", f"{API}/requests/req-1/status"):
                break
        with _TestingSession() as other:
            user = other.query(media_service.User).filter(media_service.User.id == db.query(MediaJob).one().user_id).one()
            await media_service.cancel_job(other, user, job["id"])
        server.statuses = ["canceled"]
        await asyncio.wait_for(task, timeout=5)
    db.expire_all()
    assert db.query(MediaJob).one().status == "cancelled"
    assert db.query(UsageRecord).one().status == "failed"
    assert db.query(Artifact).count() == 0


# ── After a restart ──────────────────────────────────────────────────────────

async def test_a_restart_follows_the_same_provider_job_instead_of_paying_for_another(db, client, auth_headers, store):
    server = FakeHiggsfield(statuses=("in_progress", "completed"))
    with use_engines(image=soul_route(server), video=None):
        job = _create(client, auth_headers).json()
    _running_with_handle(db, job["id"])
    reservation = db.query(UsageRecord).one()

    assert media_service.reclaim_stale_jobs(db) == 0  # followed, not failed
    await media_service.resume_job(job["id"], session_factory=_TestingSession, store=store,
                                   client_factory=factory(server))
    db.expire_all()
    assert server.count("POST", f"{API}/{SOUL}") == 0
    done = db.query(MediaJob).one()
    assert done.status == "completed" and float(done.cost_usd) == pytest.approx(0.0032)
    assert done.params["engine_metrics"]["resumed"] is True
    assert db.query(UsageRecord).filter(UsageRecord.id == reservation.id).one().status == "completed"
    assert db.query(Artifact).count() == 1


@pytest.mark.parametrize("final, job_status", [("failed", "failed"), ("nsfw", "failed"), ("canceled", "cancelled")])
async def test_a_resumed_job_that_ended_without_output_costs_nothing(db, client, auth_headers, store, final, job_status):
    server = FakeHiggsfield(statuses=(final,))
    with use_engines(image=soul_route(server), video=None):
        job = _create(client, auth_headers).json()
    _running_with_handle(db, job["id"])
    await media_service.resume_job(job["id"], session_factory=_TestingSession, store=store,
                                   client_factory=factory(server))
    db.expire_all()
    assert db.query(MediaJob).one().status == job_status
    assert db.query(UsageRecord).one().status == "failed"


def test_a_provider_job_is_failed_only_once_the_provider_no_longer_keeps_it(db, client, auth_headers, store):
    with use_engines(image=soul_route(FakeHiggsfield()), video=None):
        job = _create(client, auth_headers).json()
    row = _running_with_handle(db, job["id"])
    row.created_at = datetime.now(timezone.utc) - timedelta(days=2)
    db.commit()
    assert media_service.reclaim_stale_jobs(db) == 0
    row.created_at = datetime.now(timezone.utc) - timedelta(days=media_service.PROVIDER_FOLLOW_DAYS + 1)
    db.commit()
    assert media_service.reclaim_stale_jobs(db) == 1
    assert db.query(UsageRecord).one().status == "failed"


async def test_startup_schedules_every_provider_job_left_running(db, client, auth_headers, store, monkeypatch):
    with use_engines(image=soul_route(FakeHiggsfield()), video=None):
        job = _create(client, auth_headers).json()
    _running_with_handle(db, job["id"])
    started: list[str] = []

    async def fake_resume(job_id, **kwargs):
        started.append(job_id)

    monkeypatch.setattr(media_service, "resume_job", fake_resume)
    assert media_service.resume_provider_jobs(session_factory=_TestingSession) == 1
    await asyncio.sleep(0)
    assert started == [job["id"]]


# ── Configured by routes: off is as before, unavailable falls back ───────────

def test_with_no_routes_configured_there_is_no_higgsfield_at_all(monkeypatch):
    monkeypatch.setenv("MEDIA_IMAGE_ROUTES", "")
    monkeypatch.setenv("MEDIA_IMAGE_ENGINE", "")
    monkeypatch.setenv("MEDIA_VIDEO_ROUTES", "")
    monkeypatch.setenv("MEDIA_VIDEO_ENGINE", "")
    media_settings.cache_clear()
    from src.media.registry import image_engine, video_engine
    assert image_engine() is None and video_engine() is None


async def test_listed_but_without_credentials_the_chain_serves_the_next_route(db, client, auth_headers, store,
                                                                            monkeypatch):
    monkeypatch.setenv("HIGGSFIELD_API_KEY_ID", "")
    monkeypatch.setenv("MEDIA_IMAGE_ROUTES", "soul_2@higgsfield,dev@local")
    media_settings.cache_clear()
    from src.media.registry import image_engine

    chain = image_engine()
    assert [engine.label for engine in chain.engines] == ["higgsfield_soul_2@higgsfield", "dev@local"]
    with use_engines(image=chain, video=None):
        job = _create(client, auth_headers).json()
        await _run(job["id"], store)
    db.expire_all()
    done = db.query(MediaJob).one()
    assert done.status == "completed"
    trace = done.params["engine_trace"]
    assert [(a["engine"], a["outcome"]) for a in trace][-1] == ("dev@local", "completed")
    assert ("higgsfield_soul_2@higgsfield", "unavailable") in [(a["engine"], a["outcome"]) for a in trace]
