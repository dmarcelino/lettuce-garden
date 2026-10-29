"""Settings, read once from the environment that compose/code-server.yml passes in."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

# The editor lives under this path on the garden's host, behind the tunnel route
# of the same name.
PREFIX = "/code-server"
# code-server-editor listens only on this Unix socket, on a volume only this gate
# and the editor mount (compose/code-server.yml); the URL names it for Host.
EDITOR_SOCKET = "/run/code-server/editor.sock"
DEFAULT_UPSTREAM = "http://code-server-editor"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    team_domain: str
    audience: str
    issuer: str
    allowed_users: frozenset[str]
    public_origin: str
    upstream: str = DEFAULT_UPSTREAM

    @property
    def public_host(self) -> str:
        return urlsplit(self.public_origin).netloc

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)

        def required(key: str) -> str:
            value = env.get(key, "").strip()
            if not value:
                raise ConfigError(f"{key} is required")
            return value

        team = required("CF_ACCESS_TEAM_DOMAIN")
        users = frozenset(u.strip().lower() for u in required("ALLOWED_USERS").split(",") if u.strip())
        origin = required("PUBLIC_ORIGIN").rstrip("/")
        if not origin.startswith("https://") or urlsplit(origin).path:
            raise ConfigError("PUBLIC_ORIGIN must be https://<host>")
        return cls(
            team_domain=team,
            audience=required("CF_ACCESS_AUD"),
            issuer=env.get("CF_ACCESS_ISSUER", "").strip() or f"https://{team}.cloudflareaccess.com",
            allowed_users=users,
            public_origin=origin,
        )
