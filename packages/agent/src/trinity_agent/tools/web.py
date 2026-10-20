"""BrowseUrl — canonical tool for fetching and reading a web page's text
content. Moved here from src/services/work_service.py (Core) during the
Tool/Connector Foundation etapa: this was previously a hand-rolled
function inside work_service.py's own dispatcher, never a BaseTool, and
never reachable from the Agent ReAct loop's tool set. work_service.py
now re-exports the SSRF guard below under its old names
(_assert_url_is_safe_to_fetch, UnsafeUrlError) as thin compatibility
aliases so its own existing security regression suite
(tests/security/test_ssrf_final_gate.py) keeps passing unchanged against
the SAME implementation, now living in one canonical place instead of
being duplicated if a second consumer ever needed it.

SSRF hardening (unchanged from its original work_service.py form):
resolves the hostname via the real resolver and classifies the ACTUAL
resulting IP with Python's ipaddress module (closing decimal-IP, hex-IP,
and IPv4-mapped-IPv6 encoding tricks), re-validated at every redirect hop
(follow_redirects is deliberately OFF; each Location header is manually
re-checked before being followed).
"""
from __future__ import annotations

import asyncio
import re

from .base import BaseTool, SideEffect, ToolResult

_MAX_OUTPUT_CHARS = 4_000
_MAX_URL_LEN = 2048
_MAX_REDIRECTS = 5


class UnsafeUrlError(Exception):
    """Raised by assert_url_is_safe_to_fetch() when a URL's resolved
    address is disallowed (private/loopback/link-local/reserved/
    multicast/unspecified) — SSRF hardening, see module docstring."""


async def assert_url_is_safe_to_fetch(url: str) -> None:
    """Reject any URL whose host resolves to a private/loopback/link-
    local/reserved/multicast/unspecified address. Callers MUST call this
    again before following each redirect — it says nothing about a URL
    reached via a Location header from a prior, already-validated fetch."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeUrlError(f"Unsupported URL scheme: {parsed.scheme!r}")
    hostname = parsed.hostname
    if not hostname:
        raise UnsafeUrlError("URL has no hostname")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"Could not resolve host {hostname!r}: {exc}") from exc

    for family, _type, _proto, _canonname, sockaddr in infos:
        raw_ip = sockaddr[0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            raise UnsafeUrlError(f"Resolved address is not a valid IP: {raw_ip!r}")
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            raise UnsafeUrlError(
                f"Host {hostname!r} resolves to a disallowed address ({ip}) — "
                "internal/private/loopback/link-local/reserved targets are never fetchable."
            )


class BrowseUrl(BaseTool):
    name = "browse_url"
    description = "Fetch and read the text content of a web page."
    side_effect = SideEffect.READ_ONLY
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
        },
        "required": ["url"],
    }

    async def run(self, url: str = "", **_) -> ToolResult:
        if not url:
            return ToolResult(success=False, error="URL is required")
        if len(url) > _MAX_URL_LEN:
            return ToolResult(success=False, error="URL too long")
        if not re.match(r"^https?://", url, re.IGNORECASE):
            return ToolResult(success=False, error="Only http/https URLs are supported")
        try:
            import httpx

            current_url = url
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                for _ in range(_MAX_REDIRECTS + 1):
                    await assert_url_is_safe_to_fetch(current_url)
                    resp = await client.get(current_url, headers={"User-Agent": "TRINITY/1.0 (research bot)"})
                    if resp.status_code in (301, 302, 303, 307, 308) and "location" in resp.headers:
                        from urllib.parse import urljoin
                        current_url = urljoin(current_url, resp.headers["location"])
                        continue
                    resp.raise_for_status()
                    ct = resp.headers.get("content-type", "")
                    if "html" in ct:
                        text = re.sub(r"<[^>]+>", " ", resp.text)
                        text = re.sub(r"\s+", " ", text).strip()
                    else:
                        text = resp.text
                    return ToolResult(success=True, output=text[:_MAX_OUTPUT_CHARS])
                return ToolResult(success=False, error="Too many redirects")
        except UnsafeUrlError:
            return ToolResult(success=False, error="This URL cannot be fetched.")
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))
