"""Outbound fetching for ingestion, with the SSRF guards from DESIGN §6.2.

- https only, and only to allow-listed hosts
- redirects are followed by hand, at most 3, and each target is re-checked
- the host's resolved addresses must all be public (no private, loopback, link-local or metadata
  addresses such as 169.254.169.254)
- a response is streamed and abandoned past `max_bytes`
- at least `min_interval_s` between requests to the same host, with an honest User-Agent

Not covered: DNS rebinding between our check and httpx2's own resolution. The allow-list is a few
government hosts, which keeps that window small. Pinning the resolved IP is a Phase 5 hardening item.
"""

import asyncio
import ipaddress
import socket
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx2

Resolver = Callable[[str], Awaitable[list[str]]]
RETRYABLE = {429, 500, 502, 503, 504}


class FetchRefused(RuntimeError):
    """The URL breaks a fetch rule. Never retried."""


class FetchFailed(RuntimeError):
    """The fetch was allowed but didn't succeed after retries."""


async def system_resolver(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


@dataclass(frozen=True)
class Fetched:
    url: str  # final URL after redirects
    status: int
    body: bytes
    content_type: str


@dataclass
class SafeFetcher:
    http: httpx2.AsyncClient
    allowed_hosts: frozenset[str]
    user_agent: str
    max_bytes: int = 5_000_000
    timeout_s: float = 60.0
    min_interval_s: float = 0.1
    attempts: int = 5
    resolver: Resolver = system_resolver
    _last: dict[str, float] = field(default_factory=dict)

    async def _check(self, url: str) -> str:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https":
            raise FetchRefused(f"not https: {url}")
        if host not in self.allowed_hosts:
            raise FetchRefused(f"host not allow-listed: {host}")
        for addr in await self.resolver(host):
            ip = ipaddress.ip_address(addr)
            if not ip.is_global or ip.is_multicast:
                raise FetchRefused(f"{host} resolves to non-public address {addr}")
        return host

    async def _pace(self, host: str) -> None:
        wait = self._last.get(host, float("-inf")) + self.min_interval_s - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last[host] = time.monotonic()

    async def _once(self, url: str) -> Fetched:
        for _ in range(4):  # the first request plus at most 3 redirects
            host = await self._check(url)
            await self._pace(host)
            req = self.http.build_request("GET", url, headers={"User-Agent": self.user_agent}, timeout=self.timeout_s)
            resp = await self.http.send(req, stream=True, follow_redirects=False)
            try:
                if resp.is_redirect:
                    url = urljoin(url, resp.headers.get("location", ""))
                    continue
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise FetchRefused(f"response over {self.max_bytes:,} bytes: {url}")
                    chunks.append(chunk)
                return Fetched(url, resp.status_code, b"".join(chunks), resp.headers.get("content-type", ""))
            finally:
                await resp.aclose()
        raise FetchRefused(f"too many redirects: {url}")

    async def get(self, url: str) -> Fetched:
        """GET with retries on 429/5xx and connection errors (exponential backoff). A refused URL or a
        4xx is not retried. Callers validate the content: a 200 can still be a block page."""
        delay = 1.0
        for attempt in range(1, self.attempts + 1):
            try:
                got = await self._once(url)
                if got.status not in RETRYABLE:
                    if got.status >= 400:
                        raise FetchFailed(f"HTTP {got.status} for {url}")
                    return got
                failure: str = f"HTTP {got.status}"
            except (httpx2.TransportError, httpx2.TimeoutException) as exc:
                failure = type(exc).__name__
            if attempt < self.attempts:
                await asyncio.sleep(delay)
                delay *= 2
        raise FetchFailed(f"{failure} after {self.attempts} attempts: {url}")


def allowed(hosts: Iterable[str]) -> frozenset[str]:
    return frozenset(h.strip().lower() for h in hosts)
