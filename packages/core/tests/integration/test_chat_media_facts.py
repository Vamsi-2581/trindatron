"""The model is told what the conversation really created, from the records.

Production, 2026-09-28: a video was created in a chat (1280x544, 5.06 s, 652 KB). Asked "what size is this video?",
the model answered "1920x1080 Full HD, 7-9 MB" and asked for a new request — the stored reply for a creation is only
"creating your video…", and nothing about the delivered file reached the model. These tests hold the fix: the
system context lists each creation of the conversation with its real status and file facts, and only the
conversation owner's own jobs are ever read.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.models.media import Artifact, MediaJob
from src.models.message import Message
from src.models.session import Session
from src.prompting.context_builders import MEDIA_ARTIFACTS_LIMIT, build_task_context
from src.services.chat_service import _prepare_chat_context

CREATING = "Estou criando seu vídeo. Isso leva alguns minutos; ele aparece aqui quando ficar pronto."


def _session(client, headers) -> Session:
    response = client.post("/sessions", headers=headers, json={})
    assert response.status_code in (200, 201)
    return response.json()["id"]


def _creation(db, session_id: str, user_id: str, *, status: str = "completed", kind: str = "video",
              width: int | None = 1280, height: int | None = 544, seconds: float | None = 5.06,
              size: int = 651_987, content_type: str = "video/mp4", with_file: bool = True,
              at: datetime | None = None) -> MediaJob:
    """What a real creation leaves behind: the job, its file (when delivered) and the "creating…" reply."""
    artifact = None
    if with_file and status == "completed":
        artifact = Artifact(user_id=user_id, kind=kind, source="media", filename=f"trindatron-{kind}.mp4",
                            content_type=content_type, size_bytes=size, width=width, height=height,
                            duration_seconds=seconds, storage_backend="local", storage_key="media/x")
        db.add(artifact)
        db.flush()
    job = MediaJob(user_id=user_id, kind=kind, status=status, prompt="a futuristic city at sunset",
                   estimated_cost_usd=Decimal("0.09"), artifact_id=artifact.id if artifact else None)
    db.add(job)
    db.flush()
    at = at or datetime.now(timezone.utc) - timedelta(minutes=1)  # created before the turn that asks about it
    db.add(Message(session_id=session_id, role="user", content=f"Crie um {kind} de 5 segundos", created_at=at))
    db.add(Message(session_id=session_id, role="assistant", content=CREATING, media_job_id=job.id,
                   created_at=at + timedelta(seconds=1)))
    db.commit()
    return job


def _context(db, session_id: str) -> str:
    session = db.query(Session).filter(Session.id == session_id).one()
    return build_task_context(db, session, task_type="chat", capability="chat").render()


def test_a_delivered_video_reaches_the_model_with_its_real_facts(client, db, auth_headers, registered_user):
    session_id = _session(client, auth_headers)
    _creation(db, session_id, registered_user["user_id"])

    context = _context(db, session_id)
    assert "<artifacts>" in context
    assert "video: delivered — MP4, 1280x544, 5.1 s, 652 KB" in context
    assert "never ask for a new request for something already delivered" in context


def test_the_question_after_the_creation_carries_the_facts_to_the_model(client, db, auth_headers, registered_user):
    """The exact production turn: the follow-up question's messages hold the real size, not only "creating…"."""
    session_id = _session(client, auth_headers)
    _creation(db, session_id, registered_user["user_id"])
    session = db.query(Session).filter(Session.id == session_id).one()

    _, messages = _prepare_chat_context(db, session, "em qual tamanho está esse vídeo?")

    system = messages[0]["content"]
    assert messages[0]["role"] == "system" and "1280x544" in system and "652 KB" in system
    assert messages[-1] == {"role": "user", "content": "em qual tamanho está esse vídeo?"}
    assert any(m["role"] == "assistant" and m["content"] == CREATING for m in messages)  # the chat stays as it was


def test_a_creation_still_running_or_that_failed_is_never_described_as_delivered(client, db, auth_headers,
                                                                                  registered_user):
    session_id = _session(client, auth_headers)
    _creation(db, session_id, registered_user["user_id"], status="running")
    _creation(db, session_id, registered_user["user_id"], status="failed")
    _creation(db, session_id, registered_user["user_id"], status="cancelled")

    context = _context(db, session_id)
    assert "video: still being created, not delivered yet" in context
    assert "video: not delivered (failed)" in context and "video: not delivered (cancelled)" in context
    assert "delivered —" not in context


def test_an_image_and_a_removed_file_are_described_as_what_they_are(client, db, auth_headers, registered_user):
    session_id = _session(client, auth_headers)
    _creation(db, session_id, registered_user["user_id"], kind="image", width=1024, height=1024, seconds=None,
              size=2_400_000, content_type="image/png")
    _creation(db, session_id, registered_user["user_id"], with_file=False)

    context = _context(db, session_id)
    assert "image: delivered — PNG, 1024x1024, 2.4 MB" in context
    assert "video: delivered, but the file is no longer stored" in context


def test_the_newest_creations_are_listed_last_and_bounded(client, db, auth_headers, registered_user):
    session_id = _session(client, auth_headers)
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    for seconds in range(1, MEDIA_ARTIFACTS_LIMIT + 3):  # one creation a minute, as separate turns are
        _creation(db, session_id, registered_user["user_id"], seconds=float(seconds),
                  at=start + timedelta(minutes=seconds))

    lines = [line for line in _context(db, session_id).splitlines() if line.startswith("- video:")]
    assert len(lines) == MEDIA_ARTIFACTS_LIMIT
    assert lines[-1].endswith(f"{MEDIA_ARTIFACTS_LIMIT + 2:.1f} s, 652 KB")  # the newest is last
    assert lines[0].endswith("3.0 s, 652 KB")  # the two oldest are left out


def test_another_persons_job_is_never_described(client, db, auth_headers, registered_user):
    """A message pointing at a job that belongs to someone else says nothing about it."""
    other = client.post("/auth/register", json={"email": "other-media@test.com", "password": "securepass123",
                                               "full_name": "Other"}).json()
    other_session = _session(client, {"Authorization": f"Bearer {other['access_token']}"})
    foreign = _creation(db, other_session, db.query(Session).filter(Session.id == other_session).one().user_id,
                        width=640, height=360)

    session_id = _session(client, auth_headers)
    db.add(Message(session_id=session_id, role="assistant", content=CREATING, media_job_id=foreign.id))
    db.commit()
    context = _context(db, session_id)
    assert "640x360" not in context and "<artifacts>" not in context


def test_a_conversation_without_creations_has_no_artifacts_block(client, db, auth_headers):
    session_id = _session(client, auth_headers)
    assert "<artifacts>" not in _context(db, session_id)
