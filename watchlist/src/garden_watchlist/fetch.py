"""The fetcher: the most exposed code here, reading hostile content from URLs an
agent chose.

- Only http and https, only public addresses. The connection itself goes to an
  address resolved and checked here (PublicOnlyBackend), so a name that resolves
  differently between a check and the connect gains nothing. Redirects are
  followed by hand, each checked again, at most MAX_REDIRECTS.
- Polite: one user agent naming the project, robots.txt honoured for it (and its
  Crawl-delay), at least HOST_SPACING seconds between two requests to one host, a
  pause after a 429 or 503, conditional requests reused.
- Bounded: a timeout per request, a cap on every body.
- No cookies, no credentials, no script: a challenge or waiting room is a failure.

Agents can reach the stack network themselves, so the address rule is not their
boundary: it keeps a scheduled job from walking the stack's services or the host.
"""

from __future__ import annotations

import ipaddress
import math
import socket
import ssl
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib import robotparser
from urllib.parse import urljoin, urlsplit

import anyio
import httpcore
import httpx

from .config import (
    HOST_BACKOFF,
    HOST_SPACING,
    MAX_CRAWL_DELAY,
    MAX_REDIRECTS,
    MAX_RESPONSE,
    REQUEST_TIMEOUT,
    ROBOTS_KEEP,
    ROBOTS_RETRY,
    USER_AGENT,
    USER_AGENT_NAME,
)

Resolver = Callable[[str, int], Awaitable[list[str]]]

# Markers of a bot challenge, waiting room or CAPTCHA served instead of content.
CHALLENGE_MARKERS = (
    b"/cdn-cgi/challenge-platform/",
    b"cf-chl-",
    b"<title>just a moment",
    b"queue-it.net",
    b"queue-it_",
    b"g-recaptcha",
    b"h-captcha",
    b"captcha-delivery.com",
    b"_incapsula_resource",
    b"px-captcha",
)
WAITING_ROOM_HOSTS = ("queue-it.net",)


class FetchError(Exception):
    """A fetch that yields nothing; the message says why, in a few words."""


class HostBusy(FetchError):
    """The host asked for a longer pause than this call may wait: try again later.
    Never cached as the host's robots.txt answer."""


class BlockedAddress(Exception):
    pass


def is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def system_resolver(host: str, port: int) -> list[str]:
    infos = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


class PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """Connects only to public addresses: the name is resolved here, every address
    checked, and the connection made to the checked address. TLS still verifies
    the name (httpcore passes the URL's host as server_hostname)."""

    def __init__(self, resolver: Resolver = system_resolver, inner: httpcore.AsyncNetworkBackend | None = None):
        self._resolve = resolver
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            addresses = await self._resolve(host, port)
        except OSError as e:
            raise httpcore.ConnectError(f"cannot resolve {host}") from e
        if not addresses:
            raise httpcore.ConnectError(f"cannot resolve {host}")
        if not all(is_public(a) for a in addresses):
            raise BlockedAddress(host)
        last: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                last = e
        assert last is not None
        raise last

    async def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options: Any = None) -> Any:
        raise BlockedAddress("unix socket")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _Stream(httpx.AsyncByteStream):
    def __init__(self, response: httpcore.Response):
        self._response = response

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._response.aiter_stream():
            yield chunk

    async def aclose(self) -> None:
        await self._response.aclose()


class PublicTransport(httpx.AsyncBaseTransport):
    """httpx over an httpcore pool whose connections go through PublicOnlyBackend."""

    def __init__(self, resolver: Resolver = system_resolver):
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(),
            network_backend=PublicOnlyBackend(resolver),
            max_connections=8,
            keepalive_expiry=30.0,
            retries=0,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        core = httpcore.Request(
            method=request.method,
            url=httpcore.URL(scheme=url.raw_scheme, host=url.raw_host, port=url.port, target=url.raw_path),
            headers=request.headers.raw,
            content=request.stream,  # type: ignore[arg-type]
            extensions=request.extensions,
        )
        try:
            response = await self._pool.handle_async_request(core)
        except httpcore.TimeoutException as e:
            raise httpx.TimeoutException("timed out", request=request) from e
        except (httpcore.NetworkError, httpcore.ProtocolError, httpcore.UnsupportedProtocol) as e:
            raise httpx.TransportError(str(e) or type(e).__name__) from e
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_Stream(response),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self._pool.aclose()


@dataclass
class Page:
    url: str
    status: int
    content_type: str
    body: bytes
    etag: str = ""
    last_modified: str = ""

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def check_url(url: str) -> str:
    """The URL if it is an absolute http(s) URL without credentials, else FetchError."""
    if len(url) > 2000:
        raise FetchError("URL over 2000 characters")
    try:
        parts = urlsplit(url)
    except ValueError as e:
        raise FetchError("not a URL") from e
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("only http and https URLs")
    if parts.username or parts.password:
        raise FetchError("URLs with credentials are refused")
    host = parts.hostname
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not is_public(host):
            raise FetchError("not a public address")
    return url


def host_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def looks_challenged(body: bytes) -> bool:
    head = body[:65536].lower()
    return any(m in head for m in CHALLENGE_MARKERS)


class Fetcher:
    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
        spacing: float = HOST_SPACING,
        max_wait: float = MAX_CRAWL_DELAY,
    ):
        self._client = httpx.AsyncClient(
            transport=transport or PublicTransport(),
            timeout=httpx.Timeout(REQUEST_TIMEOUT),
            follow_redirects=False,
            trust_env=False,
            headers={"user-agent": USER_AGENT, "accept-encoding": "gzip, deflate"},
        )
        self._clock = clock
        self._sleep = sleep
        self._spacing = spacing
        # The longest a call waits for a host's pause (spacing, Crawl-delay, a
        # 429's Retry-After); a longer one fails the call at once, so one slow
        # host never holds up the others or an agent's tool call.
        self._max_wait = max_wait
        self._next_at: dict[str, float] = {}
        self._locks: dict[str, anyio.Lock] = {}
        # Per host: when read, the parser (None when nothing may be fetched), the crawl delay, why not.
        self._robots: dict[str, tuple[float, robotparser.RobotFileParser | None, float, str]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(
        self,
        url: str,
        *,
        accept: str = "*/*",
        etag: str = "",
        last_modified: str = "",
        max_bytes: int = MAX_RESPONSE,
        max_wait: float | None = None,
    ) -> Page:
        """GET `url`, following redirects by hand; FetchError for anything but a
        2xx or 304 page."""
        check_url(url)
        for _ in range(MAX_REDIRECTS + 1):
            if any(h in (urlsplit(url).hostname or "") for h in WAITING_ROOM_HOSTS):
                raise FetchError("sent to a waiting room")
            wait_limit = self._max_wait if max_wait is None else min(max_wait, self._max_wait)
            await self._allowed(url, wait_limit)
            headers = {"accept": accept}
            if etag:
                headers["if-none-match"] = etag
            if last_modified:
                headers["if-modified-since"] = last_modified
            status, response_headers, body = await self._request(url, headers, max_bytes, wait_limit)
            if status in (301, 302, 303, 307, 308):
                location = response_headers.get("location", "")
                if not location:
                    raise FetchError(f"HTTP {status} without a location")
                url = check_url(urljoin(url, location))
                etag = last_modified = ""
                continue
            if status == 304:
                return Page(url, 304, "", b"", etag, last_modified)
            if status in (429, 503):
                self._back_off(url, response_headers.get("retry-after", ""))
            if status in (401, 402, 403, 407, 429, 503) and looks_challenged(body):
                raise FetchError(f"HTTP {status}: a challenge or waiting room")
            if not 200 <= status < 300:
                raise FetchError(f"HTTP {status}")
            content_type = response_headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type in ("text/html", "application/xhtml+xml") and looks_challenged(body):
                raise FetchError("a challenge or waiting room instead of the page")
            return Page(
                url,
                status,
                content_type,
                body,
                response_headers.get("etag", ""),
                response_headers.get("last-modified", ""),
            )
        raise FetchError(f"more than {MAX_REDIRECTS} redirects")

    async def _request(
        self, url: str, headers: dict[str, str], max_bytes: int, max_wait: float
    ) -> tuple[int, httpx.Headers, bytes]:
        host = host_of(url)
        lock = self._locks.setdefault(host, anyio.Lock())
        async with lock:
            wait = self._next_at.get(host, 0.0) - self._clock()
            if wait > max_wait:
                raise HostBusy(
                    f"the site asked to wait {math.ceil(wait)} s more (a 429 or 503, or its Crawl-delay); "
                    "trying again later"
                )
            if wait > 0:
                await self._sleep(wait)
            try:
                async with self._client.stream("GET", url, headers=headers) as response:
                    declared = response.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > max_bytes:
                        raise FetchError(f"response over {max_bytes} bytes")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) > max_bytes:
                            raise FetchError(f"response over {max_bytes} bytes")
                    return response.status_code, response.headers, bytes(body)
            except BlockedAddress as e:
                raise FetchError("not a public address") from e
            except httpx.TimeoutException as e:
                raise FetchError("timed out") from e
            except httpx.HTTPError as e:
                raise FetchError(f"cannot connect ({type(e).__name__})") from e
            finally:
                delay = max(self._spacing, self._crawl_delay(host))
                self._next_at[host] = max(self._next_at.get(host, 0.0), self._clock() + delay)

    def _back_off(self, url: str, retry_after: str) -> None:
        delay = HOST_BACKOFF
        if retry_after.isdigit():
            delay = min(max(int(retry_after), 1), 3600)
        else:
            try:
                delay = min(max(parsedate_to_datetime(retry_after).timestamp() - time.time(), 1), 3600)
            except (TypeError, ValueError):
                pass
        host = host_of(url)
        self._next_at[host] = max(self._next_at.get(host, 0.0), self._clock() + delay)

    def _crawl_delay(self, host: str) -> float:
        entry = self._robots.get(host)
        return entry[2] if entry else 0.0

    async def _allowed(self, url: str, max_wait: float) -> None:
        host = host_of(url)
        entry = self._robots.get(host)
        # A robots.txt that could not be read is asked for again soon: the site may be back.
        keep = ROBOTS_KEEP if entry is not None and entry[1] is not None else ROBOTS_RETRY
        if entry is None or self._clock() - entry[0] > keep:
            entry = await self._load_robots(host, max_wait)
            self._robots[host] = entry
        parser = entry[1]
        if parser is None:
            raise FetchError(entry[3])
        if not parser.can_fetch(USER_AGENT_NAME, url):
            raise FetchError("robots.txt disallows it")

    async def _load_robots(
        self, host: str, max_wait: float
    ) -> tuple[float, robotparser.RobotFileParser | None, float, str]:
        parser = robotparser.RobotFileParser()
        url = host + "/robots.txt"
        unreadable = "robots.txt could not be read, so nothing is fetched (RFC 9309)"
        for _ in range(MAX_REDIRECTS + 1):
            try:
                status, headers, body = await self._request(url, {"accept": "text/plain"}, 512 * 1024, max_wait)
            except HostBusy:
                raise
            except FetchError as e:
                # The site itself is out of reach: say why, not that robots.txt is missing.
                return self._clock(), None, 0.0, str(e)
            if status in (301, 302, 303, 307, 308) and headers.get("location"):
                try:
                    url = check_url(urljoin(url, headers["location"]))
                except FetchError as e:
                    return self._clock(), None, 0.0, f"robots.txt redirects elsewhere: {e}"
                continue
            break
        else:
            return self._clock(), None, 0.0, unreadable
        if 200 <= status < 300:
            parser.parse(body.decode("utf-8", errors="replace").splitlines())
        elif 400 <= status < 500 and status != 429:
            # No robots.txt: everything is allowed.
            parser.allow_all = True
            parser.modified()
        else:
            return self._clock(), None, 0.0, f"{unreadable}: HTTP {status}"
        delay = parser.crawl_delay(USER_AGENT_NAME)
        return self._clock(), parser, min(float(delay or 0), MAX_CRAWL_DELAY), ""
