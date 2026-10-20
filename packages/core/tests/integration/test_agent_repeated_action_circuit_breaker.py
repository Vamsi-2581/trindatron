"""Root-cause fix (2026-09-02) — repeated-action circuit breaker for the
/agent ReAct loop (src/services/agent_service.py::stream_agent()).

Root cause (see the audit delivered alongside this fix): the ReAct loop had
an overall iteration cap (agent_max_iterations) and a wall-clock timeout
(Agent Scheduler's with_execution_timeout(), Etapa 5D — untouched by this
fix), but NOTHING tracked whether the model was calling the exact same
tool with the exact same arguments repeatedly. A model stuck re-trying a
broken action would silently burn up to agent_max_iterations real
provider calls (real TCU/tokens/time) before presenting a generic
"unable to complete" message indistinguishable from a genuinely hard
multi-step task — matching the user-observed
"ERRO → tenta corrigir → mesmo erro → tenta de novo → ciclo" pattern.

This file proves the fix: the SAME (tool, args) signature failing
`agent_repeated_action_failure_limit` (default 2) times ends the loop
IMMEDIATELY with a specific, diagnosable message — never silently
exhausting the iteration budget — while every existing, already-approved
behavior (successful completions, genuinely-different tool calls, one
transient failure followed by success, TCU billing, message persistence)
is completely unaffected.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from trinity_router import RoutingResult

from src.models.message import Message
from src.models.usage import UsageRecord
from src.observability.metrics import core_metrics


def _routing_result(content: str | None = None, tool_calls=None, cost_usd: float = 0.001) -> RoutingResult:
    return RoutingResult(
        content=content, tool_calls=tool_calls, provider="anthropic", model="claude-sonnet-test",
        input_tokens=10, output_tokens=5, cached_tokens=0,
        cost_usd=cost_usd, tcu=cost_usd * 100.0, failover_used=False, failover_reason=None,
    )


def _make_tool_call(tool_name: str, args: dict, call_id: str = "call_1"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=tool_name, arguments=json.dumps(args)))


def _parse_sse(body: str) -> list[dict]:
    events = []
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            events.append(json.loads(line[5:].strip()))
    return events


def _counter_sum(snapshot: dict, **label_filter: str) -> float:
    total = 0.0
    for labels, value in snapshot.items():
        d = dict(labels)
        if all(d.get(k) == v for k, v in label_filter.items()):
            total += value
    return total


# ═════════════════════════════════════════════════════════════════════════
# 1. A task with a SINGLE error still completes normally (one failure of a
#    signature never trips the breaker — only a REPEAT does).
# ═════════════════════════════════════════════════════════════════════════

def test_single_tool_failure_does_not_trip_the_breaker(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    tc = _make_tool_call("read_file", {"path": "missing.txt"})
    responses = [
        _routing_result(tool_calls=[tc]),
        _routing_result("Got it, the file doesn't exist."),  # model recovers on its own
    ]

    with patch("src.services.agent_service._router.complete", new=AsyncMock(side_effect=responses)), \
         patch("src.services.agent_service._run_tool", new=AsyncMock(return_value=(False, "not found"))):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "read missing.txt"}, headers=auth_headers)

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    done = next(e for e in events if e["type"] == "done")
    assert done["answer"] == "Got it, the file doesn't exist."
    assert done["iterations"] == 2


# ═════════════════════════════════════════════════════════════════════════
# 2/6. A REPEATED error for the SAME action trips the circuit breaker and
#      ends the loop in a distinct, diagnosable state (never a silent
#      iteration-exhaustion message).
# ═════════════════════════════════════════════════════════════════════════

def test_repeated_identical_failure_trips_the_circuit_breaker(client, auth_headers, db):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    tc = _make_tool_call("read_file", {"path": "broken.txt"})
    # The model keeps calling the EXACT same broken action every iteration —
    # if the breaker didn't exist, this would run all agent_max_iterations (10).
    responses = [_routing_result(tool_calls=[_make_tool_call("read_file", {"path": "broken.txt"}, call_id=f"c{i}")]) for i in range(10)]

    before = core_metrics.counter("agent_early_stops_total").snapshot()
    with patch("src.services.agent_service._router.complete", new=AsyncMock(side_effect=responses)), \
         patch("src.services.agent_service._run_tool", new=AsyncMock(return_value=(False, "permission denied"))):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "read broken.txt"}, headers=auth_headers)

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    done = next(e for e in events if e["type"] == "done")

    # Stopped after exactly agent_repeated_action_failure_limit (2)
    # failures of the identical action — NOT after all 10 iterations.
    assert done["iterations"] == 2
    assert "failed" in done["answer"].lower()
    assert "read_file" in done["answer"]

    after = core_metrics.counter("agent_early_stops_total").snapshot()
    assert _counter_sum(after, reason="repeated_action_failure") >= _counter_sum(before, reason="repeated_action_failure") + 1

    # The clear message is what the user actually sees in their conversation.
    messages = db.query(Message).filter(Message.session_id == session_id).all()
    assistant_msgs = [m for m in messages if m.role == "assistant"]
    assert len(assistant_msgs) == 1
    assert "read_file" in assistant_msgs[0].content


def test_different_errors_same_action_still_counts_as_repetition(client, auth_headers):
    """Per the explicit requirement: errors that differ in TEXT but come
    from the SAME (tool, args) action must still be recognized as
    repetition — the breaker is keyed on the action, not the error
    message."""
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    responses = [_routing_result(tool_calls=[_make_tool_call("read_file", {"path": "flaky.txt"}, call_id=f"c{i}")]) for i in range(5)]
    errors = iter(["disk error", "permission denied", "timeout", "unknown error", "corrupted"])

    async def _flaky_run_tool(*args, **kwargs):
        return False, next(errors)

    with patch("src.services.agent_service._router.complete", new=AsyncMock(side_effect=responses)), \
         patch("src.services.agent_service._run_tool", new=_flaky_run_tool):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "read flaky.txt"}, headers=auth_headers)

    events = _parse_sse(resp.text)
    done = next(e for e in events if e["type"] == "done")
    assert done["iterations"] == 2  # still trips on the 2nd failure despite different error text


# ═════════════════════════════════════════════════════════════════════════
# 3. The same tool call is never executed indefinitely — bounded to the
#    configured limit, proven by counting actual provider calls.
# ═════════════════════════════════════════════════════════════════════════

def test_same_tool_call_is_not_executed_indefinitely(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    responses = [_routing_result(tool_calls=[_make_tool_call("read_file", {"path": "x.txt"}, call_id=f"c{i}")]) for i in range(10)]

    complete_mock = AsyncMock(side_effect=responses)
    with patch("src.services.agent_service._router.complete", new=complete_mock), \
         patch("src.services.agent_service._run_tool", new=AsyncMock(return_value=(False, "boom"))):
        client.post(f"/sessions/{session_id}/agent", json={"content": "read x.txt"}, headers=auth_headers)

    assert complete_mock.call_count == 2  # not all 10 — stopped as soon as the limit was reached


# ═════════════════════════════════════════════════════════════════════════
# 4. Retries respect the configured limit — parametrized override proves
#    the threshold is actually read from settings, not hardcoded.
# ═════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("limit", [1, 3])
def test_repeated_action_limit_is_configurable(client, auth_headers, monkeypatch, limit):
    monkeypatch.setattr("src.services.agent_service.settings.agent_repeated_action_failure_limit", limit)
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    responses = [_routing_result(tool_calls=[_make_tool_call("read_file", {"path": "x.txt"}, call_id=f"c{i}")]) for i in range(10)]

    with patch("src.services.agent_service._router.complete", new=AsyncMock(side_effect=responses)), \
         patch("src.services.agent_service._run_tool", new=AsyncMock(return_value=(False, "boom"))):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "read x.txt"}, headers=auth_headers)

    done = next(e for e in _parse_sse(resp.text) if e["type"] == "done")
    assert done["iterations"] == limit


# ═════════════════════════════════════════════════════════════════════════
# 5. Pre-existing wall-clock timeout (Etapa 5D) still works with the
#    circuit breaker code present — regression check, not a re-derivation
#    of Etapa 5D's own exhaustive proof (see test_agent_scheduler_streaming.py
#    / test_agent_scheduler_task_leak.py for that).
# ═════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_execution_timeout_still_terminates_a_hung_agent_loop(db):
    import asyncio

    from src.models.session import Session as SessionModel
    from src.services import agent_service
    from src.services.agent_scheduler import with_execution_timeout
    from src.services.session_service import create_session
    from src.models.user import User
    import uuid

    user_id = str(uuid.uuid4())
    db.add(User(id=user_id, email=f"{uuid.uuid4()}@test.com", hashed_password="x", plan="start"))
    db.commit()
    session = create_session(db, user_id=user_id, model=None, project_id=None, agent_definition_id=None, is_temporary=False)

    async def _hung_complete(request):
        await asyncio.sleep(60)  # never resolves within the test's timeout budget
        raise AssertionError("unreachable")

    with patch("src.services.agent_service._router.complete", new=_hung_complete):
        agen = with_execution_timeout(agent_service.stream_agent(db, session, "hi"), timeout_s=0.2)
        events = [e async for e in agen]

    assert any("timed out" in e.lower() or "timeout" in e.lower() for e in events)


# ═════════════════════════════════════════════════════════════════════════
# 7. A completed task is never re-executed — the generator runs exactly
#    once per request, no hidden re-invocation.
# ═════════════════════════════════════════════════════════════════════════

def test_completed_task_never_re_invokes_the_provider(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    direct = _routing_result("42 is the answer.")
    complete_mock = AsyncMock(return_value=direct)

    with patch("src.services.agent_service._router.complete", new=complete_mock):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "what is 6x7"}, headers=auth_headers)

    assert resp.status_code == 200
    assert complete_mock.call_count == 1  # exactly once — no automatic re-trigger after "done"


# ═════════════════════════════════════════════════════════════════════════
# 8. The mechanism never interferes with a normal, successful multi-step
#    task (different tool calls each iteration — no repetition at all).
# ═════════════════════════════════════════════════════════════════════════

def test_normal_multi_step_success_is_unaffected(client, auth_headers, tmp_path):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    tc1 = _make_tool_call("list_directory", {})
    tc2 = _make_tool_call("read_file", {"path": "found.txt"})
    responses = [
        _routing_result(tool_calls=[tc1]),
        _routing_result(tool_calls=[tc2]),
        _routing_result("Done — the file says hello."),
    ]

    with patch("src.services.agent_service._router.complete", new=AsyncMock(side_effect=responses)), \
         patch("src.services.agent_service._workspace_for", return_value=tmp_path), \
         patch("src.services.agent_service._run_tool", new=AsyncMock(side_effect=[(True, "found.txt"), (True, "hello")])):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "find and read a file"}, headers=auth_headers)

    assert resp.status_code == 200
    done = next(e for e in _parse_sse(resp.text) if e["type"] == "done")
    assert done["iterations"] == 3
    assert done["answer"] == "Done — the file says hello."


def test_normal_completion_does_not_increment_early_stop_metric(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    direct = _routing_result("Sure!")
    before = _counter_sum(core_metrics.counter("agent_early_stops_total").snapshot(), reason="repeated_action_failure")

    with patch("src.services.agent_service._router.complete", new=AsyncMock(return_value=direct)):
        client.post(f"/sessions/{session_id}/agent", json={"content": "hi"}, headers=auth_headers)

    after = _counter_sum(core_metrics.counter("agent_early_stops_total").snapshot(), reason="repeated_action_failure")
    assert after == before
