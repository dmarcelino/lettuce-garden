from __future__ import annotations

import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from garden_common.auth import Authenticator

from garden_monitors.config import Settings
from garden_monitors.fetch import Fetcher
from garden_monitors.service import Service
from garden_monitors.store import Store

EMAIL = "alfred@gmail.com"
ORIGIN = "https://alfred-garden.example.com"
TOKEN = "ab" * 32
AGENT = "agent-11111111-2222-3333-4444-555555555555"
OTHER_AGENT = "agent-66666666-7777-8888-9999-000000000000"
NAVIGATE = {"sec-fetch-mode": "navigate", "sec-fetch-dest": "document", "sec-fetch-site": "same-origin"}
# 2027-01-15 08:00 UTC, a Friday.
T0 = 1_800_000_000.0


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


class Web:
    """A fake internet behind httpx.MockTransport: `pages[url] = (status, headers, body)`.
    robots.txt answers 404 unless a test sets it."""

    def __init__(self) -> None:
        self.pages: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.requests: list[httpx.Request] = []

    def add(
        self, url: str, body: str | bytes, content_type: str = "text/html", status: int = 200, **headers: str
    ) -> None:
        data = body.encode() if isinstance(body, str) else body
        self.pages[url] = (status, {"content-type": content_type, **headers}, data)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url in self.pages:
            status, headers, body = self.pages[url]
            return httpx.Response(status, headers=headers, content=body)
        return httpx.Response(404, headers={"content-type": "text/plain"}, content=b"not found")

    def fetched(self) -> list[str]:
        return [str(r.url) for r in self.requests if not str(r.url).endswith("/robots.txt")]


@pytest.fixture
def web() -> Web:
    return Web()


class Sleeps:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.t += seconds


@pytest.fixture
def sleeps(clock: Clock) -> Sleeps:
    return Sleeps(clock)


@pytest.fixture
def fetcher(web: Web, clock: Clock, sleeps: Sleeps) -> Fetcher:
    return Fetcher(transport=httpx.MockTransport(web.handler), clock=clock, sleep=sleeps)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        team_domain="team1",
        audience="aud1",
        issuer="https://team1.cloudflareaccess.com",
        allowed_users=frozenset({EMAIL}),
        public_origin=ORIGIN,
        executor_token=TOKEN,
        timezone="UTC",
        data_dir=str(tmp_path),
        min_interval=300,
    )


@pytest.fixture
def store(settings: Settings, clock: Clock) -> Store:
    return Store(settings.db_path, clock=clock)


@pytest.fixture
def service(store: Store, fetcher: Fetcher, settings: Settings) -> Service:
    return Service(store, fetcher, settings)


@pytest.fixture(scope="session")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_jwt(key: Any, **over: Any) -> str:
    now = int(time.time())
    claims = {
        "aud": ["aud1"],
        "iss": "https://team1.cloudflareaccess.com",
        "email": EMAIL,
        "iat": now,
        "exp": now + 3600,
        "sub": "user-1",
    }
    claims.update(over)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm="RS256")


@pytest.fixture
def authenticator(settings: Settings, signing_key: Any) -> Authenticator:
    public = signing_key.public_key()
    return Authenticator(settings, key_resolver=lambda token: public)


# -- synthetic pages (never copies of real sites) ---------------------------------
EVENTS_JSON = """{"data": {"list": {"items": [
  {"id": 1, "name": "The Long Night", "date": "2027-02-01", "link": "/e/1?utm_source=x", "place": {"name": "Hall A"}},
  {"id": 2, "name": "Morning Light", "date": "2027-02-03T21:00", "link": "/e/2", "place": {"name": "Hall B"}},
  {"id": 3, "name": "", "date": "2027-02-04", "link": "/e/3", "place": {"name": "Hall C"}}
]}}}"""


def json_spec(**over: Any) -> dict[str, Any]:
    spec = {
        "name": "city-agenda",
        "url": "https://agenda.example.org/api/events",
        "kind": "json",
        "every": "6h",
        "extract": {
            "items": "data.list.items",
            "fields": {"title": "name", "start": "date", "url": "link", "venue": "place.name"},
        },
    }
    spec.update(over)
    return spec
