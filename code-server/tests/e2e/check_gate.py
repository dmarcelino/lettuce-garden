"""The gate in front of the real editor, for scripts/code-server-e2e.sh (not a pytest
module: it needs a running code-server-editor). It builds the gate with a test
signing key in place of Cloudflare's, real HTTP and WebSockets over the editor's Unix socket,
and drives it in-process. Prints one line per check; exits 1 when any fails."""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Callable

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from garden_common.auth import Authenticator
from starlette.testclient import TestClient

from code_server_gate.app import create_app, editor_client, unix_socket_connect
from code_server_gate.config import PREFIX, Settings

ORIGIN = "https://alfred.example.com"
EMAIL = "alfred@example.com"
DOCUMENT = {"sec-fetch-mode": "navigate", "sec-fetch-dest": "document", "sec-fetch-site": "same-origin"}
FRAME = {**DOCUMENT, "sec-fetch-dest": "iframe"}
SCRIPT = {"sec-fetch-mode": "no-cors", "sec-fetch-dest": "script", "sec-fetch-site": "same-origin"}
IMAGE = {**SCRIPT, "sec-fetch-dest": "image"}
FETCH = {"sec-fetch-mode": "cors", "sec-fetch-dest": "empty", "sec-fetch-site": "same-origin"}

failures = 0


def check(name: str, fn: Callable[[], None]) -> None:
    global failures
    try:
        fn()
        print(f"ok    gate: {name}")
    except Exception as e:  # report and go on: every check runs
        failures += 1
        print(f"FAIL  gate: {name}: {type(e).__name__}: {e}")


def main() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    settings = Settings(
        team_domain="team1",
        audience="aud1",
        issuer="https://team1.cloudflareaccess.com",
        allowed_users=frozenset({EMAIL}),
        public_origin=ORIGIN,
        garden_profiles="code-server",
    )
    now = int(time.time())
    token = jwt.encode(
        {"aud": ["aud1"], "iss": settings.issuer, "email": EMAIL, "iat": now, "exp": now + 600}, key, algorithm="RS256"
    )
    authenticator = Authenticator(settings, key_resolver=lambda t: key.public_key())
    app = create_app(settings, authenticator, editor_client(), unix_socket_connect())
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as browser:
        browser.headers["cf-access-jwt-assertion"] = token
        page = {"html": ""}

        def workbench() -> None:
            r = browser.get(f"{PREFIX}/?folder=/work/agent-1", headers=DOCUMENT)
            assert r.status_code == 200, r.status_code
            assert "workbench" in r.text, "no workbench in the page"
            assert "cross-origin-opener-policy" not in r.headers, "the workbench got COOP"
            assert r.headers.get("content-security-policy") != "sandbox", "the workbench was sandboxed"
            page["html"] = r.text

        check("the workbench loads under the prefix", workbench)

        def no_marketplace() -> None:
            # The product configuration is HTML-escaped JSON in the page.
            assert "&quot;extensionsGallery&quot;:{}" in page["html"], "the gallery is not empty"

        check("the workbench has no marketplace", no_marketplace)

        def static_script() -> None:
            # Relative to the page: "stable-<commit>/static/...".
            m = re.search(r'src="(?:\./)?([a-z]+-[0-9a-f]{40}/static/[^"]+\.js)"', page["html"])
            assert m, "no static script in the page"
            r = browser.get(f"{PREFIX}/{m.group(1)}", headers=SCRIPT)
            assert r.status_code == 200, r.status_code
            assert "javascript" in r.headers["content-type"], r.headers["content-type"]
            assert r.headers["x-content-type-options"] == "nosniff"

        check("the workbench's scripts load", static_script)

        def webview_host() -> None:
            m = re.search(r"([a-z]+-[0-9a-f]{40})/static/", page["html"])
            assert m, "no static route in the page"
            path = f"/{m.group(1)}/static/out/vs/workbench/contrib/webview/browser/pre/index.html?id=1"
            r = browser.get(PREFIX + path, headers=FRAME)
            assert r.status_code == 200, r.status_code

        check("the webview host loads as a frame", webview_host)

        agent_file = "/vscode-remote-resource?path=/work/agent-1/page.html"

        def agent_page_refused() -> None:
            for headers in (DOCUMENT, FRAME):
                r = browser.get(PREFIX + agent_file, headers=headers)
                assert r.status_code == 403, (headers["sec-fetch-dest"], r.status_code)

        check("an agent's HTML file can't load as a page", agent_page_refused)

        def agent_file_sandboxed() -> None:
            r = browser.get(PREFIX + agent_file, headers=IMAGE)
            assert r.status_code == 200, r.status_code
            assert r.headers["content-security-policy"] == "sandbox"
            assert "agent-page" in r.text

        check("an agent's file loads only sandboxed", agent_file_sandboxed)

        def port_proxy_off() -> None:
            r = browser.get(f"{PREFIX}/proxy/1234/", headers=FETCH)
            assert r.status_code == 403, r.status_code

        check("the port proxy is off", port_proxy_off)

        def no_jwt() -> None:
            r = browser.get(f"{PREFIX}/", headers={**DOCUMENT, "cf-access-jwt-assertion": ""})
            assert r.status_code == 403, r.status_code

        check("no Access JWT, no editor", no_jwt)

        def websocket() -> None:
            headers = {"origin": ORIGIN, "cf-access-jwt-assertion": token}
            url = f"{PREFIX}/?reconnectionToken=e2e-1&reconnection=false&skipWebSocketFrames=false"
            with browser.websocket_connect(url, headers=headers) as ws:
                ws.send_bytes(b"\x00")  # anything: the editor answers or closes, either proves the relay

        check("a WebSocket reaches the editor through the gate", websocket)

        def websocket_foreign_origin() -> None:
            headers = {"origin": "https://alfred.example.com:8443", "cf-access-jwt-assertion": token}
            try:
                with browser.websocket_connect(f"{PREFIX}/", headers=headers):
                    raise AssertionError("accepted")
            except AssertionError:
                raise
            except Exception:
                return

        check("a WebSocket from another origin is refused", websocket_foreign_origin)

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
