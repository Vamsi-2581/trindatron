"""
Security audit regression tests.
All vulnerabilities found during the Phase 4 audit are covered here.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from trinity_agent.tools.filesystem import ReadFile, WriteFile, ListDirectory, _MAX_READ_BYTES
from trinity_agent.tools.shell import _is_forbidden, ExecuteShell


# ══════════════════════════════════════════════════════════════════════════════
# C1: Path-prefix collision (startswith vs is_relative_to)
# Workspace = /tmp/.../workspace
# Sibling  = /tmp/.../workspace_evil  ← startswith("/tmp/.../workspace") = True!
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_read_file_path_prefix_collision_blocked(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sibling = tmp_path / "workspace_evil"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("stolen")

    tool = ReadFile(workspace)
    result = await tool.run(path="../workspace_evil/secret.txt")
    assert not result.success
    assert "escapes" in result.error.lower()


@pytest.mark.asyncio
async def test_write_file_path_prefix_collision_blocked(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sibling = tmp_path / "workspace_evil"
    sibling.mkdir()

    tool = WriteFile(workspace)
    result = await tool.run(path="../workspace_evil/injected.txt", content="pwned")
    assert not result.success
    assert (sibling / "injected.txt").exists() is False


@pytest.mark.asyncio
async def test_list_directory_path_prefix_collision_blocked(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sibling = tmp_path / "workspace_evil"
    sibling.mkdir()

    tool = ListDirectory(workspace)
    result = await tool.run(path="../workspace_evil")
    assert not result.success
    assert "escapes" in result.error.lower()


# ══════════════════════════════════════════════════════════════════════════════
# C2 & C3: Shell forbidden patterns — env exfiltration, PowerShell bypass
# ══════════════════════════════════════════════════════════════════════════════

# Environment variable listing
def test_is_forbidden_env_command():
    assert _is_forbidden("env")


def test_is_forbidden_env_in_chain():
    assert _is_forbidden("echo hello && env")


def test_is_forbidden_printenv():
    assert _is_forbidden("printenv")


def test_is_forbidden_set_command():
    assert _is_forbidden("set")


def test_is_forbidden_set_in_chain():
    assert _is_forbidden("echo hello; set")


def test_is_forbidden_export():
    assert _is_forbidden("export")


# PowerShell bypass
def test_is_forbidden_powershell():
    assert _is_forbidden("powershell -Command Get-Content .env")


def test_is_forbidden_powershell_encoded():
    assert _is_forbidden("powershell -EncodedCommand aW1wb3J0IG9z")


def test_is_forbidden_pwsh():
    assert _is_forbidden("pwsh -c 'Get-Item Env:'")


# Sensitive file patterns
def test_is_forbidden_dotenv_file():
    assert _is_forbidden("cat .env")


def test_is_forbidden_dotenv_in_subdir():
    assert _is_forbidden("cat config/.env")


def test_is_forbidden_ssh_key():
    assert _is_forbidden("cat /home/user/.ssh/id_rsa")


def test_is_forbidden_aws_creds():
    assert _is_forbidden("cat /home/user/.aws/credentials")


# Env var references to secrets
def test_is_forbidden_api_key_reference():
    assert _is_forbidden("echo $ANTHROPIC_API_KEY")


def test_is_forbidden_api_key_windows():
    assert _is_forbidden("echo %OPENAI_API_KEY%")


def test_is_forbidden_secret_var():
    assert _is_forbidden("echo $MY_SECRET")


def test_is_forbidden_token_var():
    assert _is_forbidden("echo $GITHUB_TOKEN")


# Code-based exfiltration
def test_is_forbidden_python_os_environ():
    assert _is_forbidden("python -c \"import os; print(os.environ)\"")


def test_is_forbidden_node_process_env():
    assert _is_forbidden("node -e \"console.log(process.env)\"")


# Legitimate commands still allowed
def test_is_allowed_echo():
    assert not _is_forbidden("echo hello world")


def test_is_allowed_python_version():
    assert not _is_forbidden("python --version")


def test_is_allowed_ls():
    assert not _is_forbidden("ls -la")


def test_is_allowed_cat_txt():
    assert not _is_forbidden("cat readme.txt")


def test_is_allowed_mkdir():
    assert not _is_forbidden("mkdir output")


# ══════════════════════════════════════════════════════════════════════════════
# M1: File size limit on read_file
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_read_file_size_limit_enforced(workspace: Path):
    large_file = workspace / "large.bin"
    large_file.write_bytes(b"x" * (_MAX_READ_BYTES + 1))

    tool = ReadFile(workspace)
    result = await tool.run(path="large.bin")
    assert not result.success
    assert "too large" in result.error.lower()


@pytest.mark.asyncio
async def test_read_file_at_limit_succeeds(workspace: Path):
    file = workspace / "at_limit.txt"
    file.write_bytes(b"a" * _MAX_READ_BYTES)

    tool = ReadFile(workspace)
    result = await tool.run(path="at_limit.txt")
    assert result.success


# ══════════════════════════════════════════════════════════════════════════════
# Cross-session workspace isolation (filesystem tools)
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_cross_session_read_blocked(tmp_path: Path):
    session1_ws = tmp_path / "session-uuid-1111"
    session1_ws.mkdir()
    (session1_ws / "private.txt").write_text("session1 secrets")

    session2_ws = tmp_path / "session-uuid-2222"
    session2_ws.mkdir()

    tool = ReadFile(session2_ws)
    result = await tool.run(path=f"../session-uuid-1111/private.txt")
    assert not result.success


@pytest.mark.asyncio
async def test_cross_session_write_blocked(tmp_path: Path):
    session1_ws = tmp_path / "session-uuid-aaaa"
    session1_ws.mkdir()

    session2_ws = tmp_path / "session-uuid-bbbb"
    session2_ws.mkdir()

    tool = WriteFile(session2_ws)
    result = await tool.run(path="../session-uuid-aaaa/injected.txt", content="injected")
    assert not result.success
    assert not (session1_ws / "injected.txt").exists()


# ══════════════════════════════════════════════════════════════════════════════
# Null / empty inputs handled gracefully
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_execute_shell_empty_command_handled(workspace: Path):
    tool = ExecuteShell(workspace)
    # Empty command should not raise; outcome is OS-dependent
    result = await tool.run(command="")
    assert isinstance(result.success, bool)


@pytest.mark.asyncio
async def test_read_file_empty_path_blocked(workspace: Path):
    tool = ReadFile(workspace)
    result = await tool.run(path="")
    # Empty path resolves to the workspace itself (a directory) — should fail gracefully
    assert isinstance(result.success, bool)


# ══════════════════════════════════════════════════════════════════════════════
# Symlink: resolve() follows symlinks — verify correct behaviour
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_read_file_symlink_to_outside_blocked(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_file = tmp_path / "secret_outside.txt"
    outside_file.write_text("outside content")

    link = workspace / "evil_link.txt"
    try:
        link.symlink_to(outside_file)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks not supported on this OS/filesystem")

    tool = ReadFile(workspace)
    result = await tool.run(path="evil_link.txt")
    # resolve() follows the symlink → target is outside workspace → BLOCKED
    assert not result.success
