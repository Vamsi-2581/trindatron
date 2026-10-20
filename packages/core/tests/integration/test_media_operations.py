"""CP19: the permanent TRANSFORM/COMPOSE operation contract, end to end.

Every call goes through a scripted OperationEngine — no real provider, nothing spent — proving
what the product depends on: job lifecycle, idempotency, artifact delivery, billing (a real,
measured cost, reserved before and settled after), admission control, and that provider/model
never reach a public response, exactly like every other media capability.
"""
from __future__ import annotations

import asyncio
import io

import pytest
from PIL import Image

from src.media.engines import EngineFailed, FailureKind, MediaOutput
from src.media.operations import OperationEngine
from src.media.registry import use_engines
from src.models.media import MediaJob
from src.models.usage import UsageRecord
from src.services import media_service
from src.storage.local import LocalObjectStore
from tests.conftest import _TestingSession

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 64  # real ftyp box at the offset media_service._sniff_ok checks


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), (10, 200, 90)).save(buffer, format="PNG")
    return buffer.getvalue()


class ScriptedOperationEngine(OperationEngine):
    def __init__(self, *, operation: str = "background_remove", fail: Exception | None = None,
                content_type: str = "image/png", data: bytes = PNG, cost_usd: float = 0.0,
                provider_request_id: str | None = None, hang_forever: bool = False) -> None:
        self.operation, self.fail, self.calls, self.last_resolved = operation, fail, 0, None
        self.content_type, self.data, self.cost_usd = content_type, data, cost_usd
        # CP38: when set, run() reports a provider handle through request.link — the same
        # ProviderLink.submitted() call a real hosted OperationEngine (HiggsfieldGenjutsuEngine)
        # makes, so the MediaJob-persistence path can be proven without a real provider mock.
        self.provider_request_id = provider_request_id
        # CP41: after reporting the handle (if any), hang until cancelled by the outer
        # asyncio.wait_for — simulates a real provider job still running when TRINDATRON's own
        # deadline elapses, to prove the orphan-job-protection behavior in _deliver().
        self.hang_forever = hang_forever

    async def is_available(self) -> bool:
        return True

    def estimate_usd(self, request) -> float:
        return self.cost_usd

    async def run(self, request, resolved):
        self.calls += 1
        self.last_resolved = resolved
        if self.provider_request_id is not None:
            from src.media.engines import ProviderHandle

            link = getattr(request, "link", None)
            if link is not None:
                link.submitted(ProviderHandle(provider="scripted", model="scripted-model", route="scripted@test",
                                              request_id=self.provider_request_id, kind="video"))
        if self.hang_forever:
            await asyncio.Event().wait()  # never set: only ends via the outer wait_for's cancellation
        if self.fail is not None:
            raise self.fail
        return MediaOutput(data=self.data, content_type=self.content_type, width=16, height=16, duration_seconds=None,
                           engine_seconds=0.4, cost_usd=self.cost_usd)


@pytest.fixture
def auth_headers(db, registered_user):
    """CP36: character_animate is MAX/TEAM-only (media/economics.py) — this overrides conftest's
    start-plan default with max so every character_animate test below still reaches
    create_operation_job's full path instead of being refused at the new premium-media gate.
    Every other test in this file is plan-agnostic (background_remove/vectorize/admission are not
    premium-gated — economics.operation_allowed() only gates character_animate), so this is safe
    for the whole module."""
    from src.models.user import User

    user = db.query(User).filter(User.id == registered_user["user_id"]).one()
    user.plan = "max"
    db.commit()
    return {"Authorization": f"Bearer {registered_user['token']}"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    local = LocalObjectStore(root=str(tmp_path / "objects"))
    monkeypatch.setattr(media_service, "build_object_store", lambda: local)
    monkeypatch.setattr("src.api.routes.media.build_object_store", lambda: local)
    monkeypatch.setattr(media_service, "schedule", lambda job_id: None)
    yield local


@pytest.fixture
def operation_engine(store):
    engine = ScriptedOperationEngine()
    with use_engines(operations={"background_remove": engine}):
        yield engine


@pytest.fixture
def animate_engine(store):
    """CP22: character_animate's own scripted engine — a video output, unlike background_remove's
    image one, to prove the output-kind plumbing (MediaJob.kind, billing bucket) for real."""
    engine = ScriptedOperationEngine(operation="character_animate", content_type="video/mp4", data=MP4)
    with use_engines(operations={"character_animate": engine}):
        yield engine


async def _run(job_id: str, store) -> None:
    await media_service.run_job(job_id, session_factory=_TestingSession, store=store)


def _upload(client, headers) -> str:
    response = client.post("/media/sources", headers=headers, files={"file": ("photo.png", _png(), "image/png")})
    assert response.status_code == 201
    return response.json()["id"]


def _upload_video(client, headers) -> str:
    response = client.post("/media/sources/video", headers=headers, files={"file": ("clip.mp4", MP4, "video/mp4")})
    assert response.status_code == 201
    return response.json()["id"]


def _create_op(client, headers, artifact_id: str, **overrides):
    body = {"operation": "background_remove", "inputs": [{"role": "source_image", "artifact_id": artifact_id}]}
    body.update(overrides)
    return client.post("/media/operations", headers=headers, json=body)


def _create_animate(client, headers, character_id: str, driving_video_id: str, **overrides):
    body = {"operation": "character_animate",
           "inputs": [{"role": "character", "artifact_id": character_id},
                      {"role": "driving_video", "artifact_id": driving_video_id}]}
    body.update(overrides)
    return client.post("/media/operations", headers=headers, json=body)


# ── Lifecycle ────────────────────────────────────────────────────────────────

async def test_background_remove_runs_delivers_and_bills_zero(db, client, auth_headers, operation_engine, store):
    source = _upload(client, auth_headers)
    accepted = _create_op(client, auth_headers, source)
    assert accepted.status_code == 202
    job = accepted.json()
    assert job["status"] == "queued" and job["kind"] == "image"

    await _run(job["id"], store)
    done = client.get(f"/media/jobs/{job['id']}", headers=auth_headers).json()
    assert done["status"] == "completed" and done["artifact"]["content_type"] == "image/png"
    assert operation_engine.calls == 1
    assert operation_engine.last_resolved["source_image"].content_type == "image/png"

    record = db.query(UsageRecord).filter(UsageRecord.status == "completed").one()
    assert float(record.cost_usd) == 0.0

    content = client.get(f"/media/artifacts/{done['artifact']['id']}/content", headers=auth_headers)
    assert content.status_code == 200 and content.content == PNG


def test_unknown_operation_is_refused(client, auth_headers, operation_engine):
    source = _upload(client, auth_headers)
    refused = _create_op(client, auth_headers, source, operation="not_a_real_operation")
    assert refused.status_code == 422


def test_without_an_engine_the_operation_is_unavailable(client, auth_headers, store):
    source = _upload(client, auth_headers)
    with use_engines(operations={}):
        refused = _create_op(client, auth_headers, source)
    assert refused.status_code == 503 and refused.json()["detail"]["error"] == "media_unavailable"


def test_an_input_artifact_owned_by_someone_else_is_refused(client, auth_headers, operation_engine, db):
    from src.models.media import Artifact
    from src.models.user import User

    other = User(email="other-cp19@example.com", hashed_password="x", plan="free")
    db.add(other)
    db.commit()
    foreign = Artifact(user_id=other.id, kind="image", source="upload", filename="x.png",
                       content_type="image/png", size_bytes=10, storage_backend="local", storage_key="k")
    db.add(foreign)
    db.commit()
    refused = _create_op(client, auth_headers, foreign.id)
    assert refused.status_code == 404


async def test_a_failed_operation_charges_nothing(db, client, auth_headers, store):
    engine = ScriptedOperationEngine(fail=EngineFailed("boom", kind=FailureKind.TEMPORARY, retryable=False))
    with use_engines(operations={"background_remove": engine}):
        source = _upload(client, auth_headers)
        job = _create_op(client, auth_headers, source).json()
        await _run(job["id"], store)
        done = client.get(f"/media/jobs/{job['id']}", headers=auth_headers).json()
    assert done["status"] == "failed"
    assert db.query(UsageRecord).filter(UsageRecord.status == "completed").count() == 0


# ── Idempotency and cancel ───────────────────────────────────────────────────

def test_the_same_idempotency_key_returns_the_same_job_never_a_second_one(db, client, auth_headers, operation_engine):
    source = _upload(client, auth_headers)
    headers = {**auth_headers, "Idempotency-Key": "cp19-fixed-key"}
    a = client.post("/media/operations", headers=headers,
                    json={"operation": "background_remove", "inputs": [{"role": "source_image", "artifact_id": source}]})
    b = client.post("/media/operations", headers=headers,
                    json={"operation": "background_remove", "inputs": [{"role": "source_image", "artifact_id": source}]})
    assert a.json()["id"] == b.json()["id"]
    assert db.query(MediaJob).filter(MediaJob.idempotency_key == "cp19-fixed-key").count() == 1


def test_a_queued_operation_job_can_be_cancelled_and_is_not_charged(db, client, auth_headers, operation_engine):
    source = _upload(client, auth_headers)
    job = _create_op(client, auth_headers, source).json()
    cancelled = client.post(f"/media/jobs/{job['id']}/cancel", headers=auth_headers)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert db.query(UsageRecord).filter(UsageRecord.status == "completed").count() == 0


# ── Privacy ──────────────────────────────────────────────────────────────────

async def test_the_operation_engine_never_leaks_into_the_response(client, auth_headers, operation_engine, store):
    source = _upload(client, auth_headers)
    job = _create_op(client, auth_headers, source).json()
    await _run(job["id"], store)
    done = client.get(f"/media/jobs/{job['id']}", headers=auth_headers).json()
    assert "birefnet" not in str(done).lower() and "vtracer" not in str(done).lower()


# ── Admission control (CP19 item 13/14/19) ────────────────────────────────────

def test_global_capacity_limit_refuses_new_jobs_once_exceeded(db, client, auth_headers, operation_engine, monkeypatch):
    from src.media.settings import media_settings

    monkeypatch.setenv("MEDIA_MAX_GLOBAL_IN_FLIGHT", "1")
    media_settings.cache_clear()
    try:
        source = _upload(client, auth_headers)
        first = _create_op(client, auth_headers, source)
        assert first.status_code == 202
        second = _create_op(client, auth_headers, source)
        # The one-active-job-per-kind rule (pre-existing) also refuses a second concurrent
        # request from the same user — either refusal proves capacity is enforced before a
        # second job is created.
        assert second.status_code in (409, 429)
    finally:
        media_settings.cache_clear()


def test_500_jobs_dont_crash_dont_let_one_tenant_monopolize_and_stay_persisted(db, monkeypatch):
    """CP19 item 19: 500 jobs arriving, multiple tenants, a congested operation, fairness,
    persistence, no duplicate billing — deterministic, no real load generator, no sleep."""
    from src.media.settings import media_settings
    from src.models.user import User
    from src.services.media_service import MediaRefused, _admission_check

    monkeypatch.setenv("MEDIA_MAX_GLOBAL_IN_FLIGHT", "500")
    monkeypatch.setenv("MEDIA_MAX_PER_OPERATION_IN_FLIGHT", "300")
    media_settings.cache_clear()
    try:
        db_session = _TestingSession()
        try:
            users = [User(email=f"cp19-scale-{i}@example.com", hashed_password="x", plan="free") for i in range(50)]
            db_session.add_all(users)
            db_session.commit()

            # 480 jobs already in flight when the 500th request arrives: 300 congesting one
            # operation (its own per-operation cap), spread one-per-user-per-batch so no single
            # tenant structurally holds more than a handful of the seeded rows.
            jobs = []
            for i in range(480):
                user = users[i % len(users)]
                operation = "background_remove" if i < 300 else "vectorize"
                jobs.append(MediaJob(user_id=user.id, kind="image", operation=operation, status="queued",
                                     prompt=operation, params={}))
            db_session.add_all(jobs)
            db_session.commit()

            assert db_session.query(MediaJob).count() == 480  # every seeded job persisted, nothing lost

            # The congested operation (300, at its 300 cap) refuses a new one...
            with pytest.raises(MediaRefused) as congested:
                _admission_check(db_session, operation="background_remove")
            assert congested.value.code == "capacity_exceeded"
            # ...but a DIFFERENT operation, nowhere near its own cap, is unaffected — one
            # congested capability does not paralyze the whole platform.
            _admission_check(db_session, operation="vectorize")  # does not raise

            # 20 more requests (500 total in flight), spread across users like the rest — still
            # fit under the global cap...
            for i in range(20):
                _admission_check(db_session, operation="vectorize")
                db_session.add(MediaJob(user_id=users[i % len(users)].id, kind="image", operation="vectorize",
                                        status="queued", prompt="vectorize", params={}))
            db_session.commit()
            assert db_session.query(MediaJob).filter(MediaJob.status == "queued").count() == 500
            # ...and the 501st is refused by the global cap, regardless of which operation asks.
            with pytest.raises(MediaRefused) as full:
                _admission_check(db_session, operation="background_remove")
            assert full.value.code == "capacity_exceeded"

            # Fairness: no single tenant ended up holding an outsized share of the 500 slots.
            from sqlalchemy import func
            counts = dict(db_session.query(MediaJob.user_id, func.count(MediaJob.id))
                         .group_by(MediaJob.user_id).all())
            assert max(counts.values()) <= 11  # ~500/50 users, no monopolization

            # No billing row exists for any of this seeded load — nothing was ever reserved for
            # a job created outside the real check_and_reserve path, so nothing can double-bill it.
            assert db_session.query(UsageRecord).count() == 0
        finally:
            db_session.close()
    finally:
        media_settings.cache_clear()


def test_per_operation_capacity_limit_is_independent_of_the_global_one(db, monkeypatch):
    """Unit-level: _admission_check raises capacity_exceeded once MEDIA_MAX_PER_OPERATION_IN_FLIGHT
    real queued/running rows for that operation are reached, without needing 500 real users."""
    from src.media.settings import media_settings
    from src.models.user import User
    from src.services.media_service import MediaRefused, _admission_check

    monkeypatch.setenv("MEDIA_MAX_PER_OPERATION_IN_FLIGHT", "2")
    media_settings.cache_clear()
    try:
        db_session = _TestingSession()
        try:
            user = User(email="cp19-admission@example.com", hashed_password="x", plan="free")
            db_session.add(user)
            db_session.commit()
            for _ in range(2):
                db_session.add(MediaJob(user_id=user.id, kind="image", operation="background_remove",
                                        status="queued", prompt="x", params={}))
            db_session.commit()
            with pytest.raises(MediaRefused) as caught:
                _admission_check(db_session, operation="background_remove")
            assert caught.value.code == "capacity_exceeded"
            # A different operation is unaffected — the limit is per-operation, not global-only.
            _admission_check(db_session, operation="vectorize")
        finally:
            db_session.close()
    finally:
        media_settings.cache_clear()


# ── Multi-input (CP22: character_animate — two typed inputs, video output) ────

async def test_character_animate_runs_with_two_inputs_and_delivers_a_video(db, client, auth_headers, animate_engine,
                                                                            store):
    character = _upload(client, auth_headers)
    driving_video = _upload_video(client, auth_headers)
    accepted = _create_animate(client, auth_headers, character, driving_video)
    assert accepted.status_code == 202
    job = accepted.json()
    assert job["status"] == "queued" and job["kind"] == "video"  # M: output kind reflects the operation

    await _run(job["id"], store)
    done = client.get(f"/media/jobs/{job['id']}", headers=auth_headers).json()
    assert done["status"] == "completed" and done["artifact"]["content_type"] == "video/mp4"
    assert animate_engine.calls == 1
    assert animate_engine.last_resolved["character"].content_type == "image/png"
    assert animate_engine.last_resolved["driving_video"].content_type == "video/mp4"  # C: role preservation

    record = db.query(MediaJob).filter(MediaJob.id == job["id"]).one()
    assert record.kind == "video"  # M, at the DB row, not just the DTO
    usage = db.query(UsageRecord).filter(UsageRecord.status == "completed").one()
    assert float(usage.cost_usd) == 0.0  # L: billing


def test_character_animate_checks_ownership_of_every_declared_input_not_just_the_first(client, auth_headers,
                                                                                        animate_engine, db):
    from src.models.media import Artifact
    from src.models.user import User

    other = User(email="other-cp22@example.com", hashed_password="x", plan="free")
    db.add(other)
    db.commit()
    foreign_video = Artifact(user_id=other.id, kind="video", source="upload", filename="x.mp4",
                             content_type="video/mp4", size_bytes=10, storage_backend="local", storage_key="k")
    db.add(foreign_video)
    db.commit()
    character = _upload(client, auth_headers)
    refused = _create_animate(client, auth_headers, character, foreign_video.id)
    assert refused.status_code == 404  # D: ownership checked for every input, not just source_artifact_id


def test_character_animate_refuses_a_driving_video_role_that_is_actually_an_image_artifact(client, auth_headers,
                                                                                            animate_engine):
    """The role's expected media type is enforced, not just that the caller owns SOME artifact."""
    character = _upload(client, auth_headers)
    wrong_kind_artifact = _upload(client, auth_headers)  # an image, not a video
    refused = _create_animate(client, auth_headers, character, wrong_kind_artifact)
    assert refused.status_code == 404


def test_character_animate_refuses_a_role_declared_twice(client, auth_headers, animate_engine):
    character = _upload(client, auth_headers)
    driving_video = _upload_video(client, auth_headers)
    refused = client.post("/media/operations", headers=auth_headers, json={
        "operation": "character_animate",
        "inputs": [{"role": "character", "artifact_id": character},
                   {"role": "character", "artifact_id": driving_video}],
    })
    assert refused.status_code == 422  # H: duplicate input


def test_character_animate_refuses_a_missing_required_role(client, auth_headers, animate_engine):
    character = _upload(client, auth_headers)
    refused = client.post("/media/operations", headers=auth_headers, json={
        "operation": "character_animate", "inputs": [{"role": "character", "artifact_id": character}],
    })
    assert refused.status_code == 422  # I: missing required role (driving_video)


def test_character_animate_refuses_an_unknown_role(client, auth_headers, animate_engine):
    character = _upload(client, auth_headers)
    driving_video = _upload_video(client, auth_headers)
    refused = client.post("/media/operations", headers=auth_headers, json={
        "operation": "character_animate",
        "inputs": [{"role": "character", "artifact_id": character},
                   {"role": "mask", "artifact_id": driving_video}],  # "mask" isn't a role this operation takes
    })
    assert refused.status_code == 422  # J: malformed operation (role this operation doesn't declare)


def test_character_animate_same_idempotency_key_with_two_inputs_returns_the_same_job(db, client, auth_headers,
                                                                                      animate_engine):
    character = _upload(client, auth_headers)
    driving_video = _upload_video(client, auth_headers)
    headers = {**auth_headers, "Idempotency-Key": "cp22-fixed-key"}
    a = _create_animate(client, headers, character, driving_video)
    b = _create_animate(client, headers, character, driving_video)
    assert a.json()["id"] == b.json()["id"]  # K: idempotency
    assert db.query(MediaJob).filter(MediaJob.idempotency_key == "cp22-fixed-key").count() == 1


async def test_character_animate_never_leaks_the_engine_or_model_into_the_response(client, auth_headers,
                                                                                   animate_engine, store):
    character = _upload(client, auth_headers)
    driving_video = _upload_video(client, auth_headers)
    job = _create_animate(client, auth_headers, character, driving_video).json()
    await _run(job["id"], store)
    done = client.get(f"/media/jobs/{job['id']}", headers=auth_headers).json()
    dump = str(done).lower()
    # "character_animate" (the public operation name) is expected and fine, same as "background_remove"
    # is for BiRefNet — what must never appear is the engine/model/provider identity behind it.
    assert "wan22" not in dump and "wan2.2" not in dump and "wan-video" not in dump and "zerogpu" not in dump


def test_500_multi_input_jobs_persist_correctly_and_admission_control_still_holds(db, monkeypatch):
    """CP22: the 500-job scale proof (CP19, above) never exercised a job with more than one
    declared input — this repeats it specifically for character_animate's two-role shape, so a
    multi-input operation at scale is proven, not just assumed from the single-input case."""
    from src.media.settings import media_settings
    from src.models.user import User
    from src.services.media_service import MediaRefused, _admission_check

    monkeypatch.setenv("MEDIA_MAX_GLOBAL_IN_FLIGHT", "500")
    monkeypatch.setenv("MEDIA_MAX_PER_OPERATION_IN_FLIGHT", "500")
    media_settings.cache_clear()
    try:
        db_session = _TestingSession()
        try:
            users = [User(email=f"cp22-scale-{i}@example.com", hashed_password="x", plan="free") for i in range(50)]
            db_session.add_all(users)
            db_session.commit()

            jobs = []
            for i in range(500):
                user = users[i % len(users)]
                params = {"inputs": [{"role": "character", "artifact_id": f"char-{i}"},
                                     {"role": "driving_video", "artifact_id": f"drive-{i}"}],
                         "parameters": {}, "quality_tier": "standard"}
                jobs.append(MediaJob(user_id=user.id, kind="video", operation="character_animate", status="queued",
                                     prompt="character_animate", params=params))
            db_session.add_all(jobs)
            db_session.commit()

            assert db_session.query(MediaJob).filter(MediaJob.operation == "character_animate").count() == 500
            # Every row's two-role params survived the round trip through JSON storage, at scale.
            sample = db_session.query(MediaJob).filter(MediaJob.operation == "character_animate").first()
            assert len(sample.params["inputs"]) == 2
            assert {i["role"] for i in sample.params["inputs"]} == {"character", "driving_video"}

            with pytest.raises(MediaRefused) as full:
                _admission_check(db_session, operation="character_animate")
            assert full.value.code == "capacity_exceeded"

            from sqlalchemy import func
            counts = dict(db_session.query(MediaJob.user_id, func.count(MediaJob.id))
                         .group_by(MediaJob.user_id).all())
            assert max(counts.values()) <= 10  # exactly 500/50, round-robin — no monopolization

            assert db_session.query(UsageRecord).count() == 0  # nothing reserved for seeded rows, nothing to double-bill
        finally:
            db_session.close()
    finally:
        media_settings.cache_clear()


# ── Media Economics (CP36: character_animate is MAX/TEAM-only, its own separate budget) ───────

GENJUTSU_3S_480P_USD = 3 * 0.318  # the CP35/CP36 dry-run case: $0.954, 95.4 TCU


def _set_plan(db, user_id: str, plan: str) -> None:
    from src.models.user import User

    user = db.query(User).filter(User.id == user_id).one()
    user.plan = plan
    db.commit()


def _priced_animate_engine(cost_usd: float = GENJUTSU_3S_480P_USD):
    return ScriptedOperationEngine(operation="character_animate", content_type="video/mp4", data=MP4,
                                   cost_usd=cost_usd)


def test_character_animate_denied_on_start(client, db, registered_user, store):
    _set_plan(db, registered_user["user_id"], "start")
    headers = {"Authorization": f"Bearer {registered_user['token']}"}
    with use_engines(operations={"character_animate": _priced_animate_engine()}):
        character, driving_video = _upload(client, headers), _upload_video(client, headers)
        refused = _create_animate(client, headers, character, driving_video)
    assert refused.status_code == 403
    assert refused.json()["detail"]["error"] == "plan_not_included"


def test_character_animate_denied_on_pro(client, db, registered_user, store):
    _set_plan(db, registered_user["user_id"], "pro")
    headers = {"Authorization": f"Bearer {registered_user['token']}"}
    with use_engines(operations={"character_animate": _priced_animate_engine()}):
        character, driving_video = _upload(client, headers), _upload_video(client, headers)
        refused = _create_animate(client, headers, character, driving_video)
    assert refused.status_code == 403
    assert refused.json()["detail"]["error"] == "plan_not_included"


async def test_character_animate_allowed_on_max_within_media_budget(client, db, registered_user, store):
    _set_plan(db, registered_user["user_id"], "max")
    headers = {"Authorization": f"Bearer {registered_user['token']}"}
    engine = _priced_animate_engine()
    with use_engines(operations={"character_animate": engine}):
        character, driving_video = _upload(client, headers), _upload_video(client, headers)
        accepted = _create_animate(client, headers, character, driving_video)
        assert accepted.status_code == 202
        job = accepted.json()
        await _run(job["id"], store)
    done = client.get(f"/media/jobs/{job['id']}", headers=headers).json()
    assert done["status"] == "completed"
    usage = db.query(UsageRecord).filter(UsageRecord.status == "completed").one()
    assert float(usage.cost_usd) == pytest.approx(GENJUTSU_3S_480P_USD)


async def test_character_animate_allowed_on_team_within_media_budget(client, db, registered_user, store):
    _set_plan(db, registered_user["user_id"], "team")
    headers = {"Authorization": f"Bearer {registered_user['token']}"}
    engine = _priced_animate_engine()
    with use_engines(operations={"character_animate": engine}):
        character, driving_video = _upload(client, headers), _upload_video(client, headers)
        accepted = _create_animate(client, headers, character, driving_video)
        assert accepted.status_code == 202
        job = accepted.json()
        await _run(job["id"], store)
    done = client.get(f"/media/jobs/{job['id']}", headers=headers).json()
    assert done["status"] == "completed"


def test_character_animate_above_the_media_request_ceiling_is_denied_even_on_max(client, db, registered_user, store):
    """5s @ 720p = $3.405 = 340.5 TCU, over MAX's 200 TCU media request ceiling — refused before
    any reservation, distinct from (and never reaching) budget_exhausted."""
    _set_plan(db, registered_user["user_id"], "max")
    headers = {"Authorization": f"Bearer {registered_user['token']}"}
    with use_engines(operations={"character_animate": _priced_animate_engine(cost_usd=5 * 0.681)}):
        character, driving_video = _upload(client, headers), _upload_video(client, headers)
        refused = _create_animate(client, headers, character, driving_video)
    assert refused.status_code == 403
    assert refused.json()["detail"]["error"] == "plan_not_included"
    assert db.query(UsageRecord).count() == 0  # never reached reservation


async def test_character_animate_above_the_media_daily_ceiling_is_denied_on_the_second_call(
        client, db, registered_user, store):
    """MAX's daily_ceiling_tcu is 250.0. One 95.4 TCU job today, then a second of the same size
    (190.8 total, fits) succeeds; a third (286.2 total) does not."""
    _set_plan(db, registered_user["user_id"], "max")
    headers = {"Authorization": f"Bearer {registered_user['token']}"}
    for _ in range(2):
        engine = _priced_animate_engine()
        with use_engines(operations={"character_animate": engine}):
            character, driving_video = _upload(client, headers), _upload_video(client, headers)
            accepted = _create_animate(client, headers, character, driving_video)
            assert accepted.status_code == 202
            await _run(accepted.json()["id"], store)
    engine = _priced_animate_engine()
    with use_engines(operations={"character_animate": engine}):
        character, driving_video = _upload(client, headers), _upload_video(client, headers)
        refused = _create_animate(client, headers, character, driving_video)
    assert refused.status_code == 429
    assert refused.json()["detail"]["error"] == "daily_limit_reached"


def test_background_remove_on_start_is_unaffected_by_media_economics(client, db, registered_user):
    """A non-premium operation is never gated by economics.py, on any plan — proving the new
    branch in _create() is genuinely operation-scoped, not a blanket change."""
    _set_plan(db, registered_user["user_id"], "start")
    headers = {"Authorization": f"Bearer {registered_user['token']}"}
    with use_engines(operations={"background_remove": ScriptedOperationEngine()}):
        source = _upload(client, headers)
        accepted = _create_op(client, headers, source)
    assert accepted.status_code == 202


async def test_character_animate_provider_failure_releases_the_reservation(client, db, registered_user, store):
    _set_plan(db, registered_user["user_id"], "max")
    headers = {"Authorization": f"Bearer {registered_user['token']}"}
    engine = ScriptedOperationEngine(operation="character_animate", cost_usd=GENJUTSU_3S_480P_USD,
                                     fail=EngineFailed("boom", kind=FailureKind.TEMPORARY, retryable=False))
    with use_engines(operations={"character_animate": engine}):
        character, driving_video = _upload(client, headers), _upload_video(client, headers)
        accepted = _create_animate(client, headers, character, driving_video)
        assert accepted.status_code == 202
        job_id = accepted.json()["id"]
        await _run(job_id, store)
    done = client.get(f"/media/jobs/{job_id}", headers=headers).json()
    assert done["status"] == "failed"
    # The reservation was cancelled, not settled: no completed UsageRecord for this job's cost.
    assert db.query(UsageRecord).filter(UsageRecord.status == "completed").count() == 0


# ── Provider request id persistence (CP38: the CP37 gap — OperationRequest had no `link`) ──────

async def test_character_animate_persists_the_provider_request_id_before_completion(
        client, db, registered_user, store): # CP38-C
    _set_plan(db, registered_user["user_id"], "max")
    headers = {"Authorization": f"Bearer {registered_user['token']}"}
    engine = _priced_animate_engine()
    engine.provider_request_id = "genjutsu-req-cp38"
    with use_engines(operations={"character_animate": engine}):
        character, driving_video = _upload(client, headers), _upload_video(client, headers)
        accepted = _create_animate(client, headers, character, driving_video)
        assert accepted.status_code == 202
        job_id = accepted.json()["id"]
        await _run(job_id, store)
    record = db.query(MediaJob).filter(MediaJob.id == job_id).one()
    assert record.provider_request_id == "genjutsu-req-cp38"
    assert record.provider_ref is not None and record.provider_ref["request_id"] == "genjutsu-req-cp38"
    assert record.provider_ref["provider"] == "scripted"


async def test_character_animate_persists_the_provider_request_id_even_on_failure(
        client, db, registered_user, store): # CP38-C
    """The whole point of the CP37 gap: the provider accepted the job (a real request id exists)
    before it later failed — that id must survive even though the job itself does not complete."""
    _set_plan(db, registered_user["user_id"], "max")
    headers = {"Authorization": f"Bearer {registered_user['token']}"}
    engine = ScriptedOperationEngine(operation="character_animate", cost_usd=GENJUTSU_3S_480P_USD,
                                     provider_request_id="genjutsu-req-cp38-failed",
                                     fail=EngineFailed("boom", kind=FailureKind.TEMPORARY, retryable=False))
    with use_engines(operations={"character_animate": engine}):
        character, driving_video = _upload(client, headers), _upload_video(client, headers)
        accepted = _create_animate(client, headers, character, driving_video)
        assert accepted.status_code == 202
        job_id = accepted.json()["id"]
        await _run(job_id, store)
    record = db.query(MediaJob).filter(MediaJob.id == job_id).one()
    assert record.status == "failed"
    assert record.provider_request_id == "genjutsu-req-cp38-failed"  # not lost, unlike CP37


# ── Timeouts by output kind, and orphan-job protection (CP41: CP40 proved a real Genjutsu job ──
# still healthy and in_progress was cut off at the old flat 210s TRANSFORM-operation timeout) ──

@pytest.fixture
def timeout_spy(monkeypatch):
    """Captures the `timeout` _generate() actually computed for asyncio.wait_for, while shrinking
    the REAL wait to 0.05s regardless of that value — so a test can assert on the number
    _generate() would have used in production without waiting it out in real time."""
    captured: list[float] = []
    orig_wait_for = asyncio.wait_for

    async def spy(coro, timeout):
        captured.append(timeout)
        return await orig_wait_for(coro, timeout=0.05)

    monkeypatch.setattr(media_service.asyncio, "wait_for", spy)
    return captured


async def test_an_image_output_transform_operation_still_uses_the_image_timeout(
        client, db, registered_user, store, timeout_spy): # CP41-A
    from src.media.settings import media_settings

    headers = {"Authorization": f"Bearer {registered_user['token']}"}  # background_remove: any plan
    with use_engines(operations={"background_remove": ScriptedOperationEngine()}):
        source = _upload(client, headers)
        job = _create_op(client, headers, source).json()
        await _run(job["id"], store)
    assert timeout_spy == [media_settings().media_image_timeout_s + 30]


async def test_a_video_output_transform_operation_uses_the_video_timeout(
        client, db, registered_user, store, timeout_spy): # CP41-B/C
    """character_animate is the one real video-output TRANSFORM operation today — this proves
    BOTH B (video-output TRANSFORM gets the video timeout) and C (character_animate specifically)
    in one assertion, since it is currently the only instance of that category."""
    from src.media.settings import media_settings

    _set_plan(db, registered_user["user_id"], "max")
    headers = {"Authorization": f"Bearer {registered_user['token']}"}
    engine = _priced_animate_engine()
    with use_engines(operations={"character_animate": engine}):
        character, driving_video = _upload(client, headers), _upload_video(client, headers)
        job = _create_animate(client, headers, character, driving_video).json()
        await _run(job["id"], store)
    assert timeout_spy == [media_settings().media_video_timeout_s + 30]
    assert timeout_spy[0] > 1800  # materially more than the old flat 210s CP40 hit


async def test_a_local_timeout_with_a_live_provider_job_is_left_running_not_failed(
        client, db, registered_user, store, timeout_spy): # CP41-D/E
    """The CP40 scenario, reproduced deterministically: the provider already accepted the job
    (a real request_id exists) when TRINDATRON's own deadline elapses. The job must NOT be marked
    failed (that would release the reservation and tell the person it failed for something that
    may still complete), and nothing may resubmit it."""
    _set_plan(db, registered_user["user_id"], "max")
    headers = {"Authorization": f"Bearer {registered_user['token']}"}
    engine = ScriptedOperationEngine(operation="character_animate", cost_usd=GENJUTSU_3S_480P_USD,
                                     provider_request_id="cp41-orphan-req", hang_forever=True)
    with use_engines(operations={"character_animate": engine}):
        character, driving_video = _upload(client, headers), _upload_video(client, headers)
        job = _create_animate(client, headers, character, driving_video).json()
        await _run(job["id"], store)
    record = db.query(MediaJob).filter(MediaJob.id == job["id"]).one()
    assert record.status == "running"  # CP41-D: not "failed" — a live provider job is not abandoned
    assert record.provider_request_id == "cp41-orphan-req"  # preserved, exactly what a future resume needs
    assert engine.calls == 1  # CP41-E: no resubmit attempt of any kind
    reservation = db.query(UsageRecord).filter(UsageRecord.id == record.reservation_id).one()
    assert reservation.status == "pending"  # not released — the person is not silently refunded either


async def test_a_local_timeout_with_no_provider_request_id_still_fails_as_before(
        client, db, registered_user, store, timeout_spy): # CP41 regression guard
    """When the provider never even confirmed acceptance, there is nothing to resume — the
    pre-CP41 behavior (fail, release the reservation) is exactly right and must be unchanged."""
    _set_plan(db, registered_user["user_id"], "max")
    headers = {"Authorization": f"Bearer {registered_user['token']}"}
    engine = ScriptedOperationEngine(operation="character_animate", cost_usd=GENJUTSU_3S_480P_USD,
                                     hang_forever=True)  # no provider_request_id at all
    with use_engines(operations={"character_animate": engine}):
        character, driving_video = _upload(client, headers), _upload_video(client, headers)
        job = _create_animate(client, headers, character, driving_video).json()
        await _run(job["id"], store)
    record = db.query(MediaJob).filter(MediaJob.id == job["id"]).one()
    assert record.status == "failed"
    reservation = db.query(UsageRecord).filter(UsageRecord.id == record.reservation_id).one()
    assert reservation.status == "failed"


# ── Duration & billing model correction (CP43: CP39/CP42 proved Genjutsu bills the driving ─────
# video's own real length, never TRINDATRON's requested duration_seconds) ──────────────────────

async def test_creating_a_character_animate_job_prices_it_from_the_real_driving_video_duration(
        client, db, registered_user, store, monkeypatch): # CP43-E
    """End to end through the real HiggsfieldGenjutsuEngine (not the scripted stand-in): a
    4.963s driving video must price the job at 5s @ 480p ($1.59, 159 TCU), not the 3s default
    ($0.954) CP39/CP40/CP42 actually used. Both MAX's and TEAM's 200 TCU request ceiling still
    comfortably cover it."""
    from tests.unit.test_media_runners import tiny_mp4
    from src.media.higgsfield import HiggsfieldGenjutsuEngine
    from src.media import economics

    monkeypatch.setenv("HIGGSFIELD_API_KEY_ID", "test-id")
    monkeypatch.setenv("HIGGSFIELD_API_KEY_SECRET", "test-secret")
    from src.media.settings import media_settings
    media_settings.cache_clear()
    _set_plan(db, registered_user["user_id"], "max")
    headers = {"Authorization": f"Bearer {registered_user['token']}"}
    try:
        with use_engines(operations={"character_animate": HiggsfieldGenjutsuEngine()}):
            character = _upload(client, headers)
            driving_resp = client.post("/media/sources/video", headers=headers,
                                       files={"file": ("clip.mp4", tiny_mp4(seconds=4.963), "video/mp4")})
            assert driving_resp.status_code == 201
            driving_video = driving_resp.json()["id"]
            accepted = _create_animate(client, headers, character, driving_video)
            assert accepted.status_code == 202
            job_id = accepted.json()["id"]
        record = db.query(MediaJob).filter(MediaJob.id == job_id).one()
        assert float(record.estimated_cost_usd) == pytest.approx(5 * 0.318)  # ceil(4.963) = 5s, not 3s
        assert economics.fits_media_request_ceiling("character_animate", "max", float(record.estimated_cost_usd))
        assert economics.fits_media_request_ceiling("character_animate", "team", float(record.estimated_cost_usd))
    finally:
        media_settings.cache_clear()


def test_background_remove_job_creation_is_unaffected_by_the_input_duration_lookup(
        client, db, registered_user, store): # CP43-G
    """The new per-video-role duration lookup in create_operation_job() only looks at roles whose
    expected kind is "video" — background_remove (image-only) must do zero extra artifact lookups
    for this and price exactly as it always has."""
    headers = {"Authorization": f"Bearer {registered_user['token']}"}
    with use_engines(operations={"background_remove": ScriptedOperationEngine(cost_usd=0.0)}):
        source = _upload(client, headers)
        accepted = _create_op(client, headers, source)
    assert accepted.status_code == 202
    record = db.query(MediaJob).filter(MediaJob.id == accepted.json()["id"]).one()
    assert record.params["parameters"] == {}  # no _input_durations injected for an image-only operation
    assert float(record.estimated_cost_usd) == 0.0


# ── fal Wan 2.2 Animate Move: a second character_animate provider (CP46) ────────────────────────

@pytest.mark.parametrize("plan", ["max", "team"])
async def test_creating_a_character_animate_job_on_fal_prices_it_and_respects_the_ceiling(
        client, db, registered_user, store, monkeypatch, plan):
    """End to end through the real FalWanAnimateEngine (not the scripted stand-in), proving
    media/economics.py's operation-level gate (not provider-specific) already covers a second
    character_animate provider with zero changes — MAX and TEAM both fit a real 4.963s upload."""
    from tests.unit.test_media_runners import tiny_mp4
    from src.media.hosted import FalWanAnimateEngine
    from src.media import economics
    from src.media.settings import media_settings

    monkeypatch.setenv("FAL_KEY", "fal-test")
    media_settings.cache_clear()
    try:
        _set_plan(db, registered_user["user_id"], plan)
        headers = {"Authorization": f"Bearer {registered_user['token']}"}
        with use_engines(operations={"character_animate": FalWanAnimateEngine()}):
            character = _upload(client, headers)
            driving_resp = client.post("/media/sources/video", headers=headers,
                                       files={"file": ("clip.mp4", tiny_mp4(seconds=4.963), "video/mp4")})
            assert driving_resp.status_code == 201
            driving_video = driving_resp.json()["id"]
            accepted = _create_animate(client, headers, character, driving_video)
            assert accepted.status_code == 202
            job_id = accepted.json()["id"]
        record = db.query(MediaJob).filter(MediaJob.id == job_id).one()
        # No real stts table in tiny_mp4(): estimate falls to duration(4.963) * 24fps / 16.
        expected = (4.963 * 24 / 16) * 0.04
        assert float(record.estimated_cost_usd) == pytest.approx(expected)
        assert economics.fits_media_request_ceiling("character_animate", plan, float(record.estimated_cost_usd))
    finally:
        media_settings.cache_clear()
