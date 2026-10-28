"""The agent list the executor last sent, kept in memory only: after a restart
the page says it is on its way, and the executor sends one within a minute."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence


class AgentList:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._agents: list[tuple[str, str]] | None = None
        self._at = 0.0
        # Page loads so far, and how many of them the executor had seen when it last
        # read the status: a list answers only the asks made before that read.
        self._asks = 0
        self._seen = 0
        self._answered = 0

    def set(self, agents: Sequence[tuple[str, str]]) -> None:
        """(id, name) pairs, already validated (garden_common.executor.read_agents)."""
        with self._lock:
            self._agents = sorted(agents, key=lambda a: (a[1].lower(), a[0]))
            self._at = self._clock()
            self._answered = self._seen

    def ask(self) -> None:
        """The page was opened: it should list agents made since the last list."""
        with self._lock:
            self._asks += 1

    def wanted(self, fresh_for: float) -> bool:
        """Read by the executor before it lists the agents."""
        with self._lock:
            self._seen = self._asks
            return self._agents is None or self._clock() - self._at > fresh_for or self._asks > self._answered

    def agents(self) -> list[tuple[str, str]] | None:
        """Sorted by name (ignoring case), then id; None until a list arrives."""
        with self._lock:
            return None if self._agents is None else list(self._agents)
