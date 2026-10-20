"""Security etapa (post Tool/Connector Foundation, be6e1b0's own critical
finding): trinity_agent.permissions.manifest.DEFAULT_MANIFEST already
declared write_file/execute_shell as PermissionLevel.CONFIRM, but nothing
reachable from Core's real /agent endpoint ever enforced it. These tests
prove the fix — reusing the SAME async approval-gate mechanism TRINITY
WORK already had (src.services.approval_gate), not a second one.

All _router.complete and _run_tool calls are mocked, exactly like
test_agent_no_progress_hardening.py's own established pattern.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from trinity_router import RoutingResult

import src.services.approval_gate as approval_gate_module


def _routing_result(content: str | None = None, tool_calls=None, cost_usd: float = 0.001) -> RoutingResult:
    return RoutingResult(
        content=content, tool_calls=tool_calls, provider="anthropic", model="claude-sonnet-test",
        input_tokens=10, output_tokens=5, cached_tokens=0,
        cost_usd=cost_usd, tcu=cost_usd * 100.0, failover_used=False, failover_reason=None,
    )


def _make_tool_call(tool_name: str, args: dict, call_id: str = "call_1") -> SimpleNamespace:
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=tool_name, arguments=json.dumps(args)))


def _parse_sse(body: str) -> list[dict]:
    events = []
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            events.append(json.loads(line[5:].strip()))
    return events


# ── CONFIRM-level tools pause for approval ──────────────────────────────────

def test_write_file_emits_approval_required_and_approves(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    tc = _make_tool_call("write_file", {"path": "a.txt", "content": "hi"})
    with_tool = _routing_result(tool_calls=[tc])
    final = _routing_result("Wrote the file.")

    with patch("src.services.agent_service._router.complete", new=AsyncMock(side_effect=[with_tool, final])), \
         patch("src.services.agent_service.wait_for_gate", new=AsyncMock(return_value=True)), \
         patch("src.services.agent_service._run_tool", new=AsyncMock(return_value=(True, "ok"))) as run_tool:
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "write a.txt"}, headers=auth_headers)

    events = _parse_sse(resp.text)
    approval_events = [e for e in events if e["type"] == "approval_required"]
    assert len(approval_events) == 1, f"Expected approval_required, got: {events}"
    assert approval_events[0]["activity"] == "activity.tool.write_file"
    assert "action" not in approval_events[0], "internal tool names stay server-side"
    assert "action_id" in approval_events[0]
    assert "description" in approval_events[0]

    done = next((e for e in events if e["type"] == "done"), None)
    assert done is not None, "Task should complete after approval"
    assert done["answer"] == "Wrote the file."
    run_tool.assert_awaited_once()  # the REAL tool only runs AFTER approval


def test_execute_shell_emits_approval_required_and_approves(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    tc = _make_tool_call("execute_shell", {"command": "echo hi"})
    with_tool = _routing_result(tool_calls=[tc])
    final = _routing_result("Ran the command.")

    with patch("src.services.agent_service._router.complete", new=AsyncMock(side_effect=[with_tool, final])), \
         patch("src.services.agent_service.wait_for_gate", new=AsyncMock(return_value=True)), \
         patch("src.services.agent_service._run_tool", new=AsyncMock(return_value=(True, "hi"))):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "run echo hi"}, headers=auth_headers)

    events = _parse_sse(resp.text)
    approval_events = [e for e in events if e["type"] == "approval_required"]
    assert len(approval_events) == 1
    assert approval_events[0]["activity"] == "activity.tool.execute_shell"
    assert "action" not in approval_events[0]


def test_rejected_write_file_never_executes_and_ends_the_turn_normally(client, auth_headers, db):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    tc = _make_tool_call("write_file", {"path": "secrets.txt", "content": "leak"})
    with_tool = _routing_result(tool_calls=[tc])

    with patch("src.services.agent_service._router.complete", new=AsyncMock(return_value=with_tool)), \
         patch("src.services.agent_service.wait_for_gate", new=AsyncMock(return_value=False)), \
         patch("src.services.agent_service._run_tool", new=AsyncMock(return_value=(True, "ok"))) as run_tool:
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "write secrets.txt"}, headers=auth_headers)

    events = _parse_sse(resp.text)
    run_tool.assert_not_called()  # never executed — rejection must short-circuit before the real tool call
    # Declining is a choice, not a failure: no error event (the app used to say "encontrou um erro"), the turn ends
    # with `done`, its answer says the action was not done, and the model call already made is recorded, not dropped.
    assert not [e for e in events if e["type"] == "error"], events
    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1 and "não fiz essa ação" in done[0]["answer"]
    from src.models.usage import UsageRecord
    db.expire_all()
    statuses = [r.status for r in db.query(UsageRecord).filter(UsageRecord.session_id == session_id).all()]
    assert statuses == ["completed"], statuses


def test_timed_out_write_file_is_treated_as_rejected(client, auth_headers):
    """wait_for_gate() itself already returns False on a real timeout
    (see approval_gate.py) — this proves stream_agent() treats that
    return value identically to an explicit rejection, without needing
    to wait out the real 300s timeout in a test."""
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    tc = _make_tool_call("execute_shell", {"command": "rm file"})
    with_tool = _routing_result(tool_calls=[tc])

    with patch("src.services.agent_service._router.complete", new=AsyncMock(return_value=with_tool)), \
         patch("src.services.agent_service.wait_for_gate", new=AsyncMock(return_value=False)), \
         patch("src.services.agent_service._run_tool", new=AsyncMock(return_value=(True, "ok"))) as run_tool:
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "run rm"}, headers=auth_headers)

    events = _parse_sse(resp.text)
    assert not any(e["type"] == "error" for e in events)
    assert any(e["type"] == "done" for e in events)
    run_tool.assert_not_called()


# ── AUTO-level tools never pause ─────────────────────────────────────────────

def test_read_file_does_not_require_approval(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    tc = _make_tool_call("read_file", {"path": "a.txt"})
    with_tool = _routing_result(tool_calls=[tc])
    final = _routing_result("Here's the content.")

    with patch("src.services.agent_service._router.complete", new=AsyncMock(side_effect=[with_tool, final])), \
         patch("src.services.agent_service.wait_for_gate", new=AsyncMock(return_value=True)) as gate_wait, \
         patch("src.services.agent_service._run_tool", new=AsyncMock(return_value=(True, "content"))):
        resp = client.post(f"/sessions/{session_id}/agent", json={"content": "read a.txt"}, headers=auth_headers)

    events = _parse_sse(resp.text)
    assert not any(e["type"] == "approval_required" for e in events)
    gate_wait.assert_not_called()
    done = next((e for e in events if e["type"] == "done"), None)
    assert done is not None


# ── /sessions/{id}/agent/approve and /reject ────────────────────────────────

def test_approve_agent_action_requires_auth(client):
    resp = client.post("/sessions/some-id/agent/approve", json={"action_id": "x"})
    assert resp.status_code in (401, 403)


def test_approve_agent_action_session_not_found_returns_404(client, auth_headers):
    resp = client.post(
        "/sessions/nonexistent-session/agent/approve",
        json={"action_id": "x"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


def test_approve_agent_action_gate_not_found_returns_404(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    resp = client.post(
        f"/sessions/{session_id}/agent/approve",
        json={"action_id": "nonexistent-gate-id"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


def test_approve_agent_action_resolves_gate(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    action_id = "test-action-1"
    gate = approval_gate_module.create_gate(action_id, owner_id=session_id)

    resp = client.post(
        f"/sessions/{session_id}/agent/approve",
        json={"action_id": action_id},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert gate.approved is True
    assert gate.event.is_set()
    approval_gate_module._pending_gates.pop(action_id, None)


def test_reject_agent_action_resolves_gate(client, auth_headers):
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    action_id = "test-action-2"
    gate = approval_gate_module.create_gate(action_id, owner_id=session_id)

    resp = client.post(
        f"/sessions/{session_id}/agent/reject",
        json={"action_id": action_id},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert gate.approved is False
    approval_gate_module._pending_gates.pop(action_id, None)


def test_agent_cannot_resolve_another_sessions_gate(client, auth_headers):
    """A gate created for session A must not be resolvable via session B's
    approve endpoint — mirrors resolve_gate()'s own owner_id check."""
    session_a = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    session_b = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    action_id = "cross-session-action"
    gate = approval_gate_module.create_gate(action_id, owner_id=session_a)

    resp = client.post(
        f"/sessions/{session_b}/agent/approve",
        json={"action_id": action_id},
        headers=auth_headers,
    )
    assert resp.status_code == 404
    assert gate.approved is None
    approval_gate_module._pending_gates.pop(action_id, None)
