"""Settings, read once from the environment that compose/gmail-triggers.yml passes in."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from garden_common.executor import EXECUTOR_TOKEN

# The pages live under this path on the garden's host, behind the tunnel route
# of the same name. The executor's API (garden_common.executor) is outside it,
# so the tunnel never routes it.
PREFIX = "/gmail-triggers"

DEFAULT_POLL_SECONDS = 120
# The executor runs every minute; three missed runs mean it is not running.
HEARTBEAT_STALE_SECONDS = 180
# A claimed request with no result after this long was lost.
LOST_AFTER_SECONDS = 600
# The executor sends a fresh agent list when asked: when there is none, when
# it is older than this, or when the trigger form was shown since it arrived.
AGENTS_FRESH_SECONDS = 900
# Requests handed to the executor per call.
MAX_CLAIM = 20
# Google unusable for longer than this: start again from now instead of
# replaying what arrived meanwhile.
RESUME_AFTER_SECONDS = 1800

DEFAULT_MAX_PER_HOUR = 6
MAX_MAX_PER_HOUR = 60
MAX_RULES = 50
MAX_RULE_NAME = 80
MAX_RULE_PROMPT = 4000
MAX_SENDERS = 50
MAX_LABELS = 20
# Subjects reach the agent as one line of at most this many characters.
MAX_SUBJECT = 120
# Emails per request; the rest wait for the next run.
MAX_LISTED_MESSAGES = 30
# Held matches kept per rule while its hourly limit is reached.
MAX_PENDING_PER_RULE = 500
# Message metadata fetched per poll. Candidates beyond this are not checked
# (not marked handled either) and are recorded as a gap the page shows.
MAX_FETCH_PER_POLL = 200
# Handled message ids are kept this long (history ids move forward anyway).
HANDLED_KEEP_SECONDS = 30 * 86400
# Run log entries kept per rule.
RUNS_KEEP_PER_RULE = 200


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    team_domain: str
    audience: str
    issuer: str
    allowed_users: frozenset[str]
    public_origin: str
    # The executor's bearer token, GARDEN_EXECUTOR_TOKEN (compose passes it from stack.env).
    executor_token: str = field(repr=False)
    data_dir: str = "/data"
    creds_dir: str = "/creds"
    policy_dir: str = "/policy"
    poll_seconds: int = DEFAULT_POLL_SECONDS

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "triggers.db")

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
        if not origin.startswith("https://"):
            raise ConfigError("PUBLIC_ORIGIN must be https://")
        try:
            poll = int(env.get("POLL_SECONDS", "").strip() or DEFAULT_POLL_SECONDS)
        except ValueError as e:
            raise ConfigError("POLL_SECONDS must be a number of seconds") from e
        if not 30 <= poll <= 3600:
            raise ConfigError("POLL_SECONDS must be between 30 and 3600")
        token = env.get("GARDEN_EXECUTOR_TOKEN", "").strip()
        if not EXECUTOR_TOKEN.fullmatch(token):
            raise ConfigError("GARDEN_EXECUTOR_TOKEN must be 64 lowercase hex characters")
        return cls(
            team_domain=team,
            audience=required("CF_ACCESS_AUD"),
            issuer=env.get("CF_ACCESS_ISSUER", "").strip() or f"https://{team}.cloudflareaccess.com",
            allowed_users=users,
            public_origin=origin,
            executor_token=token,
            data_dir=env.get("GMAIL_TRIGGERS_DATA", "").strip() or "/data",
            creds_dir=env.get("GOOGLE_CREDS_DIR", "").strip() or "/creds",
            policy_dir=env.get("GOOGLE_POLICY_DIR", "").strip() or "/policy",
            poll_seconds=poll,
        )
