"""The executor's API at /executor/v1/ (docs/gmail-triggers.md).

It sits outside /garden/gmail-triggers, so the tunnel (which forwards only that
prefix) never routes it, and no Access check applies. Instead every call needs
the stack's executor token. Agents are root in the app-server and can read that
token, so nothing here is trusted beyond "this is what the executor (or an
agent) said": the token keeps out the other containers on the stack network.
Anything a request does, agents can already do with `letta cron add`; they
cannot create requests here, only take, drop or misreport them.
"""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .config import AGENTS_FRESH_SECONDS, EXECUTOR_PREFIX, MAX_CLAIM
from .store import Agent, Store
from .text import CONTROL, one_line

MAX_BODY = 16 * 1024
MAX_AGENTS_BODY = 1024 * 1024
MAX_AGENTS = 1000
MAX_AGENT_NAME = 200
MAX_ERROR = 500
MAX_TASK_ID = 200

REQUEST_ID = re.compile(r"[0-9a-f]{32}")
AGENT_ID = re.compile(r"agent-[A-Za-z0-9-]{1,80}")
# Requests that came through the tunnel carry these; the executor's never do.
TUNNEL_HEADERS = ("cf-access-jwt-assertion", "cf-connecting-ip", "cf-ray")
NO_STORE = {"cache-control": "no-store"}

Handler = Callable[[Request], Awaitable[Response]]


class BadRequest(Exception):
    def __init__(self, status: int = 400):
        self.status = status


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=NO_STORE)


def _done() -> Response:
    return Response(status_code=204, headers=NO_STORE)


async def _json(request: Request, cap: int) -> dict[str, Any]:
    """The body as a JSON object with "v": 1, read with a size cap."""
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise BadRequest()
    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.isdigit():
            raise BadRequest()
        if int(declared) > cap:
            raise BadRequest(413)
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > cap:
            raise BadRequest(413)
    try:
        obj = json.loads(body)
    except (ValueError, RecursionError) as e:
        raise BadRequest() from e
    if not isinstance(obj, dict) or obj.get("v") != 1:
        raise BadRequest()
    return obj


def _optional_text(obj: dict[str, Any], key: str, limit: int) -> str:
    value = obj.get(key, "")
    if not isinstance(value, str) or len(value) > limit:
        raise BadRequest()
    return one_line(value, limit)


def executor_routes(store: Store, token: str) -> list[Route]:
    expected = f"Bearer {token}".encode()

    def guarded(handler: Handler) -> Handler:
        async def endpoint(request: Request) -> Response:
            # Never under the page prefix, whatever the router made of the path.
            raw = request.scope.get("raw_path") or request.url.path.encode()
            if not raw.startswith(EXECUTOR_PREFIX.encode() + b"/"):
                return _error(404, "not found")
            if "origin" in request.headers or any(h in request.headers for h in TUNNEL_HEADERS):
                return _error(403, "forbidden")
            given = request.headers.get("authorization", "").encode()
            if not hmac.compare_digest(given, expected):
                return _error(401, "unauthorized")
            try:
                return await handler(request)
            except BadRequest as e:
                return _error(e.status, "too large" if e.status == 413 else "bad request")

        return endpoint

    async def requests(request: Request) -> Response:
        if request.method != "GET":
            # Starlette answers HEAD on GET routes; a HEAD must not claim anything.
            return _error(405, "method not allowed")
        body = {
            "v": 1,
            "requests": store.claim(MAX_CLAIM),
            # The executor runs `letta agents list` only when asked.
            "agents_wanted": store.agents_wanted(AGENTS_FRESH_SECONDS),
        }
        return JSONResponse(body, headers=NO_STORE)

    async def results(request: Request) -> Response:
        obj = await _json(request, MAX_BODY)
        rid, ok = obj.get("id"), obj.get("ok")
        if not isinstance(rid, str) or not REQUEST_ID.fullmatch(rid) or not isinstance(ok, bool):
            raise BadRequest()
        task_id = error = ""
        if ok:
            task_id = obj.get("task_id", "")
            if not isinstance(task_id, str) or len(task_id) > MAX_TASK_ID or CONTROL.search(task_id):
                raise BadRequest()
            task_id = one_line(task_id, MAX_TASK_ID)
        else:
            error = _optional_text(obj, "error", MAX_ERROR) or "Lettuce reported an error without details."
        if not store.finish_run(rid, ok, task_id=task_id, error=error):
            return _error(404, "no such claimed request")
        return _done()

    async def agents(request: Request) -> Response:
        obj = await _json(request, MAX_AGENTS_BODY)
        items = obj.get("agents")
        if not isinstance(items, list):
            raise BadRequest()
        seen: dict[str, Agent] = {}
        for item in items:
            if len(seen) >= MAX_AGENTS:
                break
            if not isinstance(item, dict):
                continue
            aid, name = item.get("id"), item.get("name")
            if not isinstance(aid, str) or not AGENT_ID.fullmatch(aid) or aid in seen:
                continue
            if not isinstance(name, str) or not 1 <= len(name) <= MAX_AGENT_NAME:
                continue
            clean = one_line(name, MAX_AGENT_NAME)
            if clean:
                seen[aid] = Agent(aid, clean)
        store.set_agents(list(seen.values()))
        return _done()

    async def heartbeat(request: Request) -> Response:
        obj = await _json(request, MAX_BODY)
        letta_ok = obj.get("letta_ok")
        if not isinstance(letta_ok, bool):
            raise BadRequest()
        store.set_heartbeat(letta_ok, _optional_text(obj, "error", MAX_ERROR))
        return _done()

    return [
        Route(f"{EXECUTOR_PREFIX}/requests", guarded(requests), methods=["GET"]),
        Route(f"{EXECUTOR_PREFIX}/results", guarded(results), methods=["POST"]),
        Route(f"{EXECUTOR_PREFIX}/agents", guarded(agents), methods=["PUT"]),
        Route(f"{EXECUTOR_PREFIX}/heartbeat", guarded(heartbeat), methods=["POST"]),
    ]
