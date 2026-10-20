from __future__ import annotations

from pathlib import Path

from trinity_agent.browser.session import BrowserSession

from .base import BaseTool, SideEffect, ToolResult

_MAX_TEXT_CHARS = 8000  # prevents context-window exhaustion, same cap as before


class BrowserNavigate(BaseTool):
    name = "browser_navigate"
    description = "Navigate to a URL in a headless browser and return the page title."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to navigate to"},
        },
        "required": ["url"],
    }

    def __init__(self, browser_session: BrowserSession):
        self._session = browser_session

    async def run(self, url: str, **_) -> ToolResult:
        try:
            page = await self._session.navigate(url)
            title = await page.title()
            return ToolResult(success=True, output=f"Navigated to: {url}\nTitle: {title}")
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))


class BrowserGetText(BaseTool):
    name = "browser_get_text"
    description = "Extract all visible text from the current browser page."
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def __init__(self, browser_session: BrowserSession):
        self._session = browser_session

    async def run(self, **_) -> ToolResult:
        try:
            text = await self._session.get_text()
            truncated = text[:_MAX_TEXT_CHARS] if len(text) > _MAX_TEXT_CHARS else text
            return ToolResult(success=True, output=truncated)
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))


class BrowserScreenshot(BaseTool):
    name = "browser_screenshot"
    description = "Take a screenshot of the current browser page and save it to the workspace."
    side_effect = SideEffect.REVERSIBLE_WRITE
    parameters = {
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "Output filename (default: screenshot.png)"},
        },
        "required": [],
    }

    def __init__(self, workspace: Path, browser_session: BrowserSession):
        self._workspace = workspace
        self._session = browser_session

    async def run(self, filename: str = "screenshot.png", **_) -> ToolResult:
        try:
            out_path = self._workspace / filename
            await self._session.screenshot(str(out_path))
            return ToolResult(success=True, output=f"Screenshot saved to {filename}")
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))


class BrowserClick(BaseTool):
    name = "browser_click"
    description = (
        "Click an element on the current browser page, identified by a CSS "
        "selector or Playwright text selector (e.g. 'button#submit', "
        "'text=Aceitar')."
    )
    # A click can submit a form, buy something, or send a message. It is an
    # action on a third-party system, not a read — it carries the same
    # side-effect class as writing a file, so the permission gateway treats
    # it accordingly.
    side_effect = SideEffect.EXTERNAL_SIDE_EFFECT
    parameters = {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS or text selector of the element to click",
            },
        },
        "required": ["selector"],
    }

    def __init__(self, browser_session: BrowserSession):
        self._session = browser_session

    async def run(self, selector: str, **_) -> ToolResult:
        try:
            await self._session.click(selector)
            return ToolResult(success=True, output=f"Clicked: {selector}")
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))


class BrowserFill(BaseTool):
    name = "browser_fill"
    description = (
        "Type a value into an input or textarea on the current browser page, "
        "identified by a CSS selector."
    )
    side_effect = SideEffect.EXTERNAL_SIDE_EFFECT
    parameters = {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "CSS selector of the field to fill",
            },
            "value": {"type": "string", "description": "Text to enter"},
        },
        "required": ["selector", "value"],
    }

    def __init__(self, browser_session: BrowserSession):
        self._session = browser_session

    async def run(self, selector: str, value: str, **_) -> ToolResult:
        try:
            await self._session.fill(selector, value)
            # The value itself is never echoed back: a filled field may be a
            # password or a token, and the tool output goes into the model's
            # context and the activity stream.
            return ToolResult(success=True, output=f"Filled: {selector}")
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))
