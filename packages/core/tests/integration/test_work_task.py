"""
Integration tests for POST /workspaces/work/{workspace_id}/execute
and the approval gate (approve/reject) endpoints.

All _router.complete and _run_work_tool calls are mocked.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from trinity_router import RoutingResult

import src.services.approval_gate as approval_gate_module
import src.services.work_service as ws_module


@pytest.fixture(autouse=True)
def _work_is_a_pro_feature(db, registered_user):
    """TRINITY WORK is included from PRO (PlanEntitlement.supports_trinity_work)
    and the workspace route enforces it, so the account these tests drive is
    on PRO — tests/integration/test_plan_feature_gates.py covers the refusal."""
    from src.models.user import User
    db.query(User).filter(User.id == registered_user["user_id"]).one().plan = "pro"
    db.commit()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _routing_result(
    content: str | None = None,
    tool_calls=None,
    cost_usd: float = 0.001,
) -> RoutingResult:
    return RoutingResult(
        content=content,
        tool_calls=tool_calls,
        provider="anthropic",
        model="claude-sonnet-test",
        input_tokens=10,
        output_tokens=5,
        cached_tokens=0,
        cost_usd=cost_usd,
        tcu=cost_usd * 100.0,
        failover_used=False,
        failover_reason=None,
    )


def _make_tool_call(tool_name: str, args: dict, call_id: str = "call_1"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=tool_name, arguments=json.dumps(args)),
    )


def _parse_sse(body: str) -> list[dict]:
    events = []
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("data:") and line[5:].strip() != "[DONE]":
            events.append(json.loads(line[5:].strip()))
    return events


def _setup_work_workspace(client, auth_headers):
    """Create a project + WORK workspace. Returns workspace_id."""
    project = client.post(
        "/projects",
        json={"name": "Work Test Project"},
        headers=auth_headers,
    ).json()
    ws = client.post(
        "/workspaces/work",
        json={"project_id": project["id"]},
        headers=auth_headers,
    ).json()
    return ws["id"]


# ── Auth & validation ─────────────────────────────────────────────────────────

def test_work_task_requires_auth(client):
    resp = client.post("/workspaces/work/any-id/execute", json={"task": "do something"})
    assert resp.status_code == 401


def test_work_task_workspace_not_found(client, auth_headers):
    resp = client.post(
        "/workspaces/work/nonexistent/execute",
        json={"task": "do something"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


def test_approve_requires_auth(client):
    resp = client.post("/workspaces/work/any-id/approve", json={"action_id": "x"})
    assert resp.status_code == 401


def test_reject_requires_auth(client):
    resp = client.post("/workspaces/work/any-id/reject", json={"action_id": "x"})
    assert resp.status_code == 401


# ── Simple direct-answer task ─────────────────────────────────────────────────

def test_simple_work_task_returns_done(client, auth_headers):
    ws_id = _setup_work_workspace(client, auth_headers)
    direct = _routing_result("Research complete: the answer is 42.")

    with patch("src.services.work_service._router.complete", new=AsyncMock(return_value=direct)):
        resp = client.post(
            f"/workspaces/work/{ws_id}/execute",
            json={"task": "Research the answer to everything"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    events = _parse_sse(resp.text)
    done = next((e for e in events if e["type"] == "done"), None)
    assert done is not None, f"No done event. Events: {events}"
    assert done["answer"] == "Research complete: the answer is 42."
    assert done["iterations"] == 1


# ── SSE key + message fields ──────────────────────────────────────────────────

def test_status_events_have_key_and_message(client, auth_headers):
    ws_id = _setup_work_workspace(client, auth_headers)
    direct = _routing_result("Done.")

    with patch("src.services.work_service._router.complete", new=AsyncMock(return_value=direct)):
        resp = client.post(
            f"/workspaces/work/{ws_id}/execute",
            json={"task": "Simple task"},
            headers=auth_headers,
        )

    events = _parse_sse(resp.text)
    status_events = [e for e in events if e["type"] == "status"]
    assert len(status_events) > 0
    for ev in status_events:
        assert "key" in ev and isinstance(ev["key"], str) and ev["key"]
        assert "message" in ev and isinstance(ev["message"], str)


# ── Web search tool call ──────────────────────────────────────────────────────

def test_work_task_web_search(client, auth_headers):
    ws_id = _setup_work_workspace(client, auth_headers)

    tc = _make_tool_call("web_search", {"query": "latest AI news"})
    with_tool = _routing_result(tool_calls=[tc])
    final = _routing_result("Found relevant results.")

    with patch("src.services.work_service._router.complete", new=AsyncMock(side_effect=[with_tool, final])), \
         patch("src.services.work_service._run_work_tool", new=AsyncMock(return_value=(True, "AI news summary"))):
        resp = client.post(
            f"/workspaces/work/{ws_id}/execute",
            json={"task": "What is the latest AI news?"},
            headers=auth_headers,
        )

    events = _parse_sse(resp.text)
    done = next((e for e in events if e["type"] == "done"), None)
    assert done is not None
    assert done["answer"] == "Found relevant results."


# ── Approval gate triggered ───────────────────────────────────────────────────

def test_approval_gate_emits_approval_required_and_approves(client, auth_headers):
    """Mocks _wait_for_gate to return True (approved) immediately, then verifies
    that approval_required was emitted and the task completed."""
    ws_id = _setup_work_workspace(client, auth_headers)

    tc = _make_tool_call("send_notification", {
        "destination": "team@example.com",
        "message": "Task done!",
    })
    with_tool = _routing_result(tool_calls=[tc])
    final = _routing_result("Notification sent.")

    with patch("src.services.work_service._router.complete", new=AsyncMock(side_effect=[with_tool, final])), \
         patch("src.services.work_service._wait_for_gate", new=AsyncMock(return_value=True)):
        resp = client.post(
            f"/workspaces/work/{ws_id}/execute",
            json={"task": "Send a notification to the team"},
            headers=auth_headers,
        )

    events = _parse_sse(resp.text)
    approval_events = [e for e in events if e["type"] == "approval_required"]
    assert len(approval_events) == 1, f"Expected approval_required, got: {events}"
    gate_ev = approval_events[0]
    assert "action_id" in gate_ev
    assert gate_ev["action"] == "send_notification"
    assert "description" in gate_ev

    done = next((e for e in events if e["type"] == "done"), None)
    assert done is not None, "Task should complete after approval"
    assert done["answer"] == "Notification sent."


# ── Approval gate rejected ────────────────────────────────────────────────────

def test_approval_gate_rejected_emits_error(client, auth_headers):
    """Mocks _wait_for_gate to return False (rejected) — task should emit error."""
    ws_id = _setup_work_workspace(client, auth_headers)

    tc = _make_tool_call("send_notification", {
        "destination": "boss@example.com",
        "message": "Don't send this!",
    })
    with_tool = _routing_result(tool_calls=[tc])

    with patch("src.services.work_service._router.complete", new=AsyncMock(return_value=with_tool)), \
         patch("src.services.work_service._wait_for_gate", new=AsyncMock(return_value=False)):
        resp = client.post(
            f"/workspaces/work/{ws_id}/execute",
            json={"task": "Send an unauthorized notification"},
            headers=auth_headers,
        )

    events = _parse_sse(resp.text)
    error_events = [e for e in events if e["type"] == "error"]
    assert len(error_events) >= 1, f"Expected error event, got: {events}"
    assert "reject" in error_events[0].get("message", "").lower() or \
           "aprovação" in error_events[0].get("message", "").lower() or \
           "aprovação" in error_events[0].get("message", "").lower()


# ── Approve endpoint ──────────────────────────────────────────────────────────

def test_approve_not_found_returns_404(client, auth_headers):
    ws_id = _setup_work_workspace(client, auth_headers)
    resp = client.post(
        f"/workspaces/work/{ws_id}/approve",
        json={"action_id": "nonexistent-gate-id"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


def test_reject_not_found_returns_404(client, auth_headers):
    ws_id = _setup_work_workspace(client, auth_headers)
    resp = client.post(
        f"/workspaces/work/{ws_id}/reject",
        json={"action_id": "nonexistent-gate-id"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


def test_resolve_gate_sets_approval(client, auth_headers):
    """Test that resolve_gate correctly sets the gate result.

    Security etapa: the gate itself now lives in the shared
    src.services.approval_gate module (owner_id, not workspace_id) —
    work_service.resolve_gate(..., workspace_id=...) remains a thin,
    unchanged wrapper over it (see that module's own docstring)."""
    import uuid
    action_id = str(uuid.uuid4())

    ws_id = _setup_work_workspace(client, auth_headers)
    gate = approval_gate_module.create_gate(action_id, owner_id=ws_id)

    resp = client.post(
        f"/workspaces/work/{ws_id}/approve",
        json={"action_id": action_id},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert gate.approved is True
    assert gate.event.is_set()

    # Clean up
    approval_gate_module._pending_gates.pop(action_id, None)


# ── Iteration limit ───────────────────────────────────────────────────────────

def test_iteration_limit_stops_loop(client, auth_headers):
    ws_id = _setup_work_workspace(client, auth_headers)

    n_iterations = 3

    def always_tool():
        tc = _make_tool_call("web_search", {"query": "loop forever"})
        return _routing_result(tool_calls=[tc])

    with patch("src.services.work_service._router.complete",
               new=AsyncMock(side_effect=[always_tool() for _ in range(n_iterations)])), \
         patch("src.services.work_service._run_work_tool",
               new=AsyncMock(return_value=(True, "result"))):
        resp = client.post(
            f"/workspaces/work/{ws_id}/execute",
            json={"task": "Keep searching forever", "max_iterations": n_iterations},
            headers=auth_headers,
        )

    events = _parse_sse(resp.text)
    # Should reach done (with empty final_answer after iteration limit)
    done = next((e for e in events if e["type"] == "done"), None)
    assert done is not None, f"Expected done event. Got: {events}"
