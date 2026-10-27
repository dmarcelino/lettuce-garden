from __future__ import annotations

import time
from typing import Any

import anyio
import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from garden_common.auth import Authenticator

from code_server_gate.config import Settings

EMAIL = "alfred@gmail.com"
ORIGIN = "https://alfred.example.com"
UPSTREAM = "http://code-server-editor"
# What a browser sends when the person opens the workbench (Fetch Metadata).
NAVIGATE = {"sec-fetch-mode": "navigate", "sec-fetch-dest": "document", "sec-fetch-site": "same-origin"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        team_domain="team1",
        audience="aud1",
        issuer="https://team1.cloudflareaccess.com",
        allowed_users=frozenset({EMAIL}),
        public_origin=ORIGIN,
        upstream=UPSTREAM,
        garden_profiles="secrets,code-server",
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


@pytest.fixture
def authenticator(settings: Settings, signing_key: Any) -> Authenticator:
    public = signing_key.public_key()
    return Authenticator(settings, key_resolver=lambda token: public)


class FakeEditor:
    """code-server-editor: records what reaches it and answers from `responses`
    (path without query -> httpx.Response), 200 "editor" otherwise. Its WebSocket
    echoes every message back."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self.responses: dict[str, httpx.Response] = {}
        self.down = False
        self.sockets: list[FakePeer] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        self.requests.append(request)
        self.bodies.append(await request.aread())
        canned = self.responses.get(request.url.path, httpx.Response(200, text="editor"))
        # A fresh stream each time, as from the network: the gate reads it raw.
        return httpx.Response(canned.status_code, headers=canned.headers, stream=httpx.ByteStream(canned.content))

    async def connect(self, url: str, headers: list[tuple[str, str]], subprotocols: list[str]) -> FakePeer:
        if self.down:
            raise OSError("connection refused")
        peer = FakePeer(url, headers, subprotocols)
        self.sockets.append(peer)
        return peer


class FakePeer:
    def __init__(self, url: str, headers: list[tuple[str, str]], subprotocols: list[str]) -> None:
        self.url = url
        self.headers = headers
        self.subprotocol = subprotocols[0] if subprotocols else None
        self.received: list[str | bytes] = []
        self.closed = False
        self._send, self._receive = anyio.create_memory_object_stream[str | bytes](16)

    async def send(self, message: str | bytes) -> None:
        self.received.append(message)
        await self._send.send(message)

    async def recv(self) -> str | bytes:
        return await self._receive.receive()

    async def close(self) -> None:
        self.closed = True
        await self._send.aclose()


@pytest.fixture
def editor() -> FakeEditor:
    return FakeEditor()
