"""The garden landing page at /garden/: a link to each garden page that is on.

lettucectl passes the sidecars GARDEN_PROFILES, the stack's profiles that add a
garden page (PROFILE_ROUTES in lib/cloudflare.sh), and routes /garden/ to the
first of those sidecars (cf_tunnel_configure), so every sidecar can serve it.
Each page lives at /garden/<profile>/; a page that is off has no route, and a
link to it would land on Lettuce.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import resources

from jinja2 import Environment, PackageLoader, select_autoescape
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.routing import Route

from .auth import Authenticator, AuthError

log = logging.getLogger("garden_common.garden")

LANDING_PATHS = ("/garden", "/garden/")
PROFILE = re.compile(r"[a-z][a-z0-9-]*")

# What the page says about each garden page; one without an entry shows its profile name.
LABELS = {
    "secrets": ("Vault", "Your personal data, shared with your agents only when you say so"),
    "gmail-triggers": ("Gmail triggers", "Ask an agent to act when mail arrives"),
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


def render_landing(email: str, profiles: str, base: str) -> str:
    """The page; `base` is the serving sidecar's prefix, which serves garden.css
    and its icon (only /garden/ itself is routed to it)."""
    return _env.get_template("landing.html").render(email=email, features=enabled(profiles), base=base)


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

    async def landing(request: Request) -> Response:
        try:
            email = await authenticator.email(request.headers)
        except AuthError as e:
            log.info("refused %s %s reason=auth", request.method, request.url.path)
            return error(str(e), e.status)
        return HTMLResponse(render_landing(email, profiles, base), headers=dict(headers))

    return [Route(path, landing) for path in LANDING_PATHS]


def stylesheet() -> bytes:
    return (resources.files("garden_common") / "static" / "garden.css").read_bytes()
