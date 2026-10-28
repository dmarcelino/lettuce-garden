"""The ASGI app: the landing page at / on the garden's host and its assets (a
verified Access JWT on every route, Fetch Metadata on the page), the executor's
API at /executor/v1/ (garden_common.executor: token, no Access, never routed by
the tunnel) and /healthz.

Each garden page lives at /<profile>/ on this host, routed by the tunnel only
while its profile is on, so the page links only to those in GARDEN_PROFILES.
With the code editor on, its entry is the list of agents instead, each a link
that opens the editor in its workspace. Lettuce alone knows the agents: the
executor sends `letta agents list` here when asked. That list is untrusted
(agents are root in the app-server and can read the executor's token), so ids
must look like ids before they go in a link and names are escaped text. The page
runs no script and holds no sidecar data: opening it touches no other sidecar.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import quote

from garden_common.auth import COOP, PAGE, Authenticator, AuthError, admit
from garden_common.executor import EXECUTOR_PREFIX, NO_STORE, done, guard, read_agents
from garden_common.garden import stylesheet
from jinja2 import Environment, PackageLoader, select_autoescape
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from . import icons
from .agents import AgentList
from .config import AGENTS_FRESH_SECONDS, EDITOR, Settings

log = logging.getLogger("garden_landing")

SECURITY_HEADERS = {
    "content-security-policy": "default-src 'none'; style-src 'self'; img-src 'self'; "
    "frame-ancestors 'none'; form-action 'none'; base-uri 'none'; object-src 'none'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "cache-control": "no-store",
    "x-frame-options": "DENY",
    **COOP,
}

# What the page says about each garden page; one without an entry shows its profile name.
LABELS = {
    "secrets": ("Vault", "Your personal data, shared with your agents only when you say so"),
    "gmail-triggers": ("Gmail triggers", "Ask an agent to act when mail arrives"),
    "code-server": ("Code editor", "Your agents' files in VS Code"),
}

Handler = Callable[[Request, str], Awaitable[Response]]


@dataclass(frozen=True)
class Feature:
    profile: str
    label: str
    blurb: str

    @property
    def path(self) -> str:
        return f"/{self.profile}/"


@dataclass(frozen=True)
class AgentRow:
    title: str
    folder: str

    @property
    def href(self) -> str:
        return f"/{EDITOR}/?folder={quote(self.folder, safe='')}"


def agent_rows(agents: list[tuple[str, str]]) -> list[AgentRow]:
    """One row per agent; agents sharing a name show their id too."""
    names = Counter(name for _, name in agents)
    return [AgentRow(f"{name} ({aid})" if names[name] > 1 else name, f"/work/{aid}") for aid, name in agents]


def create_app(settings: Settings, authenticator: Authenticator, agents: AgentList) -> Starlette:
    env = Environment(loader=PackageLoader("garden_landing", "templates"), autoescape=select_autoescape(default=True))
    garden_css = stylesheet()
    features = [Feature(p, *LABELS.get(p, (p, ""))) for p in settings.garden_profiles]

    def render(name: str, status: int = 200, **ctx: object) -> HTMLResponse:
        html = env.get_template(name).render(lettuce=settings.lettuce_origin, **ctx)
        return HTMLResponse(html, status_code=status, headers=SECURITY_HEADERS)

    def page(handler: Handler, asset: bool = False) -> Callable[[Request], Awaitable[Response]]:
        """`asset`: the stylesheet or icon, loaded by the page as a subresource; it holds nothing to protect."""

        async def endpoint(request: Request) -> Response:
            try:
                email = await admit(authenticator, request.headers, None if asset else PAGE)
            except AuthError as e:
                log.info("refused %s %s reason=%s", request.method, request.url.path, e.reason)
                # Without the email or the agents: the page carries both.
                return render("error.html", status=e.status, message=str(e))
            return await handler(request, email)

        return endpoint

    async def landing(request: Request, email: str) -> Response:
        listed = None
        if settings.lists_agents:
            # Opening the page asks for a list with the agents made since the last one.
            agents.ask()
            listed = agents.agents()
        return render(
            "landing.html",
            email=email,
            features=[f for f in features if not (settings.lists_agents and f.profile == EDITOR)],
            any_feature=bool(features),
            lists_agents=settings.lists_agents,
            agents=None if listed is None else agent_rows(listed),
            editor=f"/{EDITOR}/",
            editor_label=LABELS[EDITOR][0],
        )

    async def css(request: Request, email: str) -> Response:
        return Response(garden_css, media_type="text/css", headers={**SECURITY_HEADERS, "cache-control": "no-cache"})

    async def icon(request: Request, email: str) -> Response:
        return Response(
            icons.png(192), media_type="image/png", headers={**SECURITY_HEADERS, "cache-control": "max-age=86400"}
        )

    guarded = guard(settings.executor_token)

    async def status(request: Request) -> Response:
        # The executor runs `letta agents list` only when someone wants it.
        wanted = settings.lists_agents and agents.wanted(AGENTS_FRESH_SECONDS)
        return JSONResponse({"v": 1, "agents_wanted": wanted}, headers=NO_STORE)

    async def put_agents(request: Request) -> Response:
        agents.set(await read_agents(request))
        return done()

    async def healthz(request: Request) -> Response:
        return PlainTextResponse("ok")

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route(f"{EXECUTOR_PREFIX}/status", guarded(status), methods=["GET"]),
            Route(f"{EXECUTOR_PREFIX}/agents", guarded(put_agents), methods=["PUT"]),
            Route("/", page(landing)),
            Route("/garden.css", page(css, asset=True)),
            Route("/icon-192.png", page(icon, asset=True)),
        ],
    )
