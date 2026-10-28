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
        self._asked = False

    def set(self, agents: Sequence[tuple[str, str]]) -> None:
        """(id, name) pairs, already validated (garden_common.executor.read_agents)."""
        with self._lock:
            self._agents = sorted(agents, key=lambda a: (a[1].lower(), a[0]))
            self._at = self._clock()
            self._asked = False

    def ask(self) -> None:
        """The page was opened: it should list agents made since the last list."""
        with self._lock:
            self._asked = True

    def wanted(self, fresh_for: float) -> bool:
        with self._lock:
            return self._agents is None or self._clock() - self._at > fresh_for or self._asked

    def agents(self) -> list[tuple[str, str]] | None:
        """Sorted by name (ignoring case), then id; None until a list arrives."""
        with self._lock:
            return None if self._agents is None else list(self._agents)
