"""research(url): guard -> fetch -> extract, bounded. Internal; not exposed to users or agents yet."""
from __future__ import annotations

import time
from dataclasses import dataclass
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from src.capability.web_research.extract import Crawl4AIExtractor, ExtractionFailed, Extractor, sanitize
from src.capability.web_research.fetch import FetchRefused, fetch_page


class WebResearchSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Off until switched on AND an extractor exists. Not exposed to users or agents yet.
    web_research_enabled: bool = False
    # Crawl4AI's own interpreter (a separate venv). Empty = extraction unavailable.
    web_research_crawl4ai_python: str = ""
    # Comma-separated domains (subdomains included). Blocked always wins; an allowlist restricts to those.
    web_research_allowed_domains: str = ""
    web_research_blocked_domains: str = ""
    web_research_rate_per_minute: int = 10
    web_research_max_bytes: int = 2_000_000
    web_research_timeout_s: float = 15.0
    web_research_max_redirects: int = 3
    web_research_max_pages: int = 3
    web_research_max_markdown_chars: int = 20_000
    web_research_respect_robots: bool = True
    web_research_user_agent: str = "TRINDATRON-Research/0.1"


@lru_cache
def web_research_settings() -> WebResearchSettings:
    return WebResearchSettings()


def default_extractor() -> Extractor:
    return Crawl4AIExtractor(web_research_settings().web_research_crawl4ai_python)


@dataclass(frozen=True)
class ResearchResult:
    url: str
    final_url: str
    title: str
    markdown: str
    truncated: bool
    links_internal: int
    links_external: int
    size_bytes: int
    fetch_seconds: float
    extract_seconds: float


class ResearchUnavailable(Exception):
    """No extractor is configured: the capability is off."""


async def research(url: str, *, extractor: Extractor | None = None, fetcher=fetch_page) -> ResearchResult:
    """One public page as clean markdown. Raises FetchRefused, ExtractionFailed or ResearchUnavailable."""
    settings = web_research_settings()
    extractor = extractor or default_extractor()
    if not extractor.available():
        raise ResearchUnavailable("no extractor configured")
    page = await fetcher(url, max_bytes=settings.web_research_max_bytes,
                         max_redirects=settings.web_research_max_redirects,
                         respect_robots=settings.web_research_respect_robots,
                         timeout_s=settings.web_research_timeout_s, user_agent=settings.web_research_user_agent)
    started = time.monotonic()
    extracted = await extractor.extract(page.html, page.final_url)
    limit = settings.web_research_max_markdown_chars
    markdown = sanitize(extracted.markdown, limit)
    return ResearchResult(url=url, final_url=page.final_url, title=sanitize(extracted.title, 300), markdown=markdown,
                          truncated=len(extracted.markdown) > limit, links_internal=extracted.links_internal,
                          links_external=extracted.links_external, size_bytes=page.size_bytes,
                          fetch_seconds=page.seconds, extract_seconds=time.monotonic() - started)


async def research_many(urls: list[str], **kwargs) -> list[ResearchResult | Exception]:
    """At most `web_research_max_pages` pages, one at a time; a refused page does not stop the others."""
    results: list[ResearchResult | Exception] = []
    for url in urls[: web_research_settings().web_research_max_pages]:
        try:
            results.append(await research(url, **kwargs))
        except (FetchRefused, ExtractionFailed, ResearchUnavailable) as exc:
            results.append(exc)
    return results
