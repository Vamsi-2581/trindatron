"""Functional tests for BrowserNavigate/BrowserGetText/BrowserScreenshot —
the tool layer that consumes BrowserSession (Etapa 2).

Same pattern as test_browser_session.py: a stdlib HTTP server bound to
127.0.0.1 on a random port stands in for "the web". No test here reaches the
real internet.
"""
from __future__ import annotations

import http.server
import inspect
import threading
from urllib.parse import parse_qs, urlsplit

import pytest

from trinity_agent.browser.network_policy import resolve_and_validate_host, validate_url_scheme
from trinity_agent.browser.session import BrowserSession
from trinity_agent.tools import browser as browser_tools
from trinity_agent.tools.browser import BrowserGetText, BrowserNavigate, BrowserScreenshot

# No module-level pytestmark: two tests below are synchronous. asyncio_mode is
# "auto" in pyproject.toml, so async tests run correctly without a decorator.


async def _loopback_only_validator(url: str) -> None:
    """Test-only validator — see test_browser_session.py for rationale.
    Never used outside this test file."""
    validate_url_scheme(url)
    parsed = urlsplit(url)
    if parsed.hostname in ("127.0.0.1", "localhost"):
        return
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    await resolve_and_validate_host(parsed.hostname, port)


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_a, **_k):
        pass

    def do_GET(self):
        self.server.hits.append(self.path)
        parsed = urlsplit(self.path)

        if parsed.path == "/":
            body = b"hello from test page"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "sid=abc123; Path=/")
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/long":
            qs = parse_qs(parsed.query)
            length = int(qs.get("n", ["10000"])[0])
            body = ("x" * length).encode("ascii")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()


@pytest.fixture
def test_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.hits = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}", server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ── BrowserNavigate ──────────────────────────────────────────────────────────

async def test_navigate_returns_url_and_title(test_server):
    base_url, _server = test_server
    session = BrowserSession("s-tool-nav", url_validator=_loopback_only_validator)
    tool = BrowserNavigate(session)
    try:
        result = await tool.run(url=f"{base_url}/")
        assert result.success
        assert f"Navigated to: {base_url}/" in result.output
        assert "Title:" in result.output
    finally:
        await session.close()


async def test_navigate_ssrf_blocked_returns_failed_result_not_exception():
    """Default (production) policy — no override. Must return a failed
    ToolResult, never let the exception escape .run()."""
    session = BrowserSession("s-tool-ssrf")
    tool = BrowserNavigate(session)
    try:
        result = await tool.run(url="http://127.0.0.1/")
        assert not result.success
        assert result.error
    finally:
        await session.close()


async def test_navigate_blocked_scheme_returns_failed_result():
    session = BrowserSession("s-tool-scheme")
    tool = BrowserNavigate(session)
    try:
        result = await tool.run(url="file:///etc/passwd")
        assert not result.success
        assert "scheme not allowed" in result.error
        assert session._page is None  # never launched a browser for a blocked scheme
    finally:
        await session.close()


async def test_navigate_launch_failure_returns_failed_result(monkeypatch):
    session = BrowserSession("s-tool-launch-fail")

    class _FakeChromium:
        async def launch(self, **_kwargs):
            raise RuntimeError("simulated launch failure")

    class _FakePlaywright:
        chromium = _FakeChromium()

        async def stop(self):
            pass

    class _FakeContextManager:
        async def start(self):
            return _FakePlaywright()

    monkeypatch.setattr(
        "trinity_agent.browser.session.async_playwright",
        lambda: _FakeContextManager(),
    )

    tool = BrowserNavigate(session)
    result = await tool.run(url="https://example.com/")
    assert not result.success
    assert "Failed to launch" in result.error
    await session.close()


# ── BrowserGetText ───────────────────────────────────────────────────────────

async def test_get_text_reads_same_page_after_navigation(test_server):
    base_url, _server = test_server
    session = BrowserSession("s-tool-gettext", url_validator=_loopback_only_validator)
    nav_tool = BrowserNavigate(session)
    text_tool = BrowserGetText(session)
    try:
        nav_result = await nav_tool.run(url=f"{base_url}/")
        assert nav_result.success

        text_result = await text_tool.run()
        assert text_result.success
        assert "hello from test page" in text_result.output
    finally:
        await session.close()


async def test_get_text_truncates_at_8000_chars(test_server):
    base_url, _server = test_server
    session = BrowserSession("s-tool-truncate", url_validator=_loopback_only_validator)
    nav_tool = BrowserNavigate(session)
    text_tool = BrowserGetText(session)
    try:
        await nav_tool.run(url=f"{base_url}/long?n=20000")
        result = await text_tool.run()
        assert result.success
        assert len(result.output) == 8000
    finally:
        await session.close()


async def test_get_text_before_navigation_returns_failed_result():
    session = BrowserSession("s-tool-no-nav")
    tool = BrowserGetText(session)
    try:
        result = await tool.run()
        assert not result.success
    finally:
        await session.close()


# ── BrowserScreenshot ────────────────────────────────────────────────────────

async def test_screenshot_saves_to_correct_workspace_path(test_server, tmp_path):
    base_url, _server = test_server
    session = BrowserSession("s-tool-shot", url_validator=_loopback_only_validator)
    nav_tool = BrowserNavigate(session)
    shot_tool = BrowserScreenshot(tmp_path, session)
    try:
        await nav_tool.run(url=f"{base_url}/")
        result = await shot_tool.run(filename="out.png")
        assert result.success
        assert result.output == "Screenshot saved to out.png"
        saved = tmp_path / "out.png"
        assert saved.exists()
        assert saved.stat().st_size > 0
    finally:
        await session.close()


async def test_screenshot_default_filename(test_server, tmp_path):
    base_url, _server = test_server
    session = BrowserSession("s-tool-shot-default", url_validator=_loopback_only_validator)
    nav_tool = BrowserNavigate(session)
    shot_tool = BrowserScreenshot(tmp_path, session)
    try:
        await nav_tool.run(url=f"{base_url}/")
        result = await shot_tool.run()
        assert result.success
        assert (tmp_path / "screenshot.png").exists()
    finally:
        await session.close()


# ── Isolation between sessions, exercised through the tool layer ───────────────

async def test_isolation_between_sessions_through_tools(test_server):
    base_url, _server = test_server
    session_a = BrowserSession("s-tool-iso-a", url_validator=_loopback_only_validator)
    session_b = BrowserSession("s-tool-iso-b", url_validator=_loopback_only_validator)
    try:
        await BrowserNavigate(session_a).run(url=f"{base_url}/")
        page_a = await session_a.ensure_page()
        cookies_a = await page_a.context.cookies()
        assert any(c["name"] == "sid" for c in cookies_a)

        page_b = await session_b.ensure_page()
        cookies_b = await page_b.context.cookies()
        assert cookies_b == []
    finally:
        await session_a.close()
        await session_b.close()


# ── Architectural guarantee: tools own no lifecycle of their own ───────────────

def test_tools_module_creates_no_playwright_lifecycle():
    """Static guarantee for requirement #4/#7: the tool classes must not
    construct Playwright/Chromium/BrowserContext themselves, and must not
    depend on an externally-injected page via set_page()."""
    source = inspect.getsource(browser_tools)
    assert "async_playwright" not in source
    assert "chromium" not in source.lower()
    assert "new_context" not in source
    assert "set_page" not in source


def test_tool_constructors_require_a_browser_session():
    assert "browser_session" in inspect.signature(BrowserNavigate.__init__).parameters
    assert "browser_session" in inspect.signature(BrowserGetText.__init__).parameters
    assert "browser_session" in inspect.signature(BrowserScreenshot.__init__).parameters
