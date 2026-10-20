"""The web research contract — what the Core, agents and the runtime may rely on. Stable; internal.

    request  = WebResearchRequest(urls=("https://…",), max_pages=2)
    response = await WebResearchCapability().run(request, actor_id=user.id)

    response.pages     WebPage per page read: title + sanitized markdown + counts
    response.refused   WebPageRefusal per page not read, with a stable `code`
    errors             WebResearchDisabled (switched off / nothing to extract with),
                       WebResearchRateLimited (too many requests for this actor),
                       WebResearchInvalid (malformed request)

Everything in a response is safe to show to the model and, later, to a person: no engine,
library, host internals, IP address or provider text. Refusal codes are a closed set.
"""
from __future__ import annotations

from dataclasses import dataclass, field

REFUSAL_CODES = frozenset({
    "blocked_destination",   # not a public web address (SSRF guard, at URL or connect time)
    "domain_not_allowed",    # the operator's domain policy
    "robots_disallowed",
    "too_large",
    "unsupported_type",
    "too_many_redirects",
    "bad_redirect",
    "http_error",
    "timeout",
    "unreachable",
    "extraction_failed",
})


@dataclass(frozen=True)
class WebResearchRequest:
    urls: tuple[str, ...]
    #: At most this many pages are read (never above the operator's WEB_RESEARCH_MAX_PAGES).
    max_pages: int | None = None
    #: Markdown kept per page (never above WEB_RESEARCH_MAX_MARKDOWN_CHARS).
    max_chars_per_page: int | None = None


@dataclass(frozen=True)
class WebPage:
    url: str
    final_url: str
    title: str
    content_markdown: str
    truncated: bool
    links_internal: int
    links_external: int
    size_bytes: int


@dataclass(frozen=True)
class WebPageRefusal:
    url: str
    code: str


@dataclass(frozen=True)
class WebResearchResponse:
    pages: tuple[WebPage, ...] = field(default_factory=tuple)
    refused: tuple[WebPageRefusal, ...] = field(default_factory=tuple)
    elapsed_ms: float = 0.0


class WebResearchError(Exception):
    code = "web_research_error"


class WebResearchDisabled(WebResearchError):
    code = "disabled"


class WebResearchRateLimited(WebResearchError):
    code = "rate_limited"


class WebResearchInvalid(WebResearchError):
    code = "invalid_request"
