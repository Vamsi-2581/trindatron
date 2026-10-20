"""What web research may fetch, enforced twice: before the request and when the socket connects.

The URL check refuses anything but http(s) on ports 80/443 to a hostname, with no
credentials in the URL. It cannot see where a name will resolve, so the network
layer resolves every connection itself and refuses it unless every address is
public (not private, loopback, link-local — cloud metadata included —, CGNAT,
multicast, reserved or unspecified), then connects to the address it checked.
A redirect, a DNS answer that changes between check and connect (rebinding), or a
hostname that points inside the network all end in BlockedDestination.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

import httpcore
import httpx

ALLOWED_SCHEMES = frozenset({"http", "https"})
ALLOWED_PORTS = frozenset({80, 443})
MAX_URL_LENGTH = 2048


class BlockedDestination(Exception):
    """The destination is not a public web address. `reason` is for logs; never shown to a user as-is."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped or address.sixtofour
        if mapped is not None:
            address = mapped
        elif address.teredo is not None:
            return False
    return bool(address.is_global) and not address.is_multicast


def check_url(url: str) -> str:
    """The URL, normalised, if it may be requested at all. Raises BlockedDestination otherwise."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        raise BlockedDestination("url missing or too long")
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise BlockedDestination("scheme not allowed")
    if parts.username or parts.password:
        raise BlockedDestination("credentials in url")
    host = (parts.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise BlockedDestination("no host")
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise BlockedDestination("bad port") from exc
    if port not in ALLOWED_PORTS:
        raise BlockedDestination("port not allowed")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        pass
    else:
        raise BlockedDestination("literal ip address")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        raise BlockedDestination("local hostname")
    if "." not in host:
        raise BlockedDestination("single-label hostname")
    return parts.geturl()


async def resolve_public(host: str, port: int) -> list[str]:
    """Every address `host` resolves to, if all are public. Raises BlockedDestination otherwise."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise BlockedDestination("does not resolve") from exc
    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise BlockedDestination("does not resolve")
    blocked = [address for address in addresses if not is_public_address(address)]
    if blocked:
        raise BlockedDestination("resolves to a non-public address")
    return addresses


class PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """Connects only to addresses it has just checked; TLS still verifies the original hostname."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None, resolver=resolve_public) -> None:
        self._inner = inner or httpcore.AnyIOBackend()
        self._resolve = resolver

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if port not in ALLOWED_PORTS:
            raise httpcore.ConnectError("blocked: port not allowed")
        try:
            addresses = await self._resolve(host, port)
        except BlockedDestination as exc:
            raise httpcore.ConnectError(f"blocked: {exc.reason}") from exc
        return await self._inner.connect_tcp(addresses[0], port, timeout=timeout, local_address=local_address,
                                             socket_options=socket_options)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("blocked: unix sockets")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def guarded_transport(resolver=resolve_public, inner: httpcore.AsyncNetworkBackend | None = None) -> httpx.AsyncHTTPTransport:
    """An httpx transport whose every connection goes through PublicOnlyBackend. No proxies from the environment."""
    transport = httpx.AsyncHTTPTransport(retries=0)
    transport._pool = httpcore.AsyncConnectionPool(  # httpx 0.28 keeps its httpcore pool here
        ssl_context=httpx.create_ssl_context(), max_connections=4, http1=True, http2=False, retries=0,
        network_backend=PublicOnlyBackend(inner, resolver),
    )
    return transport
