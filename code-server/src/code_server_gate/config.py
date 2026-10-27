"""Settings, read once from the environment that compose/code-server.yml passes in."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from garden_common.garden import profiles_from_env

# The editor lives under this path, behind the tunnel route of the same name.
PREFIX = "/garden/code-server"
# code-server-editor, on the internal code-editor network that only this gate shares.
DEFAULT_UPSTREAM = "http://code-server-editor:8080"


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
    # The garden pages that are on, for the /garden/ landing page's links.
    garden_profiles: str = ""

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
            garden_profiles=profiles_from_env(env),
        )
