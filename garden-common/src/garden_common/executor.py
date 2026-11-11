"""The executor's API at /executor/v1/, as the garden sidecars that the garden
executor calls serve it (garden, gmail-triggers, watchlist): the guard in front
of every route, the JSON body reader, the agent list and when to ask for a new
one, and the request queue's routes (requests, results, agents, heartbeat) that
every sidecar whose requests the executor turns into tasks serves.

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
from dataclasses import dataclass, replace
from typing import Any, Protocol

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .text import CONTROL, one_line

EXECUTOR_PREFIX = "/executor/v1"
EXECUTOR_TOKEN = re.compile(r"[0-9a-f]{64}")

MAX_BODY = 16 * 1024
MAX_AGENTS_BODY = 1024 * 1024
MAX_AGENTS = 1000
MAX_AGENT_NAME = 200
AGENT_ID = re.compile(r"agent-[A-Za-z0-9-]{1,80}")
# A sidecar wants a new agent list when it has none, when its list is older
# than this, or when a page asked for one since (AgentAsks).
AGENTS_FRESH_SECONDS = 900
# Requests that came through the tunnel carry these; the executor's never do.
TUNNEL_HEADERS = ("cf-access-jwt-assertion", "cf-connecting-ip", "cf-ray")
NO_STORE = {"cache-control": "no-store"}
# What the executor reports back about one request.
REQUEST_ID = re.compile(r"[0-9a-f]{32}")
MAX_ERROR = 500
MAX_TASK_ID = 200

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
    entries without a well-formed id or with an overlong name are dropped, a
    repeated id counts once, names become one line (the id when nothing is left
    of one), and at most MAX_AGENTS are kept."""
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
        if isinstance(name, str) and len(name) > MAX_AGENT_NAME:
            continue
        seen[aid] = one_line(name, MAX_AGENT_NAME) or aid
    return list(seen.items())


class RequestQueue(Protocol):
    """What a sidecar whose requests the executor runs keeps (in its own storage)."""

    def claim(self, limit: int) -> list[dict[str, Any]]:
        """The oldest waiting requests, each handed out at most once."""

    def agents_wanted(self) -> bool:
        """Whether the executor should send the agent list (AgentAsks.read, then wanted)."""

    def finish(self, request_id: str, ok: bool, task_id: str, error: str) -> bool:
        """Records a claimed request's outcome; False when there was no such claimed request."""

    def set_agents(self, agents: list[tuple[str, str]]) -> None:
        """The agent list, as read_agents returns it."""

    def set_heartbeat(self, letta_ok: bool, error: str) -> None:
        """The executor's last run, for the page's "Lettuce isn't picking up requests"."""


def _optional_text(obj: dict[str, Any], key: str, limit: int) -> str:
    value = obj.get(key, "")
    if not isinstance(value, str) or len(value) > limit:
        raise BadRequest()
    return one_line(value, limit)


def request_routes(queue: RequestQueue, token: str, max_claim: int) -> list[Route]:
    """The executor's request API of one sidecar: GET requests (claims up to
    `max_claim`), POST results, PUT agents and POST heartbeat, all behind `guard`.

    Anything a request does, agents can already do with `letta cron add`; they
    cannot create requests here, only take, drop or misreport them."""
    guarded = guard(token)

    async def requests(request: Request) -> Response:
        if request.method != "GET":
            # Starlette answers HEAD on GET routes; a HEAD must not claim anything.
            return error(405, "method not allowed")
        body = {
            "v": 1,
            "requests": queue.claim(max_claim),
            # The executor runs `letta agents list` only when asked (AgentAsks).
            "agents_wanted": queue.agents_wanted(),
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
        if not queue.finish(rid, ok, task_id, error_text):
            return error(404, "no such claimed request")
        return done()

    async def agents(request: Request) -> Response:
        queue.set_agents(await read_agents(request))
        return done()

    async def heartbeat(request: Request) -> Response:
        obj = await read_json(request, MAX_BODY)
        letta_ok = obj.get("letta_ok")
        if not isinstance(letta_ok, bool):
            raise BadRequest()
        queue.set_heartbeat(letta_ok, _optional_text(obj, "error", MAX_ERROR))
        return done()

    return [
        Route(f"{EXECUTOR_PREFIX}/requests", guarded(requests), methods=["GET"]),
        Route(f"{EXECUTOR_PREFIX}/results", guarded(results), methods=["POST"]),
        Route(f"{EXECUTOR_PREFIX}/agents", guarded(agents), methods=["PUT"]),
        Route(f"{EXECUTOR_PREFIX}/heartbeat", guarded(heartbeat), methods=["POST"]),
    ]


@dataclass(frozen=True)
class AgentAsks:
    """When a sidecar wants the executor to send a new agent list; each side keeps
    it in its own storage. Each method returns the new state.

    A page that lists agents asks for a list with the ones made since the last
    (`ask`). The executor reads the status (`read`), then runs `letta agents list`
    and sends it (`listed`). That list answers only the asks made before the read:
    one made while the executor builds it may name an agent the list predates.
    """

    asks: int = 0  # asks so far
    seen: int = 0  # asks made before the executor last read the status
    answered: int = 0  # asks the last list answers
    listed_at: float | None = None  # when the last list arrived

    def ask(self) -> AgentAsks:
        return replace(self, asks=self.asks + 1)

    def read(self) -> AgentAsks:
        return replace(self, seen=self.asks)

    def listed(self, now: float) -> AgentAsks:
        return replace(self, answered=self.seen, listed_at=now)

    def wanted(self, now: float) -> bool:
        return self.listed_at is None or now - self.listed_at > AGENTS_FRESH_SECONDS or self.asks > self.answered
