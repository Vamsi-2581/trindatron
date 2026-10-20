"""
Integration tests for POST /workspaces/code/{workspace_id}/task
All _router.complete calls and DockerSandbox.run calls are mocked.
"""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from trinity_router import RoutingResult


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


def _setup_workspace(client, auth_headers, tmp_path: Path):
    """Create a project + code workspace backed by tmp_path. Returns workspace_id."""
    project = client.post(
        "/projects",
        json={"name": "Test Project"},
        headers=auth_headers,
    ).json()
    ws = client.post(
        "/workspaces/code",
        json={
            "project_id": project["id"],
            "root_path": str(tmp_path),
            "language_hint": "python",
        },
        headers=auth_headers,
    ).json()
    return ws["id"]


# ── Auth & validation ─────────────────────────────────────────────────────────

def test_task_requires_auth(client):
    resp = client.post("/workspaces/code/any-id/task", json={"task": "do something"})
    assert resp.status_code == 401


def test_task_workspace_not_found(client, auth_headers):
    resp = client.post(
        "/workspaces/code/nonexistent/task",
        json={"task": "do something"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


# ── Simple direct-answer task ─────────────────────────────────────────────────

def test_simple_task_returns_done(client, auth_headers, tmp_path):
    ws_id = _setup_workspace(client, auth_headers, tmp_path)
    direct = _routing_result("Done! Created hello.py.")

    with patch("src.services.code_service._router.complete", new=AsyncMock(return_value=direct)):
        resp = client.post(
            f"/workspaces/code/{ws_id}/task",
            json={"task": "Create a hello world Python file"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]
    events = _parse_sse(resp.text)
    assert any(e["type"] == "status" for e in events), "No status event emitted"
    done = next((e for e in events if e["type"] == "done"), None)
    assert done is not None, "No done event"
    assert done["answer"] == "Done! Created hello.py."
    assert done["iterations"] == 1


# ── SSE key + message fields validation ──────────────────────────────────────

def test_status_events_have_key_and_message(client, auth_headers, tmp_path):
    ws_id = _setup_workspace(client, auth_headers, tmp_path)
    direct = _routing_result("All done.")

    with patch("src.services.code_service._router.complete", new=AsyncMock(return_value=direct)):
        resp = client.post(
            f"/workspaces/code/{ws_id}/task",
            json={"task": "List the workspace"},
            headers=auth_headers,
        )

    events = _parse_sse(resp.text)
    status_events = [e for e in events if e["type"] == "status"]
    assert len(status_events) > 0
    for ev in status_events:
        assert "key" in ev, "Status event missing 'key' field"
        assert "message" in ev, "Status event missing 'message' field"
        assert isinstance(ev["key"], str) and ev["key"], "key must be non-empty string"


# ── Read file tool call ───────────────────────────────────────────────────────

def test_task_read_file_from_workspace(client, auth_headers, tmp_path):
    target = tmp_path / "README.md"
    target.write_text("# My project", encoding="utf-8")

    ws_id = _setup_workspace(client, auth_headers, tmp_path)

    tc = _make_tool_call("read_file", {"path": "README.md"})
    with_tool = _routing_result(tool_calls=[tc])
    final = _routing_result("I read the README.")

    responses = [with_tool, final]

    with patch("src.services.code_service._router.complete", new=AsyncMock(side_effect=responses)):
        resp = client.post(
            f"/workspaces/code/{ws_id}/task",
            json={"task": "What is in the README?"},
            headers=auth_headers,
        )

    events = _parse_sse(resp.text)
    done = next((e for e in events if e["type"] == "done"), None)
    assert done is not None
    assert done["answer"] == "I read the README."

    # The router got called twice (tool + final), confirm content was fed back
    status_events = [e for e in events if e["type"] == "status"]
    assert any("read" in e.get("key", "").lower() or "read" in e.get("message", "").lower()
               for e in status_events), "No read_file status event"


# ── Write file tool call ──────────────────────────────────────────────────────

def test_task_write_file_to_workspace(client, auth_headers, tmp_path):
    ws_id = _setup_workspace(client, auth_headers, tmp_path)

    tc = _make_tool_call("write_file", {"path": "output.txt", "content": "Hello from AI"})
    with_tool = _routing_result(tool_calls=[tc])
    final = _routing_result("Created output.txt.")

    responses = [with_tool, final]

    with patch("src.services.code_service._router.complete", new=AsyncMock(side_effect=responses)):
        resp = client.post(
            f"/workspaces/code/{ws_id}/task",
            json={"task": "Create output.txt with a greeting"},
            headers=auth_headers,
        )

    events = _parse_sse(resp.text)
    done = next((e for e in events if e["type"] == "done"), None)
    assert done is not None

    written = (tmp_path / "output.txt").read_text(encoding="utf-8")
    assert written == "Hello from AI"


# ── Path traversal blocked ────────────────────────────────────────────────────

def test_task_path_traversal_blocked(client, auth_headers, tmp_path):
    ws_id = _setup_workspace(client, auth_headers, tmp_path)

    # LLM "tries" to read /etc/passwd via path traversal
    tc = _make_tool_call("read_file", {"path": "../../etc/passwd"})
    with_tool = _routing_result(tool_calls=[tc])
    final = _routing_result("Path was blocked.")

    responses = [with_tool, final]
    complete_mock = AsyncMock(side_effect=responses)

    with patch("src.services.code_service._router.complete", new=complete_mock):
        resp = client.post(
            f"/workspaces/code/{ws_id}/task",
            json={"task": "Read /etc/passwd"},
            headers=auth_headers,
        )

    events = _parse_sse(resp.text)
    done = next((e for e in events if e["type"] == "done"), None)
    assert done is not None, "Should reach done even after blocked traversal"

    # Verify the tool result returned to LLM contained an error message
    # by checking the second call's messages — the tool message should have ERROR:
    second_call_args = complete_mock.call_args_list[1]
    messages = second_call_args[0][0].messages
    tool_messages = [m for m in messages if m["role"] == "tool"]
    assert any("ERROR" in m["content"] or "escapes" in m["content"].lower() or "Path" in m["content"]
               for m in tool_messages), "Traversal block must propagate ERROR to LLM"


# ── Execute shell tool call ───────────────────────────────────────────────────

def test_task_execute_shell_via_sandbox(client, auth_headers, tmp_path):
    ws_id = _setup_workspace(client, auth_headers, tmp_path)

    tc = _make_tool_call("execute_shell", {"command": "echo hello"})
    with_tool = _routing_result(tool_calls=[tc])
    final = _routing_result("The echo worked.")

    responses = [with_tool, final]

    sandbox_mock = MagicMock()
    sandbox_mock.is_available = AsyncMock(return_value=True)
    sandbox_mock.run = AsyncMock(return_value=(True, "hello\n"))

    with patch("src.services.code_service._router.complete", new=AsyncMock(side_effect=responses)), \
         patch("src.services.code_service.DockerSandbox", return_value=sandbox_mock):
        resp = client.post(
            f"/workspaces/code/{ws_id}/task",
            json={"task": "Run echo hello"},
            headers=auth_headers,
        )

    events = _parse_sse(resp.text)
    done = next((e for e in events if e["type"] == "done"), None)
    assert done is not None
    sandbox_mock.run.assert_called_once()


# ── Iteration limit reached ───────────────────────────────────────────────────

def test_task_iteration_limit_emits_limit_event(client, auth_headers, tmp_path):
    ws_id = _setup_workspace(client, auth_headers, tmp_path)

    # Always return a tool call → never terminates naturally → triggers limit
    def always_tool():
        tc = _make_tool_call("list_directory", {"path": "."})
        return _routing_result(tool_calls=[tc])

    n_iterations = 3  # request only 3 iterations

    complete_mock = AsyncMock(side_effect=[always_tool() for _ in range(n_iterations)])

    with patch("src.services.code_service._router.complete", new=complete_mock):
        resp = client.post(
            f"/workspaces/code/{ws_id}/task",
            json={"task": "Keep listing forever", "max_iterations": n_iterations},
            headers=auth_headers,
        )

    events = _parse_sse(resp.text)
    limit_events = [
        e for e in events
        if e["type"] == "status" and "limit" in e.get("key", "").lower()
    ]
    assert len(limit_events) >= 1, "Must emit limit_reached status event"
    assert complete_mock.call_count == n_iterations
