"""Network/SSRF policy for the browser tools.

Two independent checks, both required — neither is sufficient alone:

1. validate_url_scheme() — synchronous, cheap. Rejects file:/javascript:/data:/
   about: and any scheme other than http/https, and rejects embedded
   user:password@ credentials. This catches schemes that may never produce an
   interceptable network request in the first place (e.g. data:, javascript:),
   so it MUST run before any navigation is attempted — a route-interception
   check alone would miss these.

2. resolve_and_validate_host() — asynchronous. Resolves the hostname via real
   DNS and rejects the request if ANY resolved address is not a global/public
   IP (loopback, RFC1918, link-local incl. 169.254.169.254, IPv6 ULA/link-local,
   IPv4-mapped IPv6 equivalents of the above). This is deliberately NOT a
   one-time pre-check before goto(): BrowserSession registers a route handler
   that calls validate_request_url() for EVERY network request a page makes,
   including redirects and sub-resources. Resolving fresh at each request is
   the mitigation for DNS rebinding (TOCTOU between check and connect is
   minimized to a single round-trip, not eliminated by a stale early check)
   and for redirect-to-private-target bypasses (the redirect target is a new
   request, so it goes through the same handler and is validated on its own).

Residual risk (documented, not fixed by this module): Playwright's route API
does not expose a way to pin the actual TCP connection to the specific IP we
validated here — Chromium still performs its own connection/resolution after
route.continue_() lets the request through. A sufficiently fast rebinding
attack timed between our check and Chromium's connect is not eliminated by
this design, only narrowed to that single request's round-trip. True pinning
would require a local forwarding proxy, out of scope for this step.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = frozenset({"http", "https"})


class BlockedURLError(Exception):
    """Raised when a URL or resolved address is rejected by the browser network policy."""


def validate_url_scheme(url: str) -> None:
    """Cheap, synchronous checks that do not require any network I/O.

    Rejects non-http(s) schemes (file:, javascript:, data:, about:, and any
    other scheme) and URLs carrying embedded `user:password@host` credentials.
    """
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise BlockedURLError(f"scheme not allowed: {parsed.scheme!r}")
    if parsed.username or parsed.password:
        raise BlockedURLError("URLs with embedded credentials are not allowed")
    if not parsed.hostname:
        raise BlockedURLError("URL has no hostname")


def _is_globally_routable(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for public/global addresses — everything else is blocked.

    Allowlist, not blocklist: ipaddress.is_global already excludes private
    (RFC1918), loopback, link-local (incl. 169.254.169.254), multicast,
    reserved, and unspecified ranges for both IPv4 and IPv6 (incl. ULA
    fc00::/7 and link-local fe80::/10). IPv4-mapped IPv6 addresses
    (::ffff:127.0.0.1) are unwrapped and checked as their embedded IPv4.
    """
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        ip_obj = ip_obj.ipv4_mapped
    return ip_obj.is_global


async def resolve_and_validate_host(hostname: str, port: int) -> None:
    """Resolve `hostname` via real DNS and reject if any resolved address is non-public.

    Runs the blocking socket.getaddrinfo() call in a thread so this coroutine
    never blocks the event loop. Intended to be called fresh for every single
    request (see module docstring) — never cached across requests.
    """
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, hostname, port, type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise BlockedURLError(f"DNS resolution failed for {hostname!r}: {exc}") from exc

    if not infos:
        raise BlockedURLError(f"DNS resolution returned no addresses for {hostname!r}")

    for _family, _type, _proto, _canonname, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError as exc:
            raise BlockedURLError(f"Unparseable resolved address {ip_str!r}") from exc
        if not _is_globally_routable(ip_obj):
            raise BlockedURLError(
                f"{hostname!r} resolves to non-public address {ip_str!r}"
            )


async def validate_request_url(url: str) -> None:
    """Full validation for a single outgoing request: scheme + fresh DNS/IP check.

    Call this for every request (initial navigation, every redirect hop, and
    every sub-resource) — never only once before the first goto().
    """
    validate_url_scheme(url)
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    await resolve_and_validate_host(parsed.hostname, port)
