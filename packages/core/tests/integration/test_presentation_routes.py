"""The HTTP surface: discovery before creation, and truthful refusals.

The behaviour under test is that a capability is never offered as ready when
it cannot actually run. Every refusal here happens before the engine is woken,
because once generation starts the model calls are paid for whether or not the
result can be delivered.

The engine is stubbed at the service boundary (presentation_service.availability)
rather than over HTTP — these tests are about what the routes do with the
answer, not about the handshake, which test_presentation_job.py covers against
a real server.
"""
from __future__ import annotations

import pytest

from src.presentation.provider import ProviderAvailability
from src.services import presentation_service


@pytest.fixture
def engine_up(monkeypatch):
    async def _ok(*a, **k):
        return ProviderAvailability(True)

    monkeypatch.setattr(presentation_service, "availability", _ok)


@pytest.fixture
def engine_down(monkeypatch):
    async def _down(*a, **k):
        return ProviderAvailability(False, "container unreachable")

    monkeypatch.setattr(presentation_service, "availability", _down)


def _upgrade(db, registered_user, plan):
    """Plan is not settable over the API; set it on the row directly.

    The user id comes from the register response rather than a /users/me
    round trip — the fixture already has it, and one fewer request keeps the
    test about the presentation routes.
    """
    from src.models.user import User

    db.query(User).filter(User.id == registered_user["user_id"]).update({"plan": plan})
    db.commit()


# ── Discovery ────────────────────────────────────────────────────────────

def test_capability_is_offered_when_everything_is_ready(db, registered_user, client, auth_headers, engine_up):
    _upgrade(db, registered_user, "pro")
    r = client.get("/presentations/capability", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["max_slides"] >= 3


def test_capability_is_not_offered_when_the_engine_is_down(
    db, registered_user, client, auth_headers, engine_down
):
    """A capability advertised while its engine is unreachable is worse than
    one that is absent — the user finds out after asking."""
    _upgrade(db, registered_user, "pro")
    r = client.get("/presentations/capability", headers=auth_headers)
    assert r.json()["available"] is False


def test_capability_is_not_offered_on_the_free_plan(db, registered_user, client, auth_headers, engine_up):
    _upgrade(db, registered_user, "free")
    r = client.get("/presentations/capability", headers=auth_headers)
    body = r.json()
    assert body["available"] is False
    assert "paid plans" in body["message"]


def test_the_unavailable_message_never_names_infrastructure(
    db, registered_user, client, auth_headers, engine_down
):
    """"container unreachable" is an operator's sentence, not a customer's."""
    _upgrade(db, registered_user, "pro")
    message = client.get("/presentations/capability", headers=auth_headers).json()["message"]
    for leak in ("container", "Presenton", "Gemini", "R2", "docker", "TCU"):
        assert leak.lower() not in message.lower()


# ── Creation ─────────────────────────────────────────────────────────────

def test_creating_a_deck_queues_a_job(db, registered_user, client, auth_headers, engine_up):
    _upgrade(db, registered_user, "pro")
    r = client.post(
        "/presentations",
        json={"brief": "A deck about our Q3 sales results", "n_slides": 6},
        headers=auth_headers,
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["job_type"] == "presentation"
    assert body["status"] in ("queued", "created")


def test_the_free_plan_cannot_create_one(db, registered_user, client, auth_headers, engine_up):
    _upgrade(db, registered_user, "free")
    r = client.post(
        "/presentations",
        json={"brief": "A deck about our Q3 sales results"},
        headers=auth_headers,
    )
    assert r.status_code == 403


def test_creation_is_refused_when_the_engine_is_down(db, registered_user, client, auth_headers, engine_down):
    _upgrade(db, registered_user, "pro")
    r = client.post(
        "/presentations",
        json={"brief": "A deck about our Q3 sales results"},
        headers=auth_headers,
    )
    assert r.status_code == 503


def test_no_response_carries_a_cost_figure(db, registered_user, client, auth_headers, engine_up):
    """Same rule the execution routes already follow — TCU is internal."""
    _upgrade(db, registered_user, "pro")
    created = client.post(
        "/presentations",
        json={"brief": "A deck about our Q3 sales results"},
        headers=auth_headers,
    )
    status_body = client.get("/presentations/capability", headers=auth_headers).text
    for body in (created.text, status_body):
        for leak in ("tcu", "cost_usd", "cost"):
            assert leak not in body.lower()


@pytest.mark.parametrize("bad", [
    {"brief": "too short"},                              # under the minimum
    {"brief": "A perfectly fine brief here", "n_slides": 1},    # below MIN_SLIDES
    {"brief": "A perfectly fine brief here", "n_slides": 500},  # above MAX_SLIDES
    {"brief": "A perfectly fine brief here", "export_as": "key"},  # unsupported format
])
def test_malformed_requests_are_rejected_before_any_work(
    db, registered_user, client, auth_headers, engine_up, bad
):
    _upgrade(db, registered_user, "pro")
    r = client.post("/presentations", json=bad, headers=auth_headers)
    assert r.status_code == 422


def test_artifacts_of_a_foreign_job_are_not_listed(db, registered_user, client, auth_headers, engine_up):
    """The job route answers 404 for someone else's job; the artifact listing
    must not become a second existence oracle."""
    _upgrade(db, registered_user, "pro")
    r = client.get(
        "/execution/jobs/00000000-0000-0000-0000-000000000000/artifacts",
        headers=auth_headers,
    )
    assert r.status_code == 404


# ── The conversational path ──────────────────────────────────────────────

def _said(response) -> str:
    """Every SSE event of a chat turn, decoded: the stream escapes accents as JSON."""
    import json

    events = [line[len("data: "):] for line in response.text.splitlines() if line.startswith("data: {")]
    return " ".join(json.dumps(json.loads(event), ensure_ascii=False) for event in events)


def test_asking_for_a_deck_in_chat_creates_a_job_not_a_text_answer(
    db, registered_user, client, auth_headers, engine_up
):
    """PRESENTATION classifies as an AGENT task, so without an explicit branch
    the request would reach the agent loop and come back as a DESCRIPTION of a
    presentation. The point of the capability is the file."""
    from src.models.execution import ExecutionJob

    _upgrade(db, registered_user, "pro")
    session_id = client.post("/sessions", json={"title": "deck"},
                             headers=auth_headers).json()["id"]

    r = client.post(
        f"/sessions/{session_id}/message",
        json={"content": "Create a 10-slide presentation about the Brazilian solar market"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert "building your 10-slide presentation" in _said(r).lower()

    jobs = db.query(ExecutionJob).filter(ExecutionJob.job_type == "presentation").all()
    assert len(jobs) == 1
    assert jobs[0].user_id == registered_user["user_id"]


def test_the_chat_path_charges_nothing_when_it_only_queues_a_job(
    db, registered_user, client, auth_headers, engine_up
):
    """The chat turn's own reservation is cancelled — the job makes its own,
    sized to the deck. Keeping both would charge twice for one deck."""
    from src.models.usage import UsageRecord

    _upgrade(db, registered_user, "pro")
    session_id = client.post("/sessions", json={"title": "deck"},
                             headers=auth_headers).json()["id"]
    client.post(
        f"/sessions/{session_id}/message",
        json={"content": "Create a 10-slide presentation about the Brazilian solar market"},
        headers=auth_headers,
    )
    active = db.query(UsageRecord).filter(
        UsageRecord.user_id == registered_user["user_id"],
        UsageRecord.status.in_(("pending", "completed")),
    ).all()
    assert active == [], "the chat turn must not hold a reservation of its own"


def test_a_deck_asked_for_in_chat_is_refused_truthfully_when_the_engine_is_down(
    db, registered_user, client, auth_headers, engine_down
):
    from src.models.execution import ExecutionJob

    _upgrade(db, registered_user, "pro")
    session_id = client.post("/sessions", json={"title": "deck"},
                             headers=auth_headers).json()["id"]
    r = client.post(
        f"/sessions/{session_id}/message",
        json={"content": "Create a 10-slide presentation about the Brazilian solar market"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert "temporarily unavailable" in r.text.lower()
    assert db.query(ExecutionJob).filter(
        ExecutionJob.job_type == "presentation"
    ).all() == []


# ── The deck's card in the chat: linked to its job, then its file ─────────
# The chat queued the deck and returned the job id, but no client followed it: the web had a card for media
# jobs only, so a finished deck was never shown. The reply is now linked to its job (surviving a reload) and
# the file is offered through a short-lived link the browser can open without the session's bearer token.

def _deck_from_chat(db, registered_user, client, auth_headers):
    from src.models.execution import ExecutionJob

    _upgrade(db, registered_user, "pro")
    session_id = client.post("/sessions", json={"title": "deck"}, headers=auth_headers).json()["id"]
    r = client.post(f"/sessions/{session_id}/message", headers=auth_headers,
                    json={"content": "Create a 10-slide presentation about the Brazilian solar market"})
    assert r.status_code == 200, r.text
    job = db.query(ExecutionJob).filter(ExecutionJob.job_type == "presentation").one()
    return session_id, job, r.text


def test_the_reply_that_queued_a_deck_is_linked_to_its_job(db, registered_user, client, auth_headers, engine_up):
    import json

    session_id, job, stream = _deck_from_chat(db, registered_user, client, auth_headers)
    done = next(json.loads(line[6:]) for line in stream.splitlines()
                if line.startswith("data: {") and '"type": "done"' in line)
    assert done["job_id"] == job.id

    messages = client.get(f"/sessions/{session_id}", headers=auth_headers).json()["messages"]
    reply = [m for m in messages if m["role"] == "assistant"][-1]
    assert reply["execution_job_id"] == job.id  # the card comes back when the conversation is reopened
    assert all(m.get("execution_job_id") is None for m in messages if m["role"] == "user")


def _stored_deck(db, job, tmp_path, monkeypatch, *, expires_at=None):
    import asyncio

    from src.models.execution import ExecutionArtifact
    from src.services import artifact_service
    from src.storage.local import LocalObjectStore

    store = LocalObjectStore(root=str(tmp_path / "objects"))
    monkeypatch.setattr(artifact_service, "build_object_store", lambda: store)
    key = f"presentations/{job.user_id}/{job.id}/presentation.pptx"
    asyncio.run(store.put(key=key, data=b"PK\x03\x04deck", content_type="application/vnd.ms-powerpoint"))
    artifact = ExecutionArtifact(job_id=job.id, filename="presentation.pptx", relative_path="presentation.pptx",
                                 content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                                 size_bytes=8, storage_backend=store.backend_name, storage_key=key,
                                 expires_at=expires_at)
    db.add(artifact)
    db.commit()
    return artifact, store


def test_the_deck_is_offered_through_a_link_and_read_with_the_session_where_none_can_be_signed(
        db, registered_user, client, auth_headers, engine_up, tmp_path, monkeypatch):
    _, job, _ = _deck_from_chat(db, registered_user, client, auth_headers)
    artifact, _ = _stored_deck(db, job, tmp_path, monkeypatch)

    listed = client.get(f"/execution/jobs/{job.id}/artifacts", headers=auth_headers).json()["artifacts"]
    assert [a["id"] for a in listed] == [artifact.id]

    link = client.get(f"/execution/artifacts/{artifact.id}/link", headers=auth_headers)
    assert link.status_code == 200
    assert link.json() == {"url": None, "filename": "presentation.pptx", "content_type": artifact.content_type}
    download = client.get(f"/execution/artifacts/{artifact.id}/download", headers=auth_headers)
    assert download.status_code == 200 and download.content == b"PK\x03\x04deck"


def test_the_link_is_the_stores_signed_url_when_it_can_sign(db, registered_user, client, auth_headers, engine_up,
                                                            tmp_path, monkeypatch):
    _, job, _ = _deck_from_chat(db, registered_user, client, auth_headers)
    artifact, store = _stored_deck(db, job, tmp_path, monkeypatch)

    async def signed(**kwargs):
        return "https://storage.example/signed/presentation.pptx?sig=x"

    monkeypatch.setattr(store, "signed_url", signed)
    link = client.get(f"/execution/artifacts/{artifact.id}/link", headers=auth_headers).json()
    assert link["url"] == "https://storage.example/signed/presentation.pptx?sig=x"


def test_the_link_answers_404_for_someone_elses_file_and_410_once_it_expired(
        db, registered_user, client, auth_headers, engine_up, tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    _, job, _ = _deck_from_chat(db, registered_user, client, auth_headers)
    artifact, _ = _stored_deck(db, job, tmp_path, monkeypatch,
                               expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    assert client.get(f"/execution/artifacts/{artifact.id}/link", headers=auth_headers).status_code == 410

    other = client.post("/auth/register", json={"email": "deck-other@test.com", "password": "securepass123",
                                               "full_name": "Other"}).json()
    foreign = client.get(f"/execution/artifacts/{artifact.id}/link",
                         headers={"Authorization": f"Bearer {other['access_token']}"})
    assert foreign.status_code == 404


def test_a_deck_asked_for_in_portuguese_is_answered_and_written_in_portuguese_with_the_slides_asked(
    db, registered_user, client, auth_headers, engine_up
):
    from src.models.execution import ExecutionJob

    _upgrade(db, registered_user, "pro")
    session_id = client.post("/sessions", json={"title": "deck"},
                             headers=auth_headers).json()["id"]
    r = client.post(
        f"/sessions/{session_id}/message",
        json={"content": "Crie uma apresentação de 5 slides sobre carros elétricos no Brasil"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert "Estou criando sua apresentação de 5 slides" in _said(r)
    assert "building" not in _said(r).lower()

    job = db.query(ExecutionJob).filter(ExecutionJob.job_type == "presentation").one()
    assert job.payload["presentation"]["n_slides"] == 5
    assert job.payload["presentation"]["language"] == "Portuguese (Brazil)"


def test_the_device_language_wins_over_the_words(db, registered_user, client, auth_headers, engine_up):
    from src.models.execution import ExecutionJob

    _upgrade(db, registered_user, "pro")
    session_id = client.post("/sessions", json={"title": "deck"},
                             headers=auth_headers).json()["id"]
    r = client.post(
        f"/sessions/{session_id}/message",
        json={"content": "Create a presentation about the Brazilian solar market", "locale": "es-MX"},
        headers=auth_headers,
    )
    assert "Estoy creando tu presentación de 10 diapositivas" in _said(r)
    job = db.query(ExecutionJob).filter(ExecutionJob.job_type == "presentation").one()
    assert job.payload["presentation"]["language"] == "Spanish"


@pytest.mark.parametrize("brief, expected", [
    ("Crie uma apresentação de 5 slides sobre carros elétricos", 5),
    ("faça uma apresentação com cinco lâminas", 5),
    ("Create a 12-slide deck", 12),
    ("Haz una presentación de 8 diapositivas", 8),
    ("uma apresentação sobre os 5 maiores carros do Brasil", None),
    ("apresentação sobre carros elétricos", None),
])
def test_the_slide_count_is_read_only_when_the_request_names_it(brief, expected):
    from src.api.routes.sessions import slides_asked

    assert slides_asked(brief) == expected
