"""
Docker desktop sandbox isolation tests — Phase 5.

Validates the security guarantees of the virtual display sandbox:
  - Screenshot captures only the virtual display, not the host
  - No network access from inside the container
  - No host environment variables in the container
  - Read-only root filesystem (except /tmp and /workspace)
  - Cross-session workspace isolation
  - No host filesystem access beyond /workspace
  - PIDs limit contains fork bombs
  - Container is destroyed on stop()
  - Exec timeout kills the exec but leaves container alive
  - Xvfb is actually running and accessible on DISPLAY=:99

Build the image before running:
    docker build -t trinity-desktop-sandbox:latest
                 packages/core/docker/desktop-sandbox/

All tests are auto-skipped if Docker or the image is not available.
"""
from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path

import pytest

from trinity_sandbox import DesktopSandbox


# ─── Skip guard ───────────────────────────────────────────────────────────────

def _desktop_sandbox_ready() -> bool:
    """Return True if Docker is running and trinity-desktop-sandbox:latest exists."""
    try:
        r = subprocess.run(
            ["docker", "image", "inspect", "trinity-desktop-sandbox:latest"],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


_SKIP = pytest.mark.skipif(
    not _desktop_sandbox_ready(),
    reason="Docker not running or trinity-desktop-sandbox:latest image not built",
)

pytestmark = _SKIP


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
async def session_sandbox(tmp_path: Path):
    """Start a DesktopSandbox for a test session and stop it afterwards."""
    session_id = uuid.uuid4().hex[:12]
    workspace = tmp_path / f"session-{session_id}"
    workspace.mkdir()
    sandbox = DesktopSandbox()
    started = await sandbox.start(session_id, workspace)
    assert started, "Failed to start desktop sandbox — check image and Docker daemon"
    yield sandbox, workspace
    await sandbox.stop()


# ─── ST-06: Screenshot from virtual display, not host ─────────────────────────

async def test_screenshot_dimensions_match_xvfb_resolution(session_sandbox, tmp_path: Path):
    """ST-06: screenshot must be 1280x800 (Xvfb config), not host screen resolution."""
    sandbox, workspace = session_sandbox
    ok, filename = await sandbox.screenshot("test_screenshot.png")
    assert ok, f"Screenshot failed: {filename}"
    # Verify the file exists and has Xvfb dimensions using Python Pillow inside the container
    ok, output = await sandbox.exec(
        "python3 -c \""
        "from PIL import Image; "
        "img = Image.open('/workspace/test_screenshot.png'); "
        "print(img.size)\""
    )
    assert ok
    assert "1280" in output and "800" in output, (
        f"Expected 1280x800 (Xvfb resolution), got: {output}. "
        "This may indicate the screenshot captured the host display."
    )


async def test_screenshot_file_saved_in_workspace(session_sandbox):
    """ST-06: screenshot must be saved inside the workspace, not elsewhere."""
    sandbox, workspace = session_sandbox
    ok, filename = await sandbox.screenshot("check.png")
    assert ok
    assert (workspace / "check.png").exists(), "Screenshot file not found in workspace"


# ─── ST-07: No network access ─────────────────────────────────────────────────

async def test_desktop_container_no_outbound_network(session_sandbox):
    """ST-07: --network=none must prevent any outbound connection."""
    sandbox, _ = session_sandbox
    ok, output = await sandbox.exec(
        "python3 -c \""
        "import socket; "
        "s=socket.socket(); "
        "s.settimeout(3); "
        "s.connect(('8.8.8.8', 53))\""
        " 2>&1 || echo NO_NETWORK",
        timeout=10,
    )
    assert "NO_NETWORK" in output or not ok


async def test_desktop_container_no_loopback_to_host(session_sandbox):
    """ST-07: container loopback cannot reach the host Trinity API."""
    sandbox, _ = session_sandbox
    ok, output = await sandbox.exec(
        "python3 -c \""
        "import socket; "
        "s=socket.socket(); "
        "s.settimeout(3); "
        "s.connect(('127.0.0.1', 8000))\""
        " 2>&1 || echo NO_HOST",
        timeout=10,
    )
    assert "NO_HOST" in output or not ok


# ─── ST-08: No host environment variables ─────────────────────────────────────

async def test_desktop_container_no_api_keys_in_env(session_sandbox):
    """ST-08: ANTHROPIC_API_KEY and OPENAI_API_KEY must not be present."""
    sandbox, _ = session_sandbox
    ok, output = await sandbox.exec(
        "echo \"ANTHROPIC=$ANTHROPIC_API_KEY|OPENAI=$OPENAI_API_KEY\"",
        timeout=10,
    )
    assert ok
    assert "ANTHROPIC=|OPENAI=" in output, (
        f"Host API keys leaked into container env: {output}"
    )


async def test_desktop_container_env_is_minimal(session_sandbox):
    """ST-08: container env should only have our declared variables."""
    sandbox, _ = session_sandbox
    ok, output = await sandbox.exec("env", timeout=10)
    assert ok
    # Must have our display and safe home
    assert "DISPLAY=:99" in output
    assert "HOME=/tmp" in output
    # Must NOT have any secret-looking variables
    for secret_pattern in ("API_KEY", "SECRET", "TOKEN", "PASSWORD"):
        assert secret_pattern not in output.upper(), (
            f"Potential secret variable containing '{secret_pattern}' found in container env"
        )


# ─── ST-09: Read-only root filesystem ─────────────────────────────────────────

async def test_desktop_container_root_fs_readonly(session_sandbox):
    """ST-09: --read-only must prevent writes to the container root FS."""
    sandbox, _ = session_sandbox
    ok, output = await sandbox.exec(
        "touch /etc/hacked 2>&1 || echo READONLY",
        timeout=10,
    )
    assert "READONLY" in output or not ok


async def test_desktop_container_tmp_is_writable(session_sandbox):
    """/tmp must be writable (tmpfs) for Xvfb socket and runtime data."""
    sandbox, _ = session_sandbox
    ok, output = await sandbox.exec(
        "echo test > /tmp/write_test.txt && cat /tmp/write_test.txt",
        timeout=10,
    )
    assert ok
    assert "test" in output


async def test_desktop_container_workspace_is_writable(session_sandbox):
    """The session workspace must be writable for saving screenshots."""
    sandbox, workspace = session_sandbox
    ok, output = await sandbox.exec(
        "echo 'sandbox was here' > /workspace/write_test.txt && cat /workspace/write_test.txt",
        timeout=10,
    )
    assert ok
    assert "sandbox was here" in output
    assert (workspace / "write_test.txt").read_text().strip() == "sandbox was here"


# ─── ST-10: Cross-session workspace isolation ─────────────────────────────────

async def test_desktop_container_cannot_access_other_session_workspace(tmp_path: Path):
    """ST-10: a container for session B must not see session A's workspace."""
    ws_a = tmp_path / "session-aaaa"
    ws_a.mkdir()
    (ws_a / "secret_a.txt").write_text("session A secrets")

    ws_b = tmp_path / "session-bbbb"
    ws_b.mkdir()

    sandbox_b = DesktopSandbox()
    started = await sandbox_b.start("bbbb", ws_b)
    assert started
    try:
        ok, output = await sandbox_b.exec(
            "cat /workspace/../session-aaaa/secret_a.txt 2>&1 || echo NOT_FOUND",
            timeout=10,
        )
        assert "session A secrets" not in output
    finally:
        await sandbox_b.stop()


# ─── ST-11: No host filesystem access beyond workspace ────────────────────────

async def test_desktop_container_no_host_filesystem(session_sandbox):
    """ST-11: Windows host filesystem must not be accessible from the container.

    In WSL2, the Windows C: drive is typically mounted at /mnt/c.
    Docker containers using the WSL2 backend must NOT inherit these host mounts.
    """
    sandbox, _ = session_sandbox
    # /mnt/c is the WSL2 mount point for the Windows C: drive
    for path in ["/mnt/c", "/mnt/wsl", "/host", "/c", "/windows"]:
        ok, output = await sandbox.exec(
            f"ls {path} 2>&1 || echo NO_ACCESS",
            timeout=5,
        )
        assert "NO_ACCESS" in output or not ok, (
            f"Host path {path} appears accessible from inside the container: {output!r}"
        )


async def test_desktop_container_cannot_escape_workspace_to_host_via_symlink(session_sandbox):
    """ST-11: symlinks inside workspace cannot reach Windows host paths.

    /etc/passwd inside the container is the container's own file (expected).
    The security boundary is that Windows host paths (/mnt/c) are unreachable.
    """
    sandbox, workspace = session_sandbox
    # Try to symlink to the Windows C: drive mount (WSL2 host path)
    ok, output = await sandbox.exec(
        "ln -s /mnt/c /workspace/evil_link 2>/dev/null; "
        "stat --dereference /workspace/evil_link 2>&1 || echo NO_HOST_ACCESS",
        timeout=10,
    )
    # Either the symlink target doesn't exist or /mnt/c is inaccessible in the container
    assert "NO_HOST_ACCESS" in output or not ok, (
        f"Symlink to Windows host path /mnt/c succeeded: {output!r}"
    )


# ─── ST-12: PIDs limit ────────────────────────────────────────────────────────

_PIDS_CGROUP = "/sys/fs/cgroup"  # cgroup v2 (Docker Desktop and current Linux hosts)


async def test_desktop_container_pids_limit_contains_fork_bomb(session_sandbox):
    """ST-12: --pids-limit=128 must contain runaway process spawning.

    Checked from the kernel's own pids controller, not from timing: the limit is 128, a burst of
    256 processes never takes the container past it, the kernel refuses the excess, and the
    container answers again once the burst is over. (This used to spawn `sleep 60` and expect an
    `echo` within 10 s while all 128 slots were still held — whether a slot was free by then was
    luck, and it failed intermittently on the self-hosted runner, 1 run in 5.)
    """
    sandbox, _ = session_sandbox
    ok, limit = await sandbox.exec(f"cat {_PIDS_CGROUP}/pids.max", timeout=10)
    assert ok and limit.strip() == "128", limit

    # Short-lived and detached from the exec's output, so the burst itself ends; its exit status is
    # irrelevant (bash reports the forks the kernel refused).
    await sandbox.exec("for i in $(seq 1 256); do sleep 5 >/dev/null 2>&1 & done; echo DONE", timeout=60)

    ok, events = await sandbox.exec(f"cat {_PIDS_CGROUP}/pids.events", timeout=30)
    assert ok, events
    refused = int(events.split("max", 1)[1].split()[0])
    assert refused > 0, f"the limit never refused a fork: {events!r}"
    ok, peak = await sandbox.exec(f"cat {_PIDS_CGROUP}/pids.peak 2>/dev/null || echo none", timeout=30)
    if ok and peak.strip() != "none":  # pids.peak needs Linux >= 6.1
        assert int(peak.strip()) <= 128, peak

    # The container must stay usable: responsive again once the burst's processes are gone.
    for _ in range(30):
        ok2, output2 = await sandbox.exec("echo STILL_ALIVE", timeout=5)
        if ok2 and "STILL_ALIVE" in output2:
            break
        await asyncio.sleep(1)
    assert ok2 and "STILL_ALIVE" in output2, output2


# ─── ST-16: Container destroyed on stop() ─────────────────────────────────────

async def test_desktop_container_destroyed_on_stop(tmp_path: Path):
    """ST-16: after stop(), the Docker container must no longer exist."""
    ws = tmp_path / "session-stop-test"
    ws.mkdir()
    sandbox = DesktopSandbox()
    session_id = "stoptest" + uuid.uuid4().hex[:8]
    started = await sandbox.start(session_id, ws)
    assert started

    container_name = f"trinity-desktop-{session_id}"

    # Verify it exists while running
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
        capture_output=True, text=True,
    )
    assert result.stdout.strip() == "true"

    await sandbox.stop()

    # Verify it no longer exists
    result = subprocess.run(
        ["docker", "inspect", container_name],
        capture_output=True,
    )
    assert result.returncode != 0, "Container still exists after stop()"


# ─── ST-17: Exec timeout kills exec, not container ────────────────────────────

async def test_desktop_exec_timeout_leaves_container_running(session_sandbox):
    """ST-17: a timed-out exec must not destroy the container."""
    sandbox, _ = session_sandbox
    ok, output = await sandbox.exec("sleep 999", timeout=2)
    assert not ok
    assert "timed out" in output.lower() or "timeout" in output.lower()

    # Container must still be alive and accepting execs
    assert await sandbox.is_running()
    ok2, output2 = await sandbox.exec("echo CONTAINER_ALIVE", timeout=10)
    assert ok2
    assert "CONTAINER_ALIVE" in output2


# ─── ST-18: Workspace scripts not auto-executed on next session ───────────────

async def test_workspace_malicious_script_not_auto_executed(tmp_path: Path):
    """ST-18: a script written by session A must not auto-run when session B starts."""
    ws = tmp_path / "session-shared-ws"
    ws.mkdir()
    marker = ws / "evil_ran.txt"

    # Session A: write a "malicious" script
    evil_script = ws / "run_on_start.sh"
    evil_script.write_text(f"#!/bin/bash\ntouch /workspace/evil_ran.txt\n")

    # Session B: start with the same workspace
    sandbox_b = DesktopSandbox()
    session_id_b = "sessionb" + uuid.uuid4().hex[:8]
    started = await sandbox_b.start(session_id_b, ws)
    assert started
    try:
        # Give it a moment in case anything auto-executes
        import asyncio
        await asyncio.sleep(1)
        assert not marker.exists(), (
            "evil_ran.txt was created — the malicious script was auto-executed on session start"
        )
    finally:
        await sandbox_b.stop()


# ─── Xvfb connectivity ────────────────────────────────────────────────────────

async def test_xvfb_running_on_display_99(session_sandbox):
    """Verify Xvfb is actually running and accepting connections on :99."""
    sandbox, _ = session_sandbox
    ok, output = await sandbox.exec("xdpyinfo -display :99 | head -5", timeout=10)
    assert ok, f"xdpyinfo failed — Xvfb may not be running: {output}"
    assert "name of display" in output.lower() or ":99" in output


async def test_pyautogui_can_reach_virtual_display(session_sandbox):
    """PyAutoGUI inside the container must connect to Xvfb without error."""
    sandbox, _ = session_sandbox
    ok, output = await sandbox.exec(
        "python3 -c \""
        "import pyautogui; "
        "pyautogui.FAILSAFE=False; "
        "size = pyautogui.size(); "
        "print(f'w={size.width} h={size.height}')"
        "\"",
        timeout=15,
    )
    assert ok, f"PyAutoGUI could not connect to virtual display: {output}"
    assert "w=1280" in output and "h=800" in output


async def test_non_root_user_in_desktop_container(session_sandbox):
    """Container must run as uid 1000, never root."""
    sandbox, _ = session_sandbox
    ok, output = await sandbox.exec("id -u", timeout=10)
    assert ok
    assert output.strip() == "1000"


async def test_no_privilege_escalation_in_desktop_container(session_sandbox):
    """--cap-drop=ALL + no-new-privileges: sudo and capability escalation must fail."""
    sandbox, _ = session_sandbox
    ok, output = await sandbox.exec("sudo id 2>&1 || echo NO_SUDO", timeout=10)
    assert "NO_SUDO" in output or not ok
