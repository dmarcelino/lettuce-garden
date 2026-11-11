from __future__ import annotations

import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from garden_common.auth import Authenticator

from garden_outlook.config import Settings
from garden_outlook.service import Service

EMAIL = "alfred@gmail.com"
ORIGIN = "https://alfred-garden.example.com"
OUTLOOK_SECRET = "0123456789abcdef0123456789abcdef"
# A fake mail body that tests assert never reaches logs.
CANARY = "CANARY_OUTLOOK_5521_DO_NOT_LEAK"

NAVIGATE = {"sec-fetch-mode": "navigate", "sec-fetch-dest": "document", "sec-fetch-site": "same-origin"}
SCRIPT_FETCH = {"sec-fetch-mode": "cors", "sec-fetch-dest": "empty", "sec-fetch-site": "same-origin"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        team_domain="team1",
        audience="aud1",
        issuer="https://team1.cloudflareaccess.com",
        allowed_users=frozenset({EMAIL}),
        public_origin=ORIGIN,
        secret=OUTLOOK_SECRET,
        data_dir=str(tmp_path),
    )


@pytest.fixture
def signing_key() -> Any:
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


class FakeOutlook:
    """An httpx MockTransport standing in for Outlook REST v2.0 and the token
    endpoint. Records requests; answers canned JSON by method and path."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.refreshes = 0
        # The stored draft a send_draft re-reads: tests set its recipients.
        self.draft_recipients: list[str] = ["ok@ok.test"]
        # Attendees a GET /events/{id} reports (for update/delete escalation tests).
        self.event_attendees: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        method = request.method
        if "login.microsoftonline.com" in request.url.host:
            # Token endpoint: both the connect-time redemption and later refreshes.
            self.refreshes += 1
            return httpx.Response(200, json={"access_token": "graph-access-token-xyz", "expires_in": 3600})
        if method == "GET" and path.endswith("/mailFolders"):
            return httpx.Response(200, json={"value": [{"id": "inbox", "displayName": "Inbox", "unreadItemCount": 1, "totalItemCount": 3}]})
        if method == "GET" and "/messages/" in path and not path.endswith("/attachments"):
            return httpx.Response(
                200,
                json={
                    "id": "m1",
                    "subject": "Hello",
                    "from": {"emailAddress": {"address": "sender@ok.test"}},
                    "toRecipients": [{"emailAddress": {"address": a}} for a in self.draft_recipients],
                    "body": {"contentType": "text", "content": CANARY},
                    "bodyPreview": "preview",
                },
            )
        if method == "GET" and path.endswith("/messages"):
            return httpx.Response(200, json={"value": [{"id": "m1", "subject": "Hello", "bodyPreview": "preview"}]})
        if method == "GET" and path.endswith("/calendarView"):
            return httpx.Response(200, json={"value": [{"id": "e1", "subject": "Standup", "start": {"dateTime": "2026-01-20T09:00:00"}, "end": {"dateTime": "2026-01-20T09:15:00"}}]})
        if method == "GET" and "/events/" in path:
            attendees = [{"emailAddress": {"address": a}} for a in self.event_attendees]
            return httpx.Response(200, json={"id": "e1", "subject": "Standup", "organizer": {"emailAddress": {"address": "boss@ok.test"}}, "attendees": attendees})
        if method == "GET" and path.endswith("/todo/lists"):
            return httpx.Response(200, json={"value": [{"id": "list1", "displayName": "Tasks", "wellKnownListName": "defaultList"}]})
        if method == "GET" and path.endswith("/tasks"):
            return httpx.Response(200, json={"value": [{"id": "t1", "title": "Do it", "status": "notStarted"}]})
        if method == "POST" and (path.endswith("/send") or path.endswith("/move") or "/events" in path or "/createReply" in path or "/createForward" in path or path.endswith("/messages") or path.endswith("/tasks")):
            return httpx.Response(201, json={"id": "new1"})
        if method in ("PATCH", "DELETE"):
            return httpx.Response(204)
        if method == "POST":
            return httpx.Response(202, json={})
        return httpx.Response(200, json={"value": []})


@pytest.fixture
def fake_outlook() -> FakeOutlook:
    return FakeOutlook()


@pytest.fixture
def service(settings: Settings, fake_outlook: FakeOutlook) -> Service:
    http = httpx.AsyncClient(transport=fake_outlook.transport())
    return Service(settings, http=http)


def connect(service: Service, expires_in: float = 3600.0) -> None:
    """Put a usable session straight into the store (no bookmarklet, no redemption),
    with a Graph access token already set so tool calls do not refresh."""
    from garden_outlook.tokens import Session

    service.store.set(
        Session(
            access_token="graph-access-token-xyz",
            refresh_token="refresh-token-def-123456",
            expires_at=time.time() + expires_in,
            account=EMAIL,
            client_id="office-foci-client-id",
            token_endpoint="https://login.microsoftonline.com/organizations/oauth2/v2.0/token",
            scopes=("https://graph.microsoft.com/.default",),
        )
    )
