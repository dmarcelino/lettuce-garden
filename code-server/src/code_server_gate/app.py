"""The gate in front of the code editor (docs/code-server.md).

The editor shares the garden's origin with the vault and Gmail triggers, and
has no Access check of its own; it listens only on a Unix socket that only this gate mounts.
Everything under PREFIX passes this gate, which:
- needs a verified Access JWT on every request and WebSocket upgrade, this
  garden's own Origin on every upgrade, and this origin (Sec-Fetch-Site) for
  anything but a read; it closes a WebSocket when its JWT expires;
- strips PREFIX on the way in and puts it back on redirects;
- passes only listed headers either way: the editor never sees the person's
  Access token or cookies, and never sets a cookie, an opener
  policy or a service worker scope wider than PREFIX on the origin;
- lets only the workbench and code-server's own webview hosts load as documents
  or frames, and serves everything else nosniff with a sandbox CSP, so a file an
  agent wrote can't become a page on the origin; every answer varies with
  Sec-Fetch-Dest, so the browser's cache never hands one to another kind of request.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any, Protocol
from urllib.parse import urlsplit

import anyio
import anyio.abc
import httpx
import jwt
from garden_common.auth import COOP, JWT_HEADER, Authenticator, AuthError
from garden_common.garden import stylesheet
from jinja2 import Environment, PackageLoader, select_autoescape
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .config import EDITOR_SOCKET, PREFIX, Settings

log = logging.getLogger("code_server_gate")

# The gate's own pages (errors): no script, never framed.
GATE_HEADERS = {
    "content-security-policy": "default-src 'none'; style-src 'self'; img-src 'self'; "
    "frame-ancestors 'none'; form-action 'none'; base-uri 'none'; object-src 'none'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "cache-control": "no-store",
    "x-frame-options": "DENY",
    **COOP,
}

# What reaches the editor. Never Cookie (the Access cookie), the Access JWT,
# Authorization or any other cf-* header.
REQUEST_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "accept-language",
        "cache-control",
        "content-length",
        "content-type",
        "if-match",
        "if-modified-since",
        "if-none-match",
        "if-range",
        "if-unmodified-since",
        "origin",
        "pragma",
        "range",
        "user-agent",
    }
)
# What reaches the browser. Never Set-Cookie or Cross-Origin-*-Policy: an
# opener policy on the workbench would put it in one browsing context group with
# the garden pages, which set COOP same-origin to be cut off from it.
RESPONSE_HEADERS = frozenset(
    {
        "accept-ranges",
        "cache-control",
        "content-disposition",
        "content-encoding",
        "content-language",
        "content-length",
        "content-range",
        "content-security-policy",
        "content-type",
        "etag",
        "expires",
        "last-modified",
        "location",
        "referrer-policy",
        "service-worker-allowed",
        "vary",
        "x-content-type-options",
    }
)

# Requests that make a document out of the response.
NAVIGATIONS = frozenset({"document", "iframe", "frame", "embed", "object", "fencedframe"})
WORKERS = frozenset({"worker", "sharedworker", "serviceworker"})
# The workbench (code-server also serves it under /vscode).
WORKBENCH = re.compile(r"/(?:vscode/?)?")
# code-server's own frames, from its read-only install: the webview hosts and the
# web worker extension host.
STATIC_FRAMES = re.compile(
    r"/(?:vscode/)?[a-z]+-[0-9a-f]+/static/out/vs/workbench/"
    r"(?:contrib/webview/browser/pre/(?:index|fake)\.html|services/extensions/worker/webWorkerExtensionHostIframe\.html)"
)
METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
READS = frozenset({"GET", "HEAD"})


class Upstream(Protocol):
    """A WebSocket to the editor (websockets' client connection, or a test fake)."""

    subprotocol: str | None

    async def send(self, message: str | bytes) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


Connect = Callable[[str, list[tuple[str, str]], list[str]], Awaitable[Upstream]]


def editor_client(path: str = EDITOR_SOCKET) -> httpx.AsyncClient:
    """HTTP to the editor over its Unix socket. No read timeout: downloads and
    long requests stream for as long as they take."""
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(uds=path),
        timeout=httpx.Timeout(10.0, read=None, write=None),
        follow_redirects=False,
    )


def unix_socket_connect(path: str = EDITOR_SOCKET) -> Connect:
    """WebSockets to the editor over its Unix socket."""

    async def connect(url: str, headers: list[tuple[str, str]], subprotocols: list[str]) -> Upstream:
        from websockets.asyncio.client import unix_connect

        return await unix_connect(
            path,
            url,
            additional_headers=headers,
            subprotocols=subprotocols or None,
            max_size=None,
            open_timeout=10,
        )

    return connect


def frame_allowed(path: str, dest: str) -> bool:
    """May a response at `path` (after PREFIX) load as a `dest` (Sec-Fetch-Dest) navigation?"""
    if dest == "document":
        return WORKBENCH.fullmatch(path) is not None
    if dest in ("iframe", "frame"):
        return STATIC_FRAMES.fullmatch(path) is not None
    return False


def token_expiry(token: str) -> float:
    """`exp` of a JWT the Authenticator has already verified."""
    return float(jwt.decode(token, options={"verify_signature": False})["exp"])


def create_app(
    settings: Settings,
    authenticator: Authenticator,
    client: httpx.AsyncClient,
    connect: Connect,
    clock: Callable[[], float] = time.time,
) -> Starlette:
    env = Environment(loader=PackageLoader("code_server_gate", "templates"), autoescape=select_autoescape(default=True))
    garden_css = stylesheet()
    upstream = urlsplit(settings.upstream)
    ws_upstream = f"{'wss' if upstream.scheme == 'https' else 'ws'}://{upstream.netloc}"

    def error(message: str, status: int) -> HTMLResponse:
        html = env.get_template("error.html").render(base=PREFIX, message=message)
        return HTMLResponse(html, status_code=status, headers=GATE_HEADERS)

    def editor_path(scope: Mapping[str, Any]) -> str | None:
        """The request's path after PREFIX, still percent-encoded, with its query;
        None when PREFIX itself was percent-encoded (routing matched the decoded path)."""
        raw = (scope.get("raw_path") or scope["path"].encode()).decode("latin-1")
        if not raw.startswith(PREFIX + "/"):
            return None
        query = scope.get("query_string", b"").decode("latin-1")
        return raw[len(PREFIX) :] + (f"?{query}" if query else "")

    def forwarded(headers: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
        out = [(k, v) for k, v in headers if k.lower() in REQUEST_HEADERS]
        # code-server checks WebSocket origins against this host (http.ts getHost).
        return out + [("x-forwarded-host", settings.public_host), ("x-forwarded-proto", "https")]

    def location(value: str) -> str:
        if value.startswith("/") and not value.startswith("//"):
            return PREFIX + value
        parts = urlsplit(value)
        if parts.netloc in (settings.public_host, upstream.netloc) and not parts.path.startswith(PREFIX + "/"):
            rest = value[len(f"{parts.scheme}://{parts.netloc}") :] or "/"
            return settings.public_origin + PREFIX + rest
        return value  # relative to the request, which already carries PREFIX

    def response_headers(headers: httpx.Headers, sandboxed: bool) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, v in headers.items():
            k = k.lower()
            if k not in RESPONSE_HEADERS:
                continue
            if k == "location":
                v = location(v)
            elif k == "service-worker-allowed":
                # code-server lets its PWA worker claim "/", the whole origin; it gets PREFIX at most.
                v = PREFIX + v if v.startswith("/") else PREFIX + "/"
            out[k] = v
        out["vary"] = ", ".join(filter(None, (out.get("vary"), "Sec-Fetch-Dest")))
        out["x-content-type-options"] = "nosniff"
        out["cross-origin-resource-policy"] = "same-origin"
        if sandboxed:
            out["content-security-policy"] = "sandbox"
        return out

    async def checked(request: Request) -> Response | None:
        try:
            await authenticator.email(request.headers)
        except AuthError as e:
            log.info("refused %s %s reason=%s", request.method, request.url.path, e.reason)
            return error(str(e), e.status)
        return None

    async def editor(request: Request) -> Response:
        if refused := await checked(request):
            return refused
        path = editor_path(request.scope)
        if path is None:
            return error("Not found.", 404)
        dest = request.headers.get("sec-fetch-dest", "")
        if not dest:
            return error("this browser did not say how the page was requested; use a current browser", 403)
        if request.method not in READS and request.headers.get("sec-fetch-site") != "same-origin":
            log.info("refused %s %s reason=site", request.method, request.url.path)
            return error("cross-site request refused", 403)
        navigation = dest in NAVIGATIONS
        if navigation and not frame_allowed(path.split("?", 1)[0], dest):
            log.info("refused %s %s reason=document dest=%s", request.method, request.url.path, dest)
            return error("This can't be opened as a page; open the file in the editor instead.", 403)
        body = None if request.method in READS else request.stream()
        try:
            sent = client.build_request(
                request.method, settings.upstream + path, headers=forwarded(request.headers.items()), content=body
            )
            response = await client.send(sent, stream=True)
        except httpx.HTTPError as e:
            log.warning("editor unreachable: %s", type(e).__name__)
            return error("The editor isn't running right now; try again in a minute.", 502)
        return StreamingResponse(
            response.aiter_raw(),
            status_code=response.status_code,
            headers=response_headers(response.headers, sandboxed=not navigation and dest not in WORKERS),
            background=BackgroundTask(response.aclose),
        )

    async def editor_socket(websocket: WebSocket) -> None:
        path = editor_path(websocket.scope)
        try:
            await authenticator.email(websocket.headers)
            if websocket.headers.get("origin") != settings.public_origin:
                raise AuthError("cross-site request refused")
            if path is None:
                raise AuthError("not found", reason="path")
        except AuthError as e:
            log.info("refused websocket %s reason=%s", websocket.url.path, e.reason)
            await websocket.close(code=1008)
            return
        remaining = token_expiry(websocket.headers[JWT_HEADER]) - clock()
        headers = forwarded(websocket.headers.items())
        try:
            peer = await connect(ws_upstream + path, headers, websocket.scope.get("subprotocols", []))
        except Exception as e:  # unreachable, refused, timed out: all mean no editor
            log.warning("editor websocket unreachable: %s", type(e).__name__)
            await websocket.close(code=1011)
            return
        await websocket.accept(subprotocol=peer.subprotocol)

        async def browser_to_editor(group: anyio.abc.TaskGroup) -> None:
            try:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        break
                    data = message.get("bytes")
                    await peer.send(data if data is not None else message.get("text", ""))
            except (WebSocketDisconnect, RuntimeError):
                pass
            group.cancel_scope.cancel()

        async def editor_to_browser(group: anyio.abc.TaskGroup) -> None:
            try:
                while True:
                    data = await peer.recv()
                    if isinstance(data, bytes):
                        await websocket.send_bytes(data)
                    else:
                        await websocket.send_text(data)
            except Exception:  # the editor closed its side, however it did
                pass
            group.cancel_scope.cancel()

        try:
            with anyio.move_on_after(max(0.0, remaining)) as timer:
                async with anyio.create_task_group() as group:
                    group.start_soon(browser_to_editor, group)
                    group.start_soon(editor_to_browser, group)
        finally:
            with anyio.CancelScope(shield=True):
                await peer.close()
                try:
                    # Expired: the editor reconnects, and that upgrade needs a current sign-in.
                    await websocket.close(code=1008 if timer.cancelled_caught else 1000)
                except RuntimeError:
                    pass  # already closed

    async def garden_css_file(request: Request) -> Response:
        if refused := await checked(request):
            return refused
        return Response(garden_css, media_type="text/css", headers={**GATE_HEADERS, "cache-control": "no-cache"})

    async def icon(request: Request) -> Response:
        if refused := await checked(request):
            return refused
        return RedirectResponse(PREFIX + "/_static/src/browser/media/pwa-icon-192.png", status_code=302)

    async def healthz(request: Request) -> Response:
        return PlainTextResponse("ok")

    async def root(request: Request) -> Response:
        return RedirectResponse(PREFIX + "/", status_code=308)

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route(PREFIX, root),
            Route(PREFIX + "/garden.css", garden_css_file),
            Route(PREFIX + "/icon-192.png", icon),
            Route(PREFIX + "/{rest:path}", editor, methods=METHODS),
            WebSocketRoute(PREFIX + "/{rest:path}", editor_socket),
        ],
    )
