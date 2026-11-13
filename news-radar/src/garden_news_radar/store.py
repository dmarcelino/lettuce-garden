"""SQLite in /data (the named volume news-radar-data, mounted by this service alone):
sources and their spec history, items, health, monitors, reports (which double as
the executor's request queue), passing tests, and what the executor last reported
(agents, heartbeat)."""

from __future__ import annotations

import json
import re
import sqlite3
import statistics
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from garden_common.executor import AgentAsks

from .config import (
    HISTORY_RUNS,
    ITEMS_KEEP_SECONDS,
    PAUSE_AFTER_FAILURES,
    REPORTS_KEEP_PER_GROUP,
    SPEC_VERSIONS,
    UNHEALTHY_EMPTY_MEDIAN,
    UNHEALTHY_FAILURES,
)
from .extract import Item

MONITOR_ID = re.compile(r"[0-9a-f]{16}")
REPORT_ID = re.compile(r"[0-9a-f]{32}")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  name TEXT PRIMARY KEY,
  spec TEXT NOT NULL,
  created INTEGER NOT NULL,
  updated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS source_versions (
  name TEXT NOT NULL,
  at INTEGER NOT NULL,
  spec TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS source_versions_by_name ON source_versions (name, at);
CREATE TABLE IF NOT EXISTS source_state (
  name TEXT PRIMARY KEY,
  etag TEXT NOT NULL DEFAULT '',
  last_modified TEXT NOT NULL DEFAULT '',
  last_run INTEGER,
  last_ok INTEGER,
  failures INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  counts TEXT NOT NULL DEFAULT '[]',
  fill TEXT NOT NULL DEFAULT '{}',
  sample TEXT NOT NULL DEFAULT '',
  unhealthy_since INTEGER,
  unhealthy_reason TEXT NOT NULL DEFAULT '',
  alerted_at INTEGER
);
CREATE TABLE IF NOT EXISTS items (
  source TEXT NOT NULL,
  key TEXT NOT NULL,
  data TEXT NOT NULL,
  watched TEXT NOT NULL,
  first_seen INTEGER NOT NULL,
  changed_at INTEGER NOT NULL,
  last_seen INTEGER NOT NULL,
  PRIMARY KEY (source, key)
);
CREATE INDEX IF NOT EXISTS items_by_change ON items (source, changed_at);
CREATE TABLE IF NOT EXISTS monitors (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  name TEXT NOT NULL,
  sources TEXT NOT NULL,
  include TEXT NOT NULL,
  exclude TEXT NOT NULL,
  days_ahead INTEGER,
  news INTEGER NOT NULL,
  schedule TEXT NOT NULL,
  conversation TEXT NOT NULL,
  created INTEGER NOT NULL,
  updated INTEGER NOT NULL,
  delivered_upto INTEGER NOT NULL DEFAULT 0,
  failures INTEGER NOT NULL DEFAULT 0,
  paused INTEGER NOT NULL DEFAULT 0,
  last_empty INTEGER,
  UNIQUE (agent_id, name)
);
CREATE TABLE IF NOT EXISTS reports (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  schedule TEXT NOT NULL,
  conversation TEXT NOT NULL,
  monitors TEXT NOT NULL,
  alerted TEXT NOT NULL,
  at INTEGER NOT NULL,
  upto INTEGER NOT NULL,
  items INTEGER NOT NULL,
  status TEXT NOT NULL,
  task_id TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  done_at INTEGER,
  -- The request the executor fetches (JSON), cleared once it is claimed.
  request TEXT NOT NULL DEFAULT '',
  claimed_at INTEGER,
  -- Every row of the digest, for digest_more.
  digest TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS reports_by_status ON reports (status, at);
CREATE INDEX IF NOT EXISTS reports_by_agent ON reports (agent_id, at);
CREATE TABLE IF NOT EXISTS tests (fingerprint TEXT PRIMARY KEY, at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS agents (id TEXT PRIMARY KEY, name TEXT NOT NULL, pos INTEGER NOT NULL);
"""

# queued: waiting for the executor; claimed: handed to it, waiting for its result.
QUEUED, CLAIMED, OK, FAILED, LOST = "queued", "claimed", "ok", "failed", "lost"


@dataclass
class Monitor:
    agent_id: str
    name: str
    sources: list[str]
    schedule: str
    conversation: str = "new"
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    days_ahead: int | None = None
    news: bool = False
    id: str = ""
    created: int = 0
    updated: int = 0
    delivered_upto: int = 0
    failures: int = 0
    paused: bool = False
    last_empty: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "agent_id": self.agent_id,
            "sources": self.sources,
            "schedule": self.schedule,
            "conversation": self.conversation,
            "include": self.include,
            "exclude": self.exclude,
            "days_ahead": self.days_ahead,
            "news": self.news,
            "paused": self.paused,
        }


@dataclass
class SourceState:
    name: str
    etag: str = ""
    last_modified: str = ""
    last_run: int | None = None
    last_ok: int | None = None
    failures: int = 0
    last_error: str = ""
    counts: list[int] = field(default_factory=list)
    fill: dict[str, float] = field(default_factory=dict)
    sample: str = ""
    unhealthy_since: int | None = None
    unhealthy_reason: str = ""
    alerted_at: int | None = None

    @property
    def healthy(self) -> bool:
        return self.unhealthy_since is None


@dataclass
class Report:
    id: str
    agent_id: str
    schedule: str
    conversation: str
    monitors: list[str]
    alerted: list[str]
    at: int
    upto: int
    items: int
    status: str
    task_id: str = ""
    error: str = ""
    done_at: int | None = None
    request: dict[str, Any] | None = None
    digest: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class StoredItem:
    source: str
    key: str
    data: dict[str, Any]
    first_seen: int
    changed_at: int
    last_seen: int


@dataclass(frozen=True)
class Agent:
    id: str
    name: str


@dataclass(frozen=True)
class Heartbeat:
    at: int
    letta_ok: bool
    error: str


def new_report_id() -> str:
    return uuid.uuid4().hex


class Store:
    def __init__(self, path: str, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL" if path != ":memory:" else "PRAGMA journal_mode=MEMORY")
        self._db.executescript(SCHEMA)

    def now(self) -> int:
        return int(self._clock())

    # -- key/value state --------------------------------------------------------
    def get(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, **values: str | None) -> None:
        with self._db:
            self._set(values)

    def _set(self, values: dict[str, str | None]) -> None:
        for key, value in values.items():
            if value is None:
                self._db.execute("DELETE FROM state WHERE key = ?", (key,))
            else:
                self._db.execute(
                    "INSERT INTO state (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    # -- sources ----------------------------------------------------------------
    def source_specs(self) -> dict[str, dict[str, Any]]:
        rows = self._db.execute("SELECT name, spec FROM sources ORDER BY name")
        return {r["name"]: json.loads(r["spec"]) for r in rows}

    def source_spec(self, name: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT spec FROM sources WHERE name = ?", (name,)).fetchone()
        return json.loads(row[0]) if row else None

    def count_sources(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM sources").fetchone()[0]

    def put_source(self, name: str, spec: dict[str, Any], reset: bool) -> None:
        """Creates or replaces a source; keeps its last SPEC_VERSIONS specs. `reset`
        forgets its conditional-request state and health (a new URL or extraction)."""
        now = self.now()
        text = json.dumps(spec, sort_keys=True)
        with self._db:
            self._db.execute(
                "INSERT INTO sources (name, spec, created, updated) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET spec = excluded.spec, updated = excluded.updated",
                (name, text, now, now),
            )
            self._db.execute("INSERT INTO source_versions (name, at, spec) VALUES (?, ?, ?)", (name, now, text))
            self._db.execute(
                "DELETE FROM source_versions WHERE name = ? AND rowid NOT IN "
                "(SELECT rowid FROM source_versions WHERE name = ? ORDER BY at DESC, rowid DESC LIMIT ?)",
                (name, name, SPEC_VERSIONS),
            )
            if reset:
                self._db.execute("DELETE FROM source_state WHERE name = ?", (name,))
            self._db.execute("INSERT OR IGNORE INTO source_state (name) VALUES (?)", (name,))

    def source_versions(self, name: str) -> list[tuple[int, dict[str, Any]]]:
        rows = self._db.execute(
            "SELECT at, spec FROM source_versions WHERE name = ? ORDER BY at DESC, rowid DESC", (name,)
        )
        return [(r["at"], json.loads(r["spec"])) for r in rows]

    def delete_source(self, name: str) -> None:
        with self._db:
            for table in ("sources", "source_versions", "source_state"):
                self._db.execute(f"DELETE FROM {table} WHERE name = ?", (name,))
            self._db.execute("DELETE FROM items WHERE source = ?", (name,))

    def source_users(self, name: str) -> list[Monitor]:
        return [m for m in self.monitors() if name in m.sources]

    # -- source state and health -----------------------------------------------
    @staticmethod
    def _state(row: sqlite3.Row) -> SourceState:
        return SourceState(
            name=row["name"],
            etag=row["etag"],
            last_modified=row["last_modified"],
            last_run=row["last_run"],
            last_ok=row["last_ok"],
            failures=row["failures"],
            last_error=row["last_error"],
            counts=json.loads(row["counts"]),
            fill=json.loads(row["fill"]),
            sample=row["sample"],
            unhealthy_since=row["unhealthy_since"],
            unhealthy_reason=row["unhealthy_reason"],
            alerted_at=row["alerted_at"],
        )

    def source_state(self, name: str) -> SourceState:
        row = self._db.execute("SELECT * FROM source_state WHERE name = ?", (name,)).fetchone()
        return self._state(row) if row else SourceState(name)

    def source_states(self) -> dict[str, SourceState]:
        return {r["name"]: self._state(r) for r in self._db.execute("SELECT * FROM source_state")}

    def record_run(
        self,
        name: str,
        *,
        items: Sequence[Item] | None,
        watch: tuple[str, ...],
        required: tuple[str, ...],
        error: str = "",
        not_modified: bool = False,
        etag: str = "",
        last_modified: str = "",
        sample: str = "",
        fill: dict[str, float] | None = None,
    ) -> None:
        """One run's outcome in one transaction: new and changed items, the
        conditional-request state, and the source's health."""
        now = self.now()
        state = self.source_state(name)
        with self._db:
            self._db.execute("INSERT OR IGNORE INTO source_state (name) VALUES (?)", (name,))
            reason = ""
            if error:
                failures = state.failures + 1
                self._db.execute(
                    "UPDATE source_state SET last_run=?, failures=?, last_error=?, sample=? WHERE name=?",
                    (now, failures, error, sample, name),
                )
                if failures >= UNHEALTHY_FAILURES:
                    reason = f"{failures} failed runs in a row; the last: {error}"
                elif state.unhealthy_since is not None:
                    reason = state.unhealthy_reason
            elif not_modified:
                self._db.execute(
                    "UPDATE source_state SET last_run=?, last_ok=?, failures=0, last_error='' WHERE name=?",
                    (now, now, name),
                )
            else:
                assert items is not None
                counts = [*state.counts, len(items)][-HISTORY_RUNS:]
                self._db.execute(
                    "UPDATE source_state SET last_run=?, last_ok=?, failures=0, last_error='', etag=?, "
                    "last_modified=?, counts=?, fill=?, sample=? WHERE name=?",
                    (now, now, etag, last_modified, json.dumps(counts), json.dumps(fill or {}), sample, name),
                )
                self._store_items(name, items, watch, now)
                previous = state.counts[-HISTORY_RUNS:]
                if not items and previous and statistics.median(previous) >= UNHEALTHY_EMPTY_MEDIAN:
                    reason = f"no items, where runs usually find about {int(statistics.median(previous))}"
                elif items:
                    for f in required:
                        missing = sum(1 for i in items if not i.fields.get(f))
                        if missing * 2 > len(items):
                            reason = f"{missing} of {len(items)} items have no {f}"
                            break
            if reason:
                self._db.execute(
                    "UPDATE source_state SET unhealthy_since = COALESCE(unhealthy_since, ?), unhealthy_reason = ? "
                    "WHERE name = ?",
                    (now, reason, name),
                )
            elif error and state.unhealthy_since is None:
                pass  # a failure or two: not unhealthy yet
            else:
                self._db.execute(
                    "UPDATE source_state SET unhealthy_since = NULL, unhealthy_reason = '', alerted_at = NULL "
                    "WHERE name = ?",
                    (name,),
                )
            self._db.execute("DELETE FROM items WHERE last_seen < ?", (now - ITEMS_KEEP_SECONDS,))

    def _store_items(self, source: str, items: Sequence[Item], watch: tuple[str, ...], now: int) -> None:
        for item in items:
            data = json.dumps(item.as_dict())
            watched = item.watched(watch)
            row = self._db.execute(
                "SELECT watched FROM items WHERE source = ? AND key = ?", (source, item.key)
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO items (source, key, data, watched, first_seen, changed_at, last_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (source, item.key, data, watched, now, now, now),
                )
            elif row["watched"] != watched:
                self._db.execute(
                    "UPDATE items SET data=?, watched=?, changed_at=?, last_seen=? WHERE source=? AND key=?",
                    (data, watched, now, now, source, item.key),
                )
            else:
                self._db.execute(
                    "UPDATE items SET data=?, last_seen=? WHERE source=? AND key=?", (data, now, source, item.key)
                )

    def known_url(self, source: str, url: str, seen_since: int) -> bool:
        """Whether an item of the source with this URL was seen since then: a
        followed page is read again once that is older, so its item stays current
        and isn't dropped as gone."""
        row = self._db.execute(
            "SELECT 1 FROM items WHERE source = ? AND json_extract(data, '$.url') = ? AND last_seen >= ? LIMIT 1",
            (source, url, seen_since),
        ).fetchone()
        return row is not None

    def items_changed(self, sources: Sequence[str], since: int) -> list[StoredItem]:
        if not sources:
            return []
        marks = ",".join("?" * len(sources))
        rows = self._db.execute(
            f"SELECT * FROM items WHERE source IN ({marks}) AND changed_at > ? ORDER BY changed_at DESC, rowid DESC",
            (*sources, since),
        )
        return [
            StoredItem(r["source"], r["key"], json.loads(r["data"]), r["first_seen"], r["changed_at"], r["last_seen"])
            for r in rows
        ]

    def count_items(self, source: str) -> int:
        return self._db.execute("SELECT COUNT(*) FROM items WHERE source = ?", (source,)).fetchone()[0]

    # -- passing tests ------------------------------------------------------------
    def record_test(self, fingerprint: str) -> None:
        now = self.now()
        with self._db:
            self._db.execute(
                "INSERT INTO tests (fingerprint, at) VALUES (?, ?) ON CONFLICT(fingerprint) DO UPDATE SET at = ?",
                (fingerprint, now, now),
            )
            self._db.execute("DELETE FROM tests WHERE at < ?", (now - 86400,))

    def tested_since(self, fingerprint: str, since: int) -> bool:
        row = self._db.execute("SELECT at FROM tests WHERE fingerprint = ?", (fingerprint,)).fetchone()
        return row is not None and row[0] >= since

    # -- monitors -----------------------------------------------------------------
    @staticmethod
    def _monitor(row: sqlite3.Row) -> Monitor:
        return Monitor(
            id=row["id"],
            agent_id=row["agent_id"],
            name=row["name"],
            sources=json.loads(row["sources"]),
            include=json.loads(row["include"]),
            exclude=json.loads(row["exclude"]),
            days_ahead=row["days_ahead"],
            news=bool(row["news"]),
            schedule=row["schedule"],
            conversation=row["conversation"],
            created=row["created"],
            updated=row["updated"],
            delivered_upto=row["delivered_upto"],
            failures=row["failures"],
            paused=bool(row["paused"]),
            last_empty=row["last_empty"],
        )

    def monitors(self, agent_id: str | None = None) -> list[Monitor]:
        if agent_id is None:
            rows = self._db.execute("SELECT * FROM monitors ORDER BY agent_id, name")
        else:
            rows = self._db.execute("SELECT * FROM monitors WHERE agent_id = ? ORDER BY name", (agent_id,))
        return [self._monitor(r) for r in rows]

    def monitor(self, monitor_id: str) -> Monitor | None:
        if not MONITOR_ID.fullmatch(monitor_id):
            return None
        row = self._db.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,)).fetchone()
        return self._monitor(row) if row else None

    def monitor_named(self, agent_id: str, name: str) -> Monitor | None:
        row = self._db.execute("SELECT * FROM monitors WHERE agent_id = ? AND name = ?", (agent_id, name)).fetchone()
        return self._monitor(row) if row else None

    def count_monitors(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM monitors").fetchone()[0]

    def put_monitor(self, m: Monitor) -> Monitor:
        """Creates or replaces the agent's monitor of that name. A replaced one
        keeps what it has delivered, and comes back from a pause."""
        now = self.now()
        values = (
            json.dumps(m.sources),
            json.dumps(m.include),
            json.dumps(m.exclude),
            m.days_ahead,
            int(m.news),
            m.schedule,
            m.conversation,
        )
        with self._db:
            current = self.monitor_named(m.agent_id, m.name)
            if current:
                self._db.execute(
                    "UPDATE monitors SET sources=?, include=?, exclude=?, days_ahead=?, news=?, schedule=?, "
                    "conversation=?, updated=?, paused=0, failures=0 WHERE id=?",
                    (*values, now, current.id),
                )
                monitor_id = current.id
            else:
                monitor_id = uuid.uuid4().hex[:16]
                self._db.execute(
                    "INSERT INTO monitors (sources, include, exclude, days_ahead, news, schedule, conversation, "
                    "created, updated, id, agent_id, name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*values, now, now, monitor_id, m.agent_id, m.name),
                )
        stored = self.monitor(monitor_id)
        assert stored is not None
        return stored

    def delete_monitor(self, monitor_id: str) -> None:
        with self._db:
            self._db.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))

    def mark_empty(self, monitor_ids: Sequence[str], at: int) -> None:
        with self._db:
            self._db.executemany("UPDATE monitors SET last_empty = ? WHERE id = ?", [(at, i) for i in monitor_ids])

    # -- reports: the executor's queue ---------------------------------------------
    @staticmethod
    def _report(row: sqlite3.Row) -> Report:
        return Report(
            id=row["id"],
            agent_id=row["agent_id"],
            schedule=row["schedule"],
            conversation=row["conversation"],
            monitors=json.loads(row["monitors"]),
            alerted=json.loads(row["alerted"]),
            at=row["at"],
            upto=row["upto"],
            items=row["items"],
            status=row["status"],
            task_id=row["task_id"],
            error=row["error"],
            done_at=row["done_at"],
            digest=json.loads(row["digest"]),
        )

    def add_report(self, report: Report) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO reports (id, agent_id, schedule, conversation, monitors, alerted, at, upto, items, "
                "status, request, digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report.id,
                    report.agent_id,
                    report.schedule,
                    report.conversation,
                    json.dumps(report.monitors),
                    json.dumps(report.alerted),
                    report.at,
                    report.upto,
                    report.items,
                    report.status,
                    json.dumps(report.request) if report.request else "",
                    json.dumps(report.digest),
                ),
            )
            self._db.execute(
                "DELETE FROM reports WHERE agent_id = ? AND schedule = ? AND conversation = ? AND rowid NOT IN "
                "(SELECT rowid FROM reports WHERE agent_id = ? AND schedule = ? AND conversation = ? "
                "ORDER BY at DESC, rowid DESC LIMIT ?)",
                (
                    report.agent_id,
                    report.schedule,
                    report.conversation,
                    report.agent_id,
                    report.schedule,
                    report.conversation,
                    REPORTS_KEEP_PER_GROUP,
                ),
            )

    def report(self, report_id: str) -> Report | None:
        if not REPORT_ID.fullmatch(report_id):
            return None
        row = self._db.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
        return self._report(row) if row else None

    def pending_report(self, agent_id: str, schedule: str, conversation: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM reports WHERE agent_id = ? AND schedule = ? AND conversation = ? AND status IN (?, ?)",
            (agent_id, schedule, conversation, QUEUED, CLAIMED),
        ).fetchone()
        return row is not None

    def reports_since(self, agent_id: str, since: int) -> int:
        """Reports that started a turn, or may have, for the agent since then: a
        lost report counts, since Lettuce may well have made its task."""
        return self._db.execute(
            "SELECT COUNT(*) FROM reports WHERE agent_id = ? AND at > ? AND status IN (?, ?, ?, ?)",
            (agent_id, since, QUEUED, CLAIMED, OK, LOST),
        ).fetchone()[0]

    def last_reports(self) -> dict[str, Report]:
        """Each monitor's latest report."""
        out: dict[str, Report] = {}
        for row in self._db.execute("SELECT * FROM reports ORDER BY at, rowid"):
            report = self._report(row)
            for mid in report.monitors:
                out[mid] = report
        return out

    def failures_since(self, since: int) -> int:
        return self._db.execute(
            "SELECT COUNT(*) FROM reports WHERE status IN (?, ?) AND at > ?", (FAILED, LOST, since)
        ).fetchone()[0]

    def claim(self, limit: int) -> list[dict[str, Any]]:
        """The oldest waiting reports, marked claimed: each is handed out once."""
        now = self.now()
        with self._db:
            rows = self._db.execute(
                "SELECT id, request FROM reports WHERE status = ? AND request != '' ORDER BY at, rowid LIMIT ?",
                (QUEUED, limit),
            ).fetchall()
            self._db.executemany(
                "UPDATE reports SET status = ?, claimed_at = ?, request = '' WHERE id = ? AND status = ?",
                [(CLAIMED, now, r["id"], QUEUED) for r in rows],
            )
        return [json.loads(r["request"]) for r in rows]

    def finish(self, report_id: str, ok: bool, task_id: str, error: str) -> bool:
        """A claimed report's outcome. Delivered (ok): its monitors' items up to the
        report count as told, and its unhealthy sources as reported. Failed: they
        go into the next report, and a monitor Lettuce refused PAUSE_AFTER_FAILURES
        times in a row is paused. A report marked lost still takes its result."""
        now = self.now()
        with self._db:
            row = self._db.execute(
                "SELECT * FROM reports WHERE id = ? AND status IN (?, ?)", (report_id, CLAIMED, LOST)
            ).fetchone()
            if row is None:
                return False
            report = self._report(row)
            self._db.execute(
                "UPDATE reports SET status=?, task_id=?, error=?, done_at=? WHERE id=?",
                (OK if ok else FAILED, task_id, error, now, report_id),
            )
            marks = ",".join("?" * len(report.monitors))
            if ok:
                self._db.execute(
                    f"UPDATE monitors SET delivered_upto = MAX(delivered_upto, ?), failures = 0 WHERE id IN ({marks})",
                    (report.upto, *report.monitors),
                )
                self._db.executemany(
                    "UPDATE source_state SET alerted_at = ? WHERE name = ? AND unhealthy_since IS NOT NULL",
                    [(report.at, n) for n in report.alerted],
                )
            else:
                self._db.execute(
                    f"UPDATE monitors SET failures = failures + 1 WHERE id IN ({marks})", tuple(report.monitors)
                )
                self._db.execute(
                    f"UPDATE monitors SET paused = 1 WHERE failures >= ? AND id IN ({marks})",
                    (PAUSE_AFTER_FAILURES, *report.monitors),
                )
            return True

    def expire_claims(self, after: int) -> None:
        """Claimed reports with no result for `after` seconds are lost; their items
        go into the next report."""
        now = self.now()
        with self._db:
            self._db.execute(
                "UPDATE reports SET status=?, error=?, done_at=? WHERE status=? AND claimed_at < ?",
                (LOST, "Lettuce took the report but never said whether it made the task.", now, CLAIMED, now - after),
            )

    # -- what the executor reports ---------------------------------------------------
    def set_agents(self, agents: Sequence[Agent]) -> None:
        with self._db:
            self._db.execute("DELETE FROM agents")
            self._db.executemany(
                "INSERT OR IGNORE INTO agents (id, name, pos) VALUES (?, ?, ?)",
                [(a.id, a.name, i) for i, a in enumerate(agents)],
            )
            self._set_agent_asks(self._agent_asks().listed(self.now()))

    def ask_for_agents(self) -> None:
        """The page was shown: the executor should send a fresh list."""
        with self._db:
            self._set_agent_asks(self._agent_asks().ask())

    def agents_wanted(self) -> bool:
        """Read by the executor before it lists the agents."""
        with self._db:
            asks = self._agent_asks().read()
            self._set_agent_asks(asks)
        return asks.wanted(self.now())

    def _agent_asks(self) -> AgentAsks:
        at = self.get("agents_at")
        counts = (int(self.get(key) or 0) for key in ("agents_asks", "agents_seen", "agents_answered"))
        return AgentAsks(*counts, listed_at=None if at is None else int(at))

    def _set_agent_asks(self, asks: AgentAsks) -> None:
        self._set(
            {
                "agents_asks": str(asks.asks),
                "agents_seen": str(asks.seen),
                "agents_answered": str(asks.answered),
                "agents_at": None if asks.listed_at is None else str(int(asks.listed_at)),
            }
        )

    def agents(self) -> list[Agent] | None:
        """Sorted by name; None until the executor has sent a list."""
        if self.get("agents_at") is None:
            return None
        rows = self._db.execute("SELECT id, name FROM agents ORDER BY lower(name), id")
        return [Agent(r[0], r[1]) for r in rows]

    def set_heartbeat(self, letta_ok: bool, error: str) -> None:
        with self._db:
            self._set(
                {"heartbeat_at": str(self.now()), "heartbeat_ok": "1" if letta_ok else "0", "heartbeat_error": error}
            )

    def heartbeat(self) -> Heartbeat | None:
        at = self.get("heartbeat_at")
        if at is None:
            return None
        return Heartbeat(int(at), self.get("heartbeat_ok") == "1", self.get("heartbeat_error") or "")


class ExecutorQueue:
    """The store as garden_common.executor.request_routes sees a request queue."""

    def __init__(self, store: Store):
        self._store = store

    def claim(self, limit: int) -> list[dict[str, Any]]:
        return self._store.claim(limit)

    def agents_wanted(self) -> bool:
        return self._store.agents_wanted()

    def finish(self, request_id: str, ok: bool, task_id: str, error: str) -> bool:
        return self._store.finish(request_id, ok, task_id, error)

    def set_agents(self, agents: list[tuple[str, str]]) -> None:
        self._store.set_agents([Agent(aid, name) for aid, name in agents])

    def set_heartbeat(self, letta_ok: bool, error: str) -> None:
        self._store.set_heartbeat(letta_ok, error)
