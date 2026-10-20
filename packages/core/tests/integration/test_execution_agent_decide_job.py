"""Integration tests for job_type="agent_decide" — Persistent Agent
Runtime Tier 2's DECIDE ExecutionJob, flowing through the EXISTING
Durable Execution Worker via a NEW provider
(src/execution/agent_decision_provider.py). Same fake-the-router-only
pattern as test_execution_research_job.py — never fakes
execution_service/entitlement_service/the sandbox, so this exercises the
REAL claim/execute/billing/retry/recovery path, only the actual LLM call
is replaced.

Test matrix
-----------
DJ01) Happy path: a valid "act" JSON response -> job completed, exactly
      one UsageRecord finalized, both system and user messages sent to
      the router, RESEARCH capability forwarded (reused, not a new one).
DJ02) A valid "skip" JSON response is ALSO a successful job — "skip" is a
      legitimate decision, not a failure.
DJ03) Malformed JSON is billed (a real call happened) but the STEP fails
      (recoverable) -> job-level retry -> exhausts max_retries -> job
      ultimately "failed", with EVERY attempt's UsageRecord left
      "completed" (never "pending", never uncharged for a call that
      really happened).
DJ04) ProviderUnavailableError -> reservation cancelled -> retried ->
      succeeds on the second attempt (identical classification to the
      research provider).
DJ05) Entitlement exceeded -> permanent failure, router never called, no
      UsageRecord created at all.
DJ06) The RESEARCH capability kill switch also blocks job_type="agent_decide"
      (reused switch, not a separate one — see execution_service.py).
DJ07) job_type="agent_decide" cannot be created via POST /execution/jobs
      (422) — it is an internal-only job_type, created solely by
      agent_schedule_service.py's own Tier-2 WAKE cycle.
DJ08) job_type="shell" (FakeExecutionProvider) is completely unaffected —
      the new provider dispatch never touches an unrelated job_type.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from src.execution import agent_decision_provider
from src.models.execution import ExecutionJob
from src.models.usage import UsageRecord
from src.services import entitlement_service, execution_service, execution_worker
from trinity_router import ProviderUnavailableError, RoutingResult

pytestmark = pytest.mark.usefixtures("execution_session_override")


def _act_json(prompt: str = "summarize the observed change") -> str:
    return json.dumps({
        "decision": "act", "reason": "content changed meaningfully",
        "action": {"job_type": "research", "payload": {"steps": [{"command": prompt}]}},
    })


def _skip_json() -> str:
    return json.dumps({"decision": "skip", "reason": "nothing worth acting on"})


def _result(content: str, tcu: float = 1.0, cost_usd: float = 0.001) -> RoutingResult:
    return RoutingResult(
        content=content, tool_calls=None, provider="anthropic", model="claude-sonnet",
        input_tokens=10, output_tokens=20, cached_tokens=0,
        cost_usd=cost_usd, tcu=tcu, failover_used=False, failover_reason=None,
    )


class _FakeRouter:
    def __init__(self) -> None:
        self.calls: list = []
        self._queue: list = []

    def queue_result(self, result: RoutingResult) -> None:
        self._queue.append(result)

    def queue_exception(self, exc: Exception) -> None:
        self._queue.append(exc)

    async def complete(self, request):
        self.calls.append(request)
        if not self._queue:
            raise AssertionError("FakeRouter queue exhausted — test didn't queue enough results")
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fake_router(monkeypatch):
    fake = _FakeRouter()
    monkeypatch.setattr(agent_decision_provider, "_router", fake)
    return fake


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    monkeypatch.setattr(execution_worker, "_BACKOFF_BASE_SECONDS", 0.01)


async def test_DJ01_happy_path_act_completes_and_finalizes(client, db, registered_user, fake_router, run_worker_until_terminal):
    fake_router.queue_result(_result(content=_act_json("what changed on the page?")))

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        job_type="agent_decide", payload={"steps": [{"command": "DECIDE context here"}]},
    )
    assert job.provider_name == "agent_decision"

    job = await run_worker_until_terminal(db, job.id)

    assert job.status == "completed"
    decision = json.loads(job.result["output"])
    assert decision["decision"] == "act"

    reservation = db.query(UsageRecord).filter(UsageRecord.id == job.reservation_id).first()
    assert reservation is not None and reservation.status == "completed"

    assert len(fake_router.calls) == 1
    call = fake_router.calls[0]
    assert call.capability == "research"
    assert [m["role"] for m in call.messages] == ["system", "user"]
    assert "DECIDE" in call.messages[0]["content"] or "decide" in call.messages[0]["content"].lower()
    assert call.messages[1]["content"] == "DECIDE context here"


async def test_DJ02_valid_skip_is_also_a_success(client, db, registered_user, fake_router, run_worker_until_terminal):
    fake_router.queue_result(_result(content=_skip_json()))

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        job_type="agent_decide", payload={"steps": [{"command": "context"}]},
    )
    job = await run_worker_until_terminal(db, job.id)

    assert job.status == "completed"
    decision = json.loads(job.result["output"])
    assert decision["decision"] == "skip"


async def test_DJ03_malformed_json_billed_but_fails_after_retries(client, db, registered_user, fake_router, run_worker_until_terminal):
    fake_router.queue_result(_result(content="not json at all, sorry"))
    fake_router.queue_result(_result(content="still not json"))

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        job_type="agent_decide", payload={"steps": [{"command": "context"}]}, max_retries=1,
    )
    job = await run_worker_until_terminal(db, job.id, timeout=15.0)

    assert job.status == "failed"
    assert job.attempt_number == 2
    assert len(fake_router.calls) == 2

    reservations = db.query(UsageRecord).filter(UsageRecord.user_id == registered_user["user_id"]).all()
    assert len(reservations) == 2
    assert all(r.status == "completed" for r in reservations)  # both real calls, both genuinely billed


async def test_DJ04_provider_unavailable_retries_then_succeeds(client, db, registered_user, fake_router, run_worker_until_terminal):
    fake_router.queue_exception(ProviderUnavailableError(providers_tried=["anthropic"]))
    fake_router.queue_result(_result(content=_skip_json()))

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        job_type="agent_decide", payload={"steps": [{"command": "context"}]}, max_retries=2,
    )
    job = await run_worker_until_terminal(db, job.id, timeout=15.0)

    assert job.status == "completed"
    assert job.attempt_number == 2

    reservations = db.query(UsageRecord).filter(UsageRecord.user_id == registered_user["user_id"]).all()
    statuses = sorted(r.status for r in reservations)
    assert statuses == ["completed", "failed"]  # first cancelled (router unavailable), second billed


async def test_DJ05_entitlement_exceeded_never_calls_router(client, db, registered_user, fake_router, monkeypatch, run_worker_until_terminal):
    depleted = dataclasses.replace(entitlement_service.get_plan_entitlement("pro"), daily_tcu_limit=0.0)
    monkeypatch.setattr(entitlement_service, "get_plan_entitlement", lambda plan: depleted)

    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        job_type="agent_decide", payload={"steps": [{"command": "context"}]}, max_retries=3,
    )
    job = await run_worker_until_terminal(db, job.id)

    assert job.status == "failed"
    assert job.attempt_number == 1
    assert len(fake_router.calls) == 0
    assert db.query(UsageRecord).filter(UsageRecord.user_id == registered_user["user_id"]).count() == 0


async def test_DJ06_research_capability_kill_switch_blocks_agent_decide_too(client, db, registered_user, fake_router, monkeypatch):
    from src.services import runtime_settings
    monkeypatch.setattr(runtime_settings, "get_disabled_capabilities", lambda db: frozenset({"research"}))

    with pytest.raises(execution_service.CapabilityDisabledError):
        execution_service.create_job(
            db, user_id=registered_user["user_id"], plan="pro",
            job_type="agent_decide", payload={"steps": [{"command": "context"}]},
        )
    assert len(fake_router.calls) == 0


async def test_DJ07_agent_decide_rejected_via_http_route(client, auth_headers):
    resp = client.post(
        "/execution/jobs",
        json={"job_type": "agent_decide", "payload": {"steps": [{"command": "x"}]}},
        headers=auth_headers,
    )
    assert resp.status_code == 422


async def test_DJ08_shell_job_type_unaffected(client, db, registered_user, fake_router, tmp_path, run_worker_until_terminal):
    from src.execution.fake_provider import FakeExecutionProvider

    provider = FakeExecutionProvider(tmp_path)
    job = execution_service.create_job(
        db, user_id=registered_user["user_id"], plan="pro",
        payload={"steps": [{"command": "echo hi"}]}, provider=provider,
    )
    job = await run_worker_until_terminal(db, job.id)

    assert job.status == "completed"
    assert len(fake_router.calls) == 0
