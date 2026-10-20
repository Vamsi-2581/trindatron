"""Etapa 5D correction, item 3 — proves Request Control -> Agent Scheduler
-> TCU -> Agent/ReAct -> BrowserResourceGovernor does not deadlock,
double-release, or leak a slot in either direction.

Unlike test_agent_browser_integration.py (which patches
BrowserSession.navigate/get_text/screenshot directly, bypassing
ensure_page() and therefore never touching the real
BrowserResourceGovernor at all), the tests here fake ONLY the Playwright
launch chain (async_playwright().start().chromium.launch().new_context().
new_page()) so BrowserSession.ensure_page() runs for REAL — including a
real BrowserResourceGovernor.acquire() — with a controllable hang point at
page.goto(). This is what makes "browser genuinely in use" a real,
verifiable state (agent_service._browser_governor.active_count == 1), not
just an assumption.

On why "aclose() during a hung browser operation" is not tested directly:
reaching a suspension point INSIDE goto() requires the outer
stream_agent()/with_execution_timeout() generator's own __anext__() call
to itself be in flight (it does not return until goto() resolves) — which
requires a concurrent task, exactly like the CancelledError scenario
below. Calling .aclose() directly while a separate task is concurrently
running __anext__() raises "asynchronous generator is already running"
(verified against this codebase's own earlier attempt — see git history).
Real Starlette disconnect handling cancels the task driving the response
body generator (delivering CancelledError), it does not call .aclose()
concurrently with an in-flight __anext__() either — so the CancelledError
scenario below (test_cancellation_while_browser_navigate_in_progress) IS
the realistic equivalent of "disconnect while browser is in use", and is
what's tested. Direct aclose() while genuinely idle-between-yields is
already covered generically in test_agent_scheduler_streaming.py and
test_agent_scheduler_task_leak.py.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.capability import Capability
from src.models.session import Session as SessionModel
from src.services import agent_service
from src.services.agent_scheduler import InMemoryAgentScheduler, with_execution_timeout

pytestmark = pytest.mark.asyncio


class _FakeRoutingResult:
    model = "claude-sonnet-test"
    content = "final answer"
    tool_calls = None
    input_tokens = 5
    output_tokens = 5
    cached_tokens = 0
    cost_usd = 0.0
    tcu = 0.0


def _browser_tool_call():
    return SimpleNamespace(
        model="claude-sonnet-test", content=None,
        input_tokens=5, output_tokens=5, cached_tokens=0, cost_usd=0.0, tcu=0.0,
        tool_calls=[SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(name="browser_navigate", arguments='{"url": "https://example.com/"}'),
        )],
    )


class _FakePage:
    def __init__(self, goto_entered: asyncio.Event | None = None, goto_release: asyncio.Event | None = None):
        self._goto_entered = goto_entered
        self._goto_release = goto_release

    def set_default_navigation_timeout(self, ms):
        pass

    async def route(self, pattern, handler):
        pass

    async def goto(self, url, timeout=None):
        if self._goto_entered is not None:
            self._goto_entered.set()
        if self._goto_release is not None:
            await self._goto_release.wait()

    async def title(self):
        return "Fake Title"

    async def close(self):
        pass


class _FakeContext:
    def __init__(self, page):
        self._page = page

    async def new_page(self):
        return self._page

    async def close(self):
        pass


class _FakeBrowser:
    def __init__(self, context):
        self._context = context

    async def new_context(self):
        return self._context

    async def close(self):
        pass


class _FakeChromium:
    def __init__(self, browser):
        self._browser = browser

    async def launch(self, headless=True, timeout=None):
        return self._browser


class _FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium

    async def stop(self):
        pass


class _FakePlaywrightCM:
    def __init__(self, playwright):
        self._playwright = playwright

    async def start(self):
        return self._playwright


def _fake_async_playwright_factory(goto_entered: asyncio.Event | None = None, goto_release: asyncio.Event | None = None):
    """Builds a fake replacement for `playwright.async_api.async_playwright`
    that drives BrowserSession.ensure_page() through its REAL code path —
    real governor.acquire(), real NEW->STARTING->READY transition — down to
    a controllable page.goto()."""
    page = _FakePage(goto_entered, goto_release)
    context = _FakeContext(page)
    browser = _FakeBrowser(context)
    chromium = _FakeChromium(browser)
    playwright = _FakePlaywright(chromium)

    def _async_playwright():
        return _FakePlaywrightCM(playwright)

    return _async_playwright


@pytest.fixture
def agent_session(client, auth_headers, db) -> SessionModel:
    session_id = client.post("/sessions", json={}, headers=auth_headers).json()["id"]
    return db.query(SessionModel).filter(SessionModel.id == session_id).one()


def _governor():
    return agent_service._browser_governor


# ── 1. Agent with browser — full happy path, both layers release cleanly ───

async def test_agent_with_browser_acquires_and_releases_both_scheduler_and_governor(db, agent_session, monkeypatch):
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)

    call_count = {"n": 0}

    async def _complete(request):
        call_count["n"] += 1
        return _browser_tool_call() if call_count["n"] == 1 else _FakeRoutingResult()

    monkeypatch.setattr(agent_service._router, "complete", _complete)

    assert _governor().active_count == 0
    with patch("trinity_agent.browser.session.async_playwright", new=_fake_async_playwright_factory()):
        token = scheduler.try_acquire("user-1")
        agen = agent_service.stream_agent(
            db, agent_session, "go to example.com", capability=Capability.CHAT, agent_scheduler_token=token,
        )
        async for _ in with_execution_timeout(agen, timeout_s=5.0):
            pass

    assert scheduler.global_active_count == 0
    assert _governor().active_count == 0


# ── 2. Timeout while the browser is genuinely in use ────────────────────────

async def test_timeout_while_browser_navigate_in_progress_releases_scheduler_and_governor(db, agent_session, monkeypatch):
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)

    async def _complete(request):
        return _browser_tool_call()

    monkeypatch.setattr(agent_service._router, "complete", _complete)

    goto_entered = asyncio.Event()
    never_releases = asyncio.Event()  # goto() hangs "forever" — timeout must cut it off

    with patch(
        "trinity_agent.browser.session.async_playwright",
        new=_fake_async_playwright_factory(goto_entered, never_releases),
    ):
        token = scheduler.try_acquire("user-1")
        agen = agent_service.stream_agent(
            db, agent_session, "go to example.com", capability=Capability.CHAT, agent_scheduler_token=token,
        )
        wrapped = with_execution_timeout(agen, timeout_s=0.3)

        events: list[str] = []

        async def consume():
            async for event in wrapped:
                events.append(event)

        # Consumption runs as its own task so the wrapper's internal
        # __anext__()/deadline loop keeps advancing (driving agen all the
        # way into the gated goto() call) WHILE this test concurrently
        # observes the governor's real state — a plain sequential await
        # here would deadlock, since nothing would ever resume the
        # generator past the tool_call yield to actually reach goto().
        task = asyncio.create_task(consume())
        await asyncio.wait_for(goto_entered.wait(), timeout=5)
        assert _governor().active_count == 1, "browser must genuinely be in use at this point"

        await asyncio.wait_for(task, timeout=5)  # the wrapper's own 0.3s deadline ends this

    assert any("timed out" in e for e in events)
    assert scheduler.global_active_count == 0, "scheduler slot leaked after timeout mid-browser-op"
    assert _governor().active_count == 0, "browser governor slot leaked after timeout mid-browser-op"


# ── 3. Cancellation while the browser is genuinely in use ──────────────────

async def test_cancellation_while_browser_navigate_in_progress_releases_scheduler_and_governor(db, agent_session, monkeypatch):
    """Realistic equivalent of a client disconnect while a browser tool
    call is mid-flight — see module docstring for why this (not a direct
    aclose()) is the correct simulation here."""
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)

    async def _complete(request):
        return _browser_tool_call()

    monkeypatch.setattr(agent_service._router, "complete", _complete)

    goto_entered = asyncio.Event()
    never_releases = asyncio.Event()

    with patch(
        "trinity_agent.browser.session.async_playwright",
        new=_fake_async_playwright_factory(goto_entered, never_releases),
    ):
        token = scheduler.try_acquire("user-1")
        agen = agent_service.stream_agent(
            db, agent_session, "go to example.com", capability=Capability.CHAT, agent_scheduler_token=token,
        )
        wrapped = with_execution_timeout(agen, timeout_s=30.0)

        async def consume():
            async for _ in wrapped:
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(goto_entered.wait(), timeout=5)
        assert _governor().active_count == 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert scheduler.global_active_count == 0, "scheduler slot leaked after cancellation mid-browser-op"
    assert _governor().active_count == 0, "browser governor slot leaked after cancellation mid-browser-op"


# ── 4. Two sequential Agent+browser executions never deadlock or leak ──────

async def test_two_sequential_browser_executions_through_scheduler_never_deadlock(db, agent_session, monkeypatch):
    """A tight global cap of 1 on BOTH the scheduler and the (real,
    process-wide) BrowserResourceGovernor would deadlock permanently if
    either layer failed to release its slot after the first execution —
    the second execution could never acquire either."""
    scheduler = InMemoryAgentScheduler(global_max_concurrent=1, user_max_concurrent=1)

    call_count = {"n": 0}

    async def _complete(request):
        call_count["n"] += 1
        # Alternates: tool call, final answer, tool call, final answer, ...
        return _browser_tool_call() if call_count["n"] % 2 == 1 else _FakeRoutingResult()

    monkeypatch.setattr(agent_service._router, "complete", _complete)

    with patch("trinity_agent.browser.session.async_playwright", new=_fake_async_playwright_factory()):
        for i in range(2):
            token = scheduler.try_acquire("user-1")
            agen = agent_service.stream_agent(
                db, agent_session, f"go to example.com ({i})", capability=Capability.CHAT, agent_scheduler_token=token,
            )
            events = [e async for e in with_execution_timeout(agen, timeout_s=5.0)]
            assert any('"success": true' in e for e in events), f"iteration {i} browser tool call did not succeed"

    assert scheduler.global_active_count == 0
    assert _governor().active_count == 0
