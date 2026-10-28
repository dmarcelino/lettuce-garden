from __future__ import annotations

import time
from typing import Any
from urllib.parse import unquote

import anyio
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from garden_common.auth import Authenticator
from starlette.testclient import TestClient

from garden_landing.agents import AgentList
from garden_landing.app import create_app
from garden_landing.config import Settings

EMAIL = "alfred@gmail.com"
ORIGIN = "https://alfred-garden.example.com"
LETTUCE = "https://alfred.example.com"
TOKEN = "0f" * 32
# What a browser sends when a person opens a page (Fetch Metadata), and when a
# script calls fetch(); see garden_common.auth.check_fetch.
NAVIGATE = {"sec-fetch-mode": "navigate", "sec-fetch-dest": "document", "sec-fetch-site": "same-origin"}
SCRIPT_FETCH = {"sec-fetch-mode": "cors", "sec-fetch-dest": "empty", "sec-fetch-site": "same-origin"}
API = "/executor/v1"
AUTH = {"authorization": f"Bearer {TOKEN}"}


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


def make_settings(profiles: str = "secrets,gmail-triggers,code-server") -> Settings:
    return Settings(
        team_domain="team1",
        audience="aud1",
        issuer="https://team1.cloudflareaccess.com",
        allowed_users=frozenset({EMAIL}),
        public_origin=ORIGIN,
        lettuce_origin=LETTUCE,
        garden_profiles=tuple(p for p in profiles.split(",") if p),
        executor_token=TOKEN,
    )


@pytest.fixture(scope="session")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def other_key():
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
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256")


class Garden:
    """The app for one GARDEN_PROFILES, with its agent list and a browser
    (`page`, signed in, as a person opening the page) and the executor (`api`)."""

    def __init__(self, profiles: str, signing_key: Any, clock: Clock):
        self.settings = make_settings(profiles)
        public = signing_key.public_key()
        self.authenticator = Authenticator(self.settings, key_resolver=lambda token: public)
        self.agents = AgentList(clock=clock)
        self.app = create_app(self.settings, self.authenticator, self.agents)
        self.browser = TestClient(self.app, base_url=ORIGIN, headers=NAVIGATE, follow_redirects=False)
        self.browser.headers["cf-access-jwt-assertion"] = make_jwt(signing_key)
        self.api = TestClient(self.app, base_url="http://garden:8000")

    def page(self, **headers: str):
        return self.browser.get("/", headers=headers)

    def wanted(self) -> bool:
        r = self.api.get(f"{API}/status", headers=AUTH)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["v"] == 1 and isinstance(body["agents_wanted"], bool)
        return body["agents_wanted"]

    def put_agents(self, agents: Any, **headers: str):
        return self.api.put(f"{API}/agents", json={"v": 1, "agents": agents}, headers={**AUTH, **headers})


@pytest.fixture
def garden_for(signing_key, clock):
    def make(profiles: str = "secrets,gmail-triggers,code-server") -> Garden:
        return Garden(profiles, signing_key, clock)

    return make


@pytest.fixture
def garden(garden_for) -> Garden:
    return garden_for()


def raw_get(app: Any, raw_path: str, headers: dict[str, str]) -> int:
    """The status of a GET whose path reaches the app exactly as written, dot
    segments included (httpx, like browsers, would resolve them first)."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "server": ("garden", 8000),
        "client": ("192.0.2.1", 1234),
        "root_path": "",
        "path": unquote(raw_path),
        "raw_path": raw_path.encode(),
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    status: list[int] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status.append(message["status"])

    anyio.run(app, scope, receive, send)
    return status[0]
