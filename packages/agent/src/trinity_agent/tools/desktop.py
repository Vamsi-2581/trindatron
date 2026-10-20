"""
Desktop Computer Use tools — Phase 5.

ALL tools delegate execution to DesktopSandbox (a persistent Docker container
with an Xvfb virtual display). PyAutoGUI runs ONLY inside the container;
it is NEVER imported in this module or anywhere in the host Python process.

Security invariants enforced here:
  - Every tool checks sandbox.is_running() before acting (fail-closed).
  - Screenshot output is wrapped as external/untrusted visual content.
  - Coordinates are validated as integers in [0, 9999].
  - Key names are validated against an explicit allowlist.
  - Text for desktop_type is base64-encoded before embedding in commands
    to prevent shell injection via user-supplied strings.
  - desktop_open only allows paths inside the workspace.
"""
from __future__ import annotations

import base64
from pathlib import Path

from trinity_sandbox import DesktopSandbox

from trinity_agent.security.sanitizer import wrap_screenshot_for_llm
from .base import BaseTool, ToolResult

_COORD_MAX = 9999

# Keys that are safe to pass to pyautogui.hotkey() / pyautogui.press().
# This allowlist prevents shell injection via key name arguments.
_VALID_KEYS: frozenset[str] = frozenset({
    # Modifiers
    "ctrl", "shift", "alt", "super", "win",
    # Navigation
    "up", "down", "left", "right",
    "home", "end", "pageup", "pagedown",
    "insert", "delete",
    # Common keys
    "enter", "return", "space", "tab", "backspace", "escape", "esc",
    # Function keys
    "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
    # Letters
    "a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m",
    "n", "o", "p", "q", "r", "s", "t", "u", "v", "w", "x", "y", "z",
    # Digits
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9",
    # Symbol key names
    "minus", "equal", "bracketleft", "bracketright", "semicolon",
    "apostrophe", "comma", "period", "slash", "backslash", "grave",
})


def _validate_coord(value: object, name: str) -> tuple[bool, str]:
    if not isinstance(value, int) or not (0 <= value <= _COORD_MAX):
        return False, f"{name} must be an integer in [0, {_COORD_MAX}]"
    return True, ""


def _validate_keys(keys: list) -> tuple[bool, str]:
    if not keys:
        return False, "keys list cannot be empty"
    for k in keys:
        if str(k).lower() not in _VALID_KEYS:
            return False, f"Unknown key {k!r}. Allowed: {sorted(_VALID_KEYS)}"
    return True, ""


def _pyautogui(statement: str) -> str:
    """Build a python3 one-liner that runs a pyautogui statement with FAILSAFE off."""
    return (
        f"python3 -c \""
        f"import pyautogui; pyautogui.FAILSAFE=False; {statement}; print('OK')\""
    )


def _sandboxed_path(workspace: Path, rel_path: str) -> tuple[bool, Path | None, str]:
    ws = workspace.resolve()
    target = (ws / rel_path).resolve()
    if not target.is_relative_to(ws):
        return False, None, f"Path {rel_path!r} escapes workspace boundary"
    return True, target, ""


# ─── Tools ────────────────────────────────────────────────────────────────────

class DesktopScreenshot(BaseTool):
    name = "desktop_screenshot"
    description = (
        "Capture a screenshot of the isolated virtual desktop (Xvfb :99) and save it "
        "to the workspace. The image shows only the virtual environment — never the "
        "Windows host desktop."
    )
    parameters = {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Output filename relative to workspace (default: screenshot.png)",
            },
        },
        "required": [],
    }

    def __init__(self, workspace: Path, sandbox: DesktopSandbox) -> None:
        self._workspace = workspace
        self._sandbox = sandbox

    async def run(self, filename: str = "screenshot.png", **_) -> ToolResult:
        if not await self._sandbox.is_running():
            return ToolResult(success=False, error=self._sandbox.unavailable_message)
        ok, result = await self._sandbox.screenshot(filename)
        if not ok:
            return ToolResult(success=False, error=result)
        return ToolResult(success=True, output=wrap_screenshot_for_llm(filename))


class DesktopClick(BaseTool):
    name = "desktop_click"
    description = "Move the mouse to (x, y) in the virtual display and click the left button."
    parameters = {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "X coordinate in pixels"},
            "y": {"type": "integer", "description": "Y coordinate in pixels"},
        },
        "required": ["x", "y"],
    }

    def __init__(self, workspace: Path, sandbox: DesktopSandbox) -> None:
        self._workspace = workspace
        self._sandbox = sandbox

    async def run(self, x: int = 0, y: int = 0, **_) -> ToolResult:
        if not await self._sandbox.is_running():
            return ToolResult(success=False, error=self._sandbox.unavailable_message)
        ok, err = _validate_coord(x, "x")
        if not ok:
            return ToolResult(success=False, error=err)
        ok, err = _validate_coord(y, "y")
        if not ok:
            return ToolResult(success=False, error=err)
        ok, output = await self._sandbox.exec(
            _pyautogui(f"pyautogui.click({x}, {y})"), timeout=10
        )
        if ok:
            return ToolResult(success=True, output=f"Clicked ({x}, {y})")
        return ToolResult(success=False, error=output)


class DesktopDoubleClick(BaseTool):
    name = "desktop_double_click"
    description = "Double-click at (x, y) in the virtual display."
    parameters = {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "X coordinate"},
            "y": {"type": "integer", "description": "Y coordinate"},
        },
        "required": ["x", "y"],
    }

    def __init__(self, workspace: Path, sandbox: DesktopSandbox) -> None:
        self._workspace = workspace
        self._sandbox = sandbox

    async def run(self, x: int = 0, y: int = 0, **_) -> ToolResult:
        if not await self._sandbox.is_running():
            return ToolResult(success=False, error=self._sandbox.unavailable_message)
        for name, val in (("x", x), ("y", y)):
            ok, err = _validate_coord(val, name)
            if not ok:
                return ToolResult(success=False, error=err)
        ok, output = await self._sandbox.exec(
            _pyautogui(f"pyautogui.doubleClick({x}, {y})"), timeout=10
        )
        if ok:
            return ToolResult(success=True, output=f"Double-clicked ({x}, {y})")
        return ToolResult(success=False, error=output)


class DesktopRightClick(BaseTool):
    name = "desktop_right_click"
    description = "Right-click at (x, y) in the virtual display."
    parameters = {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "X coordinate"},
            "y": {"type": "integer", "description": "Y coordinate"},
        },
        "required": ["x", "y"],
    }

    def __init__(self, workspace: Path, sandbox: DesktopSandbox) -> None:
        self._workspace = workspace
        self._sandbox = sandbox

    async def run(self, x: int = 0, y: int = 0, **_) -> ToolResult:
        if not await self._sandbox.is_running():
            return ToolResult(success=False, error=self._sandbox.unavailable_message)
        for name, val in (("x", x), ("y", y)):
            ok, err = _validate_coord(val, name)
            if not ok:
                return ToolResult(success=False, error=err)
        ok, output = await self._sandbox.exec(
            _pyautogui(f"pyautogui.rightClick({x}, {y})"), timeout=10
        )
        if ok:
            return ToolResult(success=True, output=f"Right-clicked ({x}, {y})")
        return ToolResult(success=False, error=output)


class DesktopType(BaseTool):
    name = "desktop_type"
    description = (
        "Type text at the current cursor position in the virtual display. "
        "Text is base64-encoded before transmission to prevent shell injection. "
        "Supports printable ASCII characters."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to type"},
        },
        "required": ["text"],
    }

    def __init__(self, workspace: Path, sandbox: DesktopSandbox) -> None:
        self._workspace = workspace
        self._sandbox = sandbox

    async def run(self, text: str = "", **_) -> ToolResult:
        if not await self._sandbox.is_running():
            return ToolResult(success=False, error=self._sandbox.unavailable_message)
        if not text:
            return ToolResult(success=False, error="text cannot be empty")
        # base64-encode so arbitrary text is safe to embed in the shell command
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        cmd = (
            f"python3 -c \""
            f"import base64, pyautogui; "
            f"pyautogui.FAILSAFE=False; "
            f"t=base64.b64decode('{b64}').decode('utf-8'); "
            f"pyautogui.write(t, interval=0.02); "
            f"print('OK')\""
        )
        ok, output = await self._sandbox.exec(cmd, timeout=max(10, len(text) // 10 + 5))
        if ok:
            return ToolResult(success=True, output=f"Typed {len(text)} characters")
        return ToolResult(success=False, error=output)


class DesktopKey(BaseTool):
    name = "desktop_key"
    description = (
        "Press one or more keys simultaneously (hotkey). "
        "Example: keys=['ctrl', 'c'] presses Ctrl+C. "
        "Keys must be from the allowed set."
    )
    parameters = {
        "type": "object",
        "properties": {
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of key names to press together",
            },
        },
        "required": ["keys"],
    }

    def __init__(self, workspace: Path, sandbox: DesktopSandbox) -> None:
        self._workspace = workspace
        self._sandbox = sandbox

    async def run(self, keys: list | None = None, **_) -> ToolResult:
        if not await self._sandbox.is_running():
            return ToolResult(success=False, error=self._sandbox.unavailable_message)
        keys = keys or []
        ok, err = _validate_keys(keys)
        if not ok:
            return ToolResult(success=False, error=err)
        validated = [str(k).lower() for k in keys]
        keys_repr = ", ".join(f"'{k}'" for k in validated)
        ok, output = await self._sandbox.exec(
            _pyautogui(f"pyautogui.hotkey({keys_repr})"), timeout=10
        )
        if ok:
            return ToolResult(success=True, output=f"Pressed {'+'.join(validated)}")
        return ToolResult(success=False, error=output)


class DesktopScroll(BaseTool):
    name = "desktop_scroll"
    description = "Scroll the mouse wheel. Positive=up, negative=down."
    parameters = {
        "type": "object",
        "properties": {
            "clicks": {
                "type": "integer",
                "description": "Scroll units (positive=up, negative=down)",
            },
        },
        "required": ["clicks"],
    }

    def __init__(self, workspace: Path, sandbox: DesktopSandbox) -> None:
        self._workspace = workspace
        self._sandbox = sandbox

    async def run(self, clicks: int = 3, **_) -> ToolResult:
        if not await self._sandbox.is_running():
            return ToolResult(success=False, error=self._sandbox.unavailable_message)
        if not isinstance(clicks, int) or not (-100 <= clicks <= 100):
            return ToolResult(success=False, error="clicks must be an integer in [-100, 100]")
        ok, output = await self._sandbox.exec(
            _pyautogui(f"pyautogui.scroll({clicks})"), timeout=10
        )
        if ok:
            return ToolResult(success=True, output=f"Scrolled {clicks} clicks")
        return ToolResult(success=False, error=output)


class DesktopDrag(BaseTool):
    name = "desktop_drag"
    description = "Drag from (from_x, from_y) to (to_x, to_y) holding the left button."
    parameters = {
        "type": "object",
        "properties": {
            "from_x": {"type": "integer", "description": "Start X"},
            "from_y": {"type": "integer", "description": "Start Y"},
            "to_x":   {"type": "integer", "description": "End X"},
            "to_y":   {"type": "integer", "description": "End Y"},
        },
        "required": ["from_x", "from_y", "to_x", "to_y"],
    }

    def __init__(self, workspace: Path, sandbox: DesktopSandbox) -> None:
        self._workspace = workspace
        self._sandbox = sandbox

    async def run(
        self, from_x: int = 0, from_y: int = 0, to_x: int = 0, to_y: int = 0, **_
    ) -> ToolResult:
        if not await self._sandbox.is_running():
            return ToolResult(success=False, error=self._sandbox.unavailable_message)
        for name, val in (("from_x", from_x), ("from_y", from_y), ("to_x", to_x), ("to_y", to_y)):
            ok, err = _validate_coord(val, name)
            if not ok:
                return ToolResult(success=False, error=err)
        cmd = _pyautogui(
            f"pyautogui.moveTo({from_x}, {from_y}); "
            f"pyautogui.dragTo({to_x}, {to_y}, duration=0.5)"
        )
        ok, output = await self._sandbox.exec(cmd, timeout=15)
        if ok:
            return ToolResult(success=True, output=f"Dragged ({from_x},{from_y}) → ({to_x},{to_y})")
        return ToolResult(success=False, error=output)


class DesktopMove(BaseTool):
    name = "desktop_move"
    description = "Move the mouse cursor to (x, y) without clicking."
    parameters = {
        "type": "object",
        "properties": {
            "x": {"type": "integer", "description": "X coordinate"},
            "y": {"type": "integer", "description": "Y coordinate"},
        },
        "required": ["x", "y"],
    }

    def __init__(self, workspace: Path, sandbox: DesktopSandbox) -> None:
        self._workspace = workspace
        self._sandbox = sandbox

    async def run(self, x: int = 0, y: int = 0, **_) -> ToolResult:
        if not await self._sandbox.is_running():
            return ToolResult(success=False, error=self._sandbox.unavailable_message)
        for name, val in (("x", x), ("y", y)):
            ok, err = _validate_coord(val, name)
            if not ok:
                return ToolResult(success=False, error=err)
        ok, output = await self._sandbox.exec(
            _pyautogui(f"pyautogui.moveTo({x}, {y})"), timeout=10
        )
        if ok:
            return ToolResult(success=True, output=f"Moved mouse to ({x}, {y})")
        return ToolResult(success=False, error=output)


class DesktopOpen(BaseTool):
    name = "desktop_open"
    description = (
        "Open a workspace file using the default application in the virtual environment. "
        "Path must be relative to the workspace."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file within the workspace",
            },
        },
        "required": ["path"],
    }

    def __init__(self, workspace: Path, sandbox: DesktopSandbox) -> None:
        self._workspace = workspace
        self._sandbox = sandbox

    async def run(self, path: str = "", **_) -> ToolResult:
        if not await self._sandbox.is_running():
            return ToolResult(success=False, error=self._sandbox.unavailable_message)
        ok, _, err = _sandboxed_path(self._workspace, path)
        if not ok:
            return ToolResult(success=False, error=err)
        container_path = f"/workspace/{path}"
        ok, output = await self._sandbox.exec(
            f"xdg-open {container_path!r} &", timeout=30
        )
        if ok:
            return ToolResult(success=True, output=f"Opened {path} in virtual environment")
        return ToolResult(success=False, error=output)


class DesktopGetWindows(BaseTool):
    name = "desktop_get_windows"
    description = "List open windows in the virtual desktop (read-only)."
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, workspace: Path, sandbox: DesktopSandbox) -> None:
        self._workspace = workspace
        self._sandbox = sandbox

    async def run(self, **_) -> ToolResult:
        if not await self._sandbox.is_running():
            return ToolResult(success=False, error=self._sandbox.unavailable_message)
        ok, output = await self._sandbox.exec("wmctrl -l 2>&1", timeout=10)
        if ok:
            return ToolResult(success=True, output=output or "(no windows open)")
        return ToolResult(success=False, error=output)
