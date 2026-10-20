"""End-to-end tests for Persistent Agent Instance V1 — the full cycle
WITH an AgentInstance bound to the schedule (as opposed to
test_persistent_http_agent_e2e.py, which proves the same WAKE/CHECK/
DECIDE/ACT/EXECUTE/SENTINEL cycle with no AgentInstance at all — both
are expected to work, since a schedule with no instance is fully
legitimate).

Test matrix
-----------
1) Create AgentInstance -> configure HTTP-check schedule bound to it ->
   first WAKE establishes baseline, no ACT
2) Content changes -> second WAKE -> CHECK detects it -> DECIDE permits
   -> ACT creates ExecutionJob -> durable worker executes it -> Sentinel
   passes -> completed; instance.last_execution_job_id/last_run_at updated
3) Pause the instance -> next WAKE (even with further-changed content)
   creates NO job
4) Resume the instance -> next WAKE (content still differs from the
   pre-pause baseline) DOES create a job — not a "duplicate", a real
   detected change that accumulated during the pause
5) Manual run (separate scenario, legacy shell schedule, no HTTP check)
   -> ExecutionJob -> durable worker -> completed
6) Restart simulation (fresh DB session) -> instance/status/schedule/
   last_run/next_run all survive, no duplication
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from src.models.agent_definition import AgentDefinition
from src.models.agent_instance import AgentInstance
from src.models.agent_schedule import AgentSchedule
from src.models.execution import ExecutionJob
from src.services import agent_check_source, agent_instance_service, agent_schedule_service, execution_service
from src.execution.fake_provider import FakeExecutionProvider

pytestmark = pytest.mark.usefixtures("execution_session_override")


@dataclass
class _ServerState:
    body: bytes = b"page version 1"
    url: str = ""


@pytest.fixture
def http_server():
    state = _ServerState()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
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
    real_cls = agent_check_source.HttpCheckSource
    monkeypatch.setattr(agent_check_source, "HttpCheckSource", lambda *a, **k: real_cls(allow_private_networks=True))


@pytest.fixture(autouse=True)
def _fake_shell_provider(tmp_path, monkeypatch):
    fake = FakeExecutionProvider(tmp_path)
    monkeypatch.setattr(execution_service, "build_default_provider", lambda: fake)
    return fake


async def test_e2e_persistent_agent_instance_full_cycle_with_pause_resume(client, db, registered_user, http_server, run_worker_until_terminal):
    definition = AgentDefinition(user_id=registered_user["user_id"], name="Page Watcher", priority="normal", execution_timeout_seconds=120)
    db.add(definition)
    db.commit()
    db.refresh(definition)

    instance = agent_instance_service.create_agent_instance(
        db, user_id=registered_user["user_id"], agent_definition_id=definition.id, name="My Page Watcher",
    )
    schedule = agent_schedule_service.create_schedule(
        db, user_id=registered_user["user_id"], agent_id=definition.id, interval_seconds=600,
        job_payload={
            "check": {"type": "http", "url": http_server.url},
            "action": {"job_type": "shell", "payload": {"steps": [{"command": "echo changed"}]}},
        },
        agent_instance_id=instance.id,
    )

    # ── 1) First WAKE — baseline, no ACT ───────────────────────────────
    schedule.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    fired_1 = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))
    assert fired_1 == 0
    db.refresh(instance)
    assert instance.last_execution_job_id is None
    assert db.query(ExecutionJob).count() == 0

    # ── 2) Content changes -> second WAKE -> ACT -> worker -> Sentinel ─
    http_server.body = b"page version 2 -- changed"
    schedule.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    fired_2 = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))
    assert fired_2 == 1
    db.refresh(instance)
    job_id = instance.last_execution_job_id
    assert job_id is not None

    job = await run_worker_until_terminal(db, job_id, timeout=10.0)
    assert job.status == "completed"
    db.refresh(instance)
    assert instance.last_execution_job_id == job_id
    assert instance.failure_count == 0

    # ── 3) Pause -> further content change -> next WAKE creates NO job ─
    agent_instance_service.pause_agent_instance(db, instance.id, registered_user["user_id"])
    http_server.body = b"page version 3 -- changed again while paused"
    db.refresh(schedule)
    schedule.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    fired_3 = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))
    assert fired_3 == 0
    db.refresh(instance)
    assert instance.last_execution_job_id == job_id  # unchanged — no new job
    assert db.query(ExecutionJob).count() == 1

    # ── 4) Resume -> next WAKE detects the accumulated change -> ACT ──
    agent_instance_service.resume_agent_instance(db, instance.id, registered_user["user_id"])
    db.refresh(schedule)
    schedule.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    fired_4 = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))
    assert fired_4 == 1
    db.refresh(instance)
    second_job_id = instance.last_execution_job_id
    assert second_job_id != job_id

    second_job = await run_worker_until_terminal(db, second_job_id, timeout=10.0)
    assert second_job.status == "completed"
    assert db.query(ExecutionJob).count() == 2  # never duplicated — exactly the two real changes


async def test_e2e_manual_run_via_durable_worker(client, db, registered_user, run_worker_until_terminal):
    definition = AgentDefinition(user_id=registered_user["user_id"], name="Manual Agent")
    db.add(definition)
    db.commit()
    db.refresh(definition)

    instance = agent_instance_service.create_agent_instance(
        db, user_id=registered_user["user_id"], agent_definition_id=definition.id, name="Runner",
    )
    agent_schedule_service.create_schedule(
        db, user_id=registered_user["user_id"], agent_id=definition.id, interval_seconds=600,
        job_payload={"steps": [{"command": "echo manual"}]}, agent_instance_id=instance.id,
    )

    job = agent_instance_service.manual_run(db, instance_id=instance.id, user_id=registered_user["user_id"])
    assert job.status == "queued"

    completed = await run_worker_until_terminal(db, job.id, timeout=10.0)
    assert completed.status == "completed"
    assert "echo manual" in completed.result["output"]


async def test_e2e_restart_preserves_agent_instance_and_schedule_state(client, db, registered_user):
    from sqlalchemy.orm import sessionmaker

    definition = AgentDefinition(user_id=registered_user["user_id"], name="Restart Agent")
    db.add(definition)
    db.commit()
    db.refresh(definition)

    instance = agent_instance_service.create_agent_instance(
        db, user_id=registered_user["user_id"], agent_definition_id=definition.id, name="Persistent",
    )
    schedule = agent_schedule_service.create_schedule(
        db, user_id=registered_user["user_id"], agent_id=definition.id, interval_seconds=600,
        job_payload={"steps": [{"command": "noop"}]}, agent_instance_id=instance.id,
    )
    instance_id, schedule_id = instance.id, schedule.id

    # "Restart": brand-new Session/identity-map — same property proven in
    # test_persistent_http_agent_e2e.py's own restart test, now for the
    # AgentInstance layer specifically.
    fresh_session = sessionmaker(bind=db.get_bind())()
    try:
        reloaded_instance = fresh_session.query(AgentInstance).filter(AgentInstance.id == instance_id).first()
        reloaded_schedule = fresh_session.query(AgentSchedule).filter(AgentSchedule.id == schedule_id).first()

        assert reloaded_instance is not None
        assert reloaded_instance.status == "active"
        assert reloaded_schedule is not None
        assert reloaded_schedule.agent_instance_id == instance_id

        # The next WAKE, driven entirely from the freshly-loaded rows,
        # still works normally — no duplication, no crash from missing
        # in-process state.
        reloaded_schedule.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        fresh_session.commit()
        fired = await agent_schedule_service._fire_due_schedules(fresh_session, now=datetime.now(timezone.utc))
        assert fired == 1  # first-ever check for this schedule -> baseline is "changed" (legacy shape, no "check" key)
        assert fresh_session.query(ExecutionJob).filter(ExecutionJob.agent_id == definition.id).count() == 1
    finally:
        fresh_session.close()
