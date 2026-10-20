"""Unit tests for agent_instance_service.py — Persistent Agent Instance V1.

Test matrix
-----------
A) create AgentInstance
D) update own agent (name/project_id)
E) cannot update another user's agent
F) pause
G) resume
H) manual run
I) manual run produces an ExecutionJob via execution_service.create_job()
M) duplicate manual run protection (active job blocks a second run)
O) active_agents count (count_active_instances)
P) disabled/paused instance never creates ACT via the schedule cycle
   (see test_agent_instance_e2e.py for the full WAKE-driven version;
   this file covers the service-level pause->manual_run refusal)
U) AgentDefinition inheritance (priority/timeout) — reused unmodified
   from Persistent Agent Runtime V1's own _resolve_act_spec/effective_priority,
   proven here specifically for the manual_run() path
V) billing unchanged / W) no TCU for lifecycle operations — structural +
   behavioral: none of create/list/get/update/pause/resume touch
   UsageRecord; manual_run's own job follows the EXISTING entitlement path
Health reconciliation (failure_count/status="error" threshold)
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base
from src.models.agent_definition import AgentDefinition
from src.models.agent_instance import AgentInstance
from src.models.agent_schedule import AgentSchedule
from src.models.execution import ExecutionJob
from src.models.usage import UsageRecord
from src.models.user import User
from src.services import agent_instance_service, agent_schedule_service, execution_service
from src.execution.fake_provider import FakeExecutionProvider


@pytest.fixture
def db(tmp_path):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    original_factory = execution_service._session_factory
    execution_service._session_factory = Session
    session = Session()
    yield session
    session.close()
    execution_service._session_factory = original_factory


@pytest.fixture
def user(db):
    u = User(email="instance@test.com", hashed_password="x", full_name="I", plan="pro", timezone="UTC")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def other_user(db):
    u = User(email="instance-other@test.com", hashed_password="x", full_name="O", plan="pro", timezone="UTC")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def definition(db, user):
    a = AgentDefinition(user_id=user.id, name="Monitor", priority="high", execution_timeout_seconds=600)
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


@pytest.fixture(autouse=True)
def _fake_default_provider(tmp_path, monkeypatch):
    fake = FakeExecutionProvider(tmp_path)
    monkeypatch.setattr(execution_service, "build_default_provider", lambda: fake)
    return fake


def test_A_create_agent_instance(db, user, definition):
    instance = agent_instance_service.create_agent_instance(
        db, user_id=user.id, agent_definition_id=definition.id, name="My Monitor",
    )
    assert instance.id is not None
    assert instance.status == "active"
    assert instance.user_id == user.id
    assert instance.agent_definition_id == definition.id
    assert instance.failure_count == 0


def test_create_rejects_foreign_agent_definition(db, user, other_user, definition):
    with pytest.raises(ValueError):
        agent_instance_service.create_agent_instance(
            db, user_id=other_user.id, agent_definition_id=definition.id, name="Steal",
        )


def test_D_update_own_instance(db, user, definition):
    instance = agent_instance_service.create_agent_instance(db, user_id=user.id, agent_definition_id=definition.id, name="Old")
    updated = agent_instance_service.update_agent_instance(db, instance.id, user.id, name="New")
    assert updated.name == "New"


def test_E_cannot_update_another_users_instance(db, user, other_user, definition):
    instance = agent_instance_service.create_agent_instance(db, user_id=user.id, agent_definition_id=definition.id, name="Mine")
    result = agent_instance_service.update_agent_instance(db, instance.id, other_user.id, name="Hijacked")
    assert result is None
    db.refresh(instance)
    assert instance.name == "Mine"


def test_F_pause(db, user, definition):
    instance = agent_instance_service.create_agent_instance(db, user_id=user.id, agent_definition_id=definition.id, name="M")
    paused = agent_instance_service.pause_agent_instance(db, instance.id, user.id)
    assert paused.status == "paused"


def test_F_pause_is_idempotent(db, user, definition):
    instance = agent_instance_service.create_agent_instance(db, user_id=user.id, agent_definition_id=definition.id, name="M")
    agent_instance_service.pause_agent_instance(db, instance.id, user.id)
    twice = agent_instance_service.pause_agent_instance(db, instance.id, user.id)
    assert twice.status == "paused"


def test_G_resume(db, user, definition):
    instance = agent_instance_service.create_agent_instance(db, user_id=user.id, agent_definition_id=definition.id, name="M")
    agent_instance_service.pause_agent_instance(db, instance.id, user.id)
    resumed = agent_instance_service.resume_agent_instance(db, instance.id, user.id)
    assert resumed.status == "active"


def _instance_with_schedule(db, user, definition, *, job_payload=None):
    instance = agent_instance_service.create_agent_instance(db, user_id=user.id, agent_definition_id=definition.id, name="Runner")
    schedule = agent_schedule_service.create_schedule(
        db, user_id=user.id, agent_id=definition.id, interval_seconds=600,
        job_payload=job_payload or {"steps": [{"command": "echo hi"}]},
        agent_instance_id=instance.id,
    )
    return instance, schedule


def test_H_I_manual_run_produces_execution_job(db, user, definition):
    instance, schedule = _instance_with_schedule(db, user, definition)

    job = agent_instance_service.manual_run(db, instance_id=instance.id, user_id=user.id)

    assert isinstance(job, ExecutionJob)
    assert job.status == "queued"
    assert job.agent_id == definition.id
    db.refresh(instance)
    assert instance.last_execution_job_id == job.id
    assert instance.last_run_at is not None


def test_manual_run_no_schedule_raises(db, user, definition):
    instance = agent_instance_service.create_agent_instance(db, user_id=user.id, agent_definition_id=definition.id, name="No schedule")
    with pytest.raises(agent_instance_service.NoScheduleForInstance):
        agent_instance_service.manual_run(db, instance_id=instance.id, user_id=user.id)


def test_manual_run_paused_instance_raises(db, user, definition):
    instance, schedule = _instance_with_schedule(db, user, definition)
    agent_instance_service.pause_agent_instance(db, instance.id, user.id)
    with pytest.raises(agent_instance_service.InstanceNotRunnable):
        agent_instance_service.manual_run(db, instance_id=instance.id, user_id=user.id)


def test_M_duplicate_manual_run_protection(db, user, definition):
    instance, schedule = _instance_with_schedule(db, user, definition)
    agent_instance_service.manual_run(db, instance_id=instance.id, user_id=user.id)

    with pytest.raises(agent_instance_service.InstanceJobAlreadyActive):
        agent_instance_service.manual_run(db, instance_id=instance.id, user_id=user.id)

    assert db.query(ExecutionJob).filter(ExecutionJob.agent_id == definition.id).count() == 1


def test_manual_run_wrong_user_raises_not_found(db, user, other_user, definition):
    instance, schedule = _instance_with_schedule(db, user, definition)
    with pytest.raises(agent_instance_service.AgentInstanceNotFound):
        agent_instance_service.manual_run(db, instance_id=instance.id, user_id=other_user.id)


def test_U_manual_run_uses_agent_definition_priority_and_timeout(db, user, definition):
    instance, schedule = _instance_with_schedule(db, user, definition)
    job = agent_instance_service.manual_run(db, instance_id=instance.id, user_id=user.id)
    assert job.priority == "high"  # from AgentDefinition, no schedule override
    assert job.timeout_seconds == 600


def test_O_count_active_instances(db, user, other_user, definition):
    a = agent_instance_service.create_agent_instance(db, user_id=user.id, agent_definition_id=definition.id, name="A")
    b = agent_instance_service.create_agent_instance(db, user_id=user.id, agent_definition_id=definition.id, name="B")
    agent_instance_service.pause_agent_instance(db, b.id, user.id)

    assert agent_instance_service.count_active_instances(db, user.id) == 1


def test_V_W_lifecycle_operations_never_touch_usage_records(db, user, definition):
    instance = agent_instance_service.create_agent_instance(db, user_id=user.id, agent_definition_id=definition.id, name="M")
    agent_instance_service.update_agent_instance(db, instance.id, user.id, name="M2")
    agent_instance_service.pause_agent_instance(db, instance.id, user.id)
    agent_instance_service.resume_agent_instance(db, instance.id, user.id)
    agent_instance_service.list_agent_instances(db, user.id)
    agent_instance_service.count_active_instances(db, user.id)

    assert db.query(UsageRecord).count() == 0


def test_health_reconciliation_completed_resets_failure_count():
    instance = AgentInstance(user_id="u", agent_definition_id="a", name="n", status="active", failure_count=2)
    job = ExecutionJob(user_id="u", job_type="shell", provider_name="docker", payload={}, checkpoint={}, status="completed")
    agent_instance_service.reconcile_health_from_job(instance, job)
    assert instance.failure_count == 0
    assert instance.last_error is None


def test_health_reconciliation_failed_increments_and_records_error():
    instance = AgentInstance(user_id="u", agent_definition_id="a", name="n", status="active", failure_count=0)
    job = ExecutionJob(user_id="u", job_type="shell", provider_name="docker", payload={}, checkpoint={}, status="failed", error="boom")
    agent_instance_service.reconcile_health_from_job(instance, job)
    assert instance.failure_count == 1
    assert instance.last_error == "boom"
    assert instance.status == "active"  # below threshold


def test_health_reconciliation_crosses_threshold_sets_error_status(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "agent_instance_failure_threshold", 2)
    instance = AgentInstance(user_id="u", agent_definition_id="a", name="n", status="active", failure_count=1)
    job = ExecutionJob(user_id="u", job_type="shell", provider_name="docker", payload={}, checkpoint={}, status="failed", error="boom again")
    agent_instance_service.reconcile_health_from_job(instance, job)
    assert instance.failure_count == 2
    assert instance.status == "error"


def test_health_reconciliation_ignores_non_terminal_job():
    instance = AgentInstance(user_id="u", agent_definition_id="a", name="n", status="active", failure_count=0)
    job = ExecutionJob(user_id="u", job_type="shell", provider_name="docker", payload={}, checkpoint={}, status="retrying")
    agent_instance_service.reconcile_health_from_job(instance, job)
    assert instance.failure_count == 0  # "retrying" is never itself a failure


def test_health_reconciliation_never_overrides_paused_status():
    instance = AgentInstance(user_id="u", agent_definition_id="a", name="n", status="paused", failure_count=10)
    job = ExecutionJob(user_id="u", job_type="shell", provider_name="docker", payload={}, checkpoint={}, status="failed", error="boom")
    agent_instance_service.reconcile_health_from_job(instance, job)
    assert instance.status == "paused"  # never auto-overridden once paused


def test_metrics_created_paused_resumed_manual_run(db, user, definition):
    from src.services.agent_instance_service import (
        _instances_created_total, _instances_status_transitions_total, _manual_runs_total,
    )
    _instances_created_total.reset()
    _instances_status_transitions_total.reset()
    _manual_runs_total.reset()

    instance, schedule = _instance_with_schedule(db, user, definition)
    agent_instance_service.pause_agent_instance(db, instance.id, user.id)
    agent_instance_service.resume_agent_instance(db, instance.id, user.id)
    agent_instance_service.manual_run(db, instance_id=instance.id, user_id=user.id)

    assert sum(_instances_created_total.snapshot().values()) == 1
    transitions = _instances_status_transitions_total.snapshot()
    assert transitions[(("to_status", "active"),)] == 2  # create + resume
    assert transitions[(("to_status", "paused"),)] == 1
    assert sum(_manual_runs_total.snapshot().values()) == 1


def test_metrics_failed_transition_on_threshold_crossed(monkeypatch):
    from src.config import settings
    from src.services.agent_instance_service import _instances_status_transitions_total
    monkeypatch.setattr(settings, "agent_instance_failure_threshold", 1)
    _instances_status_transitions_total.reset()

    instance = AgentInstance(user_id="u", agent_definition_id="a", name="n", status="active", failure_count=0)
    job = ExecutionJob(user_id="u", job_type="shell", provider_name="docker", payload={}, checkpoint={}, status="failed", error="boom")
    agent_instance_service.reconcile_health_from_job(instance, job)

    assert _instances_status_transitions_total.snapshot()[(("to_status", "error"),)] == 1


def test_derive_health():
    healthy = AgentInstance(user_id="u", agent_definition_id="a", name="n", status="active", failure_count=0)
    degraded = AgentInstance(user_id="u", agent_definition_id="a", name="n", status="active", failure_count=1)
    failed = AgentInstance(user_id="u", agent_definition_id="a", name="n", status="error", failure_count=5)

    assert agent_instance_service.derive_health(healthy) == "healthy"
    assert agent_instance_service.derive_health(degraded) == "degraded"
    assert agent_instance_service.derive_health(failed) == "failed"
