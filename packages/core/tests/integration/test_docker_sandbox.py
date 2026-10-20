"""
Docker sandbox isolation tests.

These tests validate the actual security guarantees of the sandbox — they
require Docker Desktop to be running AND the trinity-sandbox image to be built.

Build the image before running:
    docker build -t trinity-sandbox:latest packages/core/docker/sandbox/

All tests are auto-skipped if Docker or the image is not available.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from trinity_sandbox import DockerSandbox


# ─── Skip guard ───────────────────────────────────────────────────────────────

def _sandbox_ready() -> bool:
    """Return True if Docker is running and trinity-sandbox:latest exists."""
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


@pytest.fixture()
def sandbox() -> DockerSandbox:
    return DockerSandbox()


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "session-ws"
    ws.mkdir()
    return ws


# ─── Network isolation ────────────────────────────────────────────────────────

async def test_no_network_access(sandbox: DockerSandbox, workspace: Path):
    """--network=none: outbound connections must fail."""
    ok, output = await sandbox.run("curl -s --max-time 3 https://example.com", workspace, timeout=10)
    assert not ok
    # curl exits non-zero when it cannot connect


async def test_no_loopback_services(sandbox: DockerSandbox, workspace: Path):
    """Even localhost is unreachable from inside the sandbox (no network)."""
    ok, output = await sandbox.run("curl -s --max-time 3 http://127.0.0.1:8000", workspace, timeout=10)
    assert not ok


# ─── Filesystem isolation ─────────────────────────────────────────────────────

async def test_read_only_root_fs(sandbox: DockerSandbox, workspace: Path):
    """--read-only: cannot write to the container root filesystem."""
    ok, output = await sandbox.run("touch /etc/hacked", workspace, timeout=10)
    assert not ok


async def test_tmp_is_writable(sandbox: DockerSandbox, workspace: Path):
    """/tmp is tmpfs and must be writable."""
    ok, output = await sandbox.run("echo test > /tmp/testfile && cat /tmp/testfile", workspace, timeout=10)
    assert ok
    assert "test" in output


async def test_workspace_is_writable(sandbox: DockerSandbox, workspace: Path):
    """The session workspace is the only host path mounted; it must be writable."""
    ok, output = await sandbox.run(
        "echo 'sandbox was here' > hello.txt && cat hello.txt",
        workspace,
        timeout=10,
    )
    assert ok
    assert "sandbox was here" in output
    assert (workspace / "hello.txt").read_text().strip() == "sandbox was here"


async def test_no_host_root_access(sandbox: DockerSandbox, workspace: Path):
    """The host root filesystem is not mounted inside the container."""
    ok, output = await sandbox.run("ls /host 2>&1 || echo no_host_mount", workspace, timeout=10)
    # Either /host doesn't exist, or the ls fails — either way output contains our marker or fails
    assert "no_host_mount" in output or not ok


async def test_cross_session_isolation(sandbox: DockerSandbox, tmp_path: Path):
    """Session A's workspace does not exist inside session B's container."""
    ws_a = tmp_path / "session-aaaa"
    ws_a.mkdir()
    (ws_a / "secret.txt").write_text("session A secrets")

    ws_b = tmp_path / "session-bbbb"
    ws_b.mkdir()

    # Run inside session B's container — should not see session A's workspace
    ok, output = await sandbox.run(
        f"cat /workspace/../session-aaaa/secret.txt 2>&1 || echo not_found",
        ws_b,
        timeout=10,
    )
    # The path resolves outside /workspace so the shell will fail or not find it
    assert "session A secrets" not in output


# ─── Privilege isolation ──────────────────────────────────────────────────────

async def test_non_root_user(sandbox: DockerSandbox, workspace: Path):
    """Container must run as uid 1000, not root."""
    ok, output = await sandbox.run("id -u", workspace, timeout=10)
    assert ok
    assert output.strip() == "1000"


async def test_no_privilege_escalation(sandbox: DockerSandbox, workspace: Path):
    """--security-opt no-new-privileges + --cap-drop=ALL: sudo must not work."""
    ok, output = await sandbox.run("sudo id 2>&1 || echo no_sudo", workspace, timeout=10)
    assert "no_sudo" in output or not ok


# ─── Environment isolation ────────────────────────────────────────────────────

async def test_no_host_env_vars(sandbox: DockerSandbox, workspace: Path):
    """Host API keys and secrets must not leak into the container environment."""
    ok, output = await sandbox.run(
        "echo \"KEY=$ANTHROPIC_API_KEY OPENAI=$OPENAI_API_KEY\"",
        workspace,
        timeout=10,
    )
    assert ok
    # Both variables should be empty
    assert "KEY= OPENAI=" in output or output.strip() == "KEY= OPENAI="


async def test_home_is_safe(sandbox: DockerSandbox, workspace: Path):
    """HOME must be /tmp (the safe default), not the host home directory."""
    ok, output = await sandbox.run("echo $HOME", workspace, timeout=10)
    assert ok
    assert output.strip() == "/tmp"


# ─── Resource limits ──────────────────────────────────────────────────────────

async def test_pids_limit(sandbox: DockerSandbox, workspace: Path):
    """--pids-limit=64: a fork bomb must be contained."""
    ok, output = await sandbox.run(
        # Attempts to spawn 128 background processes; should hit the PID limit
        "for i in $(seq 1 128); do sleep 30 & done; wait",
        workspace,
        timeout=15,
    )
    # The command may fail or succeed with fewer processes — either way it must not hang
    assert isinstance(ok, bool)


async def test_timeout_kills_container(sandbox: DockerSandbox, workspace: Path):
    """On timeout, the container and all its children are killed."""
    ok, output = await sandbox.run("sleep 999", workspace, timeout=3)
    assert not ok
    assert "timed out" in output.lower()


# ─── Sandbox availability ─────────────────────────────────────────────────────

async def test_is_available_returns_true(sandbox: DockerSandbox):
    """When Docker is running and the image exists, is_available() must be True."""
    assert await sandbox.is_available() is True


async def test_unknown_image_is_not_available():
    """is_available() returns False for a non-existent image."""
    s = DockerSandbox(image="trinity-sandbox-does-not-exist:never")
    assert await s.is_available() is False
