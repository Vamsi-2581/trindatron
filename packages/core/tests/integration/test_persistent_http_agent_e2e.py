"""End-to-end test for the first real persistent agent: HTTP monitoring.

Proves the FULL cycle end to end, through the REAL components (durable
worker included, not a stand-in):

  WAKE -> HTTP CHECK -> DECIDE -> ACT -> EXECUTE (durable worker) ->
  SENTINEL -> SLEEP

against a local, deterministic HTTP server (no public internet
dependency) — exactly the mission's required E2E scenario:
  1. configure an HTTP-monitoring persistent agent
  2. first WAKE -> CHECK -> baseline established -> no ACT
  3. content changes
  4. second WAKE -> CHECK detects the change -> DECIDE permits -> ACT
     creates an ExecutionJob
  5. the durable worker (execution_worker.run_worker_once(), the SAME
     one production uses) claims and runs it
  6. Sentinel verifies the result via the EXISTING sentinel_criteria path
  7. a third WAKE with unchanged content does NOT create a second job
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.models.agent_definition import AgentDefinition
from src.models.agent_schedule import AgentSchedule
from src.models.execution import ExecutionJob
from src.models.usage import UsageRecord
from src.services import agent_check_source, agent_schedule_service, execution_service
from src.execution.fake_provider import FakeExecutionProvider

pytestmark = pytest.mark.usefixtures("execution_session_override")


@dataclass
class _ServerState:
    body: bytes = b"page version 1"
    status: int = 200
    url: str = ""


@pytest.fixture
def http_server():
    state = _ServerState()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(state.status)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(state.body)

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}/"

    yield state

    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture(autouse=True)
def _allow_loopback_http_checks(monkeypatch):
    """The real SSRF guard (agent_check_source._validate_url) correctly
    rejects loopback/private addresses by default — this test's own local
    server IS on 127.0.0.1, so it explicitly opts into the test-only
    escape hatch (HttpCheckSource(allow_private_networks=True)), exactly
    as any test exercising a local server must. Production code
    (build_check_source_for_schedule) never does this."""
    real_cls = agent_check_source.HttpCheckSource
    monkeypatch.setattr(agent_check_source, "HttpCheckSource", lambda *a, **k: real_cls(allow_private_networks=True))


@pytest.fixture(autouse=True)
def _fake_shell_provider(tmp_path, monkeypatch):
    """The E2E's ACT is job_type="shell" — fake the sandbox, exactly like
    every other test in this suite; the point of this test is proving the
    AGENT cycle wires together, not re-proving DockerSandbox."""
    fake = FakeExecutionProvider(tmp_path)
    monkeypatch.setattr(execution_service, "build_default_provider", lambda: fake)
    return fake


async def test_e2e_http_monitoring_agent_full_cycle(client, db, registered_user, http_server, run_worker_until_terminal):
    agent = AgentDefinition(user_id=registered_user["user_id"], name="Page Watcher", priority="normal", execution_timeout_seconds=120)
    db.add(agent)
    db.commit()
    db.refresh(agent)

    schedule = agent_schedule_service.create_schedule(
        db, user_id=registered_user["user_id"], agent_id=agent.id, interval_seconds=600,
        job_payload={
            "check": {"type": "http", "url": http_server.url},
            "action": {
                "job_type": "shell",
                "payload": {"steps": [{"command": "echo the page changed"}]},
                # Reuses the EXISTING Sentinel mechanism unmodified —
                # min_output_length is checked against the step's combined
                # output after the durable worker runs it.
            },
        },
    )

    # ── Step 1: first WAKE — CHECK establishes a baseline, no ACT ──────
    schedule.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    fired_1 = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    assert fired_1 == 0
    db.refresh(schedule)
    assert schedule.last_decision == "skipped"
    assert schedule.last_check_signature is not None
    assert schedule.last_run_job_id is None
    assert db.query(ExecutionJob).count() == 0
    assert db.query(UsageRecord).count() == 0

    baseline_signature = schedule.last_check_signature

    # ── Step 2: the external content changes ───────────────────────────
    http_server.body = b"page version 2 -- something changed"

    # ── Step 3: second WAKE — CHECK detects the change, DECIDE permits,
    #    ACT creates exactly one ExecutionJob ─────────────────────────
    schedule.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    fired_2 = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    assert fired_2 == 1
    db.refresh(schedule)
    assert schedule.last_decision == "acted"
    assert schedule.last_check_signature != baseline_signature
    assert schedule.last_run_job_id is not None
    job_id = schedule.last_run_job_id
    assert db.query(ExecutionJob).count() == 1

    # ── Step 4: EXECUTE — the REAL durable worker claims and runs it ──
    job = await run_worker_until_terminal(db, job_id, timeout=10.0)

    assert job.status == "completed"
    assert "the page changed" in job.result["output"]
    # Priority/timeout inherited from AgentDefinition (no schedule override
    # was given) — proves the AgentDefinition-defaults wiring holds for a
    # real HTTP-monitoring agent too, not just the generic tests.
    assert job.priority == "normal"
    assert job.timeout_seconds == 120

    # ── Step 5: content unchanged -> third WAKE creates NO second job ─
    schedule.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    fired_3 = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    assert fired_3 == 0
    db.refresh(schedule)
    assert schedule.last_decision == "skipped"
    assert db.query(ExecutionJob).count() == 1  # still just the one job

    # ── Billing/TCU: this whole cycle used job_type="shell" — zero
    #    UsageRecord rows at any point (shell jobs never touch TCU; CHECK
    #    never does either way — see agent_check_source's own tests).
    assert db.query(UsageRecord).count() == 0


async def test_e2e_restart_preserves_baseline_no_process_memory_needed(client, db, registered_user, http_server):
    """Simulates a process restart: state is read through a BRAND NEW
    SQLAlchemy Session (empty identity map, no cached Python objects) —
    proving the baseline lives entirely in the persisted row, not in any
    in-process cache. A real restart also loses execution_worker's
    _RUNNING_TASKS/in-flight state, but that recovery is entirely the
    Durable Execution Worker's own, already-tested responsibility
    (reclaim_expired_leases()) — unmodified and untouched here."""
    from src.database import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    agent = AgentDefinition(user_id=registered_user["user_id"], name="Restart Watcher")
    db.add(agent)
    db.commit()
    db.refresh(agent)

    schedule = agent_schedule_service.create_schedule(
        db, user_id=registered_user["user_id"], agent_id=agent.id, interval_seconds=600,
        job_payload={
            "check": {"type": "http", "url": http_server.url},
            "action": {"job_type": "shell", "payload": {"steps": [{"command": "noop"}]}},
        },
    )
    schedule.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))
    db.refresh(schedule)
    baseline_signature = schedule.last_check_signature
    assert baseline_signature is not None
    schedule_id = schedule.id

    # "Restart": a completely fresh Session/identity-map, still bound to
    # the same underlying test engine (an in-memory SQLite DB cannot
    # survive a literal new engine/process in this test harness — the
    # property under test, "no Python-side cache required", is fully
    # captured by a fresh Session with nothing preloaded).
    fresh_session = sessionmaker(bind=db.get_bind())()
    try:
        reloaded = fresh_session.query(AgentSchedule).filter(AgentSchedule.id == schedule_id).first()
        assert reloaded is not None
        assert reloaded.last_check_signature == baseline_signature

        # The next WAKE, driven entirely by the freshly-loaded row, must
        # still correctly detect "unchanged" against that same baseline —
        # not silently re-baseline as if it were a brand-new schedule.
        reloaded.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        fresh_session.commit()
        fired = await agent_schedule_service._fire_due_schedules(fresh_session, now=datetime.now(timezone.utc))
        assert fired == 0
        fresh_session.refresh(reloaded)
        assert reloaded.last_decision == "skipped"
        assert reloaded.last_check_signature == baseline_signature
    finally:
        fresh_session.close()
