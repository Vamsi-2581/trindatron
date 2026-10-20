"""Web search: a real look at the live web, or an honest failure.

Why this is written by hand against DuckDuckGo's HTML endpoint rather than
through a search library: the library this tool used (`duckduckgo_search`) was
renamed upstream, and the installed shim answers every query with zero rows.
The tool reported that as `success=True` with "No results found.", which is the
worst possible failure — the agent cannot tell "the web has nothing" from "the
search never happened", so it falls back to what the model remembers and
presents it as current. A wrong answer that looks researched is worse than an
error.

So: one HTTP request, no API key, no account, and three distinguishable
outcomes — results, a genuinely empty result set, or a failure that says it
failed.
"""
from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlparse

import httpx

from .base import BaseTool, ToolResult

_ENDPOINT = "https://html.duckduckgo.com/html/"
_HEADERS = {
    # A plain browser UA: the endpoint serves a different (script-only) page to unknown clients.
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
}
_TIMEOUT_S = 20.0

_RESULT = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>'
    r'(?:.*?class="[^"]*result__snippet[^"]*"[^>]*>(?P<snippet>.*?)</a>)?',
    re.DOTALL | re.IGNORECASE,
)
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def _text(raw: str | None) -> str:
    return _SPACE.sub(" ", html.unescape(_TAG.sub(" ", raw or ""))).strip()


def _url(href: str) -> str:
    """The real destination. The endpoint wraps each result in its own redirect."""
    href = html.unescape(href or "")
    if href.startswith("//"):
        href = "https:" + href
    query = parse_qs(urlparse(href).query)
    target = query.get("uddg", [""])[0]
    return target or href


class WebSearch(BaseTool):
    name = "web_search"
    description = (
        "Search the live web and return the top results with their titles, links and snippets. "
        "Use this whenever the answer depends on what is true now rather than on general knowledge."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "max_results": {"type": "integer", "description": "Number of results (default 5, max 10)", "default": 5},
        },
        "required": ["query"],
    }
    timeout_seconds = _TIMEOUT_S

    async def run(self, query: str, max_results: int = 5, **_) -> ToolResult:
        query = (query or "").strip()
        if not query:
            return ToolResult(success=False, error="web search needs a query")
        max_results = min(max(1, int(max_results or 5)), 10)

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S, follow_redirects=True, headers=_HEADERS) as client:
                response = await client.post(_ENDPOINT, data={"q": query})
        except Exception as exc:
            return ToolResult(success=False, error=f"web search could not reach the search service ({type(exc).__name__})")

        if response.status_code != 200:
            return ToolResult(success=False, error=f"web search failed with status {response.status_code}")

        body = response.text
        results = []
        for match in _RESULT.finditer(body):
            title = _text(match.group("title"))
            url = _url(match.group("href"))
            if not title or not url:
                continue
            snippet = _text(match.group("snippet"))
            results.append(f"{len(results) + 1}. {title}\n   {url}" + (f"\n   {snippet}" if snippet else ""))
            if len(results) >= max_results:
                break

        if results:
            return ToolResult(success=True, output="\n\n".join(results))
        # No results and no result markup at all means the page was not the one this parser knows —
        # a failure of the search, not an empty web. Said as such, so nothing downstream reports a
        # search that did not happen.
        if "result__a" not in body and "result__snippet" not in body:
            return ToolResult(success=False, error="web search returned a response this tool could not read")
        return ToolResult(success=True, output=f'No results found for "{query}".')
