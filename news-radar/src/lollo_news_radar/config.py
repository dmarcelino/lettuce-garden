"""Settings, read once from the environment that compose/news-radar.yml passes in,
and the service's fixed limits."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lollo_common.executor import EXECUTOR_TOKEN

# The pages live under this path on the lollo's host, behind the tunnel route of
# the same name. /mcp and the executor's API are outside it, so the tunnel never
# routes them.
PREFIX = "/news-radar"
# How agents and the executor address the service on the stack network
# (compose/news-radar.yml); /mcp accepts this host alone.
INTERNAL_NAME = "lollo-news-radar"

# -- the executor's queue -------------------------------------------------------
# The executor runs every minute; three missed runs mean it is not running.
HEARTBEAT_STALE_SECONDS = 180
# A claimed report with no result after this long was lost.
LOST_AFTER_SECONDS = 600
# Reports handed to the executor per call.
MAX_CLAIM = 20
# The executor's limit on a request's prompt, name and description.
MAX_PROMPT = 8000
MAX_TASK_NAME = 100
MAX_TASK_DESCRIPTION = 200

# -- fetching -------------------------------------------------------------------
REQUEST_TIMEOUT = 20.0
MAX_RESPONSE = 5 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_PAGES = 10
MAX_FOLLOW = 20
# The files of a sitemap family read per run: the two highest-numbered, since a file
# just begun holds only a few pages and the rest of the newest are at the end of the
# one before. The index's own order and dates are no help: sites sort it as text, or
# give every file the same date.
SITEMAPS_READ = 2
# Files of a sitemap index looked at; the protocol allows 50,000.
MAX_INDEX_FILES = 5000
# Between two requests to one host, unless its robots.txt asks for more.
HOST_SPACING = 2.0
MAX_CRAWL_DELAY = 60.0
# A 429 or 503 from a host pauses every fetch to it for this long.
HOST_BACKOFF = 300.0
ROBOTS_KEEP = 86400
# A followed item page is read again once its item was last seen this long ago.
FOLLOW_REFRESH_SECONDS = 7 * 86400
# Agents' tool calls: the longest one waits for a host's pause, and in all; both
# well inside the 120 s Lettuce's MCP bridge gives a call.
TOOL_WAIT = 10.0
TOOL_SECONDS = 90.0
ROBOTS_RETRY = 600
USER_AGENT_NAME = "lollo-rozzo-news-radar"
USER_AGENT = f"{USER_AGENT_NAME}/0.1 (+https://github.com/dmarcelino/lollo-rozzo)"

# -- what is kept ---------------------------------------------------------------
ITEMS_KEEP_SECONDS = 90 * 86400
# Items never carried into a report once older than this.
CARRY_SECONDS = 7 * 86400
SPEC_VERSIONS = 5
REPORTS_KEEP_PER_GROUP = 100
# A passing source_test of a spec lets source_put store it for this long.
TEST_VALID_SECONDS = 15 * 60

# -- reports --------------------------------------------------------------------
MAX_ITEMS_PER_MONITOR = 50
MAX_STORIES = 20
STORY_WINDOW = 48 * 3600
STORY_OVERLAP = 0.5
# A schedule's sources are fetched again when it fires within this, and their
# last run is older.
FETCH_AHEAD_SECONDS = 20 * 60
# Reports in a row that Lettuce refused before a monitor is paused.
PAUSE_AFTER_FAILURES = 3
# An unhealthy source rides along in reports, and is reason enough for one at most this often.
ALERT_EVERY_SECONDS = 86400

# -- health ---------------------------------------------------------------------
UNHEALTHY_FAILURES = 3
UNHEALTHY_EMPTY_MEDIAN = 5
HISTORY_RUNS = 10

# -- sizes of what agents and people send ---------------------------------------
MAX_NAME = 64
MAX_TERMS = 60
MAX_TERM = 80
MAX_SOURCES_PER_MONITOR = 20
MAX_SCHEDULE = 200
MAX_DAYS_AHEAD = 366
# Text from the web, per field, as it is stored and reported.
MAX_FIELD = 300
MAX_SUMMARY = 600
MAX_URL = 2000

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
DURATION = re.compile(r"([1-9][0-9]{0,4})([mhd])")
TIME_OF_DAY = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")


class ConfigError(Exception):
    pass


def parse_duration(text: str) -> int:
    """ "30m", "6h" or "1d" in seconds; ValueError otherwise."""
    m = DURATION.fullmatch(text.strip())
    if not m:
        raise ValueError(f"{text!r} is not a duration like 30m, 6h or 1d")
    return int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]


def default_schedule(times: str) -> str:
    """NEWS_RADAR_DEFAULT_TIMES ("08:00,14:00,20:00") as cron expressions, one per
    distinct minute, joined by "; " (cron's minute and hour fields are a product)."""
    by_minute: dict[int, list[int]] = {}
    for part in times.split(","):
        m = TIME_OF_DAY.fullmatch(part.strip())
        if not m:
            raise ValueError(f"{part.strip()!r} is not a time like 08:00")
        by_minute.setdefault(int(m.group(2)), []).append(int(m.group(1)))
    if not by_minute:
        raise ValueError("no times given")
    return "; ".join(
        f"{minute} {','.join(str(h) for h in sorted(set(hours)))} * * *" for minute, hours in sorted(by_minute.items())
    )


@dataclass(frozen=True)
class Settings:
    team_domain: str
    audience: str
    issuer: str
    allowed_users: frozenset[str]
    public_origin: str
    # The executor's bearer token, LOLLO_EXECUTOR_TOKEN (compose passes it from stack.env).
    executor_token: str = field(repr=False)
    timezone: str = "UTC"
    data_dir: str = "/data"
    default_times: str = "08:00,14:00,20:00"
    max_reports_per_day: int = 6
    min_interval: int = 3600
    max_sources: int = 100
    max_monitors: int = 50

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "news-radar.db")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def default_schedule(self) -> str:
        return default_schedule(self.default_times)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)

        def required(key: str) -> str:
            value = env.get(key, "").strip()
            if not value:
                raise ConfigError(f"{key} is required")
            return value

        def number(key: str, default: int, low: int, high: int) -> int:
            raw = env.get(key, "").strip()
            if not raw:
                return default
            if not raw.isdigit() or not low <= int(raw) <= high:
                raise ConfigError(f"{key} must be a whole number from {low} to {high}")
            return int(raw)

        team = required("CF_ACCESS_TEAM_DOMAIN")
        users = frozenset(u.strip().lower() for u in required("ALLOWED_USERS").split(",") if u.strip())
        origin = required("PUBLIC_ORIGIN").rstrip("/")
        if not origin.startswith("https://"):
            raise ConfigError("PUBLIC_ORIGIN must be https://")
        token = env.get("LOLLO_EXECUTOR_TOKEN", "").strip()
        if not EXECUTOR_TOKEN.fullmatch(token):
            raise ConfigError("LOLLO_EXECUTOR_TOKEN must be 64 lowercase hex characters")
        timezone = env.get("TZ", "").strip() or "UTC"
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ConfigError(f"TZ {timezone!r} is not a known timezone") from e
        times = env.get("NEWS_RADAR_DEFAULT_TIMES", "").strip() or cls.default_times
        try:
            default_schedule(times)
        except ValueError as e:
            raise ConfigError(f"NEWS_RADAR_DEFAULT_TIMES: {e}") from e
        interval_text = env.get("NEWS_RADAR_MIN_INTERVAL", "").strip() or "1h"
        try:
            interval = parse_duration(interval_text)
        except ValueError as e:
            raise ConfigError(f"NEWS_RADAR_MIN_INTERVAL: {e}") from e
        if not 300 <= interval <= 7 * 86400:
            raise ConfigError("NEWS_RADAR_MIN_INTERVAL must be from 5m to 7d")
        return cls(
            team_domain=team,
            audience=required("CF_ACCESS_AUD"),
            issuer=env.get("CF_ACCESS_ISSUER", "").strip() or f"https://{team}.cloudflareaccess.com",
            allowed_users=users,
            public_origin=origin,
            executor_token=token,
            timezone=timezone,
            data_dir=env.get("NEWS_RADAR_DATA", "").strip() or "/data",
            default_times=times,
            max_reports_per_day=number("NEWS_RADAR_MAX_REPORTS_PER_DAY", 6, 1, 48),
            min_interval=interval,
            max_sources=number("NEWS_RADAR_MAX_SOURCES", 100, 1, 1000),
            max_monitors=number("NEWS_RADAR_MAX_MONITORS", 50, 1, 500),
        )
