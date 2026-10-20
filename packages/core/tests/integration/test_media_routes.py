"""Media creation end to end: capability discovery, jobs, storage, quotas, money, isolation.

The engine is replaced at the registry (media/registry.use_engines) with small
scripted engines; everything else is real — routes, entitlement reservation,
the daily count, object storage (a local directory), artifacts and downloads.
Jobs are run explicitly with media_service.run_job() so each test controls when
background work happens.
"""
from __future__ import annotations

import io
import json
import re
from decimal import Decimal

import pytest
from PIL import Image

from src.config import settings
from src.media import plans
from src.media.dev_engine import DevelopmentImageEngine
from src.media.engines import EngineFailed, ImageEngine, MediaOutput, VideoEngine
from src.media.registry import use_engines
from src.models.media import Artifact, MediaJob
from src.models.usage import UsageRecord
from src.models.user import User
from src.services import capability_service, media_service
from src.services.runtime_settings import DISABLED_CAPABILITIES, FREE_IMAGE_DAILY_LIMIT, set_setting
from src.storage.local import LocalObjectStore
from tests.conftest import _TestingSession

PROVIDER_NAMES = re.compile(r"comfy|z-image|qwen|wan2|ltx|hunyuan|flux|openai|gemini|google|anthropic|claude|gpt|runpod|gpu", re.I)


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (0, 120, 140)).save(buffer, format="PNG")
    return buffer.getvalue()


def _mp4() -> bytes:
    return b"\x00\x00\x00\x18ftypmp42" + b"0" * 128  # real ftyp box at the offset _sniff_ok checks


class ScriptedImageEngine(ImageEngine):
    def __init__(self, *, fail: EngineFailed | None = None, fail_times: int = 0, content_type: str = "image/png") -> None:
        self.calls = 0
        self.fail = fail
        self.fail_times = fail_times
        self.content_type = content_type
        self.last_source: bytes | None = None

    async def is_available(self, *, editing: bool = False) -> bool:
        return True

    async def generate(self, request):
        self.calls += 1
        self.last_source = request.source
        if self.fail is not None and self.calls <= self.fail_times:
            raise self.fail
        return MediaOutput(data=_png(), content_type=self.content_type, width=request.width, height=request.height,
                           duration_seconds=None, engine_seconds=2.5)


class ScriptedVideoEngine(VideoEngine):
    async def is_available(self, *, from_image: bool = False) -> bool:
        return True

    async def generate(self, request):
        return MediaOutput(data=b"\x00\x00\x00\x18ftypmp42" + b"0" * 128, content_type="video/mp4",
                           width=request.width, height=request.height, duration_seconds=float(request.duration_seconds),
                           engine_seconds=120.0)


@pytest.fixture
def store(tmp_path, monkeypatch):
    local = LocalObjectStore(root=str(tmp_path / "objects"))
    monkeypatch.setattr(media_service, "build_object_store", lambda: local)
    monkeypatch.setattr("src.api.routes.media.build_object_store", lambda: local)
    monkeypatch.setattr(media_service, "schedule", lambda job_id: None)
    capability_service.reset_cache()
    yield local
    capability_service.reset_cache()


@pytest.fixture
def engines(store):
    image, video = ScriptedImageEngine(), ScriptedVideoEngine()
    with use_engines(image=image, video=video):
        yield image, video


def _plan(db, user_id: str, plan: str) -> None:
    db.query(User).filter(User.id == user_id).update({"plan": plan})
    db.commit()


async def _run(job_id: str, store) -> None:
    await media_service.run_job(job_id, session_factory=_TestingSession, store=store)


def _create_image(client, headers, **body):
    return client.post("/media/images", headers=headers, json={"prompt": "a lighthouse at dusk", **body})


# ── Discovery ────────────────────────────────────────────────────────────────

def test_without_an_engine_nothing_media_is_offered(client, auth_headers, store):
    with use_engines(image=None, video=None):
        body = client.get("/users/me/capabilities", headers=auth_headers).json()["capabilities"]
    for name in ("image_generation", "image_editing", "video_generation", "image_to_video", "avatar_generation"):
        assert body[name] == {"available": False}
    with use_engines(image=None, video=None):
        refused = _create_image(client, auth_headers)
    assert refused.status_code == 503
    assert refused.json()["detail"]["error"] == "media_unavailable"


def _free_image_allowance() -> int:
    """FREE's daily images on a self-hosted engine: its ceiling, lowered to what its window pays for."""
    base = plans.get_media_entitlement("free")
    return plans.affordable_daily_count("free", base.image_daily_limit, media_service.image_unit_usd(base, ScriptedImageEngine()))


def test_free_gets_images_with_a_daily_quota_and_no_video(db, client, auth_headers, registered_user, engines):
    _plan(db, registered_user["user_id"], "free")
    body = client.get("/users/me/capabilities", headers=auth_headers).json()["capabilities"]
    expected = _free_image_allowance()
    assert 1 <= expected <= plans.get_media_entitlement("free").image_daily_limit
    assert body["image_generation"]["available"] is True
    assert body["image_generation"]["daily_quota"]["limit"] == expected
    assert body["image_generation"]["daily_quota"]["remaining"] == expected
    assert body["video_generation"] == {"available": False}
    assert body["avatar_generation"] == {"available": False}


def test_max_gets_video_with_its_own_limits(db, client, auth_headers, registered_user, engines):
    _plan(db, registered_user["user_id"], "max")
    video = client.get("/users/me/capabilities", headers=auth_headers).json()["capabilities"]["video_generation"]
    duration, _ = media_service.video_offer("max", plans.get_media_entitlement("max"), ScriptedVideoEngine())
    assert video["available"] is True
    # An efficient engine serves economy and quality; maximum and premium need their own engines (media/modes.py).
    assert video["limits"] == {"quality_tier": "draft", "max_resolution": "720p", "max_duration_seconds": duration,
                               "modes": ["economy", "quality"], "default_mode": "economy"}
    assert 1 <= duration <= plans.get_media_entitlement("max").max_video_duration_seconds


def test_capability_responses_never_name_an_engine_or_vendor(db, client, auth_headers, registered_user, engines):
    _plan(db, registered_user["user_id"], "max")
    assert not PROVIDER_NAMES.search(json.dumps(client.get("/users/me/capabilities", headers=auth_headers).json()))


def test_an_operator_kill_switch_turns_images_off(db, client, auth_headers, registered_user, engines):
    set_setting(db, DISABLED_CAPABILITIES, ["image"], admin_user_id=registered_user["user_id"])
    assert client.get("/users/me/capabilities", headers=auth_headers).json()["capabilities"]["image_generation"] == {"available": False}
    assert _create_image(client, auth_headers).status_code == 503


# ── Image lifecycle ──────────────────────────────────────────────────────────

async def test_an_image_job_runs_stores_an_artifact_and_bills_measured_cost(db, client, auth_headers, registered_user, engines, store):
    accepted = _create_image(client, auth_headers, aspect="landscape")
    assert accepted.status_code == 202
    job = accepted.json()
    assert job["status"] == "queued" and job["kind"] == "image"

    reservation = db.query(UsageRecord).filter(UsageRecord.status == "pending").one()
    await _run(job["id"], store)
    db.expire_all()

    done = client.get(f"/media/jobs/{job['id']}", headers=auth_headers).json()
    assert done["status"] == "completed" and done["progress"] == 1.0
    artifact = done["artifact"]
    assert artifact["content_type"] == "image/png"
    assert (artifact["width"], artifact["height"]) == (1024, 768)

    content = client.get(artifact["content_path"], headers=auth_headers)
    assert content.status_code == 200 and content.content.startswith(b"\x89PNG")
    download = client.get(artifact["download_path"], headers=auth_headers)
    assert download.headers["content-disposition"].startswith("attachment")

    record = db.query(UsageRecord).filter(UsageRecord.id == reservation.id).one()
    assert record.status == "completed"
    # 2.5 s of GPU at the configured price, never more than was reserved.
    assert Decimal(str(record.cost_usd)) > 0
    assert float(record.tcu) <= float(reservation.tcu) + 1e-9


async def test_an_idempotency_key_returns_the_same_job_and_reserves_once(db, client, auth_headers, engines):
    first = _create_image(client, {**auth_headers, "Idempotency-Key": "same-tap"})
    second = _create_image(client, {**auth_headers, "Idempotency-Key": "same-tap"})
    assert first.json()["id"] == second.json()["id"]
    assert db.query(MediaJob).count() == 1
    assert db.query(UsageRecord).count() == 1


def test_a_second_image_while_one_is_in_progress_is_refused(client, auth_headers, engines):
    assert _create_image(client, auth_headers).status_code == 202
    duplicate = _create_image(client, auth_headers)
    assert duplicate.status_code == 409 and duplicate.json()["detail"]["error"] == "job_in_progress"


async def test_the_daily_limit_is_enforced_with_its_reset_time(db, client, auth_headers, registered_user, engines, store):
    _plan(db, registered_user["user_id"], "free")
    set_setting(db, FREE_IMAGE_DAILY_LIMIT, 1, admin_user_id=registered_user["user_id"])
    job = _create_image(client, auth_headers).json()
    await _run(job["id"], store)

    refused = _create_image(client, auth_headers)
    assert refused.status_code == 429
    detail = refused.json()["detail"]
    assert detail["error"] == "daily_limit_reached" and detail["resets_at"]
    quota = client.get("/users/me/capabilities", headers=auth_headers).json()["capabilities"]["image_generation"]["daily_quota"]
    assert quota == {"limit": 1, "remaining": 0, "resets_at": quota["resets_at"]}


async def test_a_failed_job_costs_nothing_and_does_not_use_the_daily_count(db, client, auth_headers, registered_user, store):
    _plan(db, registered_user["user_id"], "free")
    set_setting(db, FREE_IMAGE_DAILY_LIMIT, 1, admin_user_id=registered_user["user_id"])
    broken = ScriptedImageEngine(fail=EngineFailed("workflow rejected"), fail_times=99)
    with use_engines(image=broken, video=None):
        job = _create_image(client, auth_headers).json()
        await _run(job["id"], store)
        db.expire_all()
        failed = client.get(f"/media/jobs/{job['id']}", headers=auth_headers).json()
        assert failed["status"] == "failed" and failed["artifact"] is None
        assert failed["error"] and not PROVIDER_NAMES.search(failed["error"])
        assert db.query(UsageRecord).one().status == "failed"
        assert broken.calls == 1  # a definitive failure is not retried
        assert _create_image(client, auth_headers).status_code == 202


async def test_a_transient_failure_is_retried_once(db, client, auth_headers, store):
    flaky = ScriptedImageEngine(fail=EngineFailed("timeout", retryable=True), fail_times=1)
    with use_engines(image=flaky, video=None):
        job = _create_image(client, auth_headers).json()
        await _run(job["id"], store)
    db.expire_all()
    assert flaky.calls == 2
    assert db.query(MediaJob).one().status == "completed"


async def test_output_that_is_not_media_is_never_stored(db, client, auth_headers, store):
    wrong = ScriptedImageEngine(content_type="text/html")
    with use_engines(image=wrong, video=None):
        job = _create_image(client, auth_headers).json()
        await _run(job["id"], store)
    db.expire_all()
    assert db.query(MediaJob).one().status == "failed"
    assert db.query(Artifact).count() == 0


def test_a_request_that_the_reservation_cannot_cover_is_refused_and_released(db, client, auth_headers, engines, monkeypatch):
    monkeypatch.setattr(media_service.entitlement_service, "get_reservation_budget_tcu", lambda db, rid: 0.0)
    refused = _create_image(client, auth_headers)
    assert refused.status_code == 429 and refused.json()["detail"]["error"] == "budget_exhausted"
    assert db.query(UsageRecord).one().status == "failed"
    assert db.query(MediaJob).count() == 0


def test_client_supplied_shapes_outside_the_plan_are_rejected(db, client, auth_headers, registered_user, engines):
    assert _create_image(client, auth_headers, aspect="panorama-8k").status_code == 422
    assert client.post("/media/images", headers=auth_headers, json={"prompt": "x" * 2001}).status_code == 422
    _plan(db, registered_user["user_id"], "max")
    too_long = client.post("/media/videos", headers=auth_headers, json={"prompt": "waves", "duration_seconds": 9})
    assert too_long.status_code == 422


# ── Editing and video ────────────────────────────────────────────────────────

async def test_editing_uses_the_users_own_uploaded_source(db, client, auth_headers, engines, store):
    image, _ = engines
    source = client.post("/media/sources", headers=auth_headers, files={"file": ("photo.png", _png(), "image/png")})
    assert source.status_code == 201
    job = _create_image(client, auth_headers, prompt="make it night", source_artifact_id=source.json()["id"]).json()
    assert job["operation"] == "edit"
    await _run(job["id"], store)
    assert image.last_source is not None and image.last_source.startswith(b"\x89PNG")


def test_a_source_must_be_an_image(client, auth_headers, engines):
    bad = client.post("/media/sources", headers=auth_headers, files={"file": ("x.html", b"<script>", "text/html")})
    assert bad.status_code == 422


def test_an_image_source_with_a_forged_content_type_is_refused(client, auth_headers, store):
    """CP22: the declared Content-Type is never trusted alone — the bytes must actually match it."""
    forged = client.post("/media/sources", headers=auth_headers,
                         files={"file": ("fake.png", b"not a real png at all" * 3, "image/png")})
    assert forged.status_code == 422


# ── Multimedia sources: video (CP22, for Wan2.2-Animate's driving_video) ────────

def test_uploading_a_video_source_succeeds_and_keeps_its_own_content_type(client, auth_headers, store):
    response = client.post("/media/sources/video", headers=auth_headers,
                           files={"file": ("clip.mp4", _mp4(), "video/mp4")})
    assert response.status_code == 201
    assert response.json()["content_type"] == "video/mp4"


def test_a_video_source_must_actually_be_a_video(client, auth_headers, store):
    bad = client.post("/media/sources/video", headers=auth_headers,
                      files={"file": ("x.html", b"<script>", "text/html")})
    assert bad.status_code == 422


def test_an_image_cannot_be_uploaded_through_the_video_source_endpoint(client, auth_headers, store):
    """The video endpoint has its own allow-list — an image content-type isn't in it."""
    wrong_kind = client.post("/media/sources/video", headers=auth_headers,
                             files={"file": ("photo.png", _png(), "image/png")})
    assert wrong_kind.status_code == 422


def test_a_video_sources_content_type_is_not_trusted_blindly(client, auth_headers, store):
    """The client claims video/mp4 but the bytes have no real ftyp box — refused, not stored."""
    fake = client.post("/media/sources/video", headers=auth_headers,
                       files={"file": ("clip.mp4", b"not really a video" * 10, "video/mp4")})
    assert fake.status_code == 422


def test_a_video_source_over_its_own_size_limit_is_refused(client, auth_headers, store, monkeypatch):
    monkeypatch.setenv("MEDIA_MAX_VIDEO_SOURCE_BYTES", "100")
    media_service.media_settings.cache_clear()
    try:
        too_big = client.post("/media/sources/video", headers=auth_headers,
                              files={"file": ("clip.mp4", _mp4() + b"0" * 200, "video/mp4")})
        assert too_big.status_code == 422
    finally:
        media_service.media_settings.cache_clear()


def test_a_video_source_under_the_video_limit_is_unaffected_by_the_separate_image_limit(client, auth_headers, store,
                                                                                        monkeypatch):
    """The image and video size caps are independent settings — shrinking the image one must not
    affect what the video endpoint accepts."""
    monkeypatch.setenv("MEDIA_MAX_SOURCE_BYTES", "10")  # far smaller than the mp4 fixture
    media_service.media_settings.cache_clear()
    try:
        ok = client.post("/media/sources/video", headers=auth_headers,
                         files={"file": ("clip.mp4", _mp4(), "video/mp4")})
        assert ok.status_code == 201
    finally:
        media_service.media_settings.cache_clear()


# ── Vectorization (CP18: deterministic, no engine/job involved) ─────────────

def test_vectorize_turns_a_raster_upload_into_a_downloadable_svg_artifact(client, auth_headers, store):
    response = client.post("/media/vectorize", headers=auth_headers, files={"file": ("photo.png", _png(), "image/png")})
    assert response.status_code == 201
    body = response.json()
    assert body["content_type"] == "image/svg+xml"

    content = client.get(f"/media/artifacts/{body['id']}/content", headers=auth_headers)
    assert content.status_code == 200
    assert content.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in content.content


def test_vectorize_refuses_a_non_image_upload_without_touching_billing(db, client, auth_headers, store):
    bad = client.post("/media/vectorize", headers=auth_headers, files={"file": ("x.html", b"<script>", "text/html")})
    assert bad.status_code == 422
    assert db.query(UsageRecord).count() == 0


def test_vectorize_never_names_the_library_in_the_response(client, auth_headers, store):
    response = client.post("/media/vectorize", headers=auth_headers, files={"file": ("photo.png", _png(), "image/png")})
    assert "vtracer" not in str(response.json()).lower()


def test_free_cannot_create_video(db, client, auth_headers, registered_user, engines):
    _plan(db, registered_user["user_id"], "free")
    refused = client.post("/media/videos", headers=auth_headers, json={"prompt": "waves"})
    assert refused.status_code == 403 and refused.json()["detail"]["error"] == "plan_not_included"


async def test_max_creates_a_video(db, client, auth_headers, registered_user, engines, store):
    _plan(db, registered_user["user_id"], "max")
    job = client.post("/media/videos", headers=auth_headers, json={"prompt": "waves on rocks", "aspect": "portrait"}).json()
    await _run(job["id"], store)
    done = client.get(f"/media/jobs/{job['id']}", headers=auth_headers).json()
    assert done["status"] == "completed"
    assert done["artifact"]["content_type"] == "video/mp4"
    # Nothing asked for: the mode's 5 s default, within the longest clip MAX's price pays for (media_service.video_offer).
    duration, _ = media_service.video_offer("max", plans.get_media_entitlement("max"), ScriptedVideoEngine())
    assert done["artifact"]["duration_seconds"] == float(min(5, duration))
    # Nothing asked about quality: economy, 480p, on MAX as on every plan.
    assert (done["artifact"]["width"], done["artifact"]["height"]) == (480, 848)


async def test_a_creation_is_opened_through_short_lived_links_a_media_element_can_load(
        db, client, auth_headers, registered_user, engines, store, monkeypatch):
    """Reading /content with fetch() follows a redirect to the bucket, which the browser blocks without CORS:
    the first video in production was stored and then "could not be opened". The card now loads links."""
    _plan(db, registered_user["user_id"], "max")
    job = client.post("/media/videos", headers=auth_headers, json={"prompt": "waves on rocks"}).json()
    await _run(job["id"], store)
    artifact = client.get(f"/media/jobs/{job['id']}", headers=auth_headers).json()["artifact"]

    # Local storage has no signed links: the card reads /content with the session instead.
    local = client.get(f"/media/artifacts/{artifact['id']}/link", headers=auth_headers).json()
    assert local["url"] is None and local["download_url"] is None
    assert local["content_type"] == "video/mp4" and local["filename"].endswith(".mp4")

    signed: list = []

    async def signed_url(*, key, expires_in, filename=None):
        signed.append((key, expires_in, filename))
        return f"https://bucket.test/{key}?expires={expires_in}" + (f"&name={filename}" if filename else "")

    monkeypatch.setattr(store, "signed_url", signed_url)
    link = client.get(f"/media/artifacts/{artifact['id']}/link", headers=auth_headers).json()
    assert link["url"].startswith("https://bucket.test/media/") and "name=" not in link["url"]
    assert link["download_url"].endswith(f"&name={local['filename']}")  # the download keeps the file's name
    assert link["expires_in"] == settings.artifact_signed_url_ttl_s
    assert all(expires == settings.artifact_signed_url_ttl_s for _, expires, _ in signed)


# ── Gallery and isolation ────────────────────────────────────────────────────

async def test_the_gallery_lists_creations_newest_first_and_filters(db, client, auth_headers, registered_user, engines, store):
    _plan(db, registered_user["user_id"], "max")
    project = client.post("/projects", headers=auth_headers, json={"name": "Campanha"}).json()
    image = _create_image(client, auth_headers, project_id=project["id"]).json()
    await _run(image["id"], store)
    video = client.post("/media/videos", headers=auth_headers, json={"prompt": "waves"}).json()
    await _run(video["id"], store)

    everything = client.get("/media/creations", headers=auth_headers).json()["creations"]
    assert [c["id"] for c in everything] == [video["id"], image["id"]]
    assert [c["id"] for c in client.get("/media/creations?kind=image", headers=auth_headers).json()["creations"]] == [image["id"]]
    in_project = client.get(f"/media/creations?project_id={project['id']}", headers=auth_headers).json()["creations"]
    assert [c["id"] for c in in_project] == [image["id"]]
    page = client.get("/media/creations?limit=1", headers=auth_headers).json()
    assert len(page["creations"]) == 1 and page["next_before"]


async def test_nobody_else_can_see_open_reuse_or_delete_a_creation(db, client, auth_headers, engines, store):
    job = _create_image(client, auth_headers).json()
    await _run(job["id"], store)
    artifact = client.get(f"/media/jobs/{job['id']}", headers=auth_headers).json()["artifact"]

    other = client.post("/auth/register", json={"email": "other@test.com", "password": "securepass123", "full_name": "Other"})
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}

    assert client.get(f"/media/jobs/{job['id']}", headers=other_headers).status_code == 404
    assert client.get(artifact["content_path"], headers=other_headers).status_code == 404
    assert client.get(artifact["download_path"], headers=other_headers).status_code == 404
    assert client.get(f"/media/artifacts/{artifact['id']}/link", headers=other_headers).status_code == 404
    assert client.delete(f"/media/creations/{job['id']}", headers=other_headers).status_code == 404
    assert client.get("/media/creations", headers=other_headers).json()["creations"] == []
    stolen_source = _create_image(client, other_headers, source_artifact_id=artifact["id"])
    assert stolen_source.status_code in (403, 404)


async def test_deleting_a_creation_removes_its_file(db, client, auth_headers, engines, store):
    job = _create_image(client, auth_headers).json()
    await _run(job["id"], store)
    artifact = client.get(f"/media/jobs/{job['id']}", headers=auth_headers).json()["artifact"]
    assert client.delete(f"/media/creations/{job['id']}", headers=auth_headers).status_code == 204
    assert client.get(artifact["content_path"], headers=auth_headers).status_code == 404
    assert db.query(Artifact).count() == 0


# ── Deployment safety ────────────────────────────────────────────────────────

async def test_the_development_engine_refuses_production():
    engine = DevelopmentImageEngine(production=True)
    assert await engine.is_available() is False


def test_local_storage_is_not_accepted_in_production(client, auth_headers, engines, monkeypatch):
    monkeypatch.setattr(media_service, "local_store_is_acceptable", lambda: False)
    capability_service.reset_cache()
    assert client.get("/users/me/capabilities", headers=auth_headers).json()["capabilities"]["image_generation"] == {"available": False}
    assert _create_image(client, auth_headers).json()["detail"]["error"] == "media_unavailable"


def test_stale_jobs_left_by_a_restart_release_their_reservation(db, client, auth_headers, engines):
    from datetime import datetime, timedelta, timezone

    job = _create_image(client, auth_headers).json()
    row = db.query(MediaJob).filter(MediaJob.id == job["id"]).one()
    row.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db.commit()
    assert media_service.reclaim_stale_jobs(db) == 1
    assert db.query(UsageRecord).one().status == "failed"
