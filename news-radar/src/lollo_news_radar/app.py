"""The ASGI app: the page under /news-radar on the lollo's host (every route needs a
verified Access JWT, every POST the lollo's Origin and a CSRF token), the MCP
server at /mcp for agents (the internal host only, no Origin), the executor's
API at /executor/v1/ (token, no Access) and /healthz. The page needs no
JavaScript. Its lifespan runs the two loops: scheduled fetches, and schedules
firing into reports."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

import anyio
from lollo_common.auth import COOP, FORM, PAGE, Authenticator, AuthError, admit
from lollo_common.executor import request_routes
from lollo_common.lollo import stylesheet
from jinja2 import Environment, PackageLoader, select_autoescape
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

from . import icons
from .config import HEARTBEAT_STALE_SECONDS, INTERNAL_NAME, LOST_AFTER_SECONDS, MAX_CLAIM, NAME, PREFIX
from .cron import Schedule
from .mcp_tools import build_server
from .service import Service, reports_per_day
from .store import MONITOR_ID, ExecutorQueue, Monitor

log = logging.getLogger("lollo_news_radar.app")

SECURITY_HEADERS = {
    "content-security-policy": "default-src 'none'; script-src 'none'; style-src 'self'; img-src 'self'; "
    "frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'",
    "x-content-type-options": "nosniff",
    # same-origin, not no-referrer: with no-referrer browsers send `Origin: null`
    # on form posts, which check_origin must refuse.
    "referrer-policy": "same-origin",
    "cache-control": "no-store",
    "x-frame-options": "DENY",
    **COOP,
}

Handler = Callable[[Request, str], Awaitable[Response]]

NOTICES = {
    "deleted": "Monitor deleted.",
    "source-deleted": "Source deleted.",
    "conversation": "Where its reports go is changed.",
}
# What the page offers for where a monitor's reports go; "here" is the
# conversation the agent set it up in, offered only when the agent said which.
CONVERSATION_CHOICES = {
    "here": "in the conversation it was set up in",
    "new": "in a new conversation each report",
    "default": "in the agent's main conversation",
}
STATUS_TEXT = {
    "queued": ("", "waiting for Lettuce"),
    "claimed": ("", "sent to Lettuce"),
    "ok": ("ok", "sent"),
    "failed": ("bad", "failed"),
    "lost": ("bad", "lost"),
}
# How often the loops look for work.
FETCH_TICK = 15.0
SCHEDULE_TICK = 20.0


class FormError(Exception):
    pass


def where_reports_go(m: Monitor) -> str:
    return CONVERSATION_CHOICES.get(m.reports_to) or f"in another conversation ({m.conversation})"


def conversation_choices(m: Monitor) -> list[tuple[str, str, bool]]:
    """(value, label, selected) for the monitor's form."""
    out = [(v, t, v == m.reports_to) for v, t in CONVERSATION_CHOICES.items() if v != "here" or m.set_up_in]
    if m.reports_to == "other":
        out.append(("keep", where_reports_go(m), True))
    return out


def _fmt_time(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


def _describe(schedule: str) -> str:
    try:
        return Schedule.parse(schedule).describe()
    except ValueError:
        return schedule


def create_app(
    service: Service, authenticator: Authenticator, executor_token: str, run_loops: bool = True
) -> Starlette:
    store = service.store
    env = Environment(
        loader=PackageLoader("lollo_news_radar", "templates"), autoescape=select_autoescape(default=True)
    )
    env.filters["time"] = _fmt_time
    env.globals.update(
        base=PREFIX,
        where_reports_go=where_reports_go,
        conversation_choices=conversation_choices,
        describe=_describe,
        status_text=lambda s: STATUS_TEXT.get(s, ("", s)),
    )
    lollo_css = stylesheet()
    app_css = (resources.files("lollo_news_radar") / "static" / "app.css").read_bytes()

    def render(name: str, email: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        html = env.get_template(name).render(email=email, csrf=authenticator.csrf_token(email) if email else "", **ctx)
        return HTMLResponse(html, status_code=status, headers=SECURITY_HEADERS)

    def error(message: str, status: int, email: str = "") -> HTMLResponse:
        return render("error.html", email, status=status, message=message)

    def back(path: str) -> RedirectResponse:
        return RedirectResponse(PREFIX + path, status_code=303, headers=SECURITY_HEADERS)

    def page(handler: Handler, post: bool = False, asset: bool = False) -> Callable[[Request], Awaitable[Response]]:
        """`asset`: a stylesheet or icon, loaded by pages as a subresource; it holds nothing to protect."""

        async def endpoint(request: Request) -> Response:
            try:
                email = await admit(authenticator, request.headers, None if asset else FORM if post else PAGE)
            except AuthError as e:
                log.info("refused %s %s reason=%s", request.method, request.url.path, e.reason)
                return error(str(e), e.status)
            if post:
                try:
                    authenticator.check_origin(request.headers)
                    token = (await request.form()).get("csrf")
                    authenticator.check_csrf(email, token if isinstance(token, str) else None)
                except AuthError as e:
                    return error(str(e), e.status, email)
            try:
                return await handler(request, email)
            except FormError as e:
                return error(str(e), 400, email)

        return endpoint

    def banners() -> list[dict[str, str]]:
        out = []
        now = store.now()
        if store.monitors():
            beat = store.heartbeat()
            if beat is None or now - beat.at > HEARTBEAT_STALE_SECONDS:
                when = f" Lettuce last checked at {_fmt_time(beat.at)}." if beat else ""
                out.append(
                    {
                        "level": "bad",
                        "title": "Lettuce isn't picking up reports",
                        "text": f"Reports wait until it does.{when} If this stays, ask whoever runs your Lettuce "
                        "to check the News Radar setup.",
                    }
                )
            elif not beat.letta_ok:
                out.append(
                    {
                        "level": "warn",
                        "title": "Lettuce reported a problem",
                        "text": beat.error or "Lettuce could not run its command line tool.",
                    }
                )
        failures = store.failures_since(now - 86400)
        if failures:
            plural = "s" if failures != 1 else ""
            out.append(
                {
                    "level": "warn",
                    "title": f"{failures} report{plural} failed in the last day",
                    "text": "Their items go into the next report. A monitor whose reports keep failing is paused "
                    "until its agent saves it again.",
                }
            )
        return out

    async def index(request: Request, email: str) -> Response:
        store.expire_claims(LOST_AFTER_SECONDS)
        # The page names agents: ask the executor for a list with any made since the last.
        store.ask_for_agents()
        names = {a.id: a.name for a in store.agents() or []}
        groups: dict[str, list[Any]] = {}
        for m in store.monitors():
            groups.setdefault(m.agent_id, []).append(m)
        states = store.source_states()
        users: dict[str, list[str]] = {}
        for m in store.monitors():
            for s in m.sources:
                users.setdefault(s, []).append(m.name)
        sources = []
        for name, raw in store.source_specs().items():
            st = states.get(name)
            sources.append(
                {
                    "name": name,
                    "kind": raw.get("kind", ""),
                    "host": urlsplit(str(raw.get("url", ""))).hostname or "",
                    "every": raw.get("every", ""),
                    "healthy": st.healthy if st else True,
                    "reason": st.unhealthy_reason if st else "",
                    "last_ok": st.last_ok if st else None,
                    "used_by": users.get(name, []),
                    "unused": not users.get(name),
                }
            )
        return render(
            "index.html",
            email,
            groups=sorted(((names.get(a, a), ms) for a, ms in groups.items()), key=lambda g: g[0].lower()),
            last_reports=store.last_reports(),
            sources=sources,
            banners=banners(),
            timezone=service.settings.timezone,
            notice=NOTICES.get(request.query_params.get("done", "")),
        )

    async def delete_monitor(request: Request, email: str) -> Response:
        mid = request.path_params["mid"]
        m = store.monitor(mid) if MONITOR_ID.fullmatch(mid) else None
        if m is None:
            raise FormError("This monitor doesn't exist (any more).")
        store.delete_monitor(m.id)
        log.info("monitor deleted on the page")
        return back("/?done=deleted")

    async def set_conversation(request: Request, email: str) -> Response:
        mid = request.path_params["mid"]
        m = store.monitor(mid) if MONITOR_ID.fullmatch(mid) else None
        if m is None:
            raise FormError("This monitor doesn't exist (any more).")
        choice = (await request.form()).get("conversation")
        if choice == "keep":
            return back("/")
        if choice == "here" and m.set_up_in:
            conversation = m.set_up_in
        elif choice in ("new", "default"):
            conversation = str(choice)
        else:
            raise FormError("Choose where its reports go from the list.")
        most = reports_per_day(store, service.settings, replace(m, conversation=conversation))
        if most > service.settings.max_reports_per_day:
            raise FormError(
                f"With that choice the agent's monitors would report up to {most} times a day, and this installation "
                f"allows {service.settings.max_reports_per_day}. Monitors on the same schedule that report to the "
                "same place share one report: choose where another of them reports."
            )
        store.set_monitor_conversation(m.id, conversation)
        log.info("monitor conversation changed on the page")
        return back("/?done=conversation")

    async def delete_source(request: Request, email: str) -> Response:
        name = request.path_params["name"]
        if not NAME.fullmatch(name) or store.source_spec(name) is None:
            raise FormError("This source doesn't exist (any more).")
        if store.source_users(name):
            raise FormError("A monitor uses this source. Delete the monitor first.")
        store.delete_source(name)
        return back("/?done=source-deleted")

    def static(body: bytes, media_type: str) -> Handler:
        async def serve(request: Request, email: str) -> Response:
            return Response(body, media_type=media_type, headers={**SECURITY_HEADERS, "cache-control": "no-cache"})

        return serve

    def icon(size: int) -> Handler:
        async def serve(request: Request, email: str) -> Response:
            return Response(
                icons.png(size), media_type="image/png", headers={**SECURITY_HEADERS, "cache-control": "max-age=86400"}
            )

        return serve

    async def healthz(request: Request) -> Response:
        return PlainTextResponse("ok")

    async def root(request: Request) -> Response:
        return RedirectResponse(PREFIX + "/", status_code=308)

    ui = [
        Route("/", page(index)),
        Route("/m/{mid}/delete", page(delete_monitor, post=True), methods=["POST"]),
        Route("/m/{mid}/conversation", page(set_conversation, post=True), methods=["POST"]),
        Route("/s/{name}/delete", page(delete_source, post=True), methods=["POST"]),
        Route("/lollo.css", page(static(lollo_css, "text/css"), asset=True)),
        Route("/app.css", page(static(app_css, "text/css"), asset=True)),
        Route("/icon-192.png", page(icon(192), asset=True)),
        Route("/icon-512.png", page(icon(512), asset=True)),
    ]

    mcp_server = build_server(service)
    mcp_app = mcp_server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        # Agents call http://lollo-news-radar:8000/mcp from the stack network and
        # send no Origin; anything else (the tunnel's host, a browser) is refused.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"{INTERNAL_NAME}:*", INTERNAL_NAME],
            allowed_origins=[],
        ),
    )

    async def fetch_loop() -> None:
        while True:
            try:
                due = service.due_sources(store.now())
                for name in due:
                    await service.fetch_source(name)
            except Exception:  # noqa: BLE001 - the loop must outlive one bad run
                log.exception("fetch loop")
            await anyio.sleep(FETCH_TICK)

    async def schedule_loop() -> None:
        while True:
            try:
                service.tick(datetime.fromtimestamp(store.now(), service.settings.tz))
                store.expire_claims(LOST_AFTER_SECONDS)
            except Exception:  # noqa: BLE001
                log.exception("schedule loop")
            await anyio.sleep(SCHEDULE_TICK)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with mcp_app.router.lifespan_context(mcp_app), anyio.create_task_group() as tg:
            if run_loops:
                tg.start_soon(fetch_loop)
                tg.start_soon(schedule_loop)
            try:
                yield
            finally:
                tg.cancel_scope.cancel()

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            *request_routes(ExecutorQueue(store), executor_token, MAX_CLAIM),
            Route(PREFIX, root),
            Mount(PREFIX, routes=ui),
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
    )
