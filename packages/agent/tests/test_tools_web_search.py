"""web_search must answer with the live web, or say it could not.

The tool it replaces called a library that had been renamed upstream: every
query returned zero rows, and the tool reported that as success with "No
results found." The agent then answered from what the model remembered and
presented it as current — a researched-looking wrong answer, which is worse
than an error. These tests pin the three outcomes apart.
"""
from __future__ import annotations

import pytest

from trinity_agent.tools import search as search_module
from trinity_agent.tools.search import WebSearch

_PAGE = """
<div class="result results_links">
  <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fone&amp;rut=x">
    First <b>result</b>
  </a>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fone">
    Something that <b>happened</b> in 2026.
  </a>
</div>
<div class="result results_links">
  <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Ftwo">Second result</a>
  <a class="result__snippet" href="#">Another snippet.</a>
</div>
"""


class _Response:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class _Client:
    def __init__(self, response: _Response | None = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *_a, **_k):
        if self._error is not None:
            raise self._error
        return self._response


def _serving(response: _Response | None = None, error: Exception | None = None):
    def factory(*_a, **_k):
        return _Client(response, error)
    return factory


@pytest.mark.asyncio
async def test_results_carry_title_real_url_and_snippet(monkeypatch):
    monkeypatch.setattr(search_module.httpx, "AsyncClient", _serving(_Response(200, _PAGE)))
    result = await WebSearch().run(query="what happened in 2026")

    assert result.success
    assert "First result" in result.output
    # The endpoint wraps every link in its own redirect; what reaches the model is where it goes.
    assert "https://example.com/one" in result.output
    assert "duckduckgo.com/l/" not in result.output
    assert "Something that happened in 2026." in result.output
    assert "Second result" in result.output


@pytest.mark.asyncio
async def test_max_results_is_respected(monkeypatch):
    monkeypatch.setattr(search_module.httpx, "AsyncClient", _serving(_Response(200, _PAGE)))
    result = await WebSearch().run(query="q", max_results=1)
    assert "First result" in result.output and "Second result" not in result.output


@pytest.mark.asyncio
async def test_an_empty_result_set_is_success_and_says_so(monkeypatch):
    """The page came back in the shape this parser knows and simply held nothing."""
    empty = '<div class="results"><div class="no-results">nothing</div><span class="result__snippet"></span></div>'
    monkeypatch.setattr(search_module.httpx, "AsyncClient", _serving(_Response(200, empty)))
    result = await WebSearch().run(query="asdkjhqwe")
    assert result.success and "No results found" in result.output


@pytest.mark.asyncio
async def test_an_unreadable_page_is_a_failure_not_an_empty_web(monkeypatch):
    """This is the exact shape of the old bug: nothing parsed, reported as a clean empty result."""
    monkeypatch.setattr(search_module.httpx, "AsyncClient", _serving(_Response(200, "<html>not the search page</html>")))
    result = await WebSearch().run(query="q")
    assert not result.success and result.error


@pytest.mark.asyncio
async def test_a_bad_status_is_a_failure(monkeypatch):
    monkeypatch.setattr(search_module.httpx, "AsyncClient", _serving(_Response(503, "")))
    result = await WebSearch().run(query="q")
    assert not result.success and "503" in result.error


@pytest.mark.asyncio
async def test_a_network_error_is_a_failure(monkeypatch):
    monkeypatch.setattr(search_module.httpx, "AsyncClient", _serving(error=TimeoutError("no route")))
    result = await WebSearch().run(query="q")
    assert not result.success and result.error


@pytest.mark.asyncio
async def test_an_empty_query_never_reaches_the_network(monkeypatch):
    def explode(*_a, **_k):
        raise AssertionError("must not call out for an empty query")

    monkeypatch.setattr(search_module.httpx, "AsyncClient", explode)
    result = await WebSearch().run(query="   ")
    assert not result.success
