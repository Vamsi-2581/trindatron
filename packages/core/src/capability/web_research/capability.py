"""WebResearchCapability — the one entry point: policy, limits and metrics around research().

    User/Agent -> WebResearchCapability.run() -> URL policy -> domain policy -> DNS/IP check at connect
               -> guarded fetch (redirects, robots, size, type, time) -> extractor (no network)
               -> sanitized WebPage -> caller

Off unless WEB_RESEARCH_ENABLED=true and an extractor is available. Not exposed to users or
agents yet: nothing imports this outside tests. Rate limit is per actor, in process
(single-instance deployment, like media jobs); a multi-instance deployment needs a shared
store before this is exposed.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque

from src.capability.web_research.contract import (
    REFUSAL_CODES,
    WebPage,
    WebPageRefusal,
    WebResearchDisabled,
    WebResearchInvalid,
    WebResearchRateLimited,
    WebResearchRequest,
    WebResearchResponse,
)
from src.capability.web_research.extract import ExtractionFailed, Extractor, sanitize
from src.capability.web_research.fetch import FetchRefused, fetch_page
from src.capability.web_research.service import default_extractor, web_research_settings
from src.observability.metrics import core_metrics

logger = logging.getLogger("trinity.web_research")

CAPABILITY = "web_research"
_WINDOW_S = 60.0

_requests = core_metrics.counter("web_research_requests_total", "Web research runs, labeled by outcome.")
_pages = core_metrics.counter("web_research_pages_total", "Pages handled, labeled by outcome/code.")
_latency_ms = core_metrics.histogram("web_research_latency_ms", "Web research run latency in milliseconds.")


def _domains(value: str) -> tuple[str, ...]:
    return tuple(part.strip().lower().lstrip(".") for part in (value or "").split(",") if part.strip())


def _matches(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def host_policy(host: str) -> bool:
    """The operator's domain policy: a blocked domain never; with an allowlist, only those domains."""
    settings = web_research_settings()
    host = (host or "").lower().rstrip(".")
    if _matches(host, _domains(settings.web_research_blocked_domains)):
        return False
    allowed = _domains(settings.web_research_allowed_domains)
    return not allowed or _matches(host, allowed)


class _RateLimiter:
    def __init__(self) -> None:
        self._calls: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, actor: str, per_minute: int, now: float) -> bool:
        calls = self._calls[actor]
        while calls and now - calls[0] > _WINDOW_S:
            calls.popleft()
        if len(calls) >= per_minute:
            return False
        calls.append(now)
        return True


_limiter = _RateLimiter()


class WebResearchCapability:
    name = CAPABILITY

    def __init__(self, *, extractor: Extractor | None = None, fetcher=fetch_page, clock=time.monotonic) -> None:
        self._extractor = extractor
        self._fetcher = fetcher
        self._clock = clock

    @property
    def extractor(self) -> Extractor:
        return self._extractor or default_extractor()

    def enabled(self) -> bool:
        return web_research_settings().web_research_enabled and self.extractor.available()

    async def run(self, request: WebResearchRequest, *, actor_id: str) -> WebResearchResponse:
        settings = web_research_settings()
        if not self.enabled():
            _requests.inc(outcome="disabled")
            raise WebResearchDisabled("web research is off")
        if not isinstance(request, WebResearchRequest) or not request.urls or not actor_id:
            _requests.inc(outcome="invalid")
            raise WebResearchInvalid("no urls")
        if not all(isinstance(url, str) and url for url in request.urls):
            _requests.inc(outcome="invalid")
            raise WebResearchInvalid("bad url")
        if not _limiter.allow(str(actor_id), max(1, settings.web_research_rate_per_minute), self._clock()):
            _requests.inc(outcome="rate_limited")
            raise WebResearchRateLimited("too many requests")

        max_pages = max(1, min(request.max_pages or settings.web_research_max_pages, settings.web_research_max_pages))
        max_chars = max(1, min(request.max_chars_per_page or settings.web_research_max_markdown_chars,
                               settings.web_research_max_markdown_chars))
        started = time.monotonic()
        pages: list[WebPage] = []
        refused: list[WebPageRefusal] = []
        for url in list(dict.fromkeys(request.urls))[:max_pages]:  # duplicates read once
            try:
                page = await self._fetcher(url, max_bytes=settings.web_research_max_bytes,
                                           max_redirects=settings.web_research_max_redirects,
                                           respect_robots=settings.web_research_respect_robots,
                                           timeout_s=settings.web_research_timeout_s,
                                           user_agent=settings.web_research_user_agent, host_policy=host_policy)
                extracted = await asyncio.wait_for(self.extractor.extract(page.html, page.final_url),
                                                   timeout=settings.web_research_timeout_s * 2)
            except FetchRefused as exc:
                code = exc.code if exc.code in REFUSAL_CODES else "unreachable"
                refused.append(WebPageRefusal(url=url, code=code))
                _pages.inc(outcome=code)
                continue
            except (ExtractionFailed, asyncio.TimeoutError):
                refused.append(WebPageRefusal(url=url, code="extraction_failed"))
                _pages.inc(outcome="extraction_failed")
                continue
            markdown = sanitize(extracted.markdown, max_chars)
            pages.append(WebPage(url=url, final_url=page.final_url, title=sanitize(extracted.title, 300),
                                 content_markdown=markdown, truncated=len(extracted.markdown) > max_chars,
                                 links_internal=extracted.links_internal, links_external=extracted.links_external,
                                 size_bytes=page.size_bytes))
            _pages.inc(outcome="read")
        elapsed_ms = (time.monotonic() - started) * 1000
        _requests.inc(outcome="completed")
        _latency_ms.observe(elapsed_ms, outcome="completed")
        return WebResearchResponse(pages=tuple(pages), refused=tuple(refused), elapsed_ms=elapsed_ms)
