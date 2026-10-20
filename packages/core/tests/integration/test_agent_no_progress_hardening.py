"""Hardening (2026-09-02) — per-tool no-progress budget for the /agent
ReAct loop, on top of the existing exact-match repeated-action circuit
breaker (test_agent_repeated_action_circuit_breaker.py).

Closes the gap identified in the AMARELO verdict of the read-only audit:
the exact-match breaker (`_action_failure_counts`, keyed on
tool_name+args) is blind to a model that varies its arguments slightly on
every failing retry — `tool X(args A)` fails, `tool X(args B)` fails,
`tool X(args C)` fails, never repeating the identical signature twice.
`_tool_failure_streaks` (keyed on tool_name ONLY, agent_service.py) closes
that gap: 3 consecutive failures of the SAME tool — regardless of its
arguments — trips a second, independent, deterministic circuit breaker.
No LLM call, no embedding, no semantic comparison — a plain in-memory
counter reset only by that same tool's own success.
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


def _make_tool_call(tool_name: str, args: dict, call_id: str) -> SimpleNamespace:
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
# 1. Identical failed actions — existing exact-match breaker still governs
#    (unchanged behavior, trips at 2, before the new streak counter would
#    even reach its own limit of 3).
# ═════════════════════════════════════════════════════════════════════════

def test_identical_failed_actions_still_use_the_exact_match_breaker(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    responses = [_routing_result(tool_calls=[_make_tool_call("read_file", {"path": "a.txt"}, f"c{i}")]) for i in range(10)]

    with patch("src.services.agent_service._router.complete", new=AsyncMock(side_effect=responses)), \
         patch("src.services.agent_service._run_tool", new=AsyncMock(return_value=(False, "boom"))):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "read a.txt"}, headers=auth_headers)

    done = next(e for e in _parse_sse(resp.text) if e["type"] == "done")
    assert done["iterations"] == 2
    assert "exact same input" in done["answer"]


# ═════════════════════════════════════════════════════════════════════════
# 2. Same tool, slightly different arguments each attempt — the NEW
#    hardening must catch this; the exact-match breaker alone cannot.
# ═════════════════════════════════════════════════════════════════════════

def test_same_tool_varying_arguments_trips_the_new_no_progress_breaker(client, auth_headers, db):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    paths = ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]
    responses = [_routing_result(tool_calls=[_make_tool_call("read_file", {"path": p}, f"c{i}")]) for i, p in enumerate(paths)]

    before = core_metrics.counter("agent_early_stops_total").snapshot()
    with patch("src.services.agent_service._router.complete", new=AsyncMock(side_effect=responses)), \
         patch("src.services.agent_service._run_tool", new=AsyncMock(return_value=(False, "not found"))):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "find a readable file"}, headers=auth_headers)

    assert resp.status_code == 200
    done = next(e for e in _parse_sse(resp.text) if e["type"] == "done")

    # 3 distinct (tool, args) signatures, none repeated -> the exact-match
    # breaker (limit=2 on an IDENTICAL signature) never trips; the new
    # per-tool streak breaker (limit=3, any args) does, at iteration 3.
    assert done["iterations"] == 3
    assert "varying inputs" in done["answer"]
    assert "read_file" in done["answer"]

    after = core_metrics.counter("agent_early_stops_total").snapshot()
    assert _counter_sum(after, reason="tool_no_progress") >= _counter_sum(before, reason="tool_no_progress") + 1

    messages = db.query(Message).filter(Message.session_id == session_id).all()
    assistant_msgs = [m for m in messages if m.role == "assistant"]
    assert len(assistant_msgs) == 1
    assert "varying inputs" in assistant_msgs[0].content


def test_varying_arguments_never_exceed_the_streak_limit_regardless_of_max_iterations(client, auth_headers):
    """Even with agent_max_iterations far larger than the streak limit,
    the agent cannot be made to consume the whole iteration budget by
    varying arguments on every attempt."""
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    responses = [_routing_result(tool_calls=[_make_tool_call("web_search", {"query": f"query variant {i}"}, f"c{i}")]) for i in range(10)]

    complete_mock = AsyncMock(side_effect=responses)
    with patch("src.services.agent_service._router.complete", new=complete_mock), \
         patch("src.services.agent_service._run_tool", new=AsyncMock(return_value=(False, "search failed"))):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "search for something"}, headers=auth_headers)

    done = next(e for e in _parse_sse(resp.text) if e["type"] == "done")
    assert done["iterations"] == 3  # agent_tool_failure_streak_limit default
    assert complete_mock.call_count == 3  # never reached anywhere near the 10-iteration budget


# ═════════════════════════════════════════════════════════════════════════
# 3. Repeated failures across DIFFERENT tools — proves per-tool isolation:
#    each tool gets its OWN streak budget, one tool's failures don't
#    contaminate another's count, and don't prematurely trip either.
# ═════════════════════════════════════════════════════════════════════════

def test_failures_across_different_tools_do_not_cross_contaminate(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    # Alternate between two DIFFERENT tools, each failing — neither tool's
    # OWN streak (2 each) reaches the per-tool limit (3), so this must run
    # to genuine completion via a later successful call, not trip early.
    responses = [
        _routing_result(tool_calls=[_make_tool_call("read_file", {"path": "a.txt"}, "c0")]),
        _routing_result(tool_calls=[_make_tool_call("list_directory", {"path": "dir1"}, "c1")]),
        _routing_result(tool_calls=[_make_tool_call("read_file", {"path": "b.txt"}, "c2")]),
        _routing_result(tool_calls=[_make_tool_call("list_directory", {"path": "dir2"}, "c3")]),
        _routing_result("I could not find a readable file after checking."),
    ]
    # Every (tool, args) signature above is DISTINCT (a.txt != b.txt,
    # dir1 != dir2) — the exact-match breaker (limit=2 on an IDENTICAL
    # signature) never trips here; only per-tool streaks matter, and
    # read_file/list_directory each reach 2, below their limit of 3.
    outcomes = iter([(False, "err1"), (False, "err2"), (False, "err3"), (False, "err4")])

    async def _run_tool_stub(*args, **kwargs):
        return next(outcomes)

    with patch("src.services.agent_service._router.complete", new=AsyncMock(side_effect=responses)), \
         patch("src.services.agent_service._run_tool", new=_run_tool_stub):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "find and read a file"}, headers=auth_headers)

    done = next(e for e in _parse_sse(resp.text) if e["type"] == "done")
    # Neither tool's streak (2 failures each) reached the limit of 3 —
    # the loop ran all 5 iterations to its genuine (model-decided) end.
    assert done["iterations"] == 5
    assert done["answer"] == "I could not find a readable file after checking."


# ═════════════════════════════════════════════════════════════════════════
# 4. Successful repeated actions are never penalized — same tool, same or
#    varying args, all succeeding, must run to full genuine completion.
# ═════════════════════════════════════════════════════════════════════════

def test_successful_repeated_actions_are_not_penalized(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    responses = [
        _routing_result(tool_calls=[_make_tool_call("read_file", {"path": "a.txt"}, "c0")]),
        _routing_result(tool_calls=[_make_tool_call("read_file", {"path": "a.txt"}, "c1")]),  # identical, but SUCCEEDS
        _routing_result(tool_calls=[_make_tool_call("read_file", {"path": "a.txt"}, "c2")]),  # identical, still SUCCEEDS
        _routing_result("Read the file three times successfully."),
    ]

    with patch("src.services.agent_service._router.complete", new=AsyncMock(side_effect=responses)), \
         patch("src.services.agent_service._run_tool", new=AsyncMock(return_value=(True, "file contents"))):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "read a.txt three times"}, headers=auth_headers)

    done = next(e for e in _parse_sse(resp.text) if e["type"] == "done")
    assert done["iterations"] == 4
    assert done["answer"] == "Read the file three times successfully."


# ═════════════════════════════════════════════════════════════════════════
# 5. Failure streak resets after genuine progress (a success of that SAME
#    tool) — the agent gets a fresh budget, it isn't "one strike and
#    permanently marked."
# ═════════════════════════════════════════════════════════════════════════

def test_streak_resets_after_the_same_tool_succeeds(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    # 2 failures (below the streak limit of 3) -> 1 success (RESETS the
    # streak) -> 2 more failures (below the limit again, counting from 0)
    # -> a final success. If the streak did NOT reset, the 4th failure
    # would be this tool's 4th overall failure and would incorrectly trip.
    responses = [
        _routing_result(tool_calls=[_make_tool_call("read_file", {"path": "x1.txt"}, "c0")]),
        _routing_result(tool_calls=[_make_tool_call("read_file", {"path": "x2.txt"}, "c1")]),
        _routing_result(tool_calls=[_make_tool_call("read_file", {"path": "good.txt"}, "c2")]),
        _routing_result(tool_calls=[_make_tool_call("read_file", {"path": "x3.txt"}, "c3")]),
        _routing_result(tool_calls=[_make_tool_call("read_file", {"path": "x4.txt"}, "c4")]),
        _routing_result("Eventually found and read the right file."),
    ]
    outcomes = iter([(False, "e1"), (False, "e2"), (True, "ok"), (False, "e3"), (False, "e4")])

    async def _run_tool_stub(*args, **kwargs):
        return next(outcomes)

    with patch("src.services.agent_service._router.complete", new=AsyncMock(side_effect=responses)), \
         patch("src.services.agent_service._run_tool", new=_run_tool_stub):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "keep trying until it works"}, headers=auth_headers)

    done = next(e for e in _parse_sse(resp.text) if e["type"] == "done")
    assert done["iterations"] == 6  # ran to genuine completion — the mid-stream success reset the streak
    assert done["answer"] == "Eventually found and read the right file."


# ═════════════════════════════════════════════════════════════════════════
# 6. Maximum iteration protection still works when NEITHER breaker trips
#    (distinct, genuinely-different, all-failing actions spread thin
#    enough that no single tool's streak or exact signature repeats).
# ═════════════════════════════════════════════════════════════════════════

def test_max_iteration_cap_still_protects_when_breakers_never_trip(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    # 8 distinct tool names, cycled across 10 calls: each tool is used at
    # most 2 times total (10 // 8 = 1, remainder 2) — below the per-tool
    # streak limit of 3 — AND every call's args are unique (`{"n": i}`),
    # so the exact-match breaker never trips either. This is deliberately
    # the "neither new breaker applies" case, isolating the pre-existing
    # agent_max_iterations backstop as the only thing that can still stop
    # the loop here.
    tools = [
        "read_file", "write_file", "list_directory", "execute_shell",
        "web_search", "browser_navigate", "browser_get_text", "browser_screenshot",
    ]
    responses = [
        _routing_result(tool_calls=[_make_tool_call(tools[i % len(tools)], {"n": i}, f"c{i}")])
        for i in range(10)
    ]

    complete_mock = AsyncMock(side_effect=responses)
    with patch("src.services.agent_service._router.complete", new=complete_mock), \
         patch("src.services.agent_service.wait_for_gate", new=AsyncMock(return_value=True)), \
         patch("src.services.agent_service._run_tool", new=AsyncMock(return_value=(False, "boom"))):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "cycle through tools"}, headers=auth_headers)

    done = next(e for e in _parse_sse(resp.text) if e["type"] == "done")
    assert done["iterations"] == 10  # neither breaker applies — the classic agent_max_iterations cap is what stops it
    assert complete_mock.call_count == 10
    assert "unable to complete" in done["answer"].lower()


# ═════════════════════════════════════════════════════════════════════════
# 7. Terminal response is emitted exactly once, however the loop ends.
# ═════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("scenario", ["exact_match", "no_progress", "normal_success"])
def test_terminal_done_event_emitted_exactly_once(client, auth_headers, scenario):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]

    if scenario == "exact_match":
        responses = [_routing_result(tool_calls=[_make_tool_call("read_file", {"path": "a.txt"}, f"c{i}")]) for i in range(10)]
        run_tool = AsyncMock(return_value=(False, "boom"))
    elif scenario == "no_progress":
        responses = [_routing_result(tool_calls=[_make_tool_call("read_file", {"path": f"{i}.txt"}, f"c{i}")]) for i in range(10)]
        run_tool = AsyncMock(return_value=(False, "boom"))
    else:
        responses = _routing_result("direct answer")
        run_tool = AsyncMock(return_value=(True, "n/a"))

    with patch("src.services.agent_service._router.complete", new=AsyncMock(side_effect=responses) if isinstance(responses, list) else AsyncMock(return_value=responses)), \
         patch("src.services.agent_service._run_tool", new=run_tool):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "go"}, headers=auth_headers)

    events = _parse_sse(resp.text)
    done_events = [e for e in events if e["type"] == "done"]
    assert len(done_events) == 1
    assert resp.text.count("[DONE]") == 1


# ═════════════════════════════════════════════════════════════════════════
# 8. Usage/TCU finalization still occurs correctly on an early no-progress
#    stop — billing must reflect what was actually consumed, exactly like
#    every other exit path.
# ═════════════════════════════════════════════════════════════════════════

def test_usage_finalized_correctly_on_no_progress_early_stop(client, auth_headers, db):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    responses = [_routing_result(tool_calls=[_make_tool_call("read_file", {"path": f"{i}.txt"}, f"c{i}")], cost_usd=0.002) for i in range(10)]

    with patch("src.services.agent_service._router.complete", new=AsyncMock(side_effect=responses)), \
         patch("src.services.agent_service._run_tool", new=AsyncMock(return_value=(False, "boom"))):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "go"}, headers=auth_headers)

    done = next(e for e in _parse_sse(resp.text) if e["type"] == "done")
    assert done["iterations"] == 3  # streak breaker, not the 10-iteration cap
    assert "cost_usd" not in done["usage"]  # billed below, never streamed to the client

    records = db.query(UsageRecord).filter(UsageRecord.session_id == session_id).all()
    assert len(records) == 1
    assert float(records[0].cost_usd) == pytest.approx(0.006, abs=1e-9)


# ═════════════════════════════════════════════════════════════════════════
# 9. No regression in existing agent tests — covered by running
#    test_agent_route.py and test_agent_repeated_action_circuit_breaker.py
#    alongside this file (see the task's "focused tests" run); not
#    duplicated here.
# ═════════════════════════════════════════════════════════════════════════
