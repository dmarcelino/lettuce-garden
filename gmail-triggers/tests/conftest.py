from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from garden_common.auth import Authenticator

from gmail_triggers.config import Settings
from gmail_triggers.google import Google, credential_file_name
from gmail_triggers.poller import Poller
from gmail_triggers.store import Agent, Store

EMAIL = "alfred@gmail.com"
ORIGIN = "https://alfred.example.com"

# What a browser sends when a person opens a page or posts its form (Fetch
# Metadata), and when a page's own script calls fetch(); see garden_common.auth.check_fetch.
NAVIGATE = {"sec-fetch-mode": "navigate", "sec-fetch-dest": "document", "sec-fetch-site": "same-origin"}
SCRIPT_FETCH = {"sec-fetch-mode": "cors", "sec-fetch-dest": "empty", "sec-fetch-site": "same-origin"}
# Stands in for an email's text: tests assert it never reaches request files,
# pages or logs.
BODY = "TEST_BODY_DO_NOT_FORWARD_51734"
READONLY = "https://www.googleapis.com/auth/gmail.readonly"
AGENT = "agent-11111111-2222-3333-4444-555555555555"
OTHER_AGENT = "agent-66666666-7777-8888-9999-000000000000"
# Gmail's own verdict for a sender at example.com.
PASS_EXAMPLE = (
    "mx.google.com; dkim=pass header.i=@example.com header.s=s1 header.b=abc; "
    "spf=pass (google.com: domain of hal@example.com designates 192.0.2.1 as permitted sender) "
    "smtp.mailfrom=hal@example.com; dmarc=pass (p=REJECT sp=REJECT dis=NONE) header.from=example.com"
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


# -- Google files -------------------------------------------------------------


def write_policy(policy_dir: str, enabled: bool = True, permissions: tuple[str, ...] = ("gmail:readonly",)) -> None:
    data = (
        {
            "enabled": True,
            "email": EMAIL,
            "clientId": "client1.apps.example.com",
            "clientSecret": "client-secret-1",
            "permissions": list(permissions),
        }
        if enabled
        else {"enabled": False, "email": None, "clientId": "", "clientSecret": "", "permissions": []}
    )
    with open(os.path.join(policy_dir, "sidecar.json"), "w") as f:
        json.dump(data, f)


def write_creds(
    creds_dir: str,
    scopes: tuple[str, ...] = (READONLY,),
    expiry: float | None = None,
    refresh_token: str = "refresh-token-1",
    token_uri: str = "https://oauth2.googleapis.com/token",
) -> str:
    path = os.path.join(creds_dir, credential_file_name(EMAIL))
    if os.path.exists(path):
        os.chmod(path, 0o644)
    exp = datetime.fromtimestamp(expiry if expiry is not None else 1_000_000_000, UTC)
    with open(path, "w") as f:
        json.dump(
            {
                "token": "file-access-token",
                "refresh_token": refresh_token,
                "token_uri": token_uri,
                "client_id": "client1.apps.example.com",
                "client_secret": "client-secret-1",
                "scopes": list(scopes),
                "expiry": exp.replace(tzinfo=None).isoformat(),
            },
            f,
        )
    os.chmod(path, 0o444)
    return path


def snapshot(directory: str) -> dict[str, tuple[bytes, int]]:
    out = {}
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        with open(path, "rb") as f:
            out[name] = (f.read(), os.stat(path).st_mtime_ns)
    return out


# -- A fake Gmail behind httpx.MockTransport ----------------------------------


class FakeGoogle:
    def __init__(self) -> None:
        self.history_id = 5000
        self.records: list[dict[str, Any]] = []
        self.messages: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []
        self.expired = False
        self.token_error: str | None = None
        self.page_size = 100
        self.access_token = "fresh-access-token"
        self.gmail_status: int | None = None
        # Raw body for gmail_status answers (None: a minimal JSON error).
        self.gmail_error_body: bytes | None = None
        # Gmail accepts the token in the creds file until a test says otherwise.
        self.file_token_valid = True
        # Called with each message id Gmail is asked for (tests change things mid-poll).
        self.on_message: Any = None
        self.labels = [
            {"id": "INBOX", "name": "INBOX", "type": "system"},
            {"id": "SENT", "name": "SENT", "type": "system"},
            {"id": "CATEGORY_UPDATES", "name": "CATEGORY_UPDATES", "type": "system"},
            {"id": "Label_1", "name": "Bills", "type": "user"},
            {"id": "Label_2", "name": "Travel", "type": "user"},
        ]
        self._n = 0

    def add(
        self,
        sender: str = "Hal <hal@example.com>",
        subject: str = "Invoice 42",
        labels: tuple[str, ...] = ("INBOX", "UNREAD"),
        auth: str | None = PASS_EXAMPLE,
    ) -> str:
        self._n += 1
        mid = f"18c{self._n:013x}"
        self.history_id += 7
        headers = [{"name": "From", "value": sender}, {"name": "Subject", "value": subject}]
        if auth is not None:
            headers.insert(0, {"name": "Authentication-Results", "value": auth})
        headers.append({"name": "X-Body-Preview", "value": BODY})
        self.messages[mid] = {
            "id": mid,
            "threadId": mid,
            "labelIds": list(labels),
            "snippet": BODY,
            "payload": {"headers": headers, "body": {"data": BODY}},
        }
        self.records.append(
            {
                "id": str(self.history_id),
                "messages": [{"id": mid, "threadId": mid}],
                "messagesAdded": [{"message": {"id": mid, "threadId": mid, "labelIds": list(labels)}}],
            }
        )
        return mid

    @property
    def gmail_calls(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == "gmail.googleapis.com"]

    @property
    def token_calls(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == "oauth2.googleapis.com"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            form = parse_qs(request.content.decode())
            assert form["grant_type"] == ["refresh_token"]
            if self.token_error:
                return httpx.Response(400, json={"error": self.token_error, "error_description": "Bad Request"})
            return httpx.Response(200, json={"access_token": self.access_token, "expires_in": 3599})
        assert request.url.host == "gmail.googleapis.com", request.url
        auth = request.headers.get("authorization", "")
        accepted = {f"Bearer {self.access_token}"} | ({"Bearer file-access-token"} if self.file_token_valid else set())
        if auth not in accepted:
            return httpx.Response(401, json={"error": {"code": 401}})
        if self.gmail_status:
            if self.gmail_error_body is not None:
                return httpx.Response(self.gmail_status, content=self.gmail_error_body)
            return httpx.Response(self.gmail_status, json={"error": {"code": self.gmail_status}})
        path = request.url.path.removeprefix("/gmail/v1/users/me")
        params = request.url.params
        if path == "/profile":
            return httpx.Response(200, json={"emailAddress": EMAIL, "historyId": str(self.history_id)})
        if path == "/labels":
            return httpx.Response(200, json={"labels": self.labels})
        if path == "/history":
            if self.expired:
                return httpx.Response(404, json={"error": {"code": 404}})
            start = int(params["startHistoryId"])
            assert params["historyTypes"] == "messageAdded"
            records = [r for r in self.records if int(r["id"]) > start]
            offset = int(params.get("pageToken") or 0)
            page = records[offset : offset + self.page_size]
            body: dict[str, Any] = {"historyId": str(self.history_id)}
            if page:
                body["history"] = page
            if offset + self.page_size < len(records):
                body["nextPageToken"] = str(offset + self.page_size)
            return httpx.Response(200, json=body)
        if path.startswith("/messages/"):
            mid = path.rsplit("/", 1)[1]
            if self.on_message:
                self.on_message(mid)
            assert params["format"] == "metadata"
            msg = self.messages.get(mid)
            if msg is None:
                return httpx.Response(404, json={"error": {"code": 404}})
            # Like Gmail with format=metadata: the snippet comes back unless
            # `fields` leaves it out; this fake ignores `fields` on purpose.
            wanted = {h.lower() for h in params.get_list("metadataHeaders")}
            headers = [h for h in msg["payload"]["headers"] if h["name"].lower() in wanted]
            # Encoded by hand: httpx's json= can't carry lone surrogates, Gmail's JSON can.
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(
                    {
                        "id": mid,
                        "threadId": mid,
                        "labelIds": msg["labelIds"],
                        "snippet": msg["snippet"],
                        "payload": {"headers": headers + [{"name": "X-Body-Preview", "value": BODY}]},
                    }
                ).encode(),
            )
        return httpx.Response(404, json={"error": {"code": 404}})


# -- The sidecar, wired together ----------------------------------------------


TOKEN = "ab" * 32


@dataclass
class Env:
    settings: Settings
    store: Store
    google: Google
    poller: Poller
    fake: FakeGoogle
    clock: Clock
    client: httpx.AsyncClient

    def write_agents(self, agents: list[tuple[str, str]] | None = None) -> None:
        agents = agents if agents is not None else [(AGENT, "lucius"), (OTHER_AGENT, "hal")]
        self.store.set_agents([Agent(i, n) for i, n in agents])

    def write_heartbeat(self, age: float = 30, letta_ok: bool = True, error: str = "") -> None:
        now = self.clock.t
        self.clock.t = now - age
        try:
            self.store.set_heartbeat(letta_ok, error)
        finally:
            self.clock.t = now

    def requests(self) -> list[dict[str, Any]]:
        """Queued requests, as the executor would get them (without claiming)."""
        db = sqlite3.connect(self.settings.db_path)
        try:
            rows = db.execute("SELECT request FROM runs WHERE request != '' ORDER BY at, rowid").fetchall()
        finally:
            db.close()
        return [json.loads(r[0]) for r in rows]

    def db_text(self) -> str:
        db = sqlite3.connect(self.settings.db_path)
        try:
            return "\n".join(db.iterdump())
        finally:
            db.close()


@pytest.fixture
def settings(tmp_path) -> Settings:
    dirs = {name: tmp_path / name for name in ("data", "creds", "policy")}
    for d in dirs.values():
        d.mkdir()
    return Settings(
        team_domain="team1",
        audience="aud1",
        issuer="https://team1.cloudflareaccess.com",
        allowed_users=frozenset({EMAIL}),
        public_origin=ORIGIN,
        executor_token=TOKEN,
        data_dir=str(dirs["data"]),
        creds_dir=str(dirs["creds"]),
        policy_dir=str(dirs["policy"]),
    )


def make_env(settings: Settings, clock: Clock, fake: FakeGoogle | None = None) -> Env:
    fake = fake or FakeGoogle()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    store = Store(settings.db_path, clock=clock)
    google = Google(settings.policy_dir, settings.creds_dir, client, clock=clock)
    poller = Poller(store, google, settings.poll_seconds)
    return Env(settings, store, google, poller, fake, clock, client)


@pytest.fixture
def env(settings, clock) -> Env:
    write_policy(settings.policy_dir)
    write_creds(settings.creds_dir)
    return make_env(settings, clock)


# -- Access JWTs --------------------------------------------------------------


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
