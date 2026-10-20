"""
Integration tests for POST /workspaces/code/{workspace_id}/execute

stream_code_execute is mocked in all tests so no Docker daemon is required.
The service-layer behavior is covered thoroughly in tests/unit/test_code_service.py.
These tests focus exclusively on the route layer:
- authentication / authorization
- request validation (422 for malformed input)
- SSE response contract
- correct handoff to the service
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_sse(body: str) -> list[dict]:
    events = []
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]"):
            events.append(json.loads(line[5:].strip()))
    return events


def _register(client, email="codeexec@test.com", password="securepass123"):
    resp = client.post("/auth/register", json={
        "email": email, "password": password, "full_name": "T",
    })
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _create_project(client, headers):
    resp = client.post("/projects", json={"name": "Test Project"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_workspace(client, headers, project_id, root_path=None):
    body = {"project_id": project_id}
    if root_path:
        body["root_path"] = root_path
    resp = client.post("/workspaces/code", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _noop_stream(*args, **kwargs):
    """Minimal mock: yields a done event so the SSE stream closes cleanly."""
    yield f'data: {json.dumps({"type": "tool_status", "status": "running", "message": "Executando comando"})}\n\n'
    yield f'data: {json.dumps({"type": "done", "success": True, "exit_code": 0, "tcu": 0.0, "cost_usd": 0.0, "duration_ms": 10})}\n\n'
    yield "data: [DONE]\n\n"


async def _error_stream(*args, **kwargs):
    """Mock stream that yields a sandbox-unavailable error."""
    yield f'data: {json.dumps({"type": "error", "message": "Sandbox indisponível."})}\n\n'


# ── Auth & authorization ──────────────────────────────────────────────────────

def test_execute_requires_auth(client):
    resp = client.post("/workspaces/code/any-id/execute", json={"command": "ls"})
    assert resp.status_code == 401


def test_execute_workspace_not_found(client, auth_headers):
    resp = client.post(
        "/workspaces/code/nonexistent-id/execute",
        json={"command": "ls"},
        headers=auth_headers,
    )
    assert resp.status_code == 404


def test_execute_workspace_wrong_user(client, auth_headers, tmp_path):
    """A workspace belonging to another user must return 404."""
    other_headers = _register(client, email="other_codeexec@test.com")
    proj = _create_project(client, other_headers)
    ws = _create_workspace(client, other_headers, proj["id"], str(tmp_path))

    with patch("src.api.routes.workspaces.stream_code_execute", new=_noop_stream), \
         patch("src.api.routes.workspaces.check_and_reserve", return_value="res-1"):
        resp = client.post(
            f"/workspaces/code/{ws['id']}/execute",
            json={"command": "ls"},
            headers=auth_headers,
        )
    assert resp.status_code == 404


# ── Request validation ────────────────────────────────────────────────────────

def test_execute_empty_command_returns_422(client, auth_headers, tmp_path):
    proj = _create_project(client, auth_headers)
    ws = _create_workspace(client, auth_headers, proj["id"], str(tmp_path))

    resp = client.post(
        f"/workspaces/code/{ws['id']}/execute",
        json={"command": ""},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_execute_timeout_zero_returns_422(client, auth_headers, tmp_path):
    proj = _create_project(client, auth_headers)
    ws = _create_workspace(client, auth_headers, proj["id"], str(tmp_path))

    resp = client.post(
        f"/workspaces/code/{ws['id']}/execute",
        json={"command": "ls", "timeout": 0},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_execute_timeout_above_max_returns_422(client, auth_headers, tmp_path):
    proj = _create_project(client, auth_headers)
    ws = _create_workspace(client, auth_headers, proj["id"], str(tmp_path))

    resp = client.post(
        f"/workspaces/code/{ws['id']}/execute",
        json={"command": "ls", "timeout": 121},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_execute_timeout_negative_returns_422(client, auth_headers, tmp_path):
    proj = _create_project(client, auth_headers)
    ws = _create_workspace(client, auth_headers, proj["id"], str(tmp_path))

    resp = client.post(
        f"/workspaces/code/{ws['id']}/execute",
        json={"command": "ls", "timeout": -1},
        headers=auth_headers,
    )
    assert resp.status_code == 422


def test_execute_missing_command_returns_422(client, auth_headers, tmp_path):
    proj = _create_project(client, auth_headers)
    ws = _create_workspace(client, auth_headers, proj["id"], str(tmp_path))

    resp = client.post(
        f"/workspaces/code/{ws['id']}/execute",
        json={"timeout": 30},
        headers=auth_headers,
    )
    assert resp.status_code == 422


# ── SSE contract ──────────────────────────────────────────────────────────────

def test_execute_returns_event_stream_content_type(client, auth_headers, tmp_path):
    proj = _create_project(client, auth_headers)
    ws = _create_workspace(client, auth_headers, proj["id"], str(tmp_path))

    with patch("src.api.routes.workspaces.stream_code_execute", new=_noop_stream), \
         patch("src.api.routes.workspaces.check_and_reserve", return_value="res-1"):
        resp = client.post(
            f"/workspaces/code/{ws['id']}/execute",
            json={"command": "echo hello", "timeout": 10},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers["content-type"]


def test_execute_done_event_in_response(client, auth_headers, tmp_path):
    proj = _create_project(client, auth_headers)
    ws = _create_workspace(client, auth_headers, proj["id"], str(tmp_path))

    with patch("src.api.routes.workspaces.stream_code_execute", new=_noop_stream), \
         patch("src.api.routes.workspaces.check_and_reserve", return_value="res-1"):
        resp = client.post(
            f"/workspaces/code/{ws['id']}/execute",
            json={"command": "echo hello"},
            headers=auth_headers,
        )

    events = _parse_sse(resp.text)
    done = next((e for e in events if e["type"] == "done"), None)
    assert done is not None
    assert done["success"] is True
    assert "tcu" in done
    assert "cost_usd" in done
    assert "duration_ms" in done


def test_execute_error_event_propagated(client, auth_headers, tmp_path):
    proj = _create_project(client, auth_headers)
    ws = _create_workspace(client, auth_headers, proj["id"], str(tmp_path))

    with patch("src.api.routes.workspaces.stream_code_execute", new=_error_stream), \
         patch("src.api.routes.workspaces.check_and_reserve", return_value="res-1"):
        resp = client.post(
            f"/workspaces/code/{ws['id']}/execute",
            json={"command": "ls"},
            headers=auth_headers,
        )

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    error_event = next((e for e in events if e["type"] == "error"), None)
    assert error_event is not None
    assert "message" in error_event


# ── Service handoff ───────────────────────────────────────────────────────────

def test_execute_calls_stream_code_execute(client, auth_headers, tmp_path):
    """Route must call stream_code_execute with the correct workspace and command."""
    proj = _create_project(client, auth_headers)
    ws = _create_workspace(client, auth_headers, proj["id"], str(tmp_path))

    captured = {}

    async def _capture_stream(db, workspace, command, timeout, **kwargs):
        captured["workspace_id"] = workspace.id
        captured["command"] = command
        captured["timeout"] = timeout
        yield f'data: {json.dumps({"type": "done", "success": True, "exit_code": 0, "tcu": 0.0, "cost_usd": 0.0, "duration_ms": 5})}\n\n'
        yield "data: [DONE]\n\n"

    with patch("src.api.routes.workspaces.stream_code_execute", new=_capture_stream), \
         patch("src.api.routes.workspaces.check_and_reserve", return_value="res-1"):
        client.post(
            f"/workspaces/code/{ws['id']}/execute",
            json={"command": "python3 --version", "timeout": 45},
            headers=auth_headers,
        )

    assert captured["workspace_id"] == ws["id"]
    assert captured["command"] == "python3 --version"
    assert captured["timeout"] == 45


def test_execute_check_and_reserve_called(client, auth_headers, tmp_path):
    """Entitlement reservation must happen before StreamingResponse is returned."""
    proj = _create_project(client, auth_headers)
    ws = _create_workspace(client, auth_headers, proj["id"], str(tmp_path))

    reserve_called = {}

    def _mock_reserve(db, user_id, session_id, plan, user_timezone=None):
        reserve_called["called"] = True
        reserve_called["session_id"] = session_id
        return "res-captured"

    with patch("src.api.routes.workspaces.stream_code_execute", new=_noop_stream), \
         patch("src.api.routes.workspaces.check_and_reserve", side_effect=_mock_reserve):
        client.post(
            f"/workspaces/code/{ws['id']}/execute",
            json={"command": "ls"},
            headers=auth_headers,
        )

    assert reserve_called.get("called") is True
    # session_id is None for workspace-level execution (documented PENDING G-5)
    assert reserve_called["session_id"] is None


# ── No host execution ─────────────────────────────────────────────────────────

def test_execute_no_subprocess_introduced(client, auth_headers, tmp_path):
    """The route must not introduce any direct subprocess execution."""
    proj = _create_project(client, auth_headers)
    ws = _create_workspace(client, auth_headers, proj["id"], str(tmp_path))

    with patch("src.api.routes.workspaces.stream_code_execute", new=_noop_stream), \
         patch("src.api.routes.workspaces.check_and_reserve", return_value="res-1"), \
         patch("subprocess.run") as mock_sub, \
         patch("os.system") as mock_os:
        client.post(
            f"/workspaces/code/{ws['id']}/execute",
            json={"command": "ls"},
            headers=auth_headers,
        )

    mock_sub.assert_not_called()
    mock_os.assert_not_called()
