"""Unit/integration tests for agent_schedule_service — the persistent-agent
trigger source. Uses the SAME in-memory SQLite fixture pattern as
test_adaptive_budget_service.py (no HTTP, no background poll loop —
_fire_due_schedules() is called directly). `_fire_due_schedules()` is
`async def` (Persistent Agent Runtime — HTTP CHECK phase: CHECK may now
do real network I/O, so every test calling it is `async def` too, `await`ed.

Test matrix
-----------
AS01) create_schedule persists a schedule with next_run_at = now + interval
AS02) create_schedule for a nonexistent/foreign agent raises ValueError
AS03) _fire_due_schedules creates an ExecutionJob for a due schedule and
      advances next_run_at
AS04) _fire_due_schedules is a no-op for a schedule that isn't due yet
AS05) _fire_due_schedules never double-fires the same schedule within one call
AS06) An inactive schedule is never fired
AS07) A capacity-refused firing is skipped, not raised out of the poll cycle
      (mission's "não simplesmente matar todos os agentes" — the poll loop
      itself must survive one schedule's refusal and keep checking others)
AS08) deactivate_schedule flips is_active and is idempotent-safe to call twice

Persistent Agent Runtime V1, Tier 1 (CHECK/DECIDE, non-LLM only)
------------------------------------------------------------------
AS09) CHECK detects a changed payload signature -> DECIDE proceeds ->
      exactly one ACT ExecutionJob created
AS10) CHECK detects an unchanged payload signature -> DECIDE skips -> no
      ExecutionJob created
AS11) last_check_signature is persisted every cycle (both branches)
AS12) last_decision is persisted ("acted" / "skipped")
AS13) last_decision_at is persisted
AS14) capacity refused after a real change was detected -> last_decision=
      "capacity_refused", no ACT, poll loop does not raise
AS15/16/17) an existing job in status queued/running/retrying for this
      SAME schedule blocks a second ACT this cycle (at-most-one-active-
      job-per-schedule invariant) — three statuses, one per test
AS18) a schedule whose last job is terminal (completed) is free to fire
      a new ACT on the next due cycle
AS19) AgentDefinition.priority is used as the ACT's priority when
      schedule.priority is None (no override)
AS20) AgentDefinition.execution_timeout_seconds is used as the ACT's
      timeout_seconds (AgentSchedule has no override field for this)
AS21) an explicit schedule.priority still wins over AgentDefinition.priority
AS22) next_run_at still advances on the fixed interval regardless of
      which DECIDE branch was taken (skip included)
AS23) CHECK never creates a UsageRecord — a "no change, skip" cycle
      leaves the usage_records table completely empty

Persistent Agent Runtime — first real agent (HTTP CHECK)
------------------------------------------------------------
See test_agent_http_check_source.py for HttpCheckSource's own unit tests
(HTTP mechanics, security, timeouts) and test_persistent_http_agent_e2e.py
for the full WAKE->CHECK->DECIDE->ACT->EXECUTE->SENTINEL->SLEEP E2E.
AS24) a schedule using the check+action job_payload shape resolves
      job_type/payload from `action`, not the whole job_payload
AS25) the legacy job_payload shape (no "check" key) is completely
      unaffected — job_type stays "shell", the whole payload is used as-is
AS26) two due schedules in the SAME poll cycle never leak state
      (signature/decision/job) into each other
AS27) two schedules for different users/agents in the SAME cycle each
      create their OWN correctly-scoped job, never cross-assigned

Persistent Agent Instance V1 — instance-status gate at the WAKE cycle
----------------------------------------------------------------------
See test_persistent_agent_instance_e2e.py for the full HTTP-monitoring
version of P/Q/R against a real changing external signal; these are the
plain-payload, service-level equivalents (lettered per the mission's own
25-item test list: P, Q, R, N, K).
AS28 (P) a schedule bound to a "disabled" AgentInstance skips the ENTIRE
      cycle (no CHECK, no ACT) even though the payload changed
AS29 (Q) same, for a "paused" AgentInstance
AS30 (R) resuming the instance allows the NEXT due wake to fire normally
AS31 (N) the at-most-one-active-job invariant still holds for an
      instance-bound schedule across two fire attempts
AS32 (K) a schedule with NO AgentInstance (agent_instance_id=None) is
      completely unaffected by the instance-status gate — the legacy path,
      re-verified explicitly in this phase's own context

Persistent Agent Runtime — Tier 2 (LLM DECIDE)
------------------------------------------------------
See test_agent_decision.py (parse_decision/build_decide_payload) and
test_execution_agent_decide_job.py (the DECIDE ExecutionJob's own
provider/billing) — these tests are about the SCHEDULER's own
create-then-consume cycle, never a real Model Router call (DECIDE jobs
here are left "queued"/manually flipped to a terminal status+result,
exactly like AS15-18 already do for ordinary ACT jobs).
AS33) CHECK detects a change on a Tier-2 schedule -> a DECIDE
      ExecutionJob (job_type="agent_decide") is created,
      pending_decision_job_id set, last_decision="decide_pending" — NOT
      counted in `fired` (a DECIDE is not itself an action)
AS34) CHECK detects no change -> no DECIDE job, last_decision="skipped"
      (the SAME non-LLM pre-filter Tier 1 uses)
AS35) a still-running pending DECIDE -> next wake does nothing (no new
      DECIDE, no ACT, pending_decision_job_id unchanged)
AS36) consuming a completed "act" DECIDE creates the ACT ExecutionJob
      (job_type="research", server-fixed sentinel_criteria), clears
      pending_decision_job_id, last_decision="acted", counted in `fired`
AS37) consuming a completed "skip" DECIDE -> last_decision="decide_skip",
      no ACT
AS38) consuming a completed DECIDE with invalid/malformed output ->
      last_decision="decide_invalid", no ACT (never an implicit ACT)
AS39) consuming a DECIDE that reached status="failed" ->
      last_decision="decide_failed", no ACT
AS40) DECIDE creation itself is capacity-refused ->
      last_decision="capacity_refused", no DECIDE job persisted
AS41) crash-safe consumption: consuming the SAME terminal DECIDE twice
      (simulating two overlapping poll cycles / a restart) creates AT
      MOST ONE ACT job — the atomic CAS claim, exercised directly
AS42) create_schedule() rejects job_payload with BOTH "action" and
      "decide" — ambiguous DECIDE strategy
AS43) an active ACT job blocks creating a NEW DECIDE (Tier 1's
      at-most-one-active-job invariant, applied one layer up)
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

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
from src.services.agent_check_source import PayloadSignatureCheckSource
from src.execution.fake_provider import FakeExecutionProvider


@pytest.fixture
def db(tmp_path):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)

    # _fire_due_schedules() -> execution_service.create_job() only ever
    # persists a queued row now (Durable Execution — see
    # execution_service.py's module docstring); nothing in THIS file's
    # tests exercises the worker itself. Kept for parity with the other
    # execution_service._session_factory-dependent fixtures and in case a
    # future test in this file also drives execution_worker directly.
    original_factory = execution_service._session_factory
    execution_service._session_factory = Session

    session = Session()
    yield session
    session.close()
    execution_service._session_factory = original_factory


@pytest.fixture
def user(db):
    u = User(email="sched@test.com", hashed_password="x", full_name="T", plan="pro", timezone="UTC")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


@pytest.fixture
def agent(db, user):
    a = AgentDefinition(user_id=user.id, name="Monitor")
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


@pytest.fixture(autouse=True)
def _fake_default_provider(tmp_path, monkeypatch):
    fake = FakeExecutionProvider(tmp_path)
    monkeypatch.setattr(execution_service, "build_default_provider", lambda: fake)
    return fake


def test_AS01_create_schedule_persists_next_run_at(db, user, agent):
    before = datetime.now(timezone.utc)
    schedule = agent_schedule_service.create_schedule(
        db, user_id=user.id, agent_id=agent.id, interval_seconds=120,
        job_payload={"steps": [{"command": "check"}]},
    )
    assert schedule.is_active is True
    assert schedule.next_run_at > before + timedelta(seconds=119)


def test_AS02_create_schedule_unknown_agent_raises(db, user):
    with pytest.raises(ValueError):
        agent_schedule_service.create_schedule(
            db, user_id=user.id, agent_id="does-not-exist", interval_seconds=60, job_payload={"steps": []},
        )


async def test_AS03_fire_due_schedule_creates_job_and_advances(db, user, agent):
    schedule = AgentSchedule(
        user_id=user.id, agent_id=agent.id, interval_seconds=300,
        job_payload={"steps": [{"command": "check"}]}, priority="low",
        next_run_at=datetime.now(timezone.utc) - timedelta(seconds=1),  # already due
    )
    db.add(schedule)
    db.commit()

    now = datetime.now(timezone.utc)
    fired = await agent_schedule_service._fire_due_schedules(db, now=now)

    assert fired == 1
    db.refresh(schedule)
    assert schedule.last_run_job_id is not None
    assert schedule.next_run_at > now + timedelta(seconds=299)
    job = db.query(ExecutionJob).filter(ExecutionJob.id == schedule.last_run_job_id).first()
    assert job is not None
    # Durable Execution: create_job() only ever persists a queued row — a
    # schedule firing must never execute work directly (mission
    # requirement 9). Nothing here awaits any task; there isn't one.
    assert job.status == "queued"


async def test_AS04_not_due_schedule_not_fired(db, user, agent):
    schedule = AgentSchedule(
        user_id=user.id, agent_id=agent.id, interval_seconds=300,
        job_payload={"steps": [{"command": "check"}]},
        next_run_at=datetime.now(timezone.utc) + timedelta(hours=1),  # far future
    )
    db.add(schedule)
    db.commit()

    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    assert fired == 0
    db.refresh(schedule)
    assert schedule.last_run_job_id is None


async def test_AS44_a_plan_without_agent_mode_never_fires_a_persistent_agent(db, user, agent):
    """A persistent agent is agent mode on a timer, and FREE has no agent mode.
    Checked at every wake, so an account downgraded after creating a schedule
    stops as well (pre-launch closure, 2026-09-13)."""
    user.plan = "free"
    schedule = AgentSchedule(
        user_id=user.id, agent_id=agent.id, interval_seconds=300,
        job_payload={"steps": [{"command": "check"}]},
        next_run_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    db.add(schedule)
    db.commit()

    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    assert fired == 0
    db.refresh(schedule)
    assert schedule.last_run_job_id is None
    assert schedule.last_decision == "skipped"
    assert db.query(ExecutionJob).count() == 0


async def test_AS05_fire_due_never_double_fires_same_call(db, user, agent):
    schedule = AgentSchedule(
        user_id=user.id, agent_id=agent.id, interval_seconds=60,
        job_payload={"steps": [{"command": "check"}]},
        next_run_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    db.add(schedule)
    db.commit()

    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))
    assert fired == 1
    # next_run_at was advanced past `now` inside the same call — a second
    # call at the same instant must not re-fire it.
    fired_again = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))
    assert fired_again == 0


async def test_AS06_inactive_schedule_never_fires(db, user, agent):
    schedule = AgentSchedule(
        user_id=user.id, agent_id=agent.id, interval_seconds=60,
        job_payload={"steps": [{"command": "check"}]}, is_active=False,
        next_run_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    db.add(schedule)
    db.commit()

    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))
    assert fired == 0


async def test_AS07_capacity_refused_firing_is_skipped_not_raised(db, user, agent, monkeypatch):
    from src.services import adaptive_budget_service

    monkeypatch.setattr(
        adaptive_budget_service, "compute_capacity",
        lambda *a, **k: adaptive_budget_service.CapacitySnapshot(
            tier=adaptive_budget_service.TIER_PAUSE_LOW_PRIORITY, capacity_remaining_pct=5.0,
            session_pct_used=95.0, session_resets_at=None, session_active=True,
            weekly_pct_used=95.0, weekly_resets_at=None,
        ),
    )
    schedule = AgentSchedule(
        user_id=user.id, agent_id=agent.id, interval_seconds=60, priority="low",
        job_payload={"steps": [{"command": "check"}]},
        next_run_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    db.add(schedule)
    db.commit()

    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))  # must not raise

    assert fired == 0
    db.refresh(schedule)
    assert schedule.last_run_job_id is None
    # next_run_at was still advanced — a refused low-priority job is
    # deferred to the NEXT cycle, not retried immediately in a hot loop.
    assert schedule.next_run_at > datetime.now(timezone.utc)


def test_AS08_deactivate_is_idempotent(db, user, agent):
    schedule = agent_schedule_service.create_schedule(
        db, user_id=user.id, agent_id=agent.id, interval_seconds=60, job_payload={"steps": []},
    )
    first = agent_schedule_service.deactivate_schedule(db, schedule.id, user.id)
    second = agent_schedule_service.deactivate_schedule(db, schedule.id, user.id)
    assert first.is_active is False
    assert second.is_active is False


# ── Persistent Agent Runtime V1, Tier 1 ──────────────────────────────────

_PAYLOAD = {"steps": [{"command": "check"}]}


def _due_schedule(db, user, agent, *, last_check_signature=None, priority=None, last_run_job_id=None, job_payload=None) -> AgentSchedule:
    schedule = AgentSchedule(
        user_id=user.id, agent_id=agent.id, interval_seconds=300,
        job_payload=job_payload if job_payload is not None else _PAYLOAD, priority=priority,
        next_run_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        last_check_signature=last_check_signature, last_run_job_id=last_run_job_id,
    )
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    return schedule


async def test_AS09_check_detects_change_decide_proceeds_to_act(db, user, agent):
    schedule = _due_schedule(db, user, agent, last_check_signature="stale-signature-not-matching-payload")

    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    assert fired == 1
    db.refresh(schedule)
    assert schedule.last_run_job_id is not None
    assert schedule.last_decision == "acted"


async def test_AS10_check_no_change_decide_skips_no_act(db, user, agent):
    real_signature = PayloadSignatureCheckSource._signature(_PAYLOAD)
    schedule = _due_schedule(db, user, agent, last_check_signature=real_signature)

    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    assert fired == 0
    db.refresh(schedule)
    assert schedule.last_run_job_id is None
    assert schedule.last_decision == "skipped"


async def test_AS11_last_check_signature_persisted_both_branches(db, user, agent):
    real_signature = PayloadSignatureCheckSource._signature(_PAYLOAD)

    changed_schedule = _due_schedule(db, user, agent, last_check_signature="stale")
    await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))
    db.refresh(changed_schedule)
    assert changed_schedule.last_check_signature == real_signature

    unchanged_schedule = _due_schedule(db, user, agent, last_check_signature=real_signature)
    await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))
    db.refresh(unchanged_schedule)
    assert unchanged_schedule.last_check_signature == real_signature


async def test_AS12_last_decision_persisted(db, user, agent):
    acted = _due_schedule(db, user, agent, last_check_signature="stale")
    await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))
    db.refresh(acted)
    assert acted.last_decision == "acted"

    real_signature = PayloadSignatureCheckSource._signature(_PAYLOAD)
    skipped = _due_schedule(db, user, agent, last_check_signature=real_signature)
    await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))
    db.refresh(skipped)
    assert skipped.last_decision == "skipped"


async def test_AS13_last_decision_at_persisted(db, user, agent):
    schedule = _due_schedule(db, user, agent, last_check_signature="stale")
    before = datetime.now(timezone.utc)

    await agent_schedule_service._fire_due_schedules(db, now=before)

    db.refresh(schedule)
    assert schedule.last_decision_at is not None
    assert abs((schedule.last_decision_at - before).total_seconds()) < 2


async def test_AS14_capacity_refused_after_change_detected(db, user, agent, monkeypatch):
    from src.services import adaptive_budget_service

    monkeypatch.setattr(
        adaptive_budget_service, "compute_capacity",
        lambda *a, **k: adaptive_budget_service.CapacitySnapshot(
            tier=adaptive_budget_service.TIER_PAUSE_LOW_PRIORITY, capacity_remaining_pct=5.0,
            session_pct_used=95.0, session_resets_at=None, session_active=True,
            weekly_pct_used=95.0, weekly_resets_at=None,
        ),
    )
    schedule = _due_schedule(db, user, agent, priority="low", last_check_signature="stale")

    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))  # must not raise

    assert fired == 0
    db.refresh(schedule)
    assert schedule.last_run_job_id is None
    assert schedule.last_decision == "capacity_refused"
    assert schedule.last_decision_at is not None


@pytest.mark.parametrize("blocking_status", ["queued", "running", "retrying"])
async def test_AS15_16_17_active_job_blocks_second_act(db, user, agent, blocking_status):
    existing_job = ExecutionJob(
        user_id=user.id, agent_id=agent.id, job_type="shell", provider_name="docker",
        payload=_PAYLOAD, checkpoint={}, status=blocking_status,
    )
    db.add(existing_job)
    db.commit()
    db.refresh(existing_job)

    schedule = _due_schedule(db, user, agent, last_check_signature="stale", last_run_job_id=existing_job.id)

    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    assert fired == 0
    db.refresh(schedule)
    assert schedule.last_run_job_id == existing_job.id  # unchanged — no second job created
    assert schedule.last_decision == "skipped"
    assert db.query(ExecutionJob).filter(ExecutionJob.agent_id == agent.id).count() == 1


async def test_AS18_terminal_job_allows_new_act_next_cycle(db, user, agent):
    completed_job = ExecutionJob(
        user_id=user.id, agent_id=agent.id, job_type="shell", provider_name="docker",
        payload=_PAYLOAD, checkpoint={}, status="completed",
    )
    db.add(completed_job)
    db.commit()
    db.refresh(completed_job)

    schedule = _due_schedule(db, user, agent, last_check_signature="stale", last_run_job_id=completed_job.id)

    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    assert fired == 1
    db.refresh(schedule)
    assert schedule.last_run_job_id != completed_job.id
    assert db.query(ExecutionJob).filter(ExecutionJob.agent_id == agent.id).count() == 2


async def test_AS19_agent_priority_used_when_schedule_has_no_override(db, user, agent):
    agent.priority = "high"
    db.commit()
    schedule = _due_schedule(db, user, agent, priority=None, last_check_signature="stale")

    await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    db.refresh(schedule)
    job = db.query(ExecutionJob).filter(ExecutionJob.id == schedule.last_run_job_id).first()
    assert job.priority == "high"


async def test_AS20_agent_execution_timeout_used(db, user, agent):
    agent.execution_timeout_seconds = 900
    db.commit()
    schedule = _due_schedule(db, user, agent, last_check_signature="stale")

    await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    db.refresh(schedule)
    job = db.query(ExecutionJob).filter(ExecutionJob.id == schedule.last_run_job_id).first()
    assert job.timeout_seconds == 900


async def test_AS21_explicit_schedule_priority_wins_over_agent_default(db, user, agent):
    agent.priority = "low"
    db.commit()
    schedule = _due_schedule(db, user, agent, priority="critical", last_check_signature="stale")

    await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    db.refresh(schedule)
    job = db.query(ExecutionJob).filter(ExecutionJob.id == schedule.last_run_job_id).first()
    assert job.priority == "critical"


async def test_AS22_next_run_at_advances_on_fixed_interval_regardless_of_decision(db, user, agent):
    real_signature = PayloadSignatureCheckSource._signature(_PAYLOAD)
    schedule = _due_schedule(db, user, agent, last_check_signature=real_signature)  # will SKIP (no change)
    now = datetime.now(timezone.utc)

    await agent_schedule_service._fire_due_schedules(db, now=now)

    db.refresh(schedule)
    assert schedule.last_decision == "skipped"
    assert schedule.next_run_at == now + timedelta(seconds=schedule.interval_seconds)


async def test_AS23_check_never_creates_a_usage_record(db, user, agent):
    real_signature = PayloadSignatureCheckSource._signature(_PAYLOAD)
    _due_schedule(db, user, agent, last_check_signature=real_signature)  # will SKIP (no change)

    await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    assert db.query(UsageRecord).count() == 0


async def test_AS24_check_action_shape_resolves_job_type_and_payload_from_action(db, user, agent):
    payload = {
        "check": {"type": "http", "url": "http://127.0.0.1:1/never-reached"},
        "action": {"job_type": "research", "payload": {"steps": [{"command": "summarize the change"}]}},
    }
    # Force a "changed" decision deterministically without any real HTTP
    # call, by overriding check_source for this cycle — this test is about
    # ACT's payload/job_type resolution, not HttpCheckSource's own
    # mechanics (covered separately).
    from src.services.agent_check_source import CheckResult, CheckSource

    class _ForceChanged(CheckSource):
        async def check(self, schedule):
            return CheckResult(changed=True, signature="forced", reason="test")

    schedule = _due_schedule(db, user, agent, last_check_signature="stale", job_payload=payload)

    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc), check_source=_ForceChanged())

    assert fired == 1
    db.refresh(schedule)
    job = db.query(ExecutionJob).filter(ExecutionJob.id == schedule.last_run_job_id).first()
    assert job.job_type == "research"
    assert job.payload == {"steps": [{"command": "summarize the change"}]}


async def test_AS25_legacy_payload_shape_unaffected(db, user, agent):
    schedule = _due_schedule(db, user, agent, last_check_signature="stale", job_payload=_PAYLOAD)

    await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    db.refresh(schedule)
    job = db.query(ExecutionJob).filter(ExecutionJob.id == schedule.last_run_job_id).first()
    assert job.job_type == "shell"
    assert job.payload == _PAYLOAD


async def test_AS26_multiple_schedules_isolated_in_same_cycle(db, user, agent):
    """Two due schedules, same poll cycle, deliberately different
    payloads/signatures — proves _fire_due_schedules()'s per-schedule
    state (signature/decision/job) never leaks across schedules
    processed in the SAME call, not just across separate test runs."""
    changed_schedule = _due_schedule(db, user, agent, last_check_signature="stale-a", job_payload={"steps": [{"command": "a"}]})
    real_sig_b = PayloadSignatureCheckSource._signature({"steps": [{"command": "b"}]})
    unchanged_schedule = _due_schedule(db, user, agent, last_check_signature=real_sig_b, job_payload={"steps": [{"command": "b"}]})

    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    assert fired == 1  # only the changed one acted
    db.refresh(changed_schedule)
    db.refresh(unchanged_schedule)
    assert changed_schedule.last_decision == "acted"
    assert changed_schedule.last_run_job_id is not None
    assert unchanged_schedule.last_decision == "skipped"
    assert unchanged_schedule.last_run_job_id is None
    assert unchanged_schedule.last_check_signature == real_sig_b  # untouched by the other schedule


async def test_AS27_isolated_across_users_and_agents(db, user, agent):
    other_user = User(email="other-sched@test.com", hashed_password="x", full_name="Other", plan="pro", timezone="UTC")
    db.add(other_user)
    db.commit()
    db.refresh(other_user)
    other_agent = AgentDefinition(user_id=other_user.id, name="Other Monitor")
    db.add(other_agent)
    db.commit()
    db.refresh(other_agent)

    mine = _due_schedule(db, user, agent, last_check_signature="stale-mine", job_payload={"steps": [{"command": "mine"}]})
    theirs = _due_schedule(db, other_user, other_agent, last_check_signature="stale-theirs", job_payload={"steps": [{"command": "theirs"}]})

    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    assert fired == 2
    db.refresh(mine)
    db.refresh(theirs)
    mine_job = db.query(ExecutionJob).filter(ExecutionJob.id == mine.last_run_job_id).first()
    theirs_job = db.query(ExecutionJob).filter(ExecutionJob.id == theirs.last_run_job_id).first()
    assert mine_job.user_id == user.id
    assert mine_job.agent_id == agent.id
    assert theirs_job.user_id == other_user.id
    assert theirs_job.agent_id == other_agent.id
    assert mine_job.id != theirs_job.id


def _instance(db, user, agent, *, status="active") -> AgentInstance:
    instance = AgentInstance(user_id=user.id, agent_definition_id=agent.id, name="Instance", status=status)
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return instance


async def test_AS28_disabled_instance_skips_entire_cycle(db, user, agent):
    instance = _instance(db, user, agent, status="disabled")
    schedule = _due_schedule(db, user, agent, last_check_signature="stale-signature-not-matching-payload")
    schedule.agent_instance_id = instance.id
    db.commit()

    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    assert fired == 0
    db.refresh(schedule)
    assert schedule.last_decision == "skipped"
    assert schedule.last_run_job_id is None
    # CHECK never ran — the stale signature is untouched, not overwritten
    # with a freshly-computed one.
    assert schedule.last_check_signature == "stale-signature-not-matching-payload"
    assert db.query(ExecutionJob).count() == 0


async def test_AS29_paused_instance_skips_entire_cycle(db, user, agent):
    instance = _instance(db, user, agent, status="paused")
    schedule = _due_schedule(db, user, agent, last_check_signature="stale-signature-not-matching-payload")
    schedule.agent_instance_id = instance.id
    db.commit()

    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    assert fired == 0
    db.refresh(schedule)
    assert schedule.last_decision == "skipped"
    assert schedule.last_run_job_id is None
    assert schedule.last_check_signature == "stale-signature-not-matching-payload"


async def test_AS30_resume_allows_next_due_wake_to_fire(db, user, agent):
    instance = _instance(db, user, agent, status="paused")
    schedule = _due_schedule(db, user, agent, last_check_signature="stale-signature-not-matching-payload")
    schedule.agent_instance_id = instance.id
    db.commit()

    blocked = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))
    assert blocked == 0

    agent_instance_service.resume_agent_instance(db, instance.id, user.id)
    db.refresh(schedule)
    schedule.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)  # due again
    db.commit()

    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))
    assert fired == 1
    db.refresh(schedule)
    assert schedule.last_decision == "acted"
    assert schedule.last_run_job_id is not None


async def test_AS31_at_most_one_active_job_holds_for_instance_bound_schedule(db, user, agent):
    instance = _instance(db, user, agent, status="active")
    schedule = _due_schedule(db, user, agent, last_check_signature="stale-signature-not-matching-payload")
    schedule.agent_instance_id = instance.id
    db.commit()

    first = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))
    assert first == 1
    db.refresh(schedule)
    db.refresh(instance)
    first_job_id = schedule.last_run_job_id
    assert instance.last_execution_job_id == first_job_id

    # The job is still non-terminal (FakeExecutionProvider queues it,
    # nothing here runs the durable worker) — a second due wake must NOT
    # create a second job.
    schedule.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    second = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    assert second == 0
    db.refresh(schedule)
    db.refresh(instance)
    assert schedule.last_decision == "skipped"
    assert schedule.last_run_job_id == first_job_id
    assert instance.last_execution_job_id == first_job_id
    assert db.query(ExecutionJob).count() == 1


async def test_AS32_legacy_schedule_with_no_instance_unaffected(db, user, agent):
    """A schedule with agent_instance_id=None (every pre-existing schedule,
    or one deliberately created without an instance) takes the SAME path
    it always did — the instance gate is a no-op for it, not a new failure
    mode."""
    schedule = _due_schedule(db, user, agent, last_check_signature="stale-signature-not-matching-payload")
    assert schedule.agent_instance_id is None

    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    assert fired == 1
    db.refresh(schedule)
    assert schedule.last_decision == "acted"
    assert schedule.last_run_job_id is not None


# ── Tier 2 — LLM DECIDE ──────────────────────────────────────────────────

from src.services.agent_check_source import CheckResult, CheckSource  # noqa: E402


class _ForceChanged(CheckSource):
    async def check(self, schedule):
        return CheckResult(changed=True, signature="forced", reason="test", observed_excerpt="the page now says X")


class _ForceUnchanged(CheckSource):
    async def check(self, schedule):
        return CheckResult(changed=False, signature=schedule.last_check_signature or "same", reason="test")


_TIER2_PAYLOAD = {"check": {"type": "http", "url": "http://127.0.0.1:1/never-reached"}, "decide": {}}


def _due_llm_schedule(db, user, agent, **overrides) -> AgentSchedule:
    return _due_schedule(db, user, agent, job_payload=_TIER2_PAYLOAD, **overrides)


async def test_AS33_check_changed_creates_decide_job(db, user, agent):
    schedule = _due_llm_schedule(db, user, agent)

    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc), check_source=_ForceChanged())

    assert fired == 0  # a DECIDE is not itself an action
    db.refresh(schedule)
    assert schedule.last_decision == "decide_pending"
    assert schedule.pending_decision_job_id is not None
    decide_job = db.query(ExecutionJob).filter(ExecutionJob.id == schedule.pending_decision_job_id).first()
    assert decide_job is not None
    assert decide_job.job_type == "agent_decide"
    assert decide_job.status == "queued"


async def test_AS34_check_unchanged_no_decide_job(db, user, agent):
    schedule = _due_llm_schedule(db, user, agent, last_check_signature="same")

    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc), check_source=_ForceUnchanged())

    assert fired == 0
    db.refresh(schedule)
    assert schedule.last_decision == "skipped"
    assert schedule.pending_decision_job_id is None
    assert db.query(ExecutionJob).count() == 0


async def test_AS35_pending_decide_still_running_is_a_noop(db, user, agent):
    schedule = _due_llm_schedule(db, user, agent)
    await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc), check_source=_ForceChanged())
    db.refresh(schedule)
    decide_job_id = schedule.pending_decision_job_id
    assert decide_job_id is not None

    schedule.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    assert fired == 0
    db.refresh(schedule)
    assert schedule.pending_decision_job_id == decide_job_id  # unchanged — still waiting
    assert db.query(ExecutionJob).count() == 1  # no second DECIDE was created


def _complete_decide_job(db, job_id: str, *, decision_json: str) -> None:
    job = db.query(ExecutionJob).filter(ExecutionJob.id == job_id).first()
    job.status = "completed"
    job.result = {"output": decision_json}
    db.commit()


def _fail_decide_job(db, job_id: str) -> None:
    job = db.query(ExecutionJob).filter(ExecutionJob.id == job_id).first()
    job.status = "failed"
    job.error = "simulated DECIDE failure"
    db.commit()


async def test_AS36_consuming_act_decision_creates_act_job(db, user, agent):
    schedule = _due_llm_schedule(db, user, agent)
    await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc), check_source=_ForceChanged())
    db.refresh(schedule)
    decide_job_id = schedule.pending_decision_job_id

    _complete_decide_job(db, decide_job_id, decision_json=json.dumps({
        "decision": "act", "reason": "worth it",
        "action": {"job_type": "research", "payload": {"steps": [{"command": "summarize"}]}},
    }))

    schedule.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    assert fired == 1
    db.refresh(schedule)
    assert schedule.pending_decision_job_id is None
    assert schedule.last_decision == "acted"
    act_job = db.query(ExecutionJob).filter(ExecutionJob.id == schedule.last_run_job_id).first()
    assert act_job is not None
    assert act_job.job_type == "research"
    assert act_job.payload == {"steps": [{"command": "summarize"}]}
    assert act_job.sentinel_criteria == {"min_output_length": 1}


async def test_AS37_consuming_skip_decision_no_act(db, user, agent):
    schedule = _due_llm_schedule(db, user, agent)
    await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc), check_source=_ForceChanged())
    db.refresh(schedule)
    decide_job_id = schedule.pending_decision_job_id

    _complete_decide_job(db, decide_job_id, decision_json=json.dumps({"decision": "skip", "reason": "not worth it"}))

    schedule.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    assert fired == 0
    db.refresh(schedule)
    assert schedule.pending_decision_job_id is None
    assert schedule.last_decision == "decide_skip"
    assert schedule.last_run_job_id is None


async def test_AS38_consuming_invalid_output_no_act(db, user, agent):
    schedule = _due_llm_schedule(db, user, agent)
    await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc), check_source=_ForceChanged())
    db.refresh(schedule)
    decide_job_id = schedule.pending_decision_job_id

    _complete_decide_job(db, decide_job_id, decision_json="not valid json")

    schedule.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    assert fired == 0
    db.refresh(schedule)
    assert schedule.pending_decision_job_id is None
    assert schedule.last_decision == "decide_invalid"
    assert schedule.last_run_job_id is None


async def test_AS39_consuming_failed_decide_job_no_act(db, user, agent):
    schedule = _due_llm_schedule(db, user, agent)
    await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc), check_source=_ForceChanged())
    db.refresh(schedule)
    decide_job_id = schedule.pending_decision_job_id

    _fail_decide_job(db, decide_job_id)

    schedule.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc))

    assert fired == 0
    db.refresh(schedule)
    assert schedule.pending_decision_job_id is None
    assert schedule.last_decision == "decide_failed"
    assert schedule.last_run_job_id is None


async def test_AS40_decide_creation_capacity_refused(db, user, agent, monkeypatch):
    from src.services import adaptive_budget_service

    monkeypatch.setattr(
        adaptive_budget_service, "compute_capacity",
        lambda *a, **k: adaptive_budget_service.CapacitySnapshot(
            tier=adaptive_budget_service.TIER_PAUSE_LOW_PRIORITY, capacity_remaining_pct=5.0,
            session_pct_used=95.0, session_resets_at=None, session_active=True,
            weekly_pct_used=95.0, weekly_resets_at=None,
        ),
    )
    schedule = _due_llm_schedule(db, user, agent, priority="low")

    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc), check_source=_ForceChanged())

    assert fired == 0
    db.refresh(schedule)
    assert schedule.last_decision == "capacity_refused"
    assert schedule.pending_decision_job_id is None
    assert db.query(ExecutionJob).count() == 0


async def test_AS41_crash_safe_consumption_at_most_one_act(db, user, agent):
    """Directly exercises the atomic CAS consumption twice for the SAME
    terminal DECIDE job — simulating two overlapping poll cycles or a
    restart between them (mission item 8, scenarios B/C/D/E). At most
    one ACT job must ever be created."""
    schedule = _due_llm_schedule(db, user, agent)
    await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc), check_source=_ForceChanged())
    db.refresh(schedule)
    decide_job_id = schedule.pending_decision_job_id
    _complete_decide_job(db, decide_job_id, decision_json=json.dumps({
        "decision": "act", "action": {"job_type": "research", "payload": {"steps": [{"command": "go"}]}},
    }))

    now = datetime.now(timezone.utc)
    user_row = db.query(User).filter(User.id == user.id).first()
    first = await agent_schedule_service._consume_pending_decision(db, schedule, agent, None, user_row, now)
    # Second call re-reads the schedule fresh, mirroring a second, fully
    # independent poll cycle observing the SAME (by-then-already-cleared)
    # pending_decision_job_id — nothing left to consume.
    db.refresh(schedule)
    second = await agent_schedule_service._consume_pending_decision(db, schedule, agent, None, user_row, now)

    assert first is True
    assert second is False
    act_jobs = db.query(ExecutionJob).filter(ExecutionJob.job_type == "research").all()
    assert len(act_jobs) == 1


def test_AS42_create_schedule_rejects_both_action_and_decide(db, user, agent):
    with pytest.raises(ValueError):
        agent_schedule_service.create_schedule(
            db, user_id=user.id, agent_id=agent.id, interval_seconds=120,
            job_payload={
                "check": {"type": "http", "url": "http://x"},
                "action": {"job_type": "shell", "payload": {"steps": [{"command": "x"}]}},
                "decide": {},
            },
        )


async def test_AS43_active_act_job_blocks_new_decide(db, user, agent):
    existing_job = ExecutionJob(
        user_id=user.id, agent_id=agent.id, job_type="research", provider_name="model_router",
        payload={"steps": [{"command": "x"}]}, checkpoint={}, status="running",
    )
    db.add(existing_job)
    db.commit()
    db.refresh(existing_job)
    schedule = _due_llm_schedule(db, user, agent, last_run_job_id=existing_job.id)

    fired = await agent_schedule_service._fire_due_schedules(db, now=datetime.now(timezone.utc), check_source=_ForceChanged())

    assert fired == 0
    db.refresh(schedule)
    assert schedule.last_decision == "skipped"
    assert schedule.pending_decision_job_id is None
    assert db.query(ExecutionJob).filter(ExecutionJob.job_type == "agent_decide").count() == 0
