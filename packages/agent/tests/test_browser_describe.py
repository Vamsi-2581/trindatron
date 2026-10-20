"""BrowserSession.describe — the accessible role and name of an element, for approval prompts. Read-only and never
opens a page of its own."""
import asyncio

from trinity_agent.browser.session import BrowserSession, _SessionState


class _Locator:
    def __init__(self, result=None, error=None, delay=0.0):
        self.first, self._result, self._error, self._delay = self, result, error, delay

    async def evaluate(self, _script):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return self._result


class _Page:
    def __init__(self, locator):
        self._locator, self.asked = locator, []

    def locator(self, selector):
        self.asked.append(selector)
        return self._locator


def _ready(session, page):
    session._page, session._state = page, _SessionState.READY
    return session


def test_before_any_page_it_says_nothing_and_opens_nothing():
    session = BrowserSession("describe-new")
    assert asyncio.run(session.describe("a")) is None
    assert session._page is None


def test_it_returns_the_role_and_name_the_page_gives():
    page = _Page(_Locator({"role": "link", "name": "More information..."}))
    session = _ready(BrowserSession("describe-ok"), page)
    assert asyncio.run(session.describe("a")) == {"role": "link", "name": "More information..."}
    assert page.asked == ["a"]


def test_a_missing_element_or_a_slow_page_is_none_not_an_error():
    missing = _ready(BrowserSession("describe-missing"), _Page(_Locator(error=TimeoutError("no element"))))
    assert asyncio.run(missing.describe("#nope")) is None
    slow = _ready(BrowserSession("describe-slow"), _Page(_Locator({"role": "link", "name": "x"}, delay=0.5)))
    assert asyncio.run(slow.describe("a", timeout_s=0.05)) is None
