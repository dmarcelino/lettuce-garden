"""The executor's API at /executor/v1/ (docs/gmail-triggers.md): the request
routes every queue-serving garden sidecar shares (garden_common.executor),
outside /gmail-triggers, so the tunnel never routes them, no Access check, and
every call needs the stack's executor token.

Anything a request does, agents can already do with `letta cron add`; they
cannot create requests here, only take, drop or misreport them.
"""

from __future__ import annotations

from typing import Any

from garden_common.executor import request_routes
from starlette.routing import Route

from .config import MAX_CLAIM
from .store import Agent, Store


class _Queue:
    """The store's run log, as the shared routes see a request queue."""

    def __init__(self, store: Store):
        self._store = store

    def claim(self, limit: int) -> list[dict[str, Any]]:
        return self._store.claim(limit)

    def agents_wanted(self) -> bool:
        return self._store.agents_wanted()

    def finish(self, request_id: str, ok: bool, task_id: str, error: str) -> bool:
        return self._store.finish_run(request_id, ok, task_id=task_id, error=error)

    def set_agents(self, agents: list[tuple[str, str]]) -> None:
        self._store.set_agents([Agent(aid, name) for aid, name in agents])

    def set_heartbeat(self, letta_ok: bool, error: str) -> None:
        self._store.set_heartbeat(letta_ok, error)


def executor_routes(store: Store, token: str) -> list[Route]:
    return request_routes(_Queue(store), token, MAX_CLAIM)
