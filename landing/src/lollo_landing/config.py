"""Settings, read once from the environment that compose/landing.yml passes in."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from lollo_common.executor import EXECUTOR_TOKEN

PROFILE = re.compile(r"[a-z][a-z0-9-]*")
# The code editor's profile: with it on, the page lists the agents.
EDITOR = "lollo-code-server"


def page_path(profile: str) -> str:
    """Where a lollo page lives: profile lollo-<page> serves /<page>/ (PROFILE_ROUTES in lib/cloudflare.sh)."""
    return f"/{profile.removeprefix('lollo-')}/"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    team_domain: str
    audience: str
    issuer: str
    allowed_users: frozenset[str]
    # The lollo's own origin, and Lettuce's (for "Back to Lettuce").
    public_origin: str
    lettuce_origin: str
    # The lollo pages that are on (LOLLO_PROFILES), in the order to list them.
    lollo_profiles: tuple[str, ...]
    # The executor's bearer token, LOLLO_EXECUTOR_TOKEN (compose passes it from stack.env).
    executor_token: str = field(repr=False)

    @property
    def lists_agents(self) -> bool:
        return EDITOR in self.lollo_profiles

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)

        def required(key: str) -> str:
            value = env.get(key, "").strip()
            if not value:
                raise ConfigError(f"{key} is required")
            return value

        def origin(key: str) -> str:
            value = required(key).rstrip("/")
            parts = urlsplit(value)
            if parts.scheme != "https" or not parts.hostname or parts.path or parts.query or parts.fragment:
                raise ConfigError(f"{key} must be https://<host>")
            return value

        team = required("CF_ACCESS_TEAM_DOMAIN")
        users = frozenset(u.strip().lower() for u in required("ALLOWED_USERS").split(",") if u.strip())
        if not users:
            raise ConfigError("ALLOWED_USERS is required")
        token = env.get("LOLLO_EXECUTOR_TOKEN", "").strip()
        if not EXECUTOR_TOKEN.fullmatch(token):
            raise ConfigError("LOLLO_EXECUTOR_TOKEN must be 64 lowercase hex characters")
        names = (p.strip() for p in env.get("LOLLO_PROFILES", "").split(","))
        return cls(
            team_domain=team,
            audience=required("CF_ACCESS_AUD"),
            issuer=env.get("CF_ACCESS_ISSUER", "").strip() or f"https://{team}.cloudflareaccess.com",
            allowed_users=users,
            public_origin=origin("PUBLIC_ORIGIN"),
            lettuce_origin=origin("LETTUCE_ORIGIN"),
            lollo_profiles=tuple(dict.fromkeys(p for p in names if PROFILE.fullmatch(p))),
            executor_token=token,
        )
