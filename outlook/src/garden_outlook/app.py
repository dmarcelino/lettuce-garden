"""The ASGI app: the MCP endpoint at /mcp (agents, stack network, garden-outlook
host only) and the Outlook page at /outlook/ (every route needs a verified Access
JWT of the garden's application). The page sets the policy, shows the connect
bookmarklet, and receives the handover at /outlook/connect."""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from importlib import resources
from typing import Any

from garden_common.auth import COOP, FETCH, FORM, PAGE, Authenticator, AuthError, admit
from garden_common.garden import stylesheet
from jinja2 import Environment, PackageLoader, select_autoescape
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request as HttpRequest
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

from . import bookmarklet, icons
from .config import PREFIX, Settings
from .levels import InvalidPolicy, LEVELS, SERVICES
from .mcp_tools import build_server
from .service import Service
from .tokens import ConnectError, LostAccess

log = logging.getLogger("garden_outlook.app")

SECURITY_HEADERS = {
    "content-security-policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "cache-control": "no-store",
    "x-frame-options": "DENY",
    **COOP,
}

Handler = Callable[[HttpRequest, str], Awaitable[Response]]
# How agents address the sidecar on the stack network (compose/outlook.yml).
INTERNAL_NAME = "garden-outlook"


def create_app(service: Service, authenticator: Authenticator, public_origin: str) -> Starlette:
    env = Environment(loader=PackageLoader("garden_outlook", "templates"), autoescape=select_autoescape(default=True))
    env.globals["base"] = PREFIX

    def render(name: str, email: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        html = env.get_template(name).render(
            email=email,
            csrf=authenticator.csrf_token(email) if email else "",
            services=SERVICES,
            levels=LEVELS,
            bookmarklet_href=bookmarklet.href(public_origin),
            bookmarklet_source=bookmarklet.readable(public_origin),
            **ctx,
        )
        return HTMLResponse(html, status_code=status, headers=SECURITY_HEADERS)

    def error(message: str, status: int, email: str = "") -> HTMLResponse:
        return render("error.html", email, status=status, message=message)

    def back() -> RedirectResponse:
        return RedirectResponse(PREFIX + "/", status_code=303, headers=SECURITY_HEADERS)

    def page(handler: Handler, post: bool = False, asset: bool = False) -> Callable[[HttpRequest], Awaitable[Response]]:
        async def endpoint(request: HttpRequest) -> Response:
            is_json = request.headers.get("content-type", "").startswith("application/json")
            try:
                use = None if asset else (FETCH if is_json else FORM) if post else PAGE
                email = await admit(authenticator, request.headers, use)
            except AuthError as e:
                log.info("refused %s %s reason=%s", request.method, request.url.path, e.reason)
                return error(str(e), e.status)
            if post:
                try:
                    authenticator.check_origin(request.headers)
                    token = request.headers.get("x-csrf-token") if is_json else (await request.form()).get("csrf")
                    authenticator.check_csrf(email, token if isinstance(token, str) else None)
                except AuthError as e:
                    return error(str(e), e.status, email)
            return await handler(request, email)

        return endpoint

    # ── pages ────────────────────────────────────────────────────────────
    async def index(request: HttpRequest, email: str) -> Response:
        return render("page.html", email, status_info=service.status())

    async def connect(request: HttpRequest, email: str) -> Response:
        try:
            payload = await request.json()
        except (ValueError, TypeError):
            return JSONResponse({"error": "expected JSON"}, status_code=400, headers=SECURITY_HEADERS)
        try:
            await service.connect(payload)
        except ConnectError as e:
            return JSONResponse({"error": str(e)}, status_code=400, headers=SECURITY_HEADERS)
        except LostAccess as e:
            # The handover was well-formed but the tenant refused the Graph
            # redemption (Conditional Access, token protection, restricted
            # first-party apps). Say so plainly.
            return JSONResponse({"error": f"Could not connect to Microsoft Graph: {e}"}, status_code=400, headers=SECURITY_HEADERS)
        log.info("%s connected an Outlook session", email)
        return JSONResponse({"ok": True, "account": service.store.account}, headers=SECURITY_HEADERS)

    async def disconnect(request: HttpRequest, email: str) -> Response:
        service.disconnect()
        log.info("%s disconnected the Outlook session", email)
        return back()

    async def update_policy(request: HttpRequest, email: str) -> Response:
        form = await request.form()
        body: dict[str, Any] = {"levels": {}}
        for svc in SERVICES:
            value = form.get(f"level_{svc}")
            body["levels"][svc] = value if isinstance(value, str) and value else None
        body["allowedRecipients"] = _lines(form.get("allowed_recipients"))
        body["sharedMailboxes"] = _lines(form.get("shared_mailboxes"))
        try:
            service.update_policy(body)
        except InvalidPolicy as e:
            return error(str(e), 400, email)
        log.info("%s updated the Outlook policy", email)
        return back()

    # ── assets ────────────────────────────────────────────────────────────
    def static_file(name: str, media: str) -> Handler:
        data = (resources.files("garden_outlook") / "static" / name).read_bytes()

        async def handler(request: HttpRequest, email: str) -> Response:
            return Response(data, media_type=media, headers={"cache-control": "no-store"})

        return handler

    def static_bytes(data: bytes, media: str) -> Handler:
        async def handler(request: HttpRequest, email: str) -> Response:
            return Response(data, media_type=media, headers={"cache-control": "no-store"})

        return handler

    def icon(size: int) -> Handler:
        async def handler(request: HttpRequest, email: str) -> Response:
            return Response(icons.png(size), media_type="image/png", headers={"cache-control": "no-store"})

        return handler

    async def healthz(request: HttpRequest) -> Response:
        return PlainTextResponse("ok")

    ui = [
        Route("/", page(index)),
        Route("/connect", page(connect, post=True), methods=["POST"]),
        Route("/disconnect", page(disconnect, post=True), methods=["POST"]),
        Route("/policy", page(update_policy, post=True), methods=["POST"]),
        Route("/app.js", page(static_file("app.js", "text/javascript"), asset=True)),
        Route("/garden.css", page(static_bytes(stylesheet(), "text/css"), asset=True)),
        Route("/app.css", page(static_file("app.css", "text/css"), asset=True)),
        Route("/icon-192.png", page(icon(192), asset=True)),
        Route("/icon-512.png", page(icon(512), asset=True)),
    ]

    mcp_server = build_server(service)
    mcp_app = mcp_server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"{INTERNAL_NAME}:*", INTERNAL_NAME],
            allowed_origins=[],
        ),
    )

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with mcp_app.router.lifespan_context(mcp_app):
            try:
                yield
            finally:
                await service.aclose()

    async def outlook_root(request: HttpRequest) -> Response:
        return RedirectResponse(PREFIX + "/", status_code=308)

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route(PREFIX, outlook_root),
            Mount(PREFIX, routes=ui),
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
    )


def _lines(value: Any) -> list[str]:
    """Split a textarea value into non-blank lines."""
    if not isinstance(value, str):
        return []
    return [line.strip() for line in re.split(r"[\r\n,]+", value) if line.strip()]
