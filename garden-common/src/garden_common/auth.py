"""Who is asking: the Cloudflare Access JWT, verified like upstream's BFF does
(bff/src/auth/cf-access.ts), CSRF tokens for every form, and Fetch Metadata
checks (check_fetch).

Shared by the garden sidecars (the vault, gmail-triggers). Agents reach them
over the Docker network without passing Access, so they can send any header
they like. Only a JWT signed by the team's Access
keys, for this stack's application, carrying an allowed email, counts.
The unsigned Cf-Access-Authenticated-User-Email header is never read.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable
from typing import Any, Protocol

import anyio
import jwt

JWT_HEADER = "cf-access-jwt-assertion"

# How a request may arrive, by Fetch Metadata (Sec-Fetch-*): browsers set those
# headers and no script can change them. The code editor (docs/code-server.md)
# shares this origin, so a script there could otherwise fetch a page, read its
# form token and post as the person. Pages load only as top-level documents
# (any site: an Access re-login arrives through a cross-site redirect), forms
# post only from this origin, and the vault's own fetch() calls only from this
# origin, carrying a token no script can now read.
PAGE = "page"
FORM = "form"
FETCH = "fetch"
COOP = {"cross-origin-opener-policy": "same-origin"}


class AccessSettings(Protocol):
    """What the checks need from a sidecar's settings."""

    @property
    def team_domain(self) -> str: ...
    @property
    def audience(self) -> str: ...
    @property
    def issuer(self) -> str: ...
    @property
    def allowed_users(self) -> frozenset[str]: ...
    @property
    def public_origin(self) -> str: ...


class AuthError(Exception):
    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


KeyResolver = Callable[[str], Any]


def jwks_resolver(team_domain: str) -> KeyResolver:
    client = jwt.PyJWKClient(
        f"https://{team_domain}.cloudflareaccess.com/cdn-cgi/access/certs", cache_keys=True, lifespan=3600
    )

    def resolve(token: str) -> Any:
        return client.get_signing_key_from_jwt(token).key

    return resolve


class Authenticator:
    def __init__(self, settings: AccessSettings, key_resolver: KeyResolver | None = None):
        self._settings = settings
        self._resolve = key_resolver or jwks_resolver(settings.team_domain)
        # Forms carry HMAC(email); the key lives only in this process, so a
        # restart invalidates open forms (the page says to reload).
        self._csrf_key = secrets.token_bytes(32)

    @property
    def origin(self) -> str:
        """The stack's public origin (PUBLIC_ORIGIN)."""
        return self._settings.public_origin

    async def email(self, headers: Any) -> str:
        token = headers.get(JWT_HEADER)
        if not token:
            raise AuthError("sign in through the stack's address; direct access is not allowed")
        try:
            key = await anyio.to_thread.run_sync(self._resolve, token)
        except jwt.PyJWKClientError as e:
            # Unreachable or unknown signing keys: fail closed.
            raise AuthError("could not verify the sign-in right now", status=503) from e
        except jwt.PyJWTError as e:
            raise AuthError("invalid sign-in token") from e
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self._settings.audience,
                issuer=self._settings.issuer,
                options={"require": ["exp", "iat", "aud", "iss"]},
            )
        except jwt.PyJWTError as e:
            raise AuthError("invalid sign-in token") from e
        email = claims.get("email")
        if not isinstance(email, str) or not email.strip():
            raise AuthError("the sign-in token has no email")
        email = email.strip().lower()
        if email not in self._settings.allowed_users:
            raise AuthError("this account may not use this stack")
        return email

    def csrf_token(self, email: str) -> str:
        return hmac.new(self._csrf_key, email.encode(), hashlib.sha256).hexdigest()

    def check_csrf(self, email: str, token: str | None) -> None:
        if not token or not hmac.compare_digest(self.csrf_token(email), token):
            raise AuthError("this form has expired; reload the page and try again")

    def check_origin(self, headers: Any) -> None:
        """Browsers send Origin on every POST; it must be this stack's own."""
        if headers.get("origin") != self._settings.public_origin:
            raise AuthError("cross-site request refused")


def check_fetch(headers: Any, use: str) -> None:
    """Refuse a request that didn't arrive the way `use` (PAGE, FORM, FETCH) arrives."""
    mode, dest, site = (headers.get(f"sec-fetch-{k}") for k in ("mode", "dest", "site"))
    if not (mode and dest and site):
        raise AuthError("this browser did not say how the page was requested; use a current browser")
    if use == FETCH:
        ok = mode in ("cors", "same-origin") and dest == "empty" and site == "same-origin"
    else:
        ok = mode == "navigate" and dest == "document" and (use == PAGE or site == "same-origin")
    if not ok:
        raise AuthError("open this page in its own browser tab; it can't be loaded from a script or a frame")
