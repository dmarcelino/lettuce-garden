"""The garden landing page at /garden/: a link to each garden page that is on.

lettucectl passes the sidecars GARDEN_PROFILES, the stack's profiles that add a
garden page (PROFILE_ROUTES in lib/cloudflare.sh), and routes /garden/ to the
first of those sidecars (cf_tunnel_configure), so every sidecar can serve it.
Each page lives at /garden/<profile>/; a page that is off has no route, and a
link to it would land on Lettuce. With the code editor on, its entry is a list of
the agents instead, each a link that opens the editor in its workspace: its one script
(static/landing-agents.js, inline and allowed by its hash) asks Lettuce for them.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import resources
from urllib.parse import urlsplit

from jinja2 import Environment, PackageLoader, select_autoescape
from markupsafe import Markup
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from .auth import PAGE, Authenticator, AuthError, admit

log = logging.getLogger("garden_common.garden")

LANDING_PATHS = ("/garden", "/garden/")
PROFILE = re.compile(r"[a-z][a-z0-9-]*")
EDITOR = "code-server"
AGENTS_SCRIPT = (resources.files("garden_common") / "static" / "landing-agents.js").read_text()
AGENTS_SCRIPT_HASH = "sha256-" + base64.b64encode(hashlib.sha256(AGENTS_SCRIPT.encode()).digest()).decode()

# What the page says about each garden page; one without an entry shows its profile name.
LABELS = {
    "secrets": ("Vault", "Your personal data, shared with your agents only when you say so"),
    "gmail-triggers": ("Gmail triggers", "Ask an agent to act when mail arrives"),
    "code-server": ("Code editor", "Your agents' files in VS Code"),
}


@dataclass(frozen=True)
class Feature:
    profile: str
    label: str
    blurb: str

    @property
    def path(self) -> str:
        return f"/garden/{self.profile}/"


_env = Environment(loader=PackageLoader("garden_common", "templates"), autoescape=select_autoescape(default=True))


def profiles_from_env(env: Mapping[str, str]) -> str:
    """GARDEN_PROFILES, keeping only well-formed profile names."""
    names = (p.strip() for p in env.get("GARDEN_PROFILES", "").split(","))
    return ",".join(p for p in names if PROFILE.fullmatch(p))


def enabled(profiles: str) -> list[Feature]:
    out = []
    for p in profiles.split(","):
        if PROFILE.fullmatch(p):
            label, blurb = LABELS.get(p, (p, ""))
            out.append(Feature(p, label, blurb))
    return out


def lists_agents(profiles: str) -> bool:
    return EDITOR in profiles.split(",")


def render_landing(email: str, profiles: str, base: str) -> str:
    """The page; `base` is the serving sidecar's prefix, which serves garden.css
    and its icon (only /garden/ itself is routed to it)."""
    script = Markup(AGENTS_SCRIPT) if lists_agents(profiles) else None
    return _env.get_template("landing.html").render(
        email=email,
        features=enabled(profiles),
        base=base,
        editor=f"/garden/{EDITOR}/",
        editor_profile=EDITOR,
        editor_label=LABELS[EDITOR][0],
        script=script,
    )


def landing_csp(origin: str, profiles: str) -> str:
    """The page's own policy: no script but its own, which may only reach
    Lettuce's /ws on this origin."""
    if lists_agents(profiles):
        u = urlsplit(origin)
        script = f"'{AGENTS_SCRIPT_HASH}'"
        connect = f"{'wss' if u.scheme == 'https' else 'ws'}://{u.netloc}/ws"
    else:
        script = connect = "'none'"
    return (
        f"default-src 'none'; script-src {script}; connect-src {connect}; style-src 'self'; img-src 'self'; "
        "frame-ancestors 'none'; form-action 'none'; base-uri 'none'; object-src 'none'"
    )


def landing_routes(
    authenticator: Authenticator,
    profiles: str,
    base: str,
    headers: Mapping[str, str],
    error: Callable[[str, int], Response],
) -> list[Route]:
    """/garden and /garden/, behind the Access check alone: the page holds no
    sidecar data, and opening it must not count as using one (the vault's
    auto-lock)."""
    page_headers = {**headers, "content-security-policy": landing_csp(authenticator.origin, profiles)}

    async def landing(request: Request) -> Response:
        try:
            email = await admit(authenticator, request.headers, PAGE)
        except AuthError as e:
            log.info("refused %s %s reason=%s", request.method, request.url.path, e.reason)
            return error(str(e), e.status)
        return HTMLResponse(render_landing(email, profiles, base), headers=page_headers)

    return [Route(path, landing) for path in LANDING_PATHS]


def stylesheet() -> bytes:
    return (resources.files("garden_common") / "static" / "garden.css").read_bytes()
