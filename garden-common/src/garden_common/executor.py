"""The executor's API at /executor/v1/, as the garden sidecars that the garden
executor calls serve it (garden, gmail-triggers): the guard in front of every
route, the JSON body reader and the agent list.

The API sits outside every page prefix, so the tunnel never routes it, and no
Access check applies. Instead every call needs the stack's executor token
(GARDEN_EXECUTOR_TOKEN). Agents are root in the app-server and can read that
token, so nothing here is trusted beyond "this is what the executor (or an
agent) said": the token keeps out the other containers on the stack network.
"""

from __future__ import annotations

import hmac
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .text import one_line

EXECUTOR_PREFIX = "/executor/v1"
EXECUTOR_TOKEN = re.compile(r"[0-9a-f]{64}")

MAX_BODY = 16 * 1024
MAX_AGENTS_BODY = 1024 * 1024
MAX_AGENTS = 1000
MAX_AGENT_NAME = 200
AGENT_ID = re.compile(r"agent-[A-Za-z0-9-]{1,80}")
# Requests that came through the tunnel carry these; the executor's never do.
TUNNEL_HEADERS = ("cf-access-jwt-assertion", "cf-connecting-ip", "cf-ray")
NO_STORE = {"cache-control": "no-store"}

Handler = Callable[[Request], Awaitable[Response]]


class BadRequest(Exception):
    def __init__(self, status: int = 400):
        self.status = status


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=NO_STORE)


def done() -> Response:
    return Response(status_code=204, headers=NO_STORE)


async def read_json(request: Request, cap: int) -> dict[str, Any]:
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


def guard(token: str) -> Callable[[Handler], Handler]:
    """Wraps an executor route: only under EXECUTOR_PREFIX as sent (whatever the
    router made of the path), never from a browser or through the tunnel, and only
    with the token. A handler's BadRequest becomes its JSON error."""
    expected = f"Bearer {token}".encode()

    def guarded(handler: Handler) -> Handler:
        async def endpoint(request: Request) -> Response:
            raw = request.scope.get("raw_path") or request.url.path.encode()
            if not raw.startswith(EXECUTOR_PREFIX.encode() + b"/"):
                return error(404, "not found")
            if "origin" in request.headers or any(h in request.headers for h in TUNNEL_HEADERS):
                return error(403, "forbidden")
            given = request.headers.get("authorization", "").encode()
            if not hmac.compare_digest(given, expected):
                return error(401, "unauthorized")
            try:
                return await handler(request)
            except BadRequest as e:
                return error(e.status, "too large" if e.status == 413 else "bad request")

        return endpoint

    return guarded


async def read_agents(request: Request) -> list[tuple[str, str]]:
    """The agent list of a PUT /executor/v1/agents body, as (id, name) pairs:
    entries without a well-formed id or a usable name are dropped, a repeated id
    counts once, names become one line, and at most MAX_AGENTS are kept."""
    obj = await read_json(request, MAX_AGENTS_BODY)
    items = obj.get("agents")
    if not isinstance(items, list):
        raise BadRequest()
    seen: dict[str, str] = {}
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
            seen[aid] = clean
    return list(seen.items())
