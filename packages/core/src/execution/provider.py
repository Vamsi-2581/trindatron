"""
ExecutionProvider — the sandbox boundary abstraction for Execution Runtime V1.

Mirrors the exact shape billing/providers/base.py already established
(ABC + dataclasses + registry-free direct construction, since — like
billing before Binance Pay — there is currently exactly one real provider):
execution_service.py (the Orchestrator) never imports DockerSandbox,
subprocess, or Docker CLI details directly; it only calls this interface.

Why this shape and not the mission prompt's literal suggestion
------------------------------------------------------------------
The prompt's example (create_environment/execute/inspect/stop/destroy/
get_status) assumes a PERSISTENT environment you create once and run many
commands against. The sandbox actually available in this repository
(trinity_sandbox.DockerSandbox — already used by code_service.py) is the
opposite: a fresh, ephemeral, auto-removed (--rm) container PER COMMAND,
by deliberate security design (see its own module docstring). Forcing a
create-once/reuse-many contract onto that primitive would mean either (a)
faking persistence that isn't there, or (b) silently switching to
trinity_sandbox.DesktopSandbox's genuinely persistent-container model
(built for Computer Use / Xvfb, not shell), which is not what a "run this
shell plan" job needs and would drag in an unrelated dependency (Xvfb,
scrot) for no reason.

So the contract here keeps environment/execution as two clearly separate
concepts but shapes each call to what the real provider actually supports:
  prepare_environment()  -> one-time, cheap: resolve/create the workspace
                             directory this job's commands will run in.
                             (DockerSandbox itself has no per-environment
                             state — this only prepares the WORKSPACE, the
                             one thing a caller must not create out of
                             sandbox bounds.)
  execute_step()          -> run ONE command, ephemeral container per call,
                             exactly DockerSandbox.run()'s existing contract.
  inspect_environment()   -> read-only: is the provider currently usable,
                             what does the workspace contain right now.
  cleanup_environment()   -> remove the workspace directory. Since each
                             execute_step() container is already auto-removed
                             by Docker itself (--rm), there is no separate
                             "stop a running environment" verb to add here —
                             it would have nothing real to do.

A provider that manages genuinely persistent environments (e.g. a future
DesktopExecutionProvider wrapping DesktopSandbox for a job_type needing a
GUI) can still implement this same ABC — inspect_environment()/
cleanup_environment() apply to it unchanged; only execute_step()'s
internals would differ (docker exec instead of docker run).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class StepOutcome:
    """Result of one execute_step() call."""

    success: bool
    output: str
    duration_ms: int
    # Populated only when the failure is classified — retry_policy.py reads
    # this to decide whether a retry makes sense. None = not yet classified
    # (execution_service.py classifies it if the provider didn't).
    recoverable: bool | None = None


@dataclass
class EnvironmentInfo:
    """Read-only snapshot from inspect_environment()."""

    available: bool
    workspace_path: str
    files: list[str] = field(default_factory=list)
    message: str = ""


class ExecutionProviderUnavailable(Exception):
    """Raised by prepare_environment()/execute_step() when the underlying
    sandbox cannot run at all (Docker not installed, image missing, daemon
    unreachable) — fail-closed, same contract DockerSandbox.is_available()
    already documents. Never silently falls back to host execution."""


class ExecutionProvider(ABC):
    """Sandbox boundary abstraction. execution_service.py depends only on
    this — never on trinity_sandbox, subprocess, or `docker` directly."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Unique identifier, e.g. "docker". Stored on ExecutionJob.provider_name."""

    @abstractmethod
    async def is_available(self) -> bool:
        """Fail-closed check — callers must verify this before prepare_environment()."""

    @abstractmethod
    async def prepare_environment(self, *, job_id: str, user_id: str) -> Path:
        """Create/resolve the isolated workspace directory for this job.
        Returns the host path — never exposed to the job's own commands as
        a host path; only ever mounted at a fixed in-container path (see
        the concrete provider)."""

    @abstractmethod
    async def execute_step(
        self, *, workspace: Path, command: str, timeout_seconds: int,
    ) -> StepOutcome:
        """Run one command inside the sandbox, scoped to `workspace`."""

    @abstractmethod
    async def inspect_environment(self, *, workspace: Path) -> EnvironmentInfo:
        """Read-only status + file listing — never mutates anything."""

    @abstractmethod
    async def cleanup_environment(self, *, workspace: Path) -> None:
        """Remove the workspace directory. Must be safe to call even if
        prepare_environment() was never called or already cleaned up
        (idempotent) — execution_service.py always calls this in a
        `finally`, including on every failure/cancel/timeout path."""
