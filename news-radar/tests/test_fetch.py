from __future__ import annotations

import httpcore
import pytest

from lollo_news_radar.fetch import BlockedAddress, FetchError, PublicOnlyBackend, check_url, is_public

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    "address, public",
    [
        ("93.184.216.34", True),
        ("2606:4700::1111", True),
        ("127.0.0.1", False),
        ("10.0.0.5", False),
        ("172.18.0.3", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("::1", False),
        ("fd00::1", False),
        ("fe80::1", False),
        ("::ffff:127.0.0.1", False),
        ("224.0.0.1", False),
        ("0.0.0.0", False),
        ("not-an-ip", False),
    ],
)
def test_is_public(address, public):
    assert is_public(address) is public


@pytest.mark.parametrize(
    "url",
    ["ftp://example.org/", "file:///etc/passwd", "http://127.0.0.1/", "http://[::1]/", "https://user:pw@example.org/"],
)
def test_check_url_refuses(url):
    with pytest.raises(FetchError):
        check_url(url)


class FakeInner(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.connected: list[str] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connected.append(host)
        raise httpcore.ConnectError("stop here")

    async def sleep(self, seconds):
        pass


async def test_backend_connects_only_to_checked_public_addresses():
    async def resolve(host, port):
        return {"good.example.org": ["93.184.216.34"], "inside.example.org": ["93.184.216.34", "172.18.0.2"]}[host]

    inner = FakeInner()
    backend = PublicOnlyBackend(resolve, inner)
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("good.example.org", 443)
    assert inner.connected == ["93.184.216.34"]  # the checked address, not the name
    with pytest.raises(BlockedAddress):
        await backend.connect_tcp("inside.example.org", 443)
    assert inner.connected == ["93.184.216.34"]
    with pytest.raises(BlockedAddress):
        await backend.connect_unix_socket("/var/run/docker.sock")


async def test_robots_disallow_is_honoured(fetcher, web):
    web.add("https://news.example.org/robots.txt", "User-agent: *\nDisallow: /private/\n", "text/plain")
    web.add("https://news.example.org/private/a", "<html>no</html>")
    with pytest.raises(FetchError, match="robots.txt disallows"):
        await fetcher.get("https://news.example.org/private/a")
    assert web.fetched() == []


async def test_robots_for_named_crawlers_only_allows_us(fetcher, web):
    web.add(
        "https://shop.example.org/robots.txt",
        "User-agent: AhrefsBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n",
        "text/plain",
    )
    web.add("https://shop.example.org/list", "<html><body>ok</body></html>")
    page = await fetcher.get("https://shop.example.org/list")
    assert page.status == 200


async def test_unreadable_robots_means_nothing_is_fetched(fetcher, web):
    web.add("https://down.example.org/robots.txt", "oops", "text/plain", status=500)
    with pytest.raises(FetchError, match="robots.txt could not be read"):
        await fetcher.get("https://down.example.org/list")


async def test_an_unreadable_robots_is_asked_again_after_ten_minutes(fetcher, web, clock):
    web.add("https://later.example.org/robots.txt", "oops", "text/plain", status=503)
    with pytest.raises(FetchError, match="HTTP 503"):
        await fetcher.get("https://later.example.org/list")
    web.add("https://later.example.org/robots.txt", "User-agent: *\nAllow: /\n", "text/plain")
    web.add("https://later.example.org/list", "<p>ok</p>")
    with pytest.raises(FetchError):
        await fetcher.get("https://later.example.org/list")  # still within the ten minutes
    clock.t += 601 + 300  # past the retry, and past the 503's pause
    assert (await fetcher.get("https://later.example.org/list")).status == 200


async def test_redirects_are_checked_each_time(fetcher, web):
    web.add("https://a.example.org/go", "", status=302, location="http://127.0.0.1:8000/mcp")
    with pytest.raises(FetchError, match="public"):
        await fetcher.get("https://a.example.org/go")
    web.add("https://a.example.org/loop", "", status=302, location="/loop")
    with pytest.raises(FetchError, match="redirects"):
        await fetcher.get("https://a.example.org/loop")
    web.add("https://a.example.org/wait", "", status=302, location="https://site.queue-it.net/?c=1")
    with pytest.raises(FetchError, match="waiting room"):
        await fetcher.get("https://a.example.org/wait")


async def test_challenge_pages_and_errors_fail(fetcher, web):
    web.add(
        "https://b.example.org/a",
        '<html><title>Just a moment...</title><script src="/cdn-cgi/challenge-platform/x"></script>',
    )
    with pytest.raises(FetchError, match="challenge"):
        await fetcher.get("https://b.example.org/a")
    web.add("https://b.example.org/b", "nope", status=403)
    with pytest.raises(FetchError, match="HTTP 403"):
        await fetcher.get("https://b.example.org/b")


async def test_size_cap(fetcher, web):
    web.add("https://c.example.org/big", "x" * 2000)
    with pytest.raises(FetchError, match="over 1000 bytes"):
        await fetcher.get("https://c.example.org/big", max_bytes=1000)


async def test_conditional_requests_and_not_modified(fetcher, web):
    web.add("https://d.example.org/feed", "", status=304)
    page = await fetcher.get("https://d.example.org/feed", etag='"v1"', last_modified="Fri, 15 Jan 2027 10:00:00 GMT")
    assert page.not_modified
    sent = [r for r in web.requests if str(r.url).endswith("/feed")][0]
    assert sent.headers["if-none-match"] == '"v1"'
    assert sent.headers["user-agent"].startswith("lollo-rozzo-news-radar/")
    assert "cookie" not in sent.headers


async def test_requests_to_one_host_are_spaced(fetcher, web, sleeps):
    web.add("https://e.example.org/robots.txt", "User-agent: *\nCrawl-delay: 10\n", "text/plain")
    web.add("https://e.example.org/1", "<p>1</p>")
    web.add("https://e.example.org/2", "<p>2</p>")
    await fetcher.get("https://e.example.org/1")
    await fetcher.get("https://e.example.org/2")
    assert sleeps.calls and max(sleeps.calls) == pytest.approx(10, abs=0.01)


async def test_a_429_pauses_the_host(fetcher, web, sleeps):
    web.add("https://f.example.org/x", "slow down", status=429, **{"retry-after": "30"})
    web.add("https://f.example.org/y", "<p>y</p>")
    with pytest.raises(FetchError, match="HTTP 429"):
        await fetcher.get("https://f.example.org/x")
    await fetcher.get("https://f.example.org/y")
    assert max(sleeps.calls) == pytest.approx(30, abs=0.01)


async def test_a_long_pause_fails_at_once_instead_of_stalling_every_fetch(fetcher, web, sleeps):
    web.add("https://g.example.org/x", "slow down", status=429, **{"retry-after": "3600"})
    web.add("https://g.example.org/y", "<p>y</p>")
    with pytest.raises(FetchError, match="HTTP 429"):
        await fetcher.get("https://g.example.org/x")
    with pytest.raises(FetchError, match="asked to wait"):
        await fetcher.get("https://g.example.org/y")
    assert max(sleeps.calls, default=0) <= 60


async def test_a_call_can_wait_less_than_a_site_asks(fetcher, web, sleeps):
    web.add("https://h.example.org/robots.txt", "User-agent: *\nCrawl-delay: 10\n", "text/plain")
    web.add("https://h.example.org/1", "<p>1</p>")
    web.add("https://h.example.org/2", "<p>2</p>")
    await fetcher.get("https://h.example.org/1", max_wait=5)
    with pytest.raises(FetchError, match="asked to wait"):
        await fetcher.get("https://h.example.org/2", max_wait=5)
    assert max(sleeps.calls, default=0) <= 5


async def test_a_name_that_resolves_inside_the_stack_is_refused_as_such(clock, sleeps):
    import httpx

    from lollo_news_radar.fetch import Fetcher

    def handler(request):
        raise BlockedAddress(request.url.host)  # what PublicOnlyBackend raises for a private address

    fetcher = Fetcher(transport=httpx.MockTransport(handler), clock=clock, sleep=sleeps)
    with pytest.raises(FetchError, match="not a public address"):
        await fetcher.get("http://lollo-vault:8000/")


async def test_a_name_that_does_not_resolve_says_so(clock, sleeps):
    import socket

    import httpx

    from lollo_news_radar.fetch import Fetcher, UnknownHost

    async def resolve(host, port):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    with pytest.raises(UnknownHost):
        await PublicOnlyBackend(resolve, FakeInner()).connect_tcp("nope.example.org", 443)

    def handler(request):
        raise UnknownHost(request.url.host)  # what PublicOnlyBackend raises

    fetcher = Fetcher(transport=httpx.MockTransport(handler), clock=clock, sleep=sleeps)
    with pytest.raises(FetchError, match="no such host: nope.example.org"):
        await fetcher.get("https://nope.example.org/")


async def test_a_pause_says_how_long(fetcher, web):
    web.add("https://p.example.org/x", "slow down", status=429, **{"retry-after": "300"})
    with pytest.raises(FetchError) as first:
        await fetcher.get("https://p.example.org/x")
    assert first.value.retry_after == 300
    with pytest.raises(FetchError, match="asked to wait") as second:
        await fetcher.get("https://p.example.org/x")
    assert 290 <= second.value.retry_after <= 300
