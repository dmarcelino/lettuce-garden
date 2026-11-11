"""The connected Outlook session: the bearer token the bookmarklet hands over, the
refresh material, and who it belongs to — sealed at rest on the data volume.

The bookmarklet (served from the Outlook page) reads what the Outlook web app
already holds for the signed-in person and POSTs it to /outlook/connect. This
module validates that payload into a Session, seals it, and later applies a
refreshed token in place. The refresh HTTP call itself lives in outlook_client.py,
which owns the HTTP client; this module only holds and persists state.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from . import crypto, logs

log = logging.getLogger("garden_outlook.tokens")


class ConnectError(ValueError):
    """A handover payload that is missing or malformed."""


class LostAccess(Exception):
    """The session is gone or the refresh was refused: the person must reconnect."""


@dataclass(frozen=True)
class Session:
    access_token: str
    refresh_token: str | None
    expires_at: float
    account: str
    client_id: str
    token_endpoint: str
    scopes: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "account": self.account,
            "client_id": self.client_id,
            "token_endpoint": self.token_endpoint,
            "scopes": list(self.scopes),
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Session:
        return cls(
            access_token=str(raw["access_token"]),
            refresh_token=raw.get("refresh_token") or None,
            expires_at=float(raw.get("expires_at") or 0),
            account=str(raw.get("account") or ""),
            client_id=str(raw.get("client_id") or ""),
            token_endpoint=str(raw.get("token_endpoint") or ""),
            scopes=tuple(str(s) for s in raw.get("scopes") or []),
        )


def _expires_at(value: Any) -> float:
    """Accept epoch seconds (int/float/str) or an ISO 8601 timestamp."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            return float(text)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return 0.0


def _https(value: Any) -> str:
    text = str(value or "").strip()
    if not text.startswith("https://"):
        raise ConnectError("token endpoint must be an https URL")
    return text


def session_from_payload(payload: Any) -> Session:
    """Validate a /outlook/connect body into a Session. Fails closed: anything
    missing or the wrong type is refused.

    The refresh token is what matters: the Graph access token is obtained by
    redeeming it (outlook_client.py), so the browser's own access token is
    optional and never used for API calls."""
    if not isinstance(payload, dict):
        raise ConnectError("expected a JSON object")
    refresh = payload.get("refreshToken")
    if not isinstance(refresh, str) or len(refresh) < 12:
        raise ConnectError("no refresh token was captured; open Outlook on the web, wait for it to load, and run the bookmarklet again")
    account = str(payload.get("account") or "").strip().lower()
    if "@" not in account:
        raise ConnectError("account is missing or not an address")
    client_id = str(payload.get("clientId") or "").strip()
    if not client_id:
        raise ConnectError("clientId is missing")
    access = payload.get("accessToken")
    access = access if isinstance(access, str) else ""
    scopes_raw = payload.get("scopes")
    scopes = tuple(s for s in scopes_raw if isinstance(s, str)) if isinstance(scopes_raw, list) else ()
    return Session(
        access_token=access,
        refresh_token=refresh,
        expires_at=_expires_at(payload.get("expiresOn")),
        account=account,
        client_id=client_id,
        token_endpoint=_https(payload.get("tokenEndpoint")),
        scopes=scopes,
    )


class SessionStore:
    """Holds the current Session in memory and sealed on disk."""

    def __init__(self, settings: Any):
        self._path = settings.token_path
        self._key = crypto.derive_key(settings.secret, crypto.load_or_create_salt(settings.salt_path))
        self._session: Session | None = self._load()
        self._protect()

    def _load(self) -> Session | None:
        try:
            with open(self._path, "rb") as handle:
                blob = handle.read()
        except FileNotFoundError:
            return None
        try:
            raw = json.loads(crypto.open_sealed(self._key, blob))
            return Session.from_json(raw)
        except (crypto.CryptoError, ValueError, KeyError) as error:
            # A session we cannot read is a session we do not have.
            log.warning("stored session unreadable (%s); treating as disconnected", type(error).__name__)
            return None

    def _save(self, session: Session) -> None:
        blob = crypto.seal(self._key, json.dumps(session.to_json()).encode("utf-8"))
        tmp = f"{self._path}.tmp"
        with open(tmp, "wb") as handle:
            handle.write(blob)
        os.replace(tmp, self._path)

    def _protect(self) -> None:
        if self._session is None:
            logs.SECRET_FILTER.protect([])
        else:
            logs.SECRET_FILTER.protect([self._session.access_token, self._session.refresh_token])

    @property
    def session(self) -> Session | None:
        return self._session

    @property
    def account(self) -> str | None:
        return self._session.account if self._session else None

    def set(self, session: Session) -> None:
        """Store and persist a session (built and validated by the caller)."""
        self._session = session
        self._save(session)
        self._protect()
        log.info("connected %s", session.account)

    def disconnect(self) -> None:
        self._session = None
        try:
            os.remove(self._path)
        except FileNotFoundError:
            pass
        self._protect()

    def apply_refresh(self, access_token: str, expires_at: float, refresh_token: str | None) -> Session:
        if self._session is None:
            raise LostAccess("not connected")
        session = replace(
            self._session,
            access_token=access_token,
            expires_at=expires_at,
            refresh_token=refresh_token or self._session.refresh_token,
        )
        self._session = session
        self._save(session)
        self._protect()
        return session

    def needs_refresh(self, window_seconds: int, now: float | None = None) -> bool:
        if self._session is None:
            return False
        now = time.time() if now is None else now
        return self._session.expires_at - now <= window_seconds
