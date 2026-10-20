"""One guarded page fetch: every hop checked, every limit enforced, nothing followed blindly.

  - the URL passes guard.check_url, and every connection goes through the public-only network layer
  - redirects are followed by hand (at most `max_redirects`), each target checked again
  - robots.txt of the origin is honoured for our user agent
  - only HTML / plain text, at most `max_bytes` (streamed: a larger body is cut off, not read)
  - one overall deadline; no cookies, no credentials, no proxies from the environment
Logs carry the host only — never a query string, which may hold tokens.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from urllib import robotparser
from urllib.parse import urljoin, urlsplit

import httpx

from src.capability.web_research.guard import BlockedDestination, check_url, guarded_transport

logger = logging.getLogger("trinity.web_research")

TEXT_TYPES = frozenset({"text/html", "application/xhtml+xml", "text/plain"})
_ROBOTS_MAX_BYTES = 200_000


class FetchRefused(Exception):
    """The page was not fetched. `code` is stable and user-safe; `reason` is for logs."""

    def __init__(self, code: str, reason: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason or code


@dataclass(frozen=True)
class FetchedPage:
    url: str
    final_url: str
    status: int
    content_type: str
    html: str
    size_bytes: int
    redirects: int
    seconds: float


def _host(url: str) -> str:
    return urlsplit(url).hostname or "?"


async def _read_capped(response: httpx.Response, limit: int) -> bytes:
    chunks, total = [], 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise FetchRefused("too_large", f"body over {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def _robots_allows(client: httpx.AsyncClient, url: str, user_agent: str) -> bool:
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        async with client.stream("GET", robots_url) as response:
            if response.status_code >= 400:
                return True  # no robots.txt: nothing is disallowed
            if response.status_code >= 300:
                return True  # a redirected robots.txt is not followed; treated as absent
            body = await _read_capped(response, _ROBOTS_MAX_BYTES)
    except FetchRefused:
        return True
    parser = robotparser.RobotFileParser()
    parser.parse(body.decode("utf-8", "replace").splitlines())
    return parser.can_fetch(user_agent, url)


def _decode(body: bytes, response: httpx.Response) -> str:
    encoding = response.charset_encoding or "utf-8"
    try:
        return body.decode(encoding, "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def _allowed(url: str, host_policy) -> str:
    """check_url, then the caller's domain policy — applied to the first URL and to every redirect target."""
    checked = check_url(url)
    if host_policy is not None and not host_policy(urlsplit(checked).hostname or ""):
        raise FetchRefused("domain_not_allowed", f"{_host(checked)} not allowed by policy")
    return checked


async def _fetch(url: str, *, transport, max_bytes: int, max_redirects: int, respect_robots: bool,
                 user_agent: str, timeout_s: float, host_policy=None) -> FetchedPage:
    started = time.monotonic()
    current = _allowed(url, host_policy)
    headers = {"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8"}
    async with httpx.AsyncClient(transport=transport, headers=headers, follow_redirects=False, trust_env=False,
                                 timeout=httpx.Timeout(timeout_s)) as client:
        if respect_robots and not await _robots_allows(client, current, user_agent):
            raise FetchRefused("robots_disallowed", f"robots.txt disallows {_host(current)}")
        for hop in range(max_redirects + 1):
            async with client.stream("GET", current) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise FetchRefused("bad_redirect", "redirect without location")
                    current = _allowed(urljoin(current, location), host_policy)
                    continue
                if response.status_code != 200:
                    raise FetchRefused("http_error", f"HTTP {response.status_code} from {_host(current)}")
                content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if content_type not in TEXT_TYPES:
                    raise FetchRefused("unsupported_type", f"{content_type or 'no type'} from {_host(current)}")
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise FetchRefused("too_large", f"declared {declared} bytes")
                body = await _read_capped(response, max_bytes)
                return FetchedPage(url=url, final_url=current, status=200, content_type=content_type,
                                   html=_decode(body, response), size_bytes=len(body), redirects=hop,
                                   seconds=time.monotonic() - started)
        raise FetchRefused("too_many_redirects", f"more than {max_redirects} redirects")


async def fetch_page(url: str, *, transport_factory=guarded_transport, max_bytes: int = 2_000_000,
                     max_redirects: int = 3, respect_robots: bool = True, timeout_s: float = 15.0,
                     user_agent: str = "TRINDATRON-Research/0.1", host_policy=None) -> FetchedPage:
    """Fetch one public page within every limit, or raise FetchRefused. Never raises anything else."""
    try:
        return await asyncio.wait_for(
            _fetch(url, transport=transport_factory(), max_bytes=max_bytes, max_redirects=max_redirects,
                   respect_robots=respect_robots, user_agent=user_agent, timeout_s=timeout_s,
                   host_policy=host_policy),
            timeout=timeout_s * 2)
    except FetchRefused as exc:
        logger.info("web research: refused | code=%s reason=%s", exc.code, exc.reason)
        raise
    except BlockedDestination as exc:
        logger.info("web research: blocked | reason=%s", exc.reason)
        raise FetchRefused("blocked_destination", exc.reason) from exc
    except asyncio.TimeoutError as exc:
        raise FetchRefused("timeout", "overall deadline") from exc
    except httpx.ConnectError as exc:
        blocked = "blocked" in str(exc)
        logger.info("web research: %s | %s", "blocked at connect" if blocked else "connect failed", type(exc).__name__)
        raise FetchRefused("blocked_destination" if blocked else "unreachable", str(exc)[:120]) from exc
    except httpx.TimeoutException as exc:
        raise FetchRefused("timeout", type(exc).__name__) from exc
    except httpx.HTTPError as exc:
        raise FetchRefused("unreachable", type(exc).__name__) from exc
