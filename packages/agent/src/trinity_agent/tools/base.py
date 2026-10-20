from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


@dataclass
class ToolResult:
    success: bool
    output: str = ""
    error: str = ""

    def to_llm_str(self) -> str:
        if self.success:
            return self.output
        return f"ERROR: {self.error}"


class SideEffect(str, Enum):
    """Classifies what a tool's execution can do to state outside the
    model's own response — the Capability/Connector Framework foundation
    (Tool Contract). Every tool declares exactly one; defaults to the
    safest value so a tool that doesn't override it is never accidentally
    under-classified.

    READ_ONLY: never changes state anywhere (reads a file, searches the
      web, fetches a page, queries a browser page's text).
    REVERSIBLE_WRITE: changes state, but only inside the sandboxed
      workspace/session, and the change can be undone/overwritten freely
      (writing a file, saving a screenshot).
    EXTERNAL_SIDE_EFFECT: changes state in a system OUTSIDE the sandbox
      (sends a message, schedules an action, calls a real external
      service) — the kind of action email/calendar/publishing/payments/
      social/ads/CRM/automation connectors will need this classification
      for.
    IRREVERSIBLE_ACTION: a side effect that cannot be reliably undone
      even in principle (an arbitrary shell command — sandboxed, but its
      blast radius inside that sandbox isn't enumerable in advance).
    """
    READ_ONLY = "read_only"
    REVERSIBLE_WRITE = "reversible_write"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"
    IRREVERSIBLE_ACTION = "irreversible_action"


class BaseTool(ABC):
    """Tool Contract — the single, canonical shape every tool declares,
    for both the Agent ReAct loop and TRINITY WORK (see
    trinity_agent.tools.registry.ToolRegistry, the shared infrastructure
    both now use).

    name/description/parameters are the pre-existing INPUT contract
    (parameters is a JSON Schema, unchanged). ToolResult (success/output/
    error) is the OUTPUT contract — also unchanged.

    Everything below is new, added for the Capability/Connector
    Framework foundation. Every field is optional/defaulted so every
    pre-existing subclass (ReadFile, WriteFile, ListDirectory, WebSearch,
    BrowserNavigate, BrowserGetText, BrowserScreenshot, ExecuteShell)
    keeps working with ZERO changes to its own definition unless a field
    below doesn't match its real, pre-existing behavior (see each
    subclass's own file for where a non-default value was set, and why).

    side_effect: see SideEffect above.
    requires_approval: whether this tool's execution must pause for an
      explicit user approval before running — mirrors the exact gate
      TRINITY WORK's `_CONFIRM_TOOLS` already implemented; formalized
      here as a per-tool declaration instead of a separately-maintained
      set of tool-name strings.
    timeout_seconds: a DECLARED timeout hint for callers/observability.
      None means "no fixed timeout is declared here" — several tools
      (e.g. ExecuteShell) already enforce their own real timeout
      internally via a `timeout` run() parameter; this field doesn't
      replace that, it's metadata a caller/registry can log or use for
      display.
    capability: a src.capability.Capability value this tool is primarily
      associated with, if any. None = general-purpose, usable by more
      than one capability's tool set (matches every current tool's real
      status — none is capability-exclusive today).
    max_retries: declarative only. 0 (the default) matches the REAL
      current behavior of every tool in this registry — none of them
      retries internally today. Existing to let a future tool/connector
      declare a real retry policy without inventing one for tools that
      don't have one.
    cost_metadata: None by default (no direct cost beyond the LLM call
      itself, true for every current tool). A future Connector-backed
      tool with its own per-call cost can populate this.
    """
    name: str
    description: str
    parameters: dict  # JSON Schema — the INPUT contract.

    side_effect: SideEffect = SideEffect.READ_ONLY
    requires_approval: bool = False
    timeout_seconds: float | None = None
    capability: str | None = None
    max_retries: int = 0
    cost_metadata: dict | None = None

    @abstractmethod
    async def run(self, **kwargs) -> ToolResult: ...

    async def is_available(self) -> bool:
        """Whether this tool can be used right now (e.g. a required
        external service/credential is configured). Defaults to True —
        every tool in this registry today has no external prerequisite
        beyond whatever its own run() already checks per-call; a future
        Connector-backed tool can override this to report real
        availability (mirrors ExecutionProvider.is_available() /
        DockerSandbox.is_available()'s existing pattern elsewhere in this
        codebase)."""
        return True

    def to_litellm_spec(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
