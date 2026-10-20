from __future__ import annotations

from pathlib import Path

from .base import BaseTool, SideEffect, ToolResult

_MAX_READ_BYTES = 512_000  # 500 KB — prevents context-window exhaustion


def _sandboxed(workspace: Path, path: str) -> tuple[bool, Path | None, str]:
    """
    Resolve target and verify it is strictly inside workspace.
    Uses Path.is_relative_to() (Python 3.9+) instead of startswith() to prevent
    the path-prefix collision where '/tmp/ws-evil'.startswith('/tmp/ws') is True.
    """
    ws_resolved = workspace.resolve()
    target = (ws_resolved / path).resolve()
    if not target.is_relative_to(ws_resolved):
        return False, None, "Path escapes workspace boundary"
    return True, target, ""


class ReadFile(BaseTool):
    name = "read_file"
    description = "Read the text contents of a file inside the agent workspace."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path to the file"},
        },
        "required": ["path"],
    }

    def __init__(self, workspace: Path):
        self._workspace = workspace

    async def run(self, path: str, **_) -> ToolResult:
        ok, target, err = _sandboxed(self._workspace, path)
        if not ok:
            return ToolResult(success=False, error=err)
        try:
            size = target.stat().st_size
            if size > _MAX_READ_BYTES:
                return ToolResult(
                    success=False,
                    error=f"File too large to read ({size:,} bytes > {_MAX_READ_BYTES:,} byte limit)",
                )
            text = target.read_text(encoding="utf-8")
            return ToolResult(success=True, output=text)
        except FileNotFoundError:
            return ToolResult(success=False, error=f"File not found: {path}")
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))


class WriteFile(BaseTool):
    name = "write_file"
    description = "Write text content to a file inside the agent workspace."
    side_effect = SideEffect.REVERSIBLE_WRITE
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path to the file"},
            "content": {"type": "string", "description": "Content to write"},
        },
        "required": ["path", "content"],
    }

    def __init__(self, workspace: Path):
        self._workspace = workspace

    async def run(self, path: str, content: str, **_) -> ToolResult:
        ok, target, err = _sandboxed(self._workspace, path)
        if not ok:
            return ToolResult(success=False, error=err)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return ToolResult(success=True, output=f"Written {len(content)} chars to {path}")
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))


class ListDirectory(BaseTool):
    name = "list_directory"
    description = "List files and directories inside the agent workspace."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path to list (default '.')"},
        },
        "required": [],
    }

    def __init__(self, workspace: Path):
        self._workspace = workspace

    async def run(self, path: str = ".", **_) -> ToolResult:
        ok, target, err = _sandboxed(self._workspace, path)
        if not ok:
            return ToolResult(success=False, error=err)
        try:
            entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
            lines = []
            for e in entries:
                tag = "FILE" if e.is_file() else "DIR "
                lines.append(f"[{tag}] {e.name}")
            return ToolResult(success=True, output="\n".join(lines) or "(empty)")
        except FileNotFoundError:
            return ToolResult(success=False, error=f"Directory not found: {path}")
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))
