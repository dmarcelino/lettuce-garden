"""The executor's API at /executor/v1/ (docs/gmail-triggers.md), behind the
guard every garden executor API shares (garden_common.executor): outside
/gmail-triggers, so the tunnel never routes it, no Access check, and every call
needs the stack's executor token.

Anything a request does, agents can already do with `letta cron add`; they
cannot create requests here, only take, drop or misreport them.
"""

from __future__ import annotations

import re
from typing import Any

from garden_common.executor import (
    EXECUTOR_PREFIX,
    MAX_BODY,
    NO_STORE,
    BadRequest,
    done,
    error,
    guard,
    read_agents,
    read_json,
)
from garden_common.text import CONTROL, one_line
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .config import AGENTS_FRESH_SECONDS, MAX_CLAIM
from .store import Agent, Store

MAX_ERROR = 500
MAX_TASK_ID = 200

REQUEST_ID = re.compile(r"[0-9a-f]{32}")


def _optional_text(obj: dict[str, Any], key: str, limit: int) -> str:
    value = obj.get(key, "")
    if not isinstance(value, str) or len(value) > limit:
        raise BadRequest()
    return one_line(value, limit)


def executor_routes(store: Store, token: str) -> list[Route]:
    guarded = guard(token)

    async def requests(request: Request) -> Response:
        if request.method != "GET":
            # Starlette answers HEAD on GET routes; a HEAD must not claim anything.
            return error(405, "method not allowed")
        body = {
            "v": 1,
            "requests": store.claim(MAX_CLAIM),
            # The executor runs `letta agents list` only when asked.
            "agents_wanted": store.agents_wanted(AGENTS_FRESH_SECONDS),
        }
        return JSONResponse(body, headers=NO_STORE)

    async def results(request: Request) -> Response:
        obj = await read_json(request, MAX_BODY)
        rid, ok = obj.get("id"), obj.get("ok")
        if not isinstance(rid, str) or not REQUEST_ID.fullmatch(rid) or not isinstance(ok, bool):
            raise BadRequest()
        task_id = error_text = ""
        if ok:
            task_id = obj.get("task_id", "")
            if not isinstance(task_id, str) or len(task_id) > MAX_TASK_ID or CONTROL.search(task_id):
                raise BadRequest()
            task_id = one_line(task_id, MAX_TASK_ID)
        else:
            error_text = _optional_text(obj, "error", MAX_ERROR) or "Lettuce reported an error without details."
        if not store.finish_run(rid, ok, task_id=task_id, error=error_text):
            return error(404, "no such claimed request")
        return done()

    async def agents(request: Request) -> Response:
        store.set_agents([Agent(aid, name) for aid, name in await read_agents(request)])
        return done()

    async def heartbeat(request: Request) -> Response:
        obj = await read_json(request, MAX_BODY)
        letta_ok = obj.get("letta_ok")
        if not isinstance(letta_ok, bool):
            raise BadRequest()
        store.set_heartbeat(letta_ok, _optional_text(obj, "error", MAX_ERROR))
        return done()

    return [
        Route(f"{EXECUTOR_PREFIX}/requests", guarded(requests), methods=["GET"]),
        Route(f"{EXECUTOR_PREFIX}/results", guarded(results), methods=["POST"]),
        Route(f"{EXECUTOR_PREFIX}/agents", guarded(agents), methods=["PUT"]),
        Route(f"{EXECUTOR_PREFIX}/heartbeat", guarded(heartbeat), methods=["POST"]),
    ]
