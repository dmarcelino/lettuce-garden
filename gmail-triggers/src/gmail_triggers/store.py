"""SQLite in /data: rules, poll state, handled message ids, held matches, the run
log (which doubles as the executor's request queue), gaps in history, and what
the executor last reported (agents, heartbeat). /data is mounted in this
sidecar only, so agents can't add, change or read rules."""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .config import HANDLED_KEEP_SECONDS, MAX_PENDING_PER_RULE, RUNS_KEEP_PER_RULE

RULE_ID = re.compile(r"[0-9a-f]{16}")
# Messages kept per run for the history page.
MAX_RUN_MESSAGES = 50

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  enabled INTEGER NOT NULL,
  label_ids TEXT NOT NULL,
  label_names TEXT NOT NULL,
  senders TEXT NOT NULL,
  verified_only INTEGER NOT NULL,
  agent_id TEXT NOT NULL,
  agent_name TEXT NOT NULL,
  conversation TEXT NOT NULL,
  prompt TEXT NOT NULL,
  max_per_hour INTEGER NOT NULL,
  created INTEGER NOT NULL,
  updated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS handled (message_id TEXT PRIMARY KEY, at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS pending (
  rule_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  sender TEXT NOT NULL,
  subject TEXT NOT NULL,
  at INTEGER NOT NULL,
  PRIMARY KEY (rule_id, message_id)
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  rule_id TEXT NOT NULL,
  at INTEGER NOT NULL,
  test INTEGER NOT NULL,
  messages TEXT NOT NULL,
  total INTEGER NOT NULL,
  status TEXT NOT NULL,
  task_id TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  done_at INTEGER,
  -- The request the executor fetches (JSON), cleared once it is claimed.
  request TEXT NOT NULL DEFAULT '',
  claimed_at INTEGER
);
CREATE INDEX IF NOT EXISTS runs_by_status ON runs (status, at);
CREATE INDEX IF NOT EXISTS runs_by_rule ON runs (rule_id, at);
CREATE TABLE IF NOT EXISTS gaps (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  start INTEGER NOT NULL,
  end INTEGER NOT NULL,
  kind TEXT NOT NULL,
  skipped INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS agents (id TEXT PRIMARY KEY, name TEXT NOT NULL, pos INTEGER NOT NULL);
"""

# queued: waiting for the executor; claimed: handed to it, waiting for its result.
QUEUED, CLAIMED, OK, FAILED, LOST = "queued", "claimed", "ok", "failed", "lost"
# Gap kinds: Gmail's history expired; Google was unusable for a while; too many at once.
GAP_EXPIRED, GAP_PAUSED, GAP_SKIPPED = "expired", "paused", "skipped"


@dataclass
class Rule:
    name: str
    label_ids: list[str]
    agent_id: str
    prompt: str
    label_names: dict[str, str] = field(default_factory=dict)
    senders: list[str] = field(default_factory=list)
    verified_only: bool = True
    agent_name: str = ""
    conversation: str = "new"
    max_per_hour: int = 6
    enabled: bool = True
    id: str = ""
    created: int = 0
    updated: int = 0

    def label_display(self) -> list[str]:
        return [self.label_names.get(lid, lid) for lid in self.label_ids]


@dataclass(frozen=True)
class Held:
    """One message waiting for its rule's next allowed run (or about to be sent)."""

    id: str
    sender: str
    subject: str


@dataclass
class Run:
    id: str
    rule_id: str
    at: int
    test: bool
    messages: list[dict[str, str]]
    total: int
    status: str
    task_id: str = ""
    error: str = ""
    done_at: int | None = None
    request: dict[str, Any] | None = None


@dataclass(frozen=True)
class Agent:
    id: str
    name: str


@dataclass(frozen=True)
class Heartbeat:
    at: int
    letta_ok: bool
    error: str


def new_request_id() -> str:
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

    # -- rules ----------------------------------------------------------------
    @staticmethod
    def _rule(row: sqlite3.Row) -> Rule:
        return Rule(
            id=row["id"],
            name=row["name"],
            enabled=bool(row["enabled"]),
            label_ids=json.loads(row["label_ids"]),
            label_names=json.loads(row["label_names"]),
            senders=json.loads(row["senders"]),
            verified_only=bool(row["verified_only"]),
            agent_id=row["agent_id"],
            agent_name=row["agent_name"],
            conversation=row["conversation"],
            prompt=row["prompt"],
            max_per_hour=row["max_per_hour"],
            created=row["created"],
            updated=row["updated"],
        )

    def rules(self, enabled_only: bool = False) -> list[Rule]:
        sql = "SELECT * FROM rules" + (" WHERE enabled = 1" if enabled_only else "") + " ORDER BY created, rowid"
        return [self._rule(r) for r in self._db.execute(sql)]

    def rule(self, rule_id: str) -> Rule | None:
        if not RULE_ID.fullmatch(rule_id):
            return None
        row = self._db.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
        return self._rule(row) if row else None

    def save_rule(self, rule: Rule) -> str:
        """Inserts (no id) or updates (id) a validated rule; returns its id."""
        now = self.now()
        values = (
            rule.name,
            int(rule.enabled),
            json.dumps(rule.label_ids),
            json.dumps(rule.label_names),
            json.dumps(rule.senders),
            int(rule.verified_only),
            rule.agent_id,
            rule.agent_name,
            rule.conversation,
            rule.prompt,
            rule.max_per_hour,
        )
        with self._db:
            if rule.id:
                self._db.execute(
                    "UPDATE rules SET name=?, enabled=?, label_ids=?, label_names=?, senders=?, verified_only=?, "
                    "agent_id=?, agent_name=?, conversation=?, prompt=?, max_per_hour=?, updated=? WHERE id=?",
                    (*values, now, rule.id),
                )
                if not rule.enabled:
                    self._db.execute("DELETE FROM pending WHERE rule_id = ?", (rule.id,))
                return rule.id
            rule_id = uuid.uuid4().hex[:16]
            self._db.execute(
                "INSERT INTO rules (name, enabled, label_ids, label_names, senders, verified_only, agent_id, "
                "agent_name, conversation, prompt, max_per_hour, created, updated, id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*values, now, now, rule_id),
            )
            return rule_id

    def set_enabled(self, rule_id: str, enabled: bool) -> None:
        with self._db:
            self._db.execute("UPDATE rules SET enabled=?, updated=? WHERE id=?", (int(enabled), self.now(), rule_id))
            if not enabled:
                # Turned off: held matches are dropped, not sent when it comes back on.
                self._db.execute("DELETE FROM pending WHERE rule_id = ?", (rule_id,))

    def delete_rule(self, rule_id: str) -> None:
        with self._db:
            for table, col in (("rules", "id"), ("pending", "rule_id"), ("runs", "rule_id")):
                self._db.execute(f"DELETE FROM {table} WHERE {col} = ?", (rule_id,))

    def count_rules(self) -> int:
        return self._db.execute("SELECT COUNT(*) FROM rules").fetchone()[0]

    # -- state ----------------------------------------------------------------
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

    # -- handled messages -----------------------------------------------------
    def unhandled(self, message_ids: Iterable[str]) -> list[str]:
        ids = list(dict.fromkeys(message_ids))
        known: set[str] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            known.update(
                r[0] for r in self._db.execute(f"SELECT message_id FROM handled WHERE message_id IN ({marks})", chunk)
            )
        return [m for m in ids if m not in known]

    # -- held matches ---------------------------------------------------------
    def pending(self, rule_id: str) -> list[Held]:
        rows = self._db.execute(
            "SELECT message_id, sender, subject FROM pending WHERE rule_id = ? ORDER BY at, rowid", (rule_id,)
        )
        return [Held(r[0], r[1], r[2]) for r in rows]

    def pending_counts(self) -> dict[str, int]:
        return {r[0]: r[1] for r in self._db.execute("SELECT rule_id, COUNT(*) FROM pending GROUP BY rule_id")}

    # -- runs -----------------------------------------------------------------
    @staticmethod
    def _run(row: sqlite3.Row) -> Run:
        return Run(
            id=row["id"],
            rule_id=row["rule_id"],
            at=row["at"],
            test=bool(row["test"]),
            messages=json.loads(row["messages"]),
            total=row["total"],
            status=row["status"],
            task_id=row["task_id"],
            error=row["error"],
            done_at=row["done_at"],
        )

    def runs_since(self, rule_id: str, since: int) -> int:
        return self._db.execute(
            "SELECT COUNT(*) FROM runs WHERE rule_id = ? AND test = 0 AND at > ?", (rule_id, since)
        ).fetchone()[0]

    def runs(self, rule_id: str, limit: int = 100) -> list[Run]:
        rows = self._db.execute(
            "SELECT * FROM runs WHERE rule_id = ? ORDER BY at DESC, rowid DESC LIMIT ?", (rule_id, limit)
        )
        return [self._run(r) for r in rows]

    def last_runs(self) -> dict[str, Run]:
        rows = self._db.execute(
            "SELECT * FROM runs r WHERE rowid = (SELECT rowid FROM runs WHERE rule_id = r.rule_id "
            "ORDER BY at DESC, rowid DESC LIMIT 1)"
        )
        return {r["rule_id"]: self._run(r) for r in rows}

    def run(self, run_id: str) -> Run | None:
        row = self._db.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._run(row) if row else None

    def failures_since(self, since: int) -> int:
        return self._db.execute(
            "SELECT COUNT(*) FROM runs WHERE status IN (?, ?) AND at > ?", (FAILED, LOST, since)
        ).fetchone()[0]

    def add_run(self, run: Run) -> None:
        with self._db:
            self._insert_run(run)

    def _insert_run(self, run: Run) -> None:
        self._db.execute(
            "INSERT INTO runs (id, rule_id, at, test, messages, total, status, task_id, error, done_at, request) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run.id,
                run.rule_id,
                run.at,
                int(run.test),
                json.dumps(run.messages[:MAX_RUN_MESSAGES]),
                run.total,
                run.status,
                run.task_id,
                run.error,
                run.done_at,
                json.dumps(run.request) if run.request else "",
            ),
        )

    # -- the executor's queue -------------------------------------------------
    def claim(self, limit: int) -> list[dict[str, Any]]:
        """The oldest waiting requests, marked claimed: each is handed out once at
        most, since a duplicate turn is worse than a lost one."""
        now = self.now()
        with self._db:
            rows = self._db.execute(
                "SELECT id, request FROM runs WHERE status = ? AND request != '' ORDER BY at, rowid LIMIT ?",
                (QUEUED, limit),
            ).fetchall()
            self._db.executemany(
                "UPDATE runs SET status = ?, claimed_at = ?, request = '' WHERE id = ? AND status = ?",
                [(CLAIMED, now, r["id"], QUEUED) for r in rows],
            )
        return [json.loads(r["request"]) for r in rows]

    def finish_run(self, run_id: str, ok: bool, task_id: str = "", error: str = "") -> bool:
        """Records a claimed run's outcome; False when there was no such claimed run.

        A run marked lost still takes its result: a claim of many requests can
        outlast LOST_AFTER_SECONDS when letta is slow, and the task did run.
        """
        with self._db:
            cur = self._db.execute(
                "UPDATE runs SET status=?, task_id=?, error=?, done_at=? WHERE id=? AND status IN (?, ?)",
                (OK if ok else FAILED, task_id, error, self.now(), run_id, CLAIMED, LOST),
            )
            return cur.rowcount == 1

    def expire_claims(self, after: int) -> None:
        """Claimed runs with no result for `after` seconds are lost."""
        now = self.now()
        with self._db:
            self._db.execute(
                "UPDATE runs SET status=?, error=?, done_at=? WHERE status=? AND claimed_at < ?",
                (LOST, "Lettuce took the request but never reported back.", now, CLAIMED, now - after),
            )

    # -- what the executor reports ---------------------------------------------
    def set_agents(self, agents: Sequence[Agent]) -> None:
        with self._db:
            self._db.execute("DELETE FROM agents")
            self._db.executemany(
                "INSERT OR IGNORE INTO agents (id, name, pos) VALUES (?, ?, ?)",
                [(a.id, a.name, i) for i, a in enumerate(agents)],
            )
            self._set({"agents_at": str(self.now()), "agents_asked": None})

    def ask_for_agents(self) -> None:
        """The trigger form was shown: the executor should send a fresh list."""
        self.set(agents_asked="1")

    def agents_wanted(self, fresh_for: int) -> bool:
        at = self.get("agents_at")
        return at is None or self.now() - int(at) > fresh_for or self.get("agents_asked") is not None

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

    # -- one poll, committed together -----------------------------------------
    def record_poll(
        self,
        *,
        handled: Sequence[str],
        history_id: str,
        held: dict[str, Sequence[Held]],
        sent: Sequence[Run],
        skipped: int = 0,
    ) -> None:
        """Marks messages handled, moves the history position, holds matches over
        the limit, queues the runs for the executor and records messages left
        unchecked, in one transaction: after a crash, either all of it happened
        or none of it."""
        now = self.now()
        with self._db:
            self._db.executemany(
                "INSERT OR IGNORE INTO handled (message_id, at) VALUES (?, ?)", [(m, now) for m in handled]
            )
            for rule_id, items in held.items():
                self._db.executemany(
                    "INSERT OR IGNORE INTO pending (rule_id, message_id, sender, subject, at) VALUES (?, ?, ?, ?, ?)",
                    [(rule_id, h.id, h.sender, h.subject, now) for h in items],
                )
                # Oldest kept: a flood past the cap is dropped, not queued forever.
                self._db.execute(
                    "DELETE FROM pending WHERE rule_id = ? AND rowid NOT IN "
                    "(SELECT rowid FROM pending WHERE rule_id = ? ORDER BY at, rowid LIMIT ?)",
                    (rule_id, rule_id, MAX_PENDING_PER_RULE),
                )
            for run in sent:
                self._db.execute("DELETE FROM pending WHERE rule_id = ?", (run.rule_id,))
                self._insert_run(run)
            if skipped:
                self._db.execute(
                    "INSERT INTO gaps (start, end, kind, skipped) VALUES (?, ?, ?, ?)", (now, now, GAP_SKIPPED, skipped)
                )
            self._set({"history_id": history_id, "last_ok": str(now), "paused_since": None})
            self._prune(now)

    def _prune(self, now: int) -> None:
        self._db.execute("DELETE FROM handled WHERE at < ?", (now - HANDLED_KEEP_SECONDS,))
        self._db.execute("DELETE FROM gaps WHERE end < ?", (now - HANDLED_KEEP_SECONDS,))
        self._db.execute(
            "DELETE FROM runs WHERE rowid IN (SELECT rowid FROM (SELECT rowid, ROW_NUMBER() OVER "
            "(PARTITION BY rule_id ORDER BY at DESC, rowid DESC) AS n FROM runs) WHERE n > ?)",
            (RUNS_KEEP_PER_RULE,),
        )

    # -- gaps -----------------------------------------------------------------
    def restart_at(self, history_id: str, kind: str) -> None:
        """Starts again from `history_id` (now), recording mail since the last
        good poll as not checked."""
        now = self.now()
        last_ok = int(self.get("last_ok") or now)
        with self._db:
            self._db.execute("INSERT INTO gaps (start, end, kind) VALUES (?, ?, ?)", (last_ok, now, kind))
            self._set({"history_id": history_id, "last_ok": str(now), "paused_since": None})

    def gaps_since(self, since: int) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT start, end, kind, skipped FROM gaps WHERE end > ? ORDER BY end DESC, id DESC", (since,)
        )
        return [{"start": r[0], "end": r[1], "kind": r[2], "skipped": r[3]} for r in rows]
