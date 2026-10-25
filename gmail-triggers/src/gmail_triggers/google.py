"""Google, read-only: the connection made in Settings -> Google, and the few Gmail
calls the poller and the form need.

The BFF writes two files on volumes this sidecar mounts read-only
(upstream bff/src/google/settings.ts):
  /policy/sidecar.json      on/off, the account, workspace-mcp's --permissions
  /creds/<account>.json     the OAuth token, in google-auth's credential format
Settings -> Google stays the single switch: when sidecar.json says disabled,
nothing here calls Google. The access token is refreshed in memory only;
workspace-mcp owns the file and rewrites it.

Only metadata is ever requested (labels, From, Subject, Authentication-Results),
never bodies or snippets.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import quote

import httpx

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
# The refresh token is only ever sent to Google.
TOKEN_URIS = frozenset({DEFAULT_TOKEN_URI, "https://accounts.google.com/o/oauth2/token"})
_G = "https://www.googleapis.com/auth/"
# Any of these lets the poller read history and metadata.
GMAIL_SCOPES = frozenset({f"{_G}gmail.readonly", f"{_G}gmail.modify", "https://mail.google.com/"})
METADATA_HEADERS = ("From", "Subject", "Authentication-Results")
MAX_FILE = 64 * 1024
# Refresh this long before Google's expiry.
TOKEN_MARGIN_SECONDS = 60
MAX_HISTORY_PAGES = 50

# Gmail's reasons (error.errors[].reason or error.status) for a passing limit,
# as opposed to a missing permission. Any 429 is a limit too.
RATE_REASONS = frozenset(
    {"rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded", "quotaExceeded", "RESOURCE_EXHAUSTED"}
)

MESSAGE_ID = re.compile(r"[A-Za-z0-9]{1,64}")
LABEL_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
HISTORY_ID = re.compile(r"[0-9]{1,30}")


class Status(StrEnum):
    OK = "ok"
    NOT_CONNECTED = "not_connected"
    DISABLED = "disabled"
    GMAIL_NOT_GRANTED = "gmail_not_granted"
    RECONNECT = "reconnect"
    ERROR = "error"


class GoogleUnavailable(Exception):
    def __init__(self, status: Status, detail: str = ""):
        super().__init__(detail or status.value)
        self.status = status
        self.detail = detail


class HistoryExpired(Exception):
    """Gmail no longer has history from the stored historyId (HTTP 404)."""


@dataclass(frozen=True)
class Policy:
    email: str
    client_id: str
    client_secret: str
    permissions: tuple[str, ...]


@dataclass(frozen=True)
class Creds:
    token: str
    expiry: float | None
    refresh_token: str
    token_uri: str
    client_id: str
    client_secret: str
    scopes: frozenset[str]

    @property
    def key(self) -> str:
        """Identifies this grant without keeping the refresh token around as a key."""
        return hashlib.sha256(self.refresh_token.encode()).hexdigest()


@dataclass(frozen=True)
class Label:
    id: str
    name: str
    type: str


@dataclass(frozen=True)
class HistoryPage:
    # (message id, label ids when added), oldest first, each id once.
    added: list[tuple[str, frozenset[str]]]
    # Where the next poll starts.
    history_id: str
    complete: bool


def credential_file_name(email: str) -> str:
    """workspace-mcp's file name for an account (upstream credentialFileName)."""
    return quote(email, safe="@._-") + ".json"


def _read_json(path: str) -> Any:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(fd, "rb") as f:
            fd = -1
            data = f.read(MAX_FILE + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > MAX_FILE:
        raise ValueError("file too large")
    return json.loads(data)


def read_policy(policy_dir: str) -> Policy:
    try:
        raw = _read_json(os.path.join(policy_dir, "sidecar.json"))
    except FileNotFoundError as e:
        raise GoogleUnavailable(Status.NOT_CONNECTED) from e
    except (OSError, ValueError) as e:
        raise GoogleUnavailable(Status.NOT_CONNECTED, "Settings -> Google could not be read") from e
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        raise GoogleUnavailable(Status.DISABLED)
    email, perms = raw.get("email"), raw.get("permissions")
    if not isinstance(email, str) or "@" not in email or not isinstance(perms, list):
        raise GoogleUnavailable(Status.NOT_CONNECTED)
    permissions = tuple(p for p in perms if isinstance(p, str))
    if not any(p.startswith("gmail:") for p in permissions):
        raise GoogleUnavailable(Status.GMAIL_NOT_GRANTED)
    return Policy(
        email=email.strip().lower(),
        client_id=str(raw.get("clientId") or ""),
        client_secret=str(raw.get("clientSecret") or ""),
        permissions=permissions,
    )


def _parse_expiry(value: Any) -> float | None:
    # google-auth writes naive UTC ISO ("2026-01-01T10:00:00.123456").
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def read_creds(creds_dir: str, policy: Policy) -> Creds:
    path = os.path.join(creds_dir, credential_file_name(policy.email))
    try:
        raw = _read_json(path)
    except FileNotFoundError as e:
        raise GoogleUnavailable(Status.NOT_CONNECTED) from e
    except (OSError, ValueError) as e:
        raise GoogleUnavailable(Status.NOT_CONNECTED, "the Google token could not be read") from e
    if not isinstance(raw, dict) or not isinstance(raw.get("refresh_token"), str) or not raw["refresh_token"]:
        raise GoogleUnavailable(Status.NOT_CONNECTED)
    scopes = raw.get("scopes")
    scope_set = frozenset(s for s in scopes if isinstance(s, str)) if isinstance(scopes, list) else frozenset()
    if not scope_set & GMAIL_SCOPES:
        raise GoogleUnavailable(Status.GMAIL_NOT_GRANTED)
    token = raw.get("token")
    return Creds(
        token=token if isinstance(token, str) else "",
        expiry=_parse_expiry(raw.get("expiry")),
        refresh_token=raw["refresh_token"],
        token_uri=str(raw.get("token_uri") or DEFAULT_TOKEN_URI),
        client_id=str(raw.get("client_id") or policy.client_id),
        client_secret=str(raw.get("client_secret") or policy.client_secret),
        scopes=scope_set,
    )


def _rate_limited(resp: httpx.Response) -> bool:
    try:
        error = resp.json().get("error")
    except (ValueError, AttributeError):
        return False
    if not isinstance(error, dict):
        return False
    reasons = {error.get("status")}
    items = error.get("errors")
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            reasons.add(item.get("reason"))
    return bool(reasons & RATE_REASONS)


@dataclass
class _Token:
    value: str
    expires_at: float
    key: str


class Google:
    def __init__(
        self, policy_dir: str, creds_dir: str, client: httpx.AsyncClient, clock: Callable[[], float] = time.time
    ):
        self._policy_dir = policy_dir
        self._creds_dir = creds_dir
        self._client = client
        self._clock = clock
        self._token: _Token | None = None
        # The last network outcome; local problems are read fresh by status().
        self._status = Status.OK
        self._detail = ""
        self._failed_key: str | None = None

    def _fail(self, status: Status, detail: str = "", key: str | None = None) -> GoogleUnavailable:
        self._status, self._detail, self._failed_key = status, detail, key
        return GoogleUnavailable(status, detail)

    def status(self) -> tuple[Status, str]:
        """For the page: what is wrong now, without calling Google."""
        try:
            policy = read_policy(self._policy_dir)
            creds = read_creds(self._creds_dir, policy)
        except GoogleUnavailable as e:
            return e.status, e.detail
        if self._status == Status.RECONNECT and self._failed_key != creds.key:
            # Reconnected in Settings since the refresh failed.
            return Status.OK, ""
        return self._status, self._detail

    async def connect(self) -> Gmail:
        """Reads the policy and token afresh; raises GoogleUnavailable when Gmail can't be used."""
        try:
            policy = read_policy(self._policy_dir)
            creds = read_creds(self._creds_dir, policy)
        except GoogleUnavailable as e:
            self._token = None
            raise self._fail(e.status, e.detail) from e
        gmail = Gmail(self, policy.email, creds)
        await self.access_token(creds)
        self._status, self._detail, self._failed_key = Status.OK, "", None
        return gmail

    async def access_token(self, creds: Creds, force: bool = False) -> str:
        now = self._clock()
        cached = self._token
        if not force and cached and cached.key == creds.key and cached.expires_at > now + TOKEN_MARGIN_SECONDS:
            return cached.value
        if not force and creds.token and creds.expiry and creds.expiry > now + TOKEN_MARGIN_SECONDS:
            # workspace-mcp keeps the file's token fresh; use it while it lasts.
            self._token = _Token(creds.token, creds.expiry, creds.key)
            return creds.token
        if creds.token_uri not in TOKEN_URIS:
            raise self._fail(Status.ERROR, "the Google token names an unknown token address")
        try:
            resp = await self._client.post(
                creds.token_uri,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": creds.refresh_token,
                    "client_id": creds.client_id,
                    "client_secret": creds.client_secret,
                },
            )
        except httpx.HTTPError as e:
            raise self._fail(Status.ERROR, "could not reach Google") from e
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if resp.status_code != 200:
            code = body.get("error")
            code = code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,40}", code) else str(resp.status_code)
            if code == "invalid_grant":
                raise self._fail(Status.RECONNECT, key=creds.key)
            raise self._fail(Status.ERROR, f"Google refused to refresh the token ({code})")
        value, expires_in = body.get("access_token"), body.get("expires_in")
        if not isinstance(value, str) or not value:
            raise self._fail(Status.ERROR, "Google sent no access token")
        lifetime = expires_in if isinstance(expires_in, int) and expires_in > 0 else 3600
        self._token = _Token(value, now + lifetime, creds.key)
        return value


class Gmail:
    """One poll's (or one page view's) view of the account."""

    def __init__(self, google: Google, email: str, creds: Creds):
        self._google = google
        self.email = email
        self._creds = creds

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        """GET a Gmail resource. None for 404; GoogleUnavailable for anything else that failed."""
        google = self._google
        for attempt in (0, 1):
            token = await google.access_token(self._creds, force=attempt > 0)
            try:
                resp = await google._client.get(
                    GMAIL_API + path, params=params, headers={"authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError as e:
                raise google._fail(Status.ERROR, "could not reach Gmail") from e
            if resp.status_code == 401 and attempt == 0:
                continue
            break
        if resp.status_code == 404:
            return None
        if resp.status_code == 429 or (resp.status_code == 403 and _rate_limited(resp)):
            # Over a rate or quota limit: passes by itself, never a pause.
            raise google._fail(Status.ERROR, "Gmail asked to slow down")
        if resp.status_code == 403:
            raise google._fail(Status.GMAIL_NOT_GRANTED)
        if resp.status_code != 200:
            raise google._fail(Status.ERROR, f"Gmail answered {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as e:
            raise google._fail(Status.ERROR, "Gmail sent something unreadable") from e
        if not isinstance(data, dict):
            raise google._fail(Status.ERROR, "Gmail sent something unreadable")
        return data

    async def profile(self) -> str:
        """The mailbox's current historyId."""
        data = await self._get("/profile")
        history_id = str((data or {}).get("historyId", ""))
        if not HISTORY_ID.fullmatch(history_id):
            raise self._google._fail(Status.ERROR, "Gmail sent no history position")
        return history_id

    async def history(self, start: str) -> HistoryPage:
        added: dict[str, set[str]] = {}
        page_token: str | None = None
        last_record = start
        for _ in range(MAX_HISTORY_PAGES):
            params: dict[str, Any] = {"startHistoryId": start, "historyTypes": "messageAdded", "maxResults": 500}
            if page_token:
                params["pageToken"] = page_token
            data = await self._get("/history", params)
            if data is None:
                raise HistoryExpired()
            records = data.get("history")
            for record in records if isinstance(records, list) else []:
                if not isinstance(record, dict):
                    continue
                rid = str(record.get("id", ""))
                if HISTORY_ID.fullmatch(rid):
                    last_record = rid
                for item in record.get("messagesAdded") or []:
                    msg = item.get("message") if isinstance(item, dict) else None
                    if not isinstance(msg, dict) or not MESSAGE_ID.fullmatch(str(msg.get("id", ""))):
                        continue
                    labels = msg.get("labelIds")
                    valid = (
                        {lb for lb in labels if isinstance(lb, str) and LABEL_ID.fullmatch(lb)}
                        if isinstance(labels, list)
                        else set()
                    )
                    added.setdefault(msg["id"], set()).update(valid)
            page_token = data.get("nextPageToken") if isinstance(data.get("nextPageToken"), str) else None
            if not page_token:
                history_id = str(data.get("historyId", ""))
                if not HISTORY_ID.fullmatch(history_id):
                    history_id = last_record
                return HistoryPage([(k, frozenset(v)) for k, v in added.items()], history_id, True)
        # Too much at once: continue after the last record next poll.
        return HistoryPage([(k, frozenset(v)) for k, v in added.items()], last_record, False)

    async def message(self, message_id: str) -> dict[str, Any] | None:
        """Labels and the three metadata headers of one message; None if it is gone.
        `fields` keeps everything else (snippet included) out of the response."""
        if not MESSAGE_ID.fullmatch(message_id):
            return None
        data = await self._get(
            f"/messages/{message_id}",
            {"format": "metadata", "metadataHeaders": list(METADATA_HEADERS), "fields": "id,labelIds,payload/headers"},
        )
        if data is None:
            return None
        labels = data.get("labelIds")
        headers = (data.get("payload") or {}).get("headers") if isinstance(data.get("payload"), dict) else None
        wanted = {h.lower() for h in METADATA_HEADERS}
        return {
            "id": message_id,
            "labelIds": [lb for lb in labels if isinstance(lb, str) and LABEL_ID.fullmatch(lb)]
            if isinstance(labels, list)
            else [],
            "headers": [
                (h["name"], h["value"])
                for h in (headers if isinstance(headers, list) else [])
                if isinstance(h, dict)
                and isinstance(h.get("name"), str)
                and isinstance(h.get("value"), str)
                and h["name"].lower() in wanted
            ],
        }

    async def labels(self) -> list[Label]:
        data = await self._get("/labels") or {}
        out = []
        for item in data.get("labels") or []:
            if not isinstance(item, dict):
                continue
            lid, name = item.get("id"), item.get("name")
            if isinstance(lid, str) and LABEL_ID.fullmatch(lid) and isinstance(name, str) and name.strip():
                out.append(Label(lid, name.strip()[:200], str(item.get("type") or "user")))
        return out
