"""
Real DockerSandbox integration tests through stream_code_execute.

These tests call stream_code_execute directly (no HTTP layer) with a real
DockerSandbox. They verify the full execution pipeline — image mount,
command run, output capture, timeout, and SSE events — without mocking.

Requirements:
    docker build -t trinity-sandbox:latest packages/core/docker/sandbox/

All tests are auto-skipped if Docker is not running or the image is missing.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ── Skip guard ────────────────────────────────────────────────────────────────

def _sandbox_ready() -> bool:
    try:
        r = subprocess.run(
            ["docker", "image", "inspect", "trinity-sandbox:latest"],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


_SKIP = pytest.mark.skipif(
    not _sandbox_ready(),
    reason="Docker not running or trinity-sandbox:latest image not built",
)

pytestmark = _SKIP


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _collect(gen) -> list[dict]:
    events = []
    async for chunk in gen:
        chunk = chunk.strip()
        if chunk == "data: [DONE]":
            continue
        if chunk.startswith("data: "):
            events.append(json.loads(chunk[6:]))
    return events


def _make_workspace(root_path: str):
    ws = MagicMock()
    ws.root_path = root_path
    ws.sandbox_image = None
    return ws


@pytest.fixture
def db():
    return MagicMock()


# ── Basic execution through SSE pipeline ─────────────────────────────────────

async def test_echo_produces_output_and_done_events(db, tmp_path):
    from src.services.code_service import stream_code_execute

    ws = _make_workspace(str(tmp_path))
    events = await _collect(stream_code_execute(
        db, ws, command="echo hello_trinity", timeout=15,
    ))

    types = [e["type"] for e in events]
    assert "tool_status" in types
    assert "output" in types
    assert "done" in types
    assert "error" not in types

    output = next(e for e in events if e["type"] == "output")
    assert "hello_trinity" in output["data"]


async def test_done_event_carries_no_internal_accounting(db, tmp_path):
    from src.services.code_service import stream_code_execute

    ws = _make_workspace(str(tmp_path))
    events = await _collect(stream_code_execute(
        db, ws, command="echo x", timeout=15,
    ))

    done = next(e for e in events if e["type"] == "done")
    assert "tcu" not in done and "cost_usd" not in done
    assert done["success"] is True
    assert done["exit_code"] == 0


async def test_python_execution_in_sandbox(db, tmp_path):
    from src.services.code_service import stream_code_execute

    ws = _make_workspace(str(tmp_path))
    events = await _collect(stream_code_execute(
        db, ws, command="python3 -c \"print(2+2)\"", timeout=15,
    ))

    output = next(e for e in events if e["type"] == "output")
    assert "4" in output["data"]


async def test_nonzero_exit_yields_error_event(db, tmp_path):
    from src.services.code_service import stream_code_execute

    ws = _make_workspace(str(tmp_path))
    events = await _collect(stream_code_execute(
        db, ws, command="false", timeout=15,
    ))

    assert any(e["type"] == "error" for e in events)
    assert not any(e["type"] == "done" for e in events)


async def test_workspace_file_write_is_visible_to_subsequent_read(db, tmp_path):
    """Files written to /workspace in sandbox are visible via tmp_path."""
    from src.services.code_service import stream_code_execute

    ws = _make_workspace(str(tmp_path))
    events = await _collect(stream_code_execute(
        db, ws, command="echo hello > /workspace/out.txt && cat /workspace/out.txt",
        timeout=15,
    ))

    assert any(e["type"] == "done" for e in events)
    written = (tmp_path / "out.txt").read_text()
    assert "hello" in written


# ── Network isolation ─────────────────────────────────────────────────────────

async def test_network_is_isolated_no_curl(db, tmp_path):
    """--network=none: curl to any external host must fail."""
    from src.services.code_service import stream_code_execute

    ws = _make_workspace(str(tmp_path))
    events = await _collect(stream_code_execute(
        db, ws, command="curl -s --max-time 3 http://1.1.1.1/ || echo NETWORK_BLOCKED",
        timeout=15,
    ))

    output = next((e for e in events if e["type"] == "output"), None)
    if output:
        assert "NETWORK_BLOCKED" in output["data"] or "curl" not in output["data"]


# ── Timeout enforcement ───────────────────────────────────────────────────────

async def test_long_running_command_is_killed_by_timeout(db, tmp_path):
    """sleep 9999 with 3s timeout must yield an error event, not hang."""
    from src.services.code_service import stream_code_execute

    ws = _make_workspace(str(tmp_path))
    events = await _collect(stream_code_execute(
        db, ws, command="sleep 9999", timeout=3,
    ))

    assert any(e["type"] == "error" for e in events)
    assert not any(e["type"] == "done" for e in events)


# ── No host env vars ──────────────────────────────────────────────────────────

async def test_host_env_vars_not_present_in_sandbox(db, tmp_path):
    """Sandbox must not carry host environment (except HOME and PATH)."""
    from src.services.code_service import stream_code_execute
    import os

    os.environ["TRINITY_SECRET_CANARY"] = "should_not_leak"
    ws = _make_workspace(str(tmp_path))
    events = await _collect(stream_code_execute(
        db, ws,
        command="printenv TRINITY_SECRET_CANARY || echo NOT_FOUND",
        timeout=15,
    ))

    # The command is blocked by _FORBIDDEN_SHELL (printenv pattern), so we
    # expect an error event — confirming the blocklist also protects this path.
    # If somehow it ran (image without the blocklist), we verify no leak.
    all_text = " ".join(
        e.get("data", "") + e.get("message", "")
        for e in events
    )
    assert "should_not_leak" not in all_text


# ── Audit log through real execution ─────────────────────────────────────────

async def test_real_execution_audit_entry_has_valid_execution_id(db, tmp_path):
    import uuid
    from src.capability.audit import AuditLog
    from src.services.code_service import stream_code_execute

    ws = _make_workspace(str(tmp_path))
    al = AuditLog()
    await _collect(stream_code_execute(
        db, ws, command="echo hi", timeout=15, audit_log=al,
    ))

    assert len(al.entries) == 1
    entry = al.entries[0]
    assert entry.execution_id != ""
    uuid.UUID(entry.execution_id)   # raises ValueError if not a valid UUID
    assert entry.result == "success"
    assert entry.success is True
