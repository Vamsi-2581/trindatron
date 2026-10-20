"""
FakeExecutionProvider — TEST ONLY. Never constructed on any production
code path (execution_service.build_default_provider() always returns
DockerExecutionProvider — see that function's docstring for the guard).

No subprocess, no Docker, no real filesystem sandboxing — a real temp
directory is still used for workspace/artifact tests to stay meaningful,
but command "execution" is entirely scripted by the test via
queue_response()/queue_exception(), the same FIFO-double pattern already
established by tests/unit/test_binance_pay_adapter.py's FakeTransport.
"""
from __future__ import annotations

import time
from pathlib import Path

from src.execution.provider import (
    EnvironmentInfo,
    ExecutionProvider,
    ExecutionProviderUnavailable,
    StepOutcome,
)


class FakeExecutionProvider(ExecutionProvider):
    def __init__(self, workspace_root: Path, *, available: bool = True) -> None:
        self._workspace_root = Path(workspace_root)
        self._available = available
        self.calls: list[dict] = []
        self._queue: list[StepOutcome | Exception] = []

    def queue_outcome(self, outcome: StepOutcome) -> None:
        self._queue.append(outcome)

    def queue_exception(self, exc: Exception) -> None:
        self._queue.append(exc)

    @property
    def provider_name(self) -> str:
        return "fake"

    async def is_available(self) -> bool:
        return self._available

    async def prepare_environment(self, *, job_id: str, user_id: str) -> Path:
        if not self._available:
            raise ExecutionProviderUnavailable("fake provider unavailable")
        workspace = self._workspace_root / user_id / job_id
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    async def execute_step(
        self, *, workspace: Path, command: str, timeout_seconds: int,
    ) -> StepOutcome:
        self.calls.append({"workspace": str(workspace), "command": command, "timeout_seconds": timeout_seconds})
        if self._queue:
            item = self._queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        # Default: succeed trivially, echoing the command — deterministic,
        # no real shell involved.
        return StepOutcome(success=True, output=f"(fake) ran: {command}", duration_ms=1)

    async def inspect_environment(self, *, workspace: Path) -> EnvironmentInfo:
        if not workspace.exists():
            return EnvironmentInfo(available=self._available, workspace_path=str(workspace), files=[], message="workspace not created yet")
        files = sorted(p.name for p in workspace.iterdir() if p.is_file())
        return EnvironmentInfo(available=self._available, workspace_path=str(workspace), files=files)

    async def cleanup_environment(self, *, workspace: Path) -> None:
        import shutil
        if workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)
