"""Unit tests for the ExecutionProvider contract — FakeExecutionProvider
(used by every other execution test) and DockerExecutionProvider (with
trinity_sandbox.DockerSandbox mocked out — no real Docker/subprocess call
in this file, consistent with "NUNCA fazer chamadas reais" for tests).

Test matrix
-----------
E01) FakeExecutionProvider — prepare/execute/inspect/cleanup happy path
E02) FakeExecutionProvider — queued outcome/exception replayed in order
E03) FakeExecutionProvider — unavailable -> ExecutionProviderUnavailable
E04) FakeExecutionProvider — cleanup is idempotent (safe to call twice)
E05) DockerExecutionProvider — prepare_environment creates an isolated
     per-user/per-job workspace directory
E06) DockerExecutionProvider — unavailable Docker -> ExecutionProviderUnavailable
E07) DockerExecutionProvider — execute_step delegates to DockerSandbox.run()
     with the job's workspace and timeout
E08) DockerExecutionProvider — a timeout-shaped failure is classified recoverable
E09) DockerExecutionProvider — an ordinary command failure is NOT recoverable
E10) DockerExecutionProvider — cleanup_environment removes the workspace directory
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.execution.docker_provider import DockerExecutionProvider
from src.execution.fake_provider import FakeExecutionProvider
from src.execution.provider import ExecutionProviderUnavailable, StepOutcome


@pytest.fixture
def tmp_root(tmp_path):
    return tmp_path


# ---------------------------------------------------------------------------
# FakeExecutionProvider
# ---------------------------------------------------------------------------


async def test_E01_fake_provider_happy_path(tmp_root):
    provider = FakeExecutionProvider(tmp_root)
    assert await provider.is_available() is True

    workspace = await provider.prepare_environment(job_id="job-1", user_id="user-1")
    assert workspace.exists()

    outcome = await provider.execute_step(workspace=workspace, command="echo hi", timeout_seconds=5)
    assert outcome.success is True
    assert "echo hi" in outcome.output

    info = await provider.inspect_environment(workspace=workspace)
    assert info.available is True

    await provider.cleanup_environment(workspace=workspace)
    assert not workspace.exists()


async def test_E02_fake_provider_queued_outcomes_replayed_in_order(tmp_root):
    provider = FakeExecutionProvider(tmp_root)
    workspace = await provider.prepare_environment(job_id="job-2", user_id="user-1")
    provider.queue_outcome(StepOutcome(success=False, output="boom", duration_ms=1, recoverable=True))
    provider.queue_outcome(StepOutcome(success=True, output="ok", duration_ms=1))

    first = await provider.execute_step(workspace=workspace, command="c1", timeout_seconds=5)
    second = await provider.execute_step(workspace=workspace, command="c2", timeout_seconds=5)

    assert first.success is False and first.recoverable is True
    assert second.success is True


async def test_E03_fake_provider_unavailable_raises(tmp_root):
    provider = FakeExecutionProvider(tmp_root, available=False)
    with pytest.raises(ExecutionProviderUnavailable):
        await provider.prepare_environment(job_id="job-3", user_id="user-1")


async def test_E04_fake_provider_cleanup_idempotent(tmp_root):
    provider = FakeExecutionProvider(tmp_root)
    workspace = await provider.prepare_environment(job_id="job-4", user_id="user-1")
    await provider.cleanup_environment(workspace=workspace)
    await provider.cleanup_environment(workspace=workspace)  # must not raise


# ---------------------------------------------------------------------------
# DockerExecutionProvider — DockerSandbox mocked, no real Docker call
# ---------------------------------------------------------------------------


def _mock_sandbox(*, available: bool = True, run_result: tuple[bool, str] = (True, "ok")):
    sandbox = AsyncMock()
    sandbox.is_available.return_value = available
    sandbox.run.return_value = run_result
    sandbox.unavailable_message = "Shell execution requires the Docker sandbox."
    return sandbox


async def test_E05_docker_provider_creates_isolated_workspace(tmp_root):
    sandbox = _mock_sandbox()
    provider = DockerExecutionProvider(str(tmp_root), sandbox=sandbox)

    ws1 = await provider.prepare_environment(job_id="job-a", user_id="user-x")
    ws2 = await provider.prepare_environment(job_id="job-b", user_id="user-y")

    assert ws1 != ws2
    assert ws1.exists() and ws2.exists()
    assert "user-x" in str(ws1) and "job-a" in str(ws1)
    assert "user-y" in str(ws2) and "job-b" in str(ws2)


async def test_E06_docker_provider_unavailable_raises(tmp_root):
    sandbox = _mock_sandbox(available=False)
    provider = DockerExecutionProvider(str(tmp_root), sandbox=sandbox)

    with pytest.raises(ExecutionProviderUnavailable):
        await provider.prepare_environment(job_id="job-c", user_id="user-1")


async def test_E07_docker_provider_execute_step_delegates_to_sandbox_run(tmp_root):
    sandbox = _mock_sandbox(run_result=(True, "hello"))
    provider = DockerExecutionProvider(str(tmp_root), sandbox=sandbox)
    workspace = await provider.prepare_environment(job_id="job-d", user_id="user-1")

    outcome = await provider.execute_step(workspace=workspace, command="echo hello", timeout_seconds=42)

    sandbox.run.assert_awaited_once_with("echo hello", workspace, timeout=42)
    assert outcome.success is True
    assert outcome.output == "hello"


async def test_E08_docker_provider_classifies_timeout_as_recoverable(tmp_root):
    sandbox = _mock_sandbox(run_result=(False, "Command timed out after 30s"))
    provider = DockerExecutionProvider(str(tmp_root), sandbox=sandbox)
    workspace = await provider.prepare_environment(job_id="job-e", user_id="user-1")

    outcome = await provider.execute_step(workspace=workspace, command="sleep 100", timeout_seconds=30)

    assert outcome.success is False
    assert outcome.recoverable is True


async def test_E09_docker_provider_ordinary_failure_not_recoverable(tmp_root):
    sandbox = _mock_sandbox(run_result=(False, "Exit code 1:\ngrep: no matches found"))
    provider = DockerExecutionProvider(str(tmp_root), sandbox=sandbox)
    workspace = await provider.prepare_environment(job_id="job-f", user_id="user-1")

    outcome = await provider.execute_step(workspace=workspace, command="grep x y", timeout_seconds=30)

    assert outcome.success is False
    assert outcome.recoverable is False


async def test_E10_docker_provider_cleanup_removes_workspace(tmp_root):
    sandbox = _mock_sandbox()
    provider = DockerExecutionProvider(str(tmp_root), sandbox=sandbox)
    workspace = await provider.prepare_environment(job_id="job-g", user_id="user-1")
    assert workspace.exists()

    await provider.cleanup_environment(workspace=workspace)

    assert not workspace.exists()
