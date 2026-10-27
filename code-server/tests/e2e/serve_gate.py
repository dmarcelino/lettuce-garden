"""The gate as cloudflared would feed it, for scripts/code-server-e2e.sh --browser
(never in a stack): plain HTTP on :8000 with PUBLIC_ORIGIN http://localhost:8000 (the
browser shares its network namespace; browsers send Fetch Metadata only to secure
origins, and localhost is one), and every request and WebSocket given a valid
Access JWT signed with a test key, as Access would add one after a sign-in."""

from __future__ import annotations

import time

import httpx
import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from garden_common.auth import JWT_HEADER, Authenticator

from code_server_gate.app import create_app
from code_server_gate.config import Settings

EMAIL = "alfred@example.com"


def main() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = Settings(
        team_domain="team1",
        audience="aud1",
        issuer="https://team1.cloudflareaccess.com",
        allowed_users=frozenset({EMAIL}),
        public_origin="http://localhost:8000",
        garden_profiles="code-server",
    )
    authenticator = Authenticator(settings, key_resolver=lambda t: key.public_key())
    gate = create_app(settings, authenticator, httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None)))

    async def access(scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            now = int(time.time())
            token = jwt.encode(
                {"aud": ["aud1"], "iss": settings.issuer, "email": EMAIL, "iat": now, "exp": now + 3600},
                key,
                algorithm="RS256",
            )
            headers = [(k, v) for k, v in scope["headers"] if k != JWT_HEADER.encode()]
            scope = {**scope, "headers": [*headers, (JWT_HEADER.encode(), token.encode())]}
        await gate(scope, receive, send)

    uvicorn.run(access, host="0.0.0.0", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
