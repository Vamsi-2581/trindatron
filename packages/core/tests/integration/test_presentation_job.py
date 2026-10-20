"""A presentation as a durable job: queued, executed, stored, billed.

This drives the REAL worker claim/execute/finish cycle (run_worker_until_terminal)
against a fake engine and a real local object store, so the things that only
show up when the pieces are connected are actually exercised: the reservation
is created and finalized exactly once, the artifact row points at a stored
object, the deck is never written to the workspace the worker deletes, and a
quota refusal ends the job instead of retrying into the same wall.

The engine is a stand-in HTTP server rather than a mock object because the
handshake is where the published documentation and the running service
disagree, and a mock would agree with whichever one I wrote it from.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.models.execution import ExecutionArtifact, ExecutionJob
from src.models.usage import UsageRecord
from src.models.user import User
from src.services import execution_service
from src.storage.local import LocalObjectStore

pytestmark = pytest.mark.usefixtures("execution_session_override")

FAKE_PPTX = b"PK\x03\x04" + b"\x00" * 1024


class _Engine(BaseHTTPRequestHandler):
    mode = "ok"           # ok | quota | broken | asleep
    wake_after = 0        # probes to refuse before reporting ready
    wake_timeout_s = 6.0  # the provider's wake budget for this test
    probes = 0
    generate_bodies: list[dict] = []

    def log_message(self, *a):
        pass

    def _json(self, code, payload, cookie=False):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if cookie:
            self.send_header("Set-Cookie", "presenton_session=s3ss10n; Path=/")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/api/v1/auth/status"):
            type(self).probes += 1
            if type(self).probes <= type(self).wake_after:
                # A scaled-to-zero container that has not finished booting.
                return self._json(503, {"detail": "starting"})
            return self._json(200, {"configured": True})
        if self.path.startswith("/static/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(FAKE_PPTX)))
            self.end_headers()
            self.wfile.write(FAKE_PPTX)
            return
        self._json(404, {"detail": "nope"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) or b"{}"
        if self.path.startswith("/api/v1/auth/login"):
            return self._json(200, {"authenticated": True}, cookie=True)
        if self.path.startswith("/api/v1/ppt/presentation/generate"):
            type(self).generate_bodies.append(json.loads(raw))
            if type(self).mode == "quota":
                return self._json(429, {"detail": "rate limited"})
            if type(self).mode == "broken":
                return self._json(500, {"detail": "boom"})
            return self._json(200, {
                "presentation_id": "deck-1",
                "path": "/static/deck-1.pptx",
            })
        self._json(404, {"detail": "nope"})


@pytest.fixture
def engine():
    _Engine.mode = "ok"
    _Engine.wake_after = 0
    _Engine.wake_timeout_s = 6.0
    _Engine.probes = 0
    _Engine.generate_bodies = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Engine)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", _Engine
    srv.shutdown()


@pytest.fixture
def wired(engine, tmp_path, monkeypatch):
    """Point the presentation provider at the fake engine and a real local
    store, without touching any other job type's wiring."""
    base, engine_cls = engine
    store = LocalObjectStore(root=str(tmp_path / "objects"))

    from src.execution import presentation_provider as pp
    from src.presentation.presenton import PresentonProvider

    original = execution_service.build_presentation_provider

    def _build(*, user_id, plan, user_timezone, session_id, options=None):
        return pp.PresentationExecutionProvider(
            str(tmp_path / "ws"),
            user_id=user_id, plan=plan, user_timezone=user_timezone,
            session_id=session_id,
            session_factory=execution_service._session_factory,
            options=options,
            engine=PresentonProvider(
                base_url=base, username="u", password="p",
                timeout_s=30, wake_timeout_s=engine_cls.wake_timeout_s, wake_poll_interval_s=0.05,
            ),
            object_store=store,
        )

    monkeypatch.setattr(execution_service, "build_presentation_provider", _build)
    yield base, engine_cls, store
    execution_service.build_presentation_provider = original


def _user(db, plan="pro"):
    import uuid

    user = User(email=f"job-{uuid.uuid4().hex[:8]}@example.com", hashed_password="x",
                plan=plan, timezone="America/Sao_Paulo")
    db.add(user)
    db.commit()
    return user


def _create(db, user, **options):
    payload = {
        "steps": [{"command": options.pop("brief", "A deck about our Q3 sales results")}],
        "presentation": {"n_slides": 5, "language": "English", "export_as": "pptx", **options},
    }
    return execution_service.create_job(
        db, user_id=user.id, plan=user.plan, user_timezone=user.timezone,
        job_type="presentation", payload=payload, timeout_seconds=60, max_retries=1,
    )


# ── The happy path ───────────────────────────────────────────────────────

async def test_a_deck_job_completes_and_stores_its_artifact(db, wired, run_worker_until_terminal):
    base, engine_cls, store = wired
    user = _user(db)
    job = _create(db, user)

    finished = await run_worker_until_terminal(db, job.id, timeout=20.0)
    assert finished.status == "completed", finished.error

    artifacts = db.query(ExecutionArtifact).filter(ExecutionArtifact.job_id == job.id).all()
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.storage_backend == "local"
    assert artifact.storage_key.startswith(f"presentations/{user.id}/{job.id}/")
    assert artifact.size_bytes == len(FAKE_PPTX)
    # The bytes are really in the store, not merely recorded.
    assert await store.get(key=artifact.storage_key) == FAKE_PPTX


async def test_the_deck_is_never_left_in_the_workspace_the_worker_deletes(
    db, wired, run_worker_until_terminal, tmp_path
):
    """The whole argument for object storage: a file written to the job
    workspace is gone minutes after it is made."""
    base, engine_cls, store = wired
    user = _user(db)
    job = _create(db, user)
    await run_worker_until_terminal(db, job.id, timeout=20.0)

    ws = tmp_path / "ws" / "executions" / user.id / job.id
    assert not ws.exists() or not any(ws.iterdir())


async def test_usage_is_reserved_once_and_finalized_once(db, wired, run_worker_until_terminal):
    user = _user(db)
    job = _create(db, user)
    await run_worker_until_terminal(db, job.id, timeout=20.0)

    records = db.query(UsageRecord).filter(UsageRecord.user_id == user.id).all()
    assert len(records) == 1, "exactly one ledger entry per deck"
    assert records[0].status == "completed"
    assert float(records[0].tcu) > 0


async def test_the_requested_slide_count_reaches_the_engine(db, wired, run_worker_until_terminal):
    base, engine_cls, store = wired
    user = _user(db)
    job = _create(db, user, n_slides=7)
    await run_worker_until_terminal(db, job.id, timeout=20.0)

    assert engine_cls.generate_bodies, "the engine was never asked to generate"
    assert engine_cls.generate_bodies[0]["n_slides"] == 7


# ── Cold start ───────────────────────────────────────────────────────────

async def test_a_sleeping_engine_is_woken_rather_than_declared_dead(
    db, wired, run_worker_until_terminal
):
    """Scaled-to-zero means the first probe legitimately fails. Treating that
    as an outage would make the capability permanently unavailable on the
    deployment shape we chose deliberately."""
    base, engine_cls, store = wired
    engine_cls.wake_after = 3
    # What is asserted is that refusals are polled through, not how fast. Each probe opens a real HTTP client
    # against a real local server, and under a loaded full suite four of them once took longer than the 6 s
    # budget. The poll returns on the first ready answer, so a wide budget costs nothing when the machine is idle.
    engine_cls.wake_timeout_s = 30.0
    user = _user(db)
    job = _create(db, user)

    finished = await run_worker_until_terminal(db, job.id, timeout=45.0)
    assert finished.status == "completed", finished.error
    assert engine_cls.probes > 3


# ── Failure paths ────────────────────────────────────────────────────────

async def test_a_quota_refusal_fails_the_job_without_retrying(
    db, wired, run_worker_until_terminal
):
    """Measured: the provider's free tier caps at 20 requests per DAY and one
    deck needs more. Retrying spends the remainder faster and lengthens the
    outage, so this must be permanent for the attempt."""
    base, engine_cls, store = wired
    engine_cls.mode = "quota"
    user = _user(db)
    job = _create(db, user)

    finished = await run_worker_until_terminal(db, job.id, timeout=20.0)
    assert finished.status == "failed"
    assert finished.attempt_number == 1, "a quota refusal must not be retried"
    assert len(engine_cls.generate_bodies) == 1


async def test_a_failed_generation_leaves_no_pending_reservation(
    db, wired, run_worker_until_terminal
):
    """A dangling 'pending' row counts against the user's quota forever."""
    base, engine_cls, store = wired
    engine_cls.mode = "quota"
    user = _user(db)
    job = _create(db, user)
    await run_worker_until_terminal(db, job.id, timeout=20.0)

    pending = db.query(UsageRecord).filter(
        UsageRecord.user_id == user.id, UsageRecord.status == "pending"
    ).all()
    assert pending == []


async def test_a_failed_generation_stores_nothing(db, wired, run_worker_until_terminal):
    base, engine_cls, store = wired
    engine_cls.mode = "broken"
    user = _user(db)
    job = _create(db, user)
    await run_worker_until_terminal(db, job.id, timeout=20.0)

    assert db.query(ExecutionArtifact).filter(
        ExecutionArtifact.job_id == job.id
    ).all() == []


async def test_a_deck_is_refused_before_the_engine_runs_when_the_window_is_spent(
    db, wired, run_worker_until_terminal
):
    """The economic invariant: no model call happens for a deck that cannot be
    paid for. Once generation starts the money is spent regardless."""
    from datetime import datetime, timedelta, timezone

    base, engine_cls, store = wired
    user = _user(db, "start")
    # Exhaust START's daily window outright.
    db.add(UsageRecord(
        user_id=user.id, session_id=None, message_id=None, model="m",
        input_tokens=0, output_tokens=0, cached_tokens=0,
        cost_usd=0.45, tcu=45.0, duration_ms=0, status="completed",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    ))
    db.commit()

    job = _create(db, user)
    finished = await run_worker_until_terminal(db, job.id, timeout=20.0)

    assert finished.status == "failed"
    assert engine_cls.generate_bodies == [], "the engine must never have been asked"


# ── Cancellation ─────────────────────────────────────────────────────────

async def test_a_queued_deck_can_be_cancelled_before_it_runs(db, wired):
    user = _user(db)
    job = _create(db, user)

    cancelled = execution_service.cancel_job(db, job.id, user.id)
    assert cancelled.status == "cancelled" or cancelled.cancel_requested is True

    base, engine_cls, store = wired
    assert engine_cls.generate_bodies == []
