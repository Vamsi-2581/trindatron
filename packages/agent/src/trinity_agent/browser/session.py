"""BrowserSession — Playwright lifecycle owner, one per TRINITY session.

Lifecycle: Playwright -> Browser -> BrowserContext -> Page, created lazily on
the first call that needs a page. Never more than one Page per session (no
API in this class creates a second one). Never shares a BrowserContext
across sessions — callers must construct one BrowserSession per session_id
and must not reuse it across different sessions.

State machine (see _SessionState): NEW -> STARTING -> READY -> CLOSING ->
CLOSED, with a terminal FAILED state if startup itself fails. Two locks
enforce it, always acquired in this order (never the reverse, anywhere in
this class — that fixed order is what makes the locking deadlock-free):

  1. `_lifecycle_lock` — guards NEW/STARTING/READY/CLOSING/CLOSED/FAILED
     transitions. Held for the *entire* duration of a launch or of close(),
     which gives single-flight startup (a second concurrent ensure_page()
     simply waits, then observes READY and returns the same Page) and
     single-flight close (a second concurrent close() waits, then observes
     CLOSED and returns) for free, without extra bookkeeping.
  2. `_op_lock` — guards the actual Page operations (goto/evaluate/
     screenshot) so two operations on the same session's Page never run
     concurrently. close() also takes this (after `_lifecycle_lock`) so it
     waits for any in-flight operation to finish before tearing the Page
     down, rather than pulling it out from under an active call.

  navigate()/get_text()/screenshot() only ever hold one of the two locks at
  a time (never nested), so the only place both are held together is
  close() itself, always in the 1-then-2 order above.

close() is best-effort and idempotent: safe to call multiple times
(including concurrently), safe to call even if launch never completed or is
still in flight, and never raises — callers (e.g. a `finally` block around
an SSE stream) must be able to call it unconditionally without their own
try/except.

Capacity governance: a BrowserResourceGovernor slot is acquired before any
Chromium process is launched and held for the session's entire READY
lifetime, released exactly once during teardown. See governor.py.
"""
from __future__ import annotations

import asyncio
import enum
import logging
from typing import Awaitable, Callable, Coroutine
from urllib.parse import urljoin

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Route,
    async_playwright,
)

from .governor import BrowserCapacityTimeoutError, BrowserResourceGovernor, _SlotToken
from .network_policy import BlockedURLError, validate_request_url, validate_url_scheme

logger = logging.getLogger("trinity_agent.browser.session")

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

LAUNCH_TIMEOUT_MS = 10_000
NAVIGATION_TIMEOUT_MS = 30_000
EVALUATE_TIMEOUT_S = 10
SCREENSHOT_TIMEOUT_MS = 10_000
# Click/fill wait for the element to be actionable (visible, enabled,
# stable). Shorter than navigation: if a selector is not ready within
# this, the page is not in the state the agent believes it is, and
# failing fast is better than blocking a run on a wrong assumption.
INTERACTION_TIMEOUT_MS = 10_000

# Governor defaults used only when a caller doesn't supply its own governor
# (e.g. trinity_agent's own tests, or any future standalone consumer). Core
# passes its own settings-driven governor + timeouts into every
# BrowserSession it constructs (see agent_service.py) — these are a safe
# fallback, not the production configuration surface.
DEFAULT_GOVERNOR_CAPACITY = 4
CAPACITY_WAIT_TIMEOUT_S = 30.0
SHUTDOWN_TIMEOUT_S = 15.0

UrlValidator = Callable[[str], Awaitable[None]]

_default_governor: BrowserResourceGovernor | None = None


def _get_default_governor() -> BrowserResourceGovernor:
    global _default_governor
    if _default_governor is None:
        _default_governor = BrowserResourceGovernor(capacity=DEFAULT_GOVERNOR_CAPACITY)
    return _default_governor


class BrowserSessionError(Exception):
    """Raised when the browser cannot be launched, is used before/after its
    usable window, or a launch previously failed terminally."""


class _SessionState(enum.Enum):
    NEW = "new"
    STARTING = "starting"
    READY = "ready"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


class BrowserSession:
    def __init__(
        self,
        session_id: str,
        *,
        governor: BrowserResourceGovernor | None = None,
        capacity_wait_timeout_s: float | None = CAPACITY_WAIT_TIMEOUT_S,
        shutdown_timeout_s: float = SHUTDOWN_TIMEOUT_S,
        launch_timeout_ms: float = LAUNCH_TIMEOUT_MS,
        navigation_timeout_ms: float = NAVIGATION_TIMEOUT_MS,
        evaluate_timeout_s: float = EVALUATE_TIMEOUT_S,
        screenshot_timeout_ms: float = SCREENSHOT_TIMEOUT_MS,
        interaction_timeout_ms: float = INTERACTION_TIMEOUT_MS,
        url_validator: UrlValidator | None = None,
    ) -> None:
        self._session_id = session_id
        self._governor = governor or _get_default_governor()
        self._capacity_wait_timeout_s = capacity_wait_timeout_s
        self._shutdown_timeout_s = shutdown_timeout_s
        self._launch_timeout_ms = launch_timeout_ms
        self._navigation_timeout_ms = navigation_timeout_ms
        self._evaluate_timeout_s = evaluate_timeout_s
        self._screenshot_timeout_ms = screenshot_timeout_ms
        self._interaction_timeout_ms = interaction_timeout_ms
        # Overridable only for tests exercising session/route mechanics against
        # a local test server; production code must always use the default
        # (real DNS + global-IP policy from network_policy.py).
        self._url_validator: UrlValidator = url_validator or validate_request_url

        self._state = _SessionState.NEW
        self._lifecycle_lock = asyncio.Lock()
        self._op_lock = asyncio.Lock()

        self._slot_token: _SlotToken | None = None
        self._teardown_task: asyncio.Task | None = None
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def state(self) -> str:
        """String state name — read-only introspection for tests/diagnostics."""
        return self._state.value

    async def ensure_page(self) -> Page:
        """Return this session's single Page, launching the browser on first call.

        Idempotent: a second call returns the same Page instance without
        launching anything new — this, plus the fact that no other API on
        this class can create a page, is the enforcement mechanism for "at
        most one Page per BrowserSession" and for single-flight startup
        (two concurrent callers serialize on `_lifecycle_lock`; the loser
        just observes READY and returns).
        """
        async with self._lifecycle_lock:
            if self._state is _SessionState.READY:
                return self._page
            if self._state in (_SessionState.CLOSING, _SessionState.CLOSED):
                raise BrowserSessionError(
                    f"BrowserSession {self._session_id!r} is closed"
                )
            if self._state is _SessionState.FAILED:
                # No automatic recovery: a session that failed to start once
                # stays failed. Retrying transparently would risk silently
                # reacquiring a governor slot and repeatedly hammering a
                # host/environment that is genuinely broken (e.g. Chromium
                # missing system deps) on every subsequent tool call in the
                # same conversation. Callers get a new BrowserSession per
                # stream_agent() execution anyway, so this only matters
                # within a single already-degraded run.
                raise BrowserSessionError(
                    f"BrowserSession {self._session_id!r} previously failed to "
                    "start and is not retried automatically"
                )

            assert self._state is _SessionState.NEW
            self._state = _SessionState.STARTING
            logger.info("browser startup: begin (session=%s)", self._session_id)
            try:
                self._slot_token = await self._governor.acquire(
                    timeout=self._capacity_wait_timeout_s
                )
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    timeout=self._launch_timeout_ms,
                )
                self._context = await self._browser.new_context()
                page = await self._context.new_page()
                page.set_default_navigation_timeout(self._navigation_timeout_ms)
                await page.route("**/*", self._on_route)
                self._page = page
                self._state = _SessionState.READY
                logger.info("browser startup: ready (session=%s)", self._session_id)
                return page
            except BaseException as exc:
                self._state = _SessionState.FAILED
                logger.warning(
                    "browser startup: failed (session=%s): %s",
                    self._session_id, type(exc).__name__,
                )
                # Shares the exact same single-flight, shielded teardown
                # machinery as close() (see _ensure_teardown_started_and_wait)
                # — a failed launch and a concurrent/later close() can never
                # each run their own teardown pass.
                await self._ensure_teardown_started_and_wait()
                if isinstance(exc, (BrowserCapacityTimeoutError, asyncio.CancelledError)):
                    raise
                raise BrowserSessionError(
                    f"Failed to launch browser session {self._session_id!r}: {exc}"
                ) from exc

    async def _on_route(self, route: Route) -> None:
        try:
            await self._url_validator(route.request.url)
        except BlockedURLError:
            await route.abort("blockedbyclient")
            return
        # Playwright calls this handler only for the FIRST request of a redirect
        # chain: after route.continue_() Chromium follows a 302 to any host —
        # cloud metadata, the private network — without coming back here. So the
        # request is made without following redirects, the Location is checked
        # with the same validator, and a fulfilled 3xx makes the browser issue
        # the next hop as a new request, which is validated here again.
        try:
            response = await route.fetch(max_redirects=0)
        except Exception:
            await route.abort("failed")
            return
        if response.status in _REDIRECT_STATUSES:
            location = response.headers.get("location")
            if location:
                try:
                    await self._url_validator(urljoin(route.request.url, location))
                except BlockedURLError:
                    await route.abort("blockedbyclient")
                    return
        await route.fulfill(response=response)

    async def navigate(self, url: str) -> Page:
        """Navigate the session's page to `url`.

        Scheme/credentials are checked synchronously up front (blocks file:,
        javascript:, data:, about:, and non-http(s) schemes outright, since
        some of these never produce an interceptable network request). Host/IP
        validation for the actual HTTP(S) request — and for every redirect it
        may produce — happens in the route handler registered by
        ensure_page(), not here.
        """
        validate_url_scheme(url)
        page = await self.ensure_page()
        logger.debug("browser operation: navigate begin (session=%s)", self._session_id)
        async with self._op_lock:
            self._require_ready()
            try:
                await page.goto(url, timeout=self._navigation_timeout_ms)
            except Exception:
                logger.warning(
                    "browser operation: navigate failed (session=%s)", self._session_id,
                )
                raise
        return page

    async def get_text(self) -> str:
        if self._state is _SessionState.NEW:
            raise BrowserSessionError("Page not initialized — call navigate() first")
        self._require_ready()
        async with self._op_lock:
            self._require_ready()
            try:
                return await asyncio.wait_for(
                    self._page.evaluate("() => document.body.innerText"),
                    timeout=self._evaluate_timeout_s,
                )
            except Exception:
                logger.warning(
                    "browser operation: get_text failed (session=%s)", self._session_id,
                )
                raise

    async def screenshot(self, path: str) -> bytes:
        if self._state is _SessionState.NEW:
            raise BrowserSessionError("Page not initialized — call navigate() first")
        self._require_ready()
        async with self._op_lock:
            self._require_ready()
            try:
                return await self._page.screenshot(path=path, timeout=self._screenshot_timeout_ms)
            except Exception:
                logger.warning(
                    "browser operation: screenshot failed (session=%s)", self._session_id,
                )
                raise

    async def click(self, selector: str) -> None:
        """Click the element matching `selector` on the current page.

        Same guarantees as every other operation here: the page must already
        exist (so a click cannot be used to reach a URL that never passed
        navigation checks), the op lock serialises it against concurrent
        work, and it is bounded by a timeout.

        A click that triggers navigation stays inside this page and this
        context, so the route handler installed by ensure_page() validates
        the resulting request exactly as it validates a direct navigate() —
        the network policy is not bypassable this way.
        """
        if self._state is _SessionState.NEW:
            raise BrowserSessionError("Page not initialized — call navigate() first")
        self._require_ready()
        async with self._op_lock:
            self._require_ready()
            try:
                await self._page.click(selector, timeout=self._interaction_timeout_ms)
            except Exception:
                logger.warning(
                    "browser operation: click failed (session=%s)", self._session_id,
                )
                raise

    async def describe(self, selector: str, *, timeout_s: float = 1.5) -> dict[str, str] | None:
        """What the element `selector` matches is, as a person would name it: {"role", "name"}.

        Read-only and best effort, for approval prompts — "Clicar no link “More information”" instead of
        "Clicar em 'a'". Looks at the page that is already open and never opens one; takes no op lock, since it
        reads and never acts; gives up after `timeout_s`. None when there is no page yet, nothing matches, or the
        page does not answer in time.
        """
        if self._state is _SessionState.NEW or self._page is None:
            return None
        script = """(el) => {
            const tag = el.tagName.toLowerCase();
            const type = (el.getAttribute('type') || '').toLowerCase();
            const role = el.getAttribute('role') || (tag === 'a' ? 'link' : tag === 'button' || type === 'submit' || type === 'button' ? 'button'
                : tag === 'input' || tag === 'textarea' || tag === 'select' ? 'field' : 'element');
            const label = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
            const name = el.getAttribute('aria-label') || (label && label.innerText) || el.innerText
                || el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('alt') || el.getAttribute('value') || '';
            return {role, name: name.replace(/\\s+/g, ' ').trim().slice(0, 80)};
        }"""
        try:
            described = await asyncio.wait_for(self._page.locator(selector).first.evaluate(script), timeout=timeout_s)
        except Exception:
            return None
        if not isinstance(described, dict):
            return None
        return {"role": str(described.get("role") or "element"), "name": str(described.get("name") or "")}

    async def fill(self, selector: str, value: str) -> None:
        """Type `value` into the field matching `selector`.

        Playwright's fill() clears the field and sets the value directly
        rather than replaying keystrokes — no key handler can observe a
        partial value, and the selector must resolve to an editable element
        or this raises instead of typing somewhere unintended.
        """
        if self._state is _SessionState.NEW:
            raise BrowserSessionError("Page not initialized — call navigate() first")
        self._require_ready()
        async with self._op_lock:
            self._require_ready()
            try:
                await self._page.fill(selector, value, timeout=self._interaction_timeout_ms)
            except Exception:
                logger.warning(
                    "browser operation: fill failed (session=%s)", self._session_id,
                )
                raise

    def _require_ready(self) -> None:
        if self._state is not _SessionState.READY:
            raise BrowserSessionError(
                f"BrowserSession {self._session_id!r} is not ready (state={self._state.value})"
            )

    async def close(self) -> None:
        """Best-effort, idempotent teardown — never raises, safe to call
        repeatedly and safe to call concurrently.

        Exactly one teardown task is ever created per session (tracked in
        `_teardown_task`, created under `_lifecycle_lock` so two racing
        callers can't each spawn their own): every caller of close(),
        however many there are and whenever they arrive, shields onto and
        awaits that same task instead of re-running the teardown sequence.

        That single task is itself shielded from cancellation: if a caller
        of close() is cancelled while waiting, the teardown is NOT abandoned
        partway through (which would risk leaving later resources — or the
        governor slot — unreleased in this call) and NOT cancelled into a
        possibly-inconsistent Playwright IPC state. It keeps running to
        completion and flips the session to CLOSED via its own done-callback
        whenever it actually finishes, independently of whether any caller
        stuck around to await it.
        """
        async with self._lifecycle_lock:
            if self._state is _SessionState.CLOSED:
                return
            if self._state is not _SessionState.FAILED:
                self._state = _SessionState.CLOSING
            await self._ensure_teardown_started_and_wait()

    async def _ensure_teardown_started_and_wait(self) -> None:
        """Create the session's one and only teardown task if it doesn't
        exist yet, then shield onto and await it.

        Shared by close() and ensure_page()'s startup-failure path — always
        called with `_lifecycle_lock` already held, so "check _teardown_task,
        maybe create it" is itself single-flight: a failed launch and a
        concurrent/later close() can never each spawn their own teardown
        pass, they always converge on the same one.
        """
        if self._teardown_task is None:
            self._teardown_task = asyncio.ensure_future(self._teardown_under_op_lock())
            self._teardown_task.add_done_callback(self._on_teardown_done)
        try:
            await asyncio.shield(self._teardown_task)
        except asyncio.CancelledError:
            logger.debug(
                "browser teardown: cancelled while waiting (session=%s); "
                "teardown continues in background",
                self._session_id,
            )
            raise

    async def _teardown_under_op_lock(self) -> None:
        """The actual teardown task body — holds `_op_lock` for its real
        duration (released only when teardown genuinely finishes), so an
        operation that grabs `_op_lock` right as teardown starts still runs
        to completion first, and no new operation can slip in mid-teardown."""
        async with self._op_lock:
            await self._teardown_resources()

    def _on_teardown_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return  # only possible if the process itself is shutting down
        exc = task.exception()
        if exc is not None:
            # _teardown_resources() is designed to never raise — surface
            # unexpectedly if it somehow does, but still finalize state:
            # every individual resource close is already independently
            # best-effort, so nothing here can leave a resource unaccounted.
            logger.warning(
                "browser teardown: task raised unexpectedly (session=%s): %s",
                self._session_id, exc,
            )
        if self._state is _SessionState.CLOSING:
            self._state = _SessionState.CLOSED
            logger.info("browser session closed (session=%s)", self._session_id)
        # else: state is FAILED (terminal) — teardown just finished releasing
        # whatever partial resources existed; the FAILED verdict itself,
        # and its "not retried automatically" behavior, is unchanged.

    async def _teardown_resources(self) -> None:
        """Best-effort teardown of whatever Playwright resources exist, in
        the correct order, individually guarded so one failure doesn't
        prevent the rest from running, and each bounded by
        `_shutdown_timeout_s` without ever cancelling the underlying close
        call itself (see _run_with_shutdown_timeout). Never raises."""
        for attr, method_name in (
            ("_page", "close"),
            ("_context", "close"),
            ("_browser", "close"),
            ("_playwright", "stop"),
        ):
            resource = getattr(self, attr)
            if resource is None:
                continue
            setattr(self, attr, None)
            try:
                await self._run_with_shutdown_timeout(
                    getattr(resource, method_name)(), label=f"{attr}.{method_name}"
                )
            except Exception:
                pass  # best-effort — one resource failing to close must not block the rest

        if self._slot_token is not None:
            self._slot_token.release()
            self._slot_token = None

    async def _run_with_shutdown_timeout(self, coro: Coroutine, *, label: str) -> None:
        """Wait for `coro` up to `_shutdown_timeout_s`, WITHOUT cancelling it
        if the timeout fires or if this call itself is cancelled.

        Playwright's close()/stop() calls accept no native timeout parameter
        (checked directly against the installed version — Page.close,
        BrowserContext.close, Browser.close, Playwright.stop all take no
        `timeout` kwarg), and cancelling one of them mid-flight could leave
        its internal IPC/transport in an inconsistent state that then
        corrupts a *later* close attempt — exactly the "naive wait_for"
        failure mode this is deliberately avoiding. Instead: wrap `coro` in
        its own Task and asyncio.shield() it, so neither our own timeout nor
        an external cancellation of this call ever reaches the underlying
        close — we only ever stop *waiting* for it. If we give up waiting,
        the task keeps running to completion in the background; its result
        (success or failure) is swallowed via a done-callback so asyncio
        doesn't log a spurious "exception was never retrieved" warning for
        an outcome nobody is in a position to act on anymore.
        """
        task = asyncio.ensure_future(coro)
        task.add_done_callback(lambda t: self._swallow_task_result(t, label))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self._shutdown_timeout_s)
        except asyncio.TimeoutError:
            logger.warning(
                "browser shutdown: %s exceeded %.1fs, detaching (session=%s)",
                label, self._shutdown_timeout_s, self._session_id,
            )
        except asyncio.CancelledError:
            logger.debug(
                "browser shutdown: %s wait cancelled, detaching (session=%s)",
                label, self._session_id,
            )
            raise

    def _swallow_task_result(self, task: asyncio.Task, label: str) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.debug(
                "browser shutdown: detached %s finished with error (session=%s): %s",
                label, self._session_id, exc,
            )
