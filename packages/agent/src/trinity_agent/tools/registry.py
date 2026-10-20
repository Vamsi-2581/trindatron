from __future__ import annotations

import logging
import time

from .base import BaseTool, ToolResult

logger = logging.getLogger("trinity.tools.registry")


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def all(self) -> list[BaseTool]:
        return list(self._tools.values())

    def litellm_specs(self) -> list[dict]:
        return [t.to_litellm_spec() for t in self._tools.values()]

    async def call(self, name: str, args: dict) -> ToolResult:
        """Execute a registered tool, logging one structured observability
        line per call (Capability/Connector Framework foundation —
        "why/what did TRINDATRON just do", mirrors trinity_router.audit's
        own no-secrets-no-prompt-content convention: never logs `args`
        content or tool output, only metadata already known about the
        tool itself plus outcome/latency)."""
        tool = self._tools.get(name)
        if tool is None:
            logger.warning("tool_registry: unknown tool requested | tool=%s", name)
            return ToolResult(success=False, error=f"Unknown tool: {name!r}")

        start = time.monotonic()
        result = await tool.run(**args)
        latency_ms = (time.monotonic() - start) * 1000

        logger.info(
            "tool_registry: call | tool=%s capability=%s side_effect=%s "
            "requires_approval=%s success=%s latency_ms=%.1f",
            name, tool.capability or "", tool.side_effect.value,
            tool.requires_approval, result.success, latency_ms,
        )
        return result
