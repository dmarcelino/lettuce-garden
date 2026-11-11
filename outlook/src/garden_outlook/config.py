"""Settings, read once from the environment compose/outlook.yml passes in."""

from __future__ import annotations

import os
from dataclasses import dataclass

# The pages live under this path on the garden host, behind the tunnel route of
# the same name, and agents reach the MCP endpoint at http://garden-outlook:8000/mcp.
PREFIX = "/outlook"

# Microsoft Graph is the API this talks to. The access token is obtained by
# redeeming the browser's first-party refresh token for a Graph token (FOCI;
# see outlook_client.py), so no app registration is needed. GRAPH_SCOPE uses
# `.default`: the delegated permissions the signed-in user already has on the
# first-party client, plus a refresh token.
GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default offline_access"

# Refresh the access token once it has less than this left.
REFRESH_WINDOW_SECONDS = 300
# A call waits at most this long for the MCP bridge (bff/src/mcp-bridge/client.ts
# gives a tool call 120 s).
HTTP_TIMEOUT_SECONDS = 45
# Message bodies are returned as text, cut to this many characters.
MAX_BODY_CHARS = 20000
# An attachment larger than this is described, not returned inline.
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
# The most messages/events/tasks a list tool returns in one call.
MAX_PAGE = 50


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    team_domain: str
    audience: str
    issuer: str
    allowed_users: frozenset[str]
    public_origin: str
    # Encrypts the stored session at rest; generated once per stack by lettucectl,
    # held only in the environment, never written to the data volume.
    secret: str
    data_dir: str

    @property
    def token_path(self) -> str:
        return os.path.join(self.data_dir, "session.enc")

    @property
    def policy_path(self) -> str:
        return os.path.join(self.data_dir, "policy.json")

    @property
    def salt_path(self) -> str:
        return os.path.join(self.data_dir, "salt")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)

        def required(key: str) -> str:
            value = env.get(key, "").strip()
            if not value:
                raise ConfigError(f"{key} is required")
            return value

        secret = required("OUTLOOK_SECRET")
        if len(secret) < 32:
            raise ConfigError("OUTLOOK_SECRET is too short")
        allowed = frozenset(
            part.strip().lower() for part in env.get("ALLOWED_USERS", "").split(",") if part.strip()
        )
        if not allowed:
            raise ConfigError("ALLOWED_USERS is required")
        return cls(
            team_domain=required("CF_ACCESS_TEAM_DOMAIN"),
            audience=required("CF_ACCESS_AUD"),
            issuer=required("CF_ACCESS_ISSUER"),
            allowed_users=allowed,
            public_origin=required("PUBLIC_ORIGIN"),
            secret=secret,
            data_dir=env.get("OUTLOOK_DATA_DIR", "/data").strip() or "/data",
        )
