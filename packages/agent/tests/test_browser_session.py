"""Functional tests for BrowserSession — real Chromium against a local test server.

No test here ever navigates to the real internet: a stdlib HTTP server bound
to 127.0.0.1 on a random port stands in for "the web". SSRF/redirect/DNS
rebinding tests use real (or realistically monkeypatched) DNS resolution so
the actual network_policy code path is exercised, not a stub of it.
"""
from __future__ import annotations

import asyncio
import http.server
import socket
import threading
from urllib.parse import urlsplit

import pytest

from trinity_agent.browser.network_policy import (
    BlockedURLError,
    resolve_and_validate_host,
    validate_url_scheme,
)
from trinity_agent.browser.session import BrowserSession, BrowserSessionError

pytestmark = pytest.mark.asyncio

_real_getaddrinfo = socket.getaddrinfo


def _fake_getaddrinfo(ip: str):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr = (ip, 80, 0, 0) if family == socket.AF_INET6 else (ip, 80)
    return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]


async def _loopback_only_validator(url: str) -> None:
    """Test-only validator: allow 127.0.0.1/localhost through unconditionally
    (so tests can reach the local test server, which the real policy would
    otherwise correctly refuse as loopback), delegate everything else to the
    real DNS/IP validation. Never used outside this test file."""
    validate_url_scheme(url)
    parsed = urlsplit(url)
    if parsed.hostname in ("127.0.0.1", "localhost"):
        return
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    await resolve_and_validate_host(parsed.hostname, port)


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_a, **_k):  # silence stdlib access log
        pass

    def do_GET(self):
        self.server.hits.append(self.path)
        parsed = urlsplit(self.path)

        if parsed.path == "/":
            body = b"hello"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Set-Cookie", "sid=abc123; Path=/")
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/redirect":
            from urllib.parse import parse_qs
            target = parse_qs(parsed.query).get("to", [""])[0]
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return

        if parsed.path == "/slow":
            import time
            from urllib.parse import parse_qs
            seconds = float(parse_qs(parsed.query).get("seconds", ["5"])[0])
            time.sleep(seconds)
            body = b"slow-done"
            self.send_response(200)
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


# ── Happy path: real navigation over HTTP ───────────────────────────────────────

async def test_navigate_and_get_text_success(test_server):
    base_url, _server = test_server
    session = BrowserSession("s-happy", url_validator=_loopback_only_validator)
    try:
        await session.navigate(f"{base_url}/")
        text = await session.get_text()
        assert "hello" in text
    finally:
        await session.close()


async def test_screenshot_saves_file(test_server, tmp_path):
    base_url, _server = test_server
    session = BrowserSession("s-shot-real", url_validator=_loopback_only_validator)
    try:
        await session.navigate(f"{base_url}/")
        out = tmp_path / "shot.png"
        data = await session.screenshot(str(out))
        assert out.exists()
        assert out.stat().st_size > 0
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        await session.close()


# ── Timeouts ──────────────────────────────────────────────────────────────────

async def test_navigation_timeout_raises(test_server):
    base_url, _server = test_server
    session = BrowserSession(
        "s-nav-timeout",
        url_validator=_loopback_only_validator,
        navigation_timeout_ms=500,
    )
    try:
        with pytest.raises(Exception, match="Timeout"):
            await session.navigate(f"{base_url}/slow?seconds=5")
    finally:
        await session.close()


async def test_evaluate_timeout_raises(test_server):
    base_url, _server = test_server
    session = BrowserSession(
        "s-eval-timeout",
        url_validator=_loopback_only_validator,
        evaluate_timeout_s=0.3,
    )
    try:
        await session.navigate(f"{base_url}/")
        page = await session.ensure_page()

        async def _hang(*_a, **_k):
            await asyncio.sleep(999)

        page.evaluate = _hang
        with pytest.raises(asyncio.TimeoutError):
            await session.get_text()
    finally:
        await session.close()


async def test_screenshot_timeout_value_is_wired_through(test_server, tmp_path):
    base_url, _server = test_server
    session = BrowserSession(
        "s-shot-timeout",
        url_validator=_loopback_only_validator,
        screenshot_timeout_ms=4242,
    )
    try:
        await session.navigate(f"{base_url}/")
        page = await session.ensure_page()
        captured = {}

        async def _fake_screenshot(*, path=None, timeout=None, **_kwargs):
            captured["timeout"] = timeout
            with open(path, "wb") as f:
                f.write(b"\x89PNG\r\n\x1a\n")
            return b"\x89PNG\r\n\x1a\n"

        page.screenshot = _fake_screenshot
        await session.screenshot(str(tmp_path / "shot.png"))
        assert captured["timeout"] == 4242
    finally:
        await session.close()


# ── SSRF: scheme allowlist (blocked before any browser launch) ─────────────────

@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "data:text/html,<script>alert(1)</script>",
    "javascript:alert(1)",
    "about:blank",
    "ftp://example.com/file",
])
async def test_navigate_blocked_scheme_never_launches_browser(url):
    session = BrowserSession("s-scheme-block")
    try:
        with pytest.raises(BlockedURLError):
            await session.navigate(url)
        assert session._page is None  # scheme check runs before ensure_page()
    finally:
        await session.close()


async def test_navigate_credentials_in_url_blocked():
    session = BrowserSession("s-cred-block")
    try:
        with pytest.raises(BlockedURLError, match="credentials"):
            await session.navigate("https://user:pass@example.com/")
        assert session._page is None
    finally:
        await session.close()


# ── SSRF: default (production) policy against a real loopback target ───────────

async def test_default_policy_blocks_loopback_navigation(test_server):
    """No url_validator override — this is the real production policy.
    Navigating to our own loopback test server must be refused, and the
    server must never actually receive the request."""
    base_url, server = test_server
    session = BrowserSession("s-ssrf-default")
    try:
        with pytest.raises(Exception):
            await session.navigate(f"{base_url}/")
        assert server.hits == []
    finally:
        await session.close()


# ── SSRF: redirect to a private target is blocked, not just the entry URL ──────

async def test_redirect_to_metadata_endpoint_blocked(test_server):
    base_url, _server = test_server
    session = BrowserSession("s-redirect-block", url_validator=_loopback_only_validator)
    try:
        redirect_url = f"{base_url}/redirect?to=http://169.254.169.254/latest/meta-data/"
        with pytest.raises(Exception):
            await session.navigate(redirect_url)
    finally:
        await session.close()


async def test_redirect_to_rfc1918_blocked(test_server):
    base_url, _server = test_server
    session = BrowserSession("s-redirect-rfc1918", url_validator=_loopback_only_validator)
    try:
        redirect_url = f"{base_url}/redirect?to=http://10.0.0.5/internal"
        with pytest.raises(Exception):
            await session.navigate(redirect_url)
    finally:
        await session.close()


# ── SSRF: DNS rebinding on the redirect target is blocked ──────────────────────

async def test_redirect_dns_rebinding_blocked(test_server, monkeypatch):
    """Redirect target is a hostname that resolves to a private IP — proves
    the redirect hop gets its own fresh DNS validation, not a cached/stale
    check from before the redirect happened."""
    base_url, _server = test_server

    def _rebinding_getaddrinfo(host, *args, **kwargs):
        if host == "rebind.invalid.test":
            return _fake_getaddrinfo("10.0.0.5")
        return _real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _rebinding_getaddrinfo)

    session = BrowserSession("s-rebinding", url_validator=_loopback_only_validator)
    try:
        redirect_url = f"{base_url}/redirect?to=http://rebind.invalid.test/"
        with pytest.raises(Exception):
            await session.navigate(redirect_url)
    finally:
        await session.close()


# ── Isolation between sessions ──────────────────────────────────────────────────

async def test_cookies_not_shared_between_sessions(test_server):
    base_url, _server = test_server
    session_a = BrowserSession("s-iso-a", url_validator=_loopback_only_validator)
    session_b = BrowserSession("s-iso-b", url_validator=_loopback_only_validator)
    try:
        await session_a.navigate(f"{base_url}/")
        page_a = await session_a.ensure_page()
        cookies_a = await page_a.context.cookies()
        assert any(c["name"] == "sid" for c in cookies_a)

        # Session B never navigated anywhere — its context must be empty,
        # not inheriting anything from session A's context.
        page_b = await session_b.ensure_page()
        cookies_b = await page_b.context.cookies()
        assert cookies_b == []
    finally:
        await session_a.close()
        await session_b.close()


# ── Max one Page per session ─────────────────────────────────────────────────────

async def test_ensure_page_is_idempotent(test_server):
    base_url, _server = test_server
    session = BrowserSession("s-one-page", url_validator=_loopback_only_validator)
    try:
        page1 = await session.ensure_page()
        page2 = await session.ensure_page()
        assert page1 is page2
        assert len(session._context.pages) == 1
    finally:
        await session.close()


# ── Cleanup ───────────────────────────────────────────────────────────────────

async def test_close_is_idempotent(test_server):
    base_url, _server = test_server
    session = BrowserSession("s-close", url_validator=_loopback_only_validator)
    await session.navigate(f"{base_url}/")
    await session.close()
    assert session._page is None
    assert session._context is None
    assert session._browser is None
    assert session._playwright is None
    await session.close()  # must not raise a second time


async def test_close_before_any_launch_is_safe():
    session = BrowserSession("s-never-launched")
    await session.close()  # must not raise


async def test_close_called_from_finally_after_generator_cancelled(test_server):
    """Mirrors the pattern stream_agent() will use: browser lifecycle torn
    down from a `finally` block even when the async generator is aborted
    mid-stream (GeneratorExit, e.g. SSE client disconnect)."""
    base_url, _server = test_server
    session = BrowserSession("s-gen-cancel", url_validator=_loopback_only_validator)
    closed = {"value": False}

    async def _consume():
        try:
            await session.navigate(f"{base_url}/")
            yield "started"
            await asyncio.sleep(999)
            yield "unreachable"  # pragma: no cover
        finally:
            await session.close()
            closed["value"] = True

    gen = _consume()
    first = await gen.__anext__()
    assert first == "started"
    await gen.aclose()
    assert closed["value"] is True
    assert session._page is None


# ── Chromium launch failure ──────────────────────────────────────────────────

async def test_launch_failure_raises_browser_session_error(monkeypatch):
    session = BrowserSession("s-launch-fail")

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

    with pytest.raises(BrowserSessionError, match="Failed to launch"):
        await session.ensure_page()
    assert session._page is None
    assert session._browser is None

    await session.close()  # must still be safe after a failed launch


# ═══════════════════════════════════════════════════════════════════════════
# Etapa 4B — Resource Governor & Concurrency Hardening
#
# The tests above (real Chromium) cover the happy path, timeouts, SSRF,
# isolation, and basic cleanup/GeneratorExit. Everything below adds: state
# machine transitions, lifecycle edge cases (double/concurrent/mid-startup
# close), cancellation at every stage, same-session operation serialization,
# cross-session governor-bounded concurrency, repeated-cycle leak checks, and
# simulated crash/failure modes.
#
# A small fake Playwright chain (below) gives deterministic, fast control
# over *when* each lifecycle step completes — via asyncio.Event gates, never
# sleeps — for the race/cancellation tests that would otherwise depend on
# real Chromium's launch timing. The existing real-Chromium tests above are
# untouched; this section adds coverage, it does not replace them.
# ═══════════════════════════════════════════════════════════════════════════

from trinity_agent.browser.governor import (  # noqa: E402
    BrowserCapacityTimeoutError,
    BrowserResourceGovernor,
)
from trinity_agent.browser.session import _SessionState  # noqa: E402


class _FakePage:
    def __init__(self):
        self.close_calls = 0
        self.goto_calls = 0
        self.evaluate_calls = 0
        self.screenshot_calls = 0
        self.goto_raises: Exception | None = None
        self.concurrent_op_counter = _ConcurrencyCounter()

    def set_default_navigation_timeout(self, *_a, **_k):
        pass

    async def route(self, *_a, **_k):
        pass

    async def title(self):
        return "Fake Title"

    async def goto(self, url, *, timeout=None):
        async with self.concurrent_op_counter:
            self.goto_calls += 1
            await asyncio.sleep(0)
            if self.goto_raises is not None:
                raise self.goto_raises

    async def evaluate(self, *_a, **_k):
        async with self.concurrent_op_counter:
            self.evaluate_calls += 1
            await asyncio.sleep(0)
            return "fake page text"

    async def screenshot(self, *, path=None, timeout=None):
        async with self.concurrent_op_counter:
            self.screenshot_calls += 1
            await asyncio.sleep(0)
            return b"\x89PNG\r\n\x1a\n"

    async def close(self, **_k):
        self.close_calls += 1


class _ConcurrencyCounter:
    """Detects whether two async operations ever run inside this block at
    the same time — used to prove same-session operations are serialized
    without relying on timing, only on actual overlap detection."""

    def __init__(self):
        self._current = 0
        self.max_concurrent = 0

    async def __aenter__(self):
        self._current += 1
        self.max_concurrent = max(self.max_concurrent, self._current)
        return self

    async def __aexit__(self, *_exc):
        self._current -= 1


class _FakeContext:
    def __init__(self, page: _FakePage):
        self._page = page
        self.close_calls = 0

    async def new_page(self):
        return self._page

    async def close(self, **_k):
        self.close_calls += 1


class _FakeBrowser:
    def __init__(self, context: _FakeContext):
        self._context = context
        self.close_calls = 0

    async def new_context(self):
        return self._context

    async def close(self, **_k):
        self.close_calls += 1


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser, *, launch_gate: asyncio.Event | None = None, launch_raises: Exception | None = None):
        self._browser = browser
        self._launch_gate = launch_gate
        self._launch_raises = launch_raises
        self.launch_calls = 0

    async def launch(self, **_kwargs):
        self.launch_calls += 1
        if self._launch_gate is not None:
            await self._launch_gate.wait()
        if self._launch_raises is not None:
            raise self._launch_raises
        return self._browser


class _FakePlaywright:
    def __init__(self, chromium: _FakeChromium):
        self.chromium = chromium
        self.stop_calls = 0

    async def stop(self):
        self.stop_calls += 1


class _FakeContextManager:
    def __init__(self, playwright: _FakePlaywright):
        self._playwright = playwright

    async def start(self):
        return self._playwright


def _make_fake_chain(*, launch_gate=None, launch_raises=None, goto_raises=None):
    """Build a full fake Playwright -> Browser -> Context -> Page chain with
    controllable timing/failure. Returns the individual fakes so tests can
    assert on call counts and inject Events."""
    page = _FakePage()
    page.goto_raises = goto_raises
    context = _FakeContext(page)
    browser = _FakeBrowser(context)
    chromium = _FakeChromium(browser, launch_gate=launch_gate, launch_raises=launch_raises)
    playwright = _FakePlaywright(chromium)
    return playwright, browser, context, page


def _patch_playwright(monkeypatch, playwright: _FakePlaywright):
    monkeypatch.setattr(
        "trinity_agent.browser.session.async_playwright",
        lambda: _FakeContextManager(playwright),
    )


# ── State machine / lifecycle (B) ───────────────────────────────────────────

async def test_state_is_new_before_any_call():
    session = BrowserSession("s-state-new")
    assert session.state == "new"


async def test_state_is_ready_after_ensure_page(monkeypatch):
    playwright, *_ = _make_fake_chain()
    _patch_playwright(monkeypatch, playwright)
    session = BrowserSession("s-state-ready")
    await session.ensure_page()
    assert session.state == "ready"
    await session.close()


async def test_state_is_failed_after_launch_failure(monkeypatch):
    playwright, *_ = _make_fake_chain(launch_raises=RuntimeError("boom"))
    _patch_playwright(monkeypatch, playwright)
    session = BrowserSession("s-state-failed")
    with pytest.raises(BrowserSessionError):
        await session.ensure_page()
    assert session.state == "failed"
    await session.close()  # must remain safe


async def test_state_is_closed_after_close(monkeypatch):
    playwright, *_ = _make_fake_chain()
    _patch_playwright(monkeypatch, playwright)
    session = BrowserSession("s-state-closed")
    await session.ensure_page()
    await session.close()
    assert session.state == "closed"


async def test_ensure_page_on_failed_session_raises_without_retrying_launch(monkeypatch):
    playwright, browser, *_ = _make_fake_chain(launch_raises=RuntimeError("boom"))
    _patch_playwright(monkeypatch, playwright)
    session = BrowserSession("s-no-retry")
    with pytest.raises(BrowserSessionError):
        await session.ensure_page()
    assert playwright.chromium.launch_calls == 1

    with pytest.raises(BrowserSessionError, match="not retried automatically"):
        await session.ensure_page()
    assert playwright.chromium.launch_calls == 1  # still 1 — no retry attempted


async def test_operations_on_closed_session_raise(monkeypatch):
    playwright, *_ = _make_fake_chain()
    _patch_playwright(monkeypatch, playwright)
    session = BrowserSession("s-closed-ops")
    await session.ensure_page()
    await session.close()

    with pytest.raises(BrowserSessionError):
        await session.navigate("https://example.com/")
    with pytest.raises(BrowserSessionError):
        await session.get_text()
    with pytest.raises(BrowserSessionError):
        await session.screenshot("out.png")
    with pytest.raises(BrowserSessionError):
        await session.ensure_page()


async def test_close_during_startup_waits_then_tears_down(monkeypatch):
    launch_gate = asyncio.Event()
    playwright, browser, context, page = _make_fake_chain(launch_gate=launch_gate)
    _patch_playwright(monkeypatch, playwright)
    session = BrowserSession("s-close-during-startup")

    ensure_task = asyncio.create_task(session.ensure_page())
    await asyncio.sleep(0)  # let it reach the gated launch() call
    assert session.state == "starting"

    close_task = asyncio.create_task(session.close())
    await asyncio.sleep(0)  # close() should now be blocked on _lifecycle_lock

    # Startup hasn't finished yet — close() must not have torn anything down.
    assert browser.close_calls == 0
    assert session.state == "starting"

    launch_gate.set()  # let the launch complete
    await ensure_task  # startup succeeds...
    await close_task   # ...then close() proceeds and tears down

    assert session.state == "closed"
    assert browser.close_calls == 1
    assert context.close_calls == 1
    assert page.close_calls == 1


async def test_concurrent_close_calls_result_in_single_teardown(monkeypatch):
    playwright, browser, context, page = _make_fake_chain()
    _patch_playwright(monkeypatch, playwright)
    session = BrowserSession("s-concurrent-close")
    await session.ensure_page()

    results = await asyncio.gather(
        session.close(), session.close(), session.close(),
        return_exceptions=True,
    )
    assert all(r is None for r in results)  # close() never raises
    assert browser.close_calls == 1  # torn down exactly once, not 3 times
    assert context.close_calls == 1
    assert page.close_calls == 1
    assert session.state == "closed"


async def test_close_before_ensure_page_never_launches(monkeypatch):
    playwright, browser, *_ = _make_fake_chain()
    _patch_playwright(monkeypatch, playwright)
    session = BrowserSession("s-close-first")
    await session.close()
    assert session.state == "closed"
    assert playwright.chromium.launch_calls == 0
    assert browser.close_calls == 0  # nothing to close — never created


# ── Cancellation (C) ─────────────────────────────────────────────────────────

async def test_cancellation_while_waiting_for_capacity(monkeypatch):
    gov = BrowserResourceGovernor(capacity=1)
    playwright1, *_ = _make_fake_chain()
    holder = BrowserSession("s-cap-holder", governor=gov)
    _patch_playwright(monkeypatch, playwright1)
    await holder.ensure_page()
    assert gov.active_count == 1

    waiter = BrowserSession("s-cap-waiter", governor=gov, capacity_wait_timeout_s=None)
    waiting_started = asyncio.Event()

    async def _wait_for_slot():
        waiting_started.set()
        await waiter.ensure_page()

    task = asyncio.create_task(_wait_for_slot())
    await asyncio.wait_for(waiting_started.wait(), timeout=5)
    await asyncio.sleep(0)  # let it actually reach the blocking governor.acquire()
    assert gov.waiting_count == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert gov.waiting_count == 0
    assert gov.active_count == 1  # only the holder — cancellation consumed no slot
    assert waiter.state == "failed"  # startup was interrupted; terminal, not retried

    await holder.close()
    assert gov.active_count == 0


async def test_cancellation_during_chromium_launch_releases_governor_slot(monkeypatch):
    gov = BrowserResourceGovernor(capacity=2)
    launch_gate = asyncio.Event()
    playwright, browser, *_ = _make_fake_chain(launch_gate=launch_gate)
    _patch_playwright(monkeypatch, playwright)
    session = BrowserSession("s-cancel-launch", governor=gov)

    task = asyncio.create_task(session.ensure_page())
    await asyncio.sleep(0)
    assert session.state == "starting"
    assert gov.active_count == 1  # slot acquired before the (gated) launch call

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert session.state == "failed"
    assert gov.active_count == 0  # released even though cancelled mid-launch
    launch_gate.set()  # let the fake's internal await resolve so nothing lingers


async def test_cancellation_during_navigate_releases_op_lock(monkeypatch):
    playwright, browser, context, page = _make_fake_chain()
    _patch_playwright(monkeypatch, playwright)
    session = BrowserSession("s-cancel-nav")
    await session.ensure_page()

    goto_started = asyncio.Event()
    goto_may_finish = asyncio.Event()

    async def _gated_goto(url, *, timeout=None):
        goto_started.set()
        await goto_may_finish.wait()

    page.goto = _gated_goto

    task = asyncio.create_task(session.navigate("https://example.com/"))
    await asyncio.wait_for(goto_started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    goto_may_finish.set()

    # _op_lock must be free now — a subsequent operation must not hang.
    page.goto = _FakePage.goto.__get__(page)  # restore real fake behavior
    await asyncio.wait_for(session.navigate("https://example.com/"), timeout=5)
    await session.close()


async def test_cancellation_during_get_text_releases_op_lock(monkeypatch):
    playwright, browser, context, page = _make_fake_chain()
    _patch_playwright(monkeypatch, playwright)
    session = BrowserSession("s-cancel-gettext")
    await session.navigate("https://example.com/")

    eval_started = asyncio.Event()
    eval_may_finish = asyncio.Event()

    async def _gated_evaluate(*_a, **_k):
        eval_started.set()
        await eval_may_finish.wait()

    page.evaluate = _gated_evaluate

    task = asyncio.create_task(session.get_text())
    await asyncio.wait_for(eval_started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    eval_may_finish.set()

    page.evaluate = _FakePage.evaluate.__get__(page)
    text = await asyncio.wait_for(session.get_text(), timeout=5)
    assert text == "fake page text"
    await session.close()


async def test_cancellation_during_screenshot_releases_op_lock(monkeypatch):
    playwright, browser, context, page = _make_fake_chain()
    _patch_playwright(monkeypatch, playwright)
    session = BrowserSession("s-cancel-screenshot")
    await session.navigate("https://example.com/")

    shot_started = asyncio.Event()
    shot_may_finish = asyncio.Event()

    async def _gated_screenshot(*, path=None, timeout=None):
        shot_started.set()
        await shot_may_finish.wait()

    page.screenshot = _gated_screenshot

    task = asyncio.create_task(session.screenshot("out.png"))
    await asyncio.wait_for(shot_started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    shot_may_finish.set()

    page.screenshot = _FakePage.screenshot.__get__(page)
    data = await asyncio.wait_for(session.screenshot("out2.png"), timeout=5)
    assert data == b"\x89PNG\r\n\x1a\n"
    await session.close()


async def test_cancellation_during_close_still_releases_everything(monkeypatch):
    """close() itself is cancelled while waiting on the shielded teardown —
    the teardown must still run to completion in the background and the
    governor slot must still end up released."""
    gov = BrowserResourceGovernor(capacity=1)
    close_gate = asyncio.Event()
    page_close_entered = asyncio.Event()
    playwright, browser, context, page = _make_fake_chain()
    _patch_playwright(monkeypatch, playwright)

    async def _gated_page_close(**_k):
        page.close_calls += 1
        page_close_entered.set()
        await close_gate.wait()

    page.close = _gated_page_close

    session = BrowserSession("s-cancel-close", governor=gov)
    await session.ensure_page()
    assert gov.active_count == 1

    close_task = asyncio.create_task(session.close())
    await asyncio.wait_for(page_close_entered.wait(), timeout=5)
    assert page.close_calls == 1  # teardown reached (gated) page.close()

    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    # The background teardown is still in flight (gated) — nothing has
    # leaked yet, but it also hasn't finished, so the slot is still held.
    assert gov.active_count == 1

    close_gate.set()  # let the background teardown finish
    await asyncio.wait_for(_poll_until(lambda: session.state == "closed"), timeout=5)
    assert gov.active_count == 0


async def _poll_until(predicate, *, interval: float = 0.01):
    """Used only where no Event can observe a background task's own
    completion from the outside (e.g. a detached teardown task's final
    done-callback) — bounded by the caller's wait_for, never the sole
    synchronization mechanism for a concurrency assertion itself."""
    while not predicate():
        await asyncio.sleep(interval)


# ── Same-session operation serialization (D) ────────────────────────────────

async def test_two_concurrent_navigates_never_run_goto_simultaneously(monkeypatch):
    playwright, *_ , page = _make_fake_chain()
    _patch_playwright(monkeypatch, playwright)
    session = BrowserSession("s-serialize-nav-nav")
    await session.ensure_page()

    await asyncio.gather(
        session.navigate("https://example.com/a"),
        session.navigate("https://example.com/b"),
    )
    assert page.concurrent_op_counter.max_concurrent == 1
    assert page.goto_calls == 2
    await session.close()


async def test_navigate_and_screenshot_never_run_simultaneously(monkeypatch):
    playwright, *_ , page = _make_fake_chain()
    _patch_playwright(monkeypatch, playwright)
    session = BrowserSession("s-serialize-nav-shot")
    await session.ensure_page()

    await asyncio.gather(
        session.navigate("https://example.com/"),
        session.screenshot("out.png"),
    )
    assert page.concurrent_op_counter.max_concurrent == 1
    await session.close()


async def test_get_text_and_screenshot_never_run_simultaneously(monkeypatch):
    playwright, *_ , page = _make_fake_chain()
    _patch_playwright(monkeypatch, playwright)
    session = BrowserSession("s-serialize-text-shot")
    await session.navigate("https://example.com/")

    await asyncio.gather(
        session.get_text(),
        session.screenshot("out.png"),
    )
    assert page.concurrent_op_counter.max_concurrent == 1
    await session.close()


async def test_many_concurrent_operations_fully_serialize(monkeypatch):
    playwright, *_ , page = _make_fake_chain()
    _patch_playwright(monkeypatch, playwright)
    session = BrowserSession("s-serialize-many")
    await session.ensure_page()

    ops = [session.navigate("https://example.com/") for _ in range(5)]
    ops += [session.get_text() for _ in range(5)]
    ops += [session.screenshot(f"out{i}.png") for i in range(5)]
    await asyncio.gather(*ops)

    assert page.concurrent_op_counter.max_concurrent == 1
    assert page.goto_calls == 5
    assert page.evaluate_calls == 5
    assert page.screenshot_calls == 5
    await session.close()


# ── Cross-session concurrency, governor-bounded (E) ─────────────────────────

async def test_cross_session_operations_run_in_parallel_not_serialized(monkeypatch):
    """Confirms the per-session op lock does NOT leak across sessions —
    two different sessions' navigate() calls must be able to overlap."""
    gov = BrowserResourceGovernor(capacity=2)
    playwright_a, *_ , page_a = _make_fake_chain()
    playwright_b, *_ , page_b = _make_fake_chain()

    session_a = BrowserSession("s-cross-a", governor=gov)
    _patch_playwright(monkeypatch, playwright_a)
    await session_a.ensure_page()

    session_b = BrowserSession("s-cross-b", governor=gov)
    _patch_playwright(monkeypatch, playwright_b)
    await session_b.ensure_page()

    shared_counter = _ConcurrencyCounter()

    async def _gated_goto_a(url, *, timeout=None):
        async with shared_counter:
            await asyncio.sleep(0)

    async def _gated_goto_b(url, *, timeout=None):
        async with shared_counter:
            await asyncio.sleep(0)

    page_a.goto = _gated_goto_a
    page_b.goto = _gated_goto_b

    await asyncio.gather(
        session_a.navigate("https://example.com/"),
        session_b.navigate("https://example.com/"),
    )
    # Two independent sessions' operations are allowed to overlap — this is
    # the inverse assertion of the same-session serialization tests above.
    assert shared_counter.max_concurrent == 2

    await session_a.close()
    await session_b.close()


async def test_real_browser_third_session_waits_for_governor_capacity(test_server):
    """Real Chromium, small governor capacity: a 3rd session must not launch
    a 3rd Chromium process until one of the first two closes."""
    base_url, _server = test_server
    gov = BrowserResourceGovernor(capacity=2)
    s1 = BrowserSession("s-real-cap-1", governor=gov, url_validator=_loopback_only_validator)
    s2 = BrowserSession("s-real-cap-2", governor=gov, url_validator=_loopback_only_validator)
    s3 = BrowserSession("s-real-cap-3", governor=gov, url_validator=_loopback_only_validator,
                         capacity_wait_timeout_s=None)
    try:
        await s1.navigate(f"{base_url}/")
        await s2.navigate(f"{base_url}/")
        assert gov.active_count == 2

        s3_ready = asyncio.Event()

        async def _start_s3():
            await s3.navigate(f"{base_url}/")
            s3_ready.set()

        task = asyncio.create_task(_start_s3())
        await asyncio.sleep(0)
        assert not s3_ready.is_set()
        assert gov.waiting_count == 1

        await s1.close()  # frees a slot
        await asyncio.wait_for(s3_ready.wait(), timeout=10)
        await task
        assert gov.active_count == 2  # s2 + s3, s1 already closed
    finally:
        await s1.close()
        await s2.close()
        await s3.close()


# ── Resource leak checks (F) ────────────────────────────────────────────────

async def test_repeated_create_close_cycles_return_governor_to_initial_state(monkeypatch):
    gov = BrowserResourceGovernor(capacity=2)
    for i in range(20):
        playwright, *_ = _make_fake_chain()
        _patch_playwright(monkeypatch, playwright)
        session = BrowserSession(f"s-cycle-{i}", governor=gov)
        await session.ensure_page()
        assert gov.active_count == 1
        await session.close()
        assert gov.active_count == 0
    assert gov.available_capacity == 2
    assert gov.waiting_count == 0


async def test_repeated_startup_failures_do_not_leak_governor_capacity(monkeypatch):
    gov = BrowserResourceGovernor(capacity=2)
    for i in range(20):
        playwright, *_ = _make_fake_chain(launch_raises=RuntimeError("boom"))
        _patch_playwright(monkeypatch, playwright)
        session = BrowserSession(f"s-fail-cycle-{i}", governor=gov)
        with pytest.raises(BrowserSessionError):
            await session.ensure_page()
        assert gov.active_count == 0
    assert gov.available_capacity == 2


async def test_repeated_cancellations_do_not_leak_governor_capacity(monkeypatch):
    gov = BrowserResourceGovernor(capacity=2)
    for i in range(20):
        launch_gate = asyncio.Event()
        playwright, *_ = _make_fake_chain(launch_gate=launch_gate)
        _patch_playwright(monkeypatch, playwright)
        session = BrowserSession(f"s-cancel-cycle-{i}", governor=gov)
        task = asyncio.create_task(session.ensure_page())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        launch_gate.set()
        assert gov.active_count == 0
    assert gov.available_capacity == 2


# ── Crash / failure modes (G) ───────────────────────────────────────────────

async def test_operation_failure_after_ready_does_not_change_state(monkeypatch):
    """A mid-life failure (e.g. the renderer process crashed) is not a
    startup failure — the session stays READY, matching existing behavior
    where the tool layer (not the session) decides what to do with the
    error. No automatic state transition, no automatic recovery."""
    playwright, *_ , page = _make_fake_chain(goto_raises=ConnectionError("renderer crashed"))
    _patch_playwright(monkeypatch, playwright)
    session = BrowserSession("s-mid-life-crash")
    await session.ensure_page()
    assert session.state == "ready"

    with pytest.raises(ConnectionError):
        await session.navigate("https://example.com/")
    assert session.state == "ready"  # unchanged — not auto-failed

    await session.close()  # still closes cleanly afterward


async def test_context_new_page_failure_during_startup_releases_governor_slot(monkeypatch):
    gov = BrowserResourceGovernor(capacity=1)
    playwright, browser, context, _page = _make_fake_chain()

    async def _raising_new_page():
        raise RuntimeError("new_page failed")

    context.new_page = _raising_new_page
    _patch_playwright(monkeypatch, playwright)

    session = BrowserSession("s-newpage-fail", governor=gov)
    with pytest.raises(BrowserSessionError):
        await session.ensure_page()
    assert session.state == "failed"
    assert gov.active_count == 0
    assert browser.close_calls == 1  # browser was created — partial cleanup ran
    assert context.close_calls == 1


async def test_playwright_start_failure_releases_governor_slot(monkeypatch):
    gov = BrowserResourceGovernor(capacity=1)

    class _RaisingContextManager:
        async def start(self):
            raise RuntimeError("playwright failed to start")

    monkeypatch.setattr(
        "trinity_agent.browser.session.async_playwright",
        lambda: _RaisingContextManager(),
    )

    session = BrowserSession("s-pw-start-fail", governor=gov)
    with pytest.raises(BrowserSessionError):
        await session.ensure_page()
    assert session.state == "failed"
    assert gov.active_count == 0


# ── H/I. Configuration passed through to BrowserSession ─────────────────────

async def test_zero_capacity_wait_timeout_fails_immediately_when_full(monkeypatch):
    gov = BrowserResourceGovernor(capacity=1)
    playwright1, *_ = _make_fake_chain()
    holder = BrowserSession("s-zero-timeout-holder", governor=gov)
    _patch_playwright(monkeypatch, playwright1)
    await holder.ensure_page()

    waiter = BrowserSession("s-zero-timeout-waiter", governor=gov, capacity_wait_timeout_s=0)
    with pytest.raises(BrowserCapacityTimeoutError):
        await waiter.ensure_page()

    await holder.close()


async def test_governor_capacity_timeout_is_distinguishable_from_session_error(monkeypatch):
    gov = BrowserResourceGovernor(capacity=1)
    playwright1, *_ = _make_fake_chain()
    holder = BrowserSession("s-distinguish-holder", governor=gov)
    _patch_playwright(monkeypatch, playwright1)
    await holder.ensure_page()

    waiter = BrowserSession("s-distinguish-waiter", governor=gov, capacity_wait_timeout_s=0.01)
    with pytest.raises(BrowserCapacityTimeoutError):
        await waiter.ensure_page()
    # And NOT a bare BrowserSessionError / generic Exception match-by-accident:
    assert waiter.state == "failed"

    await holder.close()


# ── Multiple close() callers repeatedly cancelled (second-cancellation audit) ─
#
# Verified against the real CPython asyncio.shield() source before writing
# this test (not assumed): shield() creates a brand-new `outer` future on
# every call, and cancelling one caller's `outer` only detaches that
# caller's own done-callback from `inner` (the shared teardown Task) —
# `inner` itself is never touched. Our own `_on_teardown_done` callback is
# registered directly on `self._teardown_task` at creation time, completely
# independent of any shield() wrapper, so it fires exactly once whenever the
# task genuinely finishes, no matter how many callers came and went or how
# many of them were cancelled along the way. The only way to reach a
# genuinely different, more dangerous code path — cancelling the teardown
# Task itself — would require calling .cancel() directly on
# `self._teardown_task`, which nothing in this codebase ever does (grepped:
# zero `.cancel()` calls in session.py); the sole realistic trigger for that
# would be whole-process/interpreter shutdown sweeping every live Task,
# at which point the in-memory governor itself is being destroyed too, so
# "a new session can't acquire capacity" stops being a meaningful concern.
#
# This test represents the real, reachable risk: many callers of close(),
# repeatedly cancelled, none of them ever interrupting the actual teardown.

async def test_many_cancelled_close_callers_never_prevent_teardown_completion(monkeypatch):
    gov = BrowserResourceGovernor(capacity=1)
    page_close_entered = asyncio.Event()
    close_gate = asyncio.Event()
    playwright, browser, context, page = _make_fake_chain()
    _patch_playwright(monkeypatch, playwright)

    async def _gated_page_close(**_k):
        page.close_calls += 1
        page_close_entered.set()
        await close_gate.wait()

    page.close = _gated_page_close

    session = BrowserSession("s-many-cancelled-closers", governor=gov)
    await session.ensure_page()
    assert gov.active_count == 1

    # First caller starts the (single) teardown task and gets stuck waiting
    # on the gated page.close().
    first_caller = asyncio.create_task(session.close())
    await asyncio.wait_for(page_close_entered.wait(), timeout=5)
    assert page.close_calls == 1

    # Repeatedly spin up new close() callers, each of which attaches to the
    # SAME underlying teardown task via its own shield() wrapper, and cancel
    # each one before it can complete — simulating a caller being cancelled
    # over and over, e.g. a client that keeps disconnecting/retrying.
    for _ in range(10):
        extra_caller = asyncio.create_task(session.close())
        await asyncio.sleep(0)  # let it reach the shielded await
        extra_caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await extra_caller
        # Cancelling a caller must never touch the shared teardown task.
        assert page.close_calls == 1
        assert browser.close_calls == 0  # teardown hasn't progressed past page yet

    # Cancel the very first caller too, for good measure.
    first_caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_caller
    assert page.close_calls == 1  # still just the one real close() attempt

    # Now let the gated page.close() actually finish. The teardown task —
    # untouched by any of the 11 cancelled callers above — must run to
    # completion on its own and finish releasing everything.
    close_gate.set()
    await asyncio.wait_for(_poll_until(lambda: session.state == "closed"), timeout=5)
    assert browser.close_calls == 1
    assert context.close_calls == 1
    assert gov.active_count == 0  # slot released despite 11 cancelled waiters
    assert gov.waiting_count == 0

    # A brand-new session must be able to acquire the now-freed capacity —
    # directly demonstrating requirement #7 (a new session is never blocked
    # by the repeatedly-cancelled close() callers above).
    playwright2, *_ = _make_fake_chain()
    _patch_playwright(monkeypatch, playwright2)
    new_session = BrowserSession("s-after-many-cancels", governor=gov)
    await asyncio.wait_for(new_session.ensure_page(), timeout=5)
    assert gov.active_count == 1
    await new_session.close()
    assert gov.active_count == 0
