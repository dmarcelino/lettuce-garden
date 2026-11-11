"""The façade the app and the MCP tools share: the connected session, the stored
policy, and the Outlook client. Policy is enforced in mcp_tools.py, which holds the
per-tool level and recipient rules; this module holds state and persists it."""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .config import Settings
from .levels import Policy, apply_update, parse_policy, policy_to_json, serving
from .outlook_client import OutlookClient
from .recipients import AllowedRecipients
from .tokens import SessionStore, session_from_payload

log = logging.getLogger("garden_outlook.service")


class Service:
    def __init__(self, settings: Settings, http: Any | None = None):
        self._settings = settings
        self.store = SessionStore(settings)
        self.client = OutlookClient(settings, self.store, http)
        self._policy = self._load_policy()

    # -- policy -----------------------------------------------------------

    def _load_policy(self) -> Policy:
        try:
            with open(self._settings.policy_path, encoding="utf-8") as handle:
                return parse_policy(json.load(handle))
        except FileNotFoundError:
            return Policy()
        except (OSError, ValueError) as error:
            log.warning("policy file unreadable (%s); starting with none", type(error).__name__)
            return Policy()

    def _save_policy(self, policy: Policy) -> None:
        tmp = f"{self._settings.policy_path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(policy_to_json(policy), handle, indent=2)
        os.replace(tmp, self._settings.policy_path)

    @property
    def policy(self) -> Policy:
        return self._policy

    def update_policy(self, body: Any) -> Policy:
        self._policy = apply_update(self._policy, body)
        self._save_policy(self._policy)
        return self._policy

    def allowed_recipients(self) -> AllowedRecipients:
        """The guard for sends and invitations: the list plus the connected account."""
        return AllowedRecipients(self._policy.allowed_recipients, self.store.account)

    # -- session ----------------------------------------------------------

    async def connect(self, payload: Any) -> None:
        """Validate the handover and immediately redeem the refresh token for a
        Graph token, so a tenant that refuses (Conditional Access, token
        protection, restricted first-party apps) fails here, not on the first
        tool call. Raises ConnectError (bad payload) or LostAccess (refused)."""
        session = session_from_payload(payload)
        stored = await self.client.redeem_for_session(session)
        self.store.set(stored)

    def disconnect(self) -> None:
        self.store.disconnect()

    def status(self) -> dict[str, Any]:
        """What the /outlook/ page shows: connection and policy, never the token."""
        session = self.store.session
        return {
            "connected": session is not None,
            "account": session.account if session else None,
            "can_refresh": bool(session and session.refresh_token),
            "serving": session is not None and serving(self._policy),
            "policy": policy_to_json(self._policy),
        }

    async def aclose(self) -> None:
        await self.client.aclose()
