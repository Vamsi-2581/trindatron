"""
DockerExecutionProvider — wraps the EXISTING trinity_sandbox.DockerSandbox.

Does not reimplement isolation: every security property (network=none,
read-only rootfs, dropped capabilities, non-root user, memory/CPU/PIDs
limits, --rm auto-cleanup) is DockerSandbox's own, already-reviewed
contract (packages/sandbox/src/trinity_sandbox/docker_sandbox.py) — this
class only adapts that contract to the ExecutionProvider shape and manages
the workspace directory boundary.

Windows dev / Linux prod
--------------------------
DockerSandbox itself is already OS-agnostic: it shells out to `docker`,
which on Windows dev talks to Docker Desktop's Linux VM, and on Linux prod
talks to the daemon directly — identical container behavior either way, no
WSL assumption anywhere in this file or in DockerSandbox. is_available()
fails closed (False) if Docker isn't reachable or the image isn't built —
this provider surfaces that as ExecutionProviderUnavailable rather than
falling back to host execution, on both platforms alike.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from trinity_sandbox import DockerSandbox

from src.execution.provider import (
    EnvironmentInfo,
    ExecutionProvider,
    ExecutionProviderUnavailable,
    StepOutcome,
)

# Failure substrings DockerSandbox.run() itself produces for conditions that
# are transient by nature (see docker_sandbox.py) — used to classify a step
# failure as recoverable. Deliberately narrow: an ordinary non-zero exit
# code from the user's OWN command (e.g. "grep: no matches") is NOT in this
# list and is correctly treated as a permanent failure, not retried.
_RECOVERABLE_MARKERS: tuple[str, ...] = (
    "Command timed out after",
    "docker executable not found in PATH",
    "Sandbox error:",
)


class DockerExecutionProvider(ExecutionProvider):
    def __init__(self, workspace_root: str, *, sandbox: DockerSandbox | None = None) -> None:
        self._workspace_root = Path(workspace_root)
        self._sandbox = sandbox or DockerSandbox()

    @property
    def provider_name(self) -> str:
        return "docker"

    async def is_available(self) -> bool:
        return await self._sandbox.is_available()

    async def prepare_environment(self, *, job_id: str, user_id: str) -> Path:
        if not await self.is_available():
            raise ExecutionProviderUnavailable(self._sandbox.unavailable_message)
        # user_id is embedded in the path (not just job_id) so two jobs can
        # never collide onto the same directory even under a hypothetical
        # job_id reuse bug — an extra, cheap isolation layer on top of the
        # UUID already being effectively collision-proof on its own.
        workspace = self._workspace_root / "executions" / user_id / job_id
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    async def execute_step(
        self, *, workspace: Path, command: str, timeout_seconds: int,
    ) -> StepOutcome:
        start = time.monotonic()
        success, output = await self._sandbox.run(command, workspace, timeout=timeout_seconds)
        duration_ms = int((time.monotonic() - start) * 1000)
        recoverable = None
        if not success:
            recoverable = any(marker in output for marker in _RECOVERABLE_MARKERS)
        return StepOutcome(success=success, output=output, duration_ms=duration_ms, recoverable=recoverable)

    async def inspect_environment(self, *, workspace: Path) -> EnvironmentInfo:
        available = await self.is_available()
        if not workspace.exists():
            return EnvironmentInfo(available=available, workspace_path=str(workspace), files=[], message="workspace not created yet")
        files = sorted(p.name for p in workspace.iterdir() if p.is_file())
        return EnvironmentInfo(available=available, workspace_path=str(workspace), files=files)

    async def cleanup_environment(self, *, workspace: Path) -> None:
        # Idempotent — safe even if prepare_environment() was never called
        # or a previous cleanup already ran (mission requirement: "Nenhum
        # sandbox abandonado", and cleanup must run on every exit path
        # including ones where the workspace might not exist).
        if workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)
