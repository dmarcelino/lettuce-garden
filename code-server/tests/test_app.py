import time

import httpx
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from code_server_gate.app import create_app
from code_server_gate.config import PREFIX, ConfigError, Settings

from .conftest import EMAIL, NAVIGATE, ORIGIN, make_jwt

P = PREFIX
COMMIT = "stable-22ddf60dbc52f164cff13620ab515086dd08c90a"
WEBVIEW = f"/{COMMIT}/static/out/vs/workbench/contrib/webview/browser/pre/index.html"
AGENT_FILE = "/vscode-remote-resource?path=/work/agent-1/page.html&tkn=x"
IMAGE = {"sec-fetch-mode": "no-cors", "sec-fetch-dest": "image", "sec-fetch-site": "same-origin"}
SCRIPT = {"sec-fetch-mode": "cors", "sec-fetch-dest": "empty", "sec-fetch-site": "same-origin"}


def gate(settings, authenticator, editor, clock=time.time):
    client = httpx.AsyncClient(transport=httpx.MockTransport(editor.handle))
    return create_app(settings, authenticator, client, connect=editor.connect, clock=clock)


@pytest.fixture
def browser(settings, authenticator, editor, signing_key):
    with TestClient(gate(settings, authenticator, editor), base_url=ORIGIN, follow_redirects=False) as c:
        c.headers.update(NAVIGATE)
        c.headers["cf-access-jwt-assertion"] = make_jwt(signing_key)
        yield c


# -- Access ----------------------------------------------------------------------


@pytest.mark.parametrize("token", ["", "garbage", "other-key", {"email": "hal@example.com"}, {"exp": 1}, {"aud": "x"}])
def test_every_route_needs_a_valid_access_jwt(browser, editor, signing_key, other_key, token):
    if token == "other-key":
        value = make_jwt(other_key)
    elif isinstance(token, dict):
        value = make_jwt(signing_key, **token)
    else:
        value = token
    for path in [f"{P}/", f"{P}/{COMMIT}/static/x.js", f"{P}/garden.css", f"{P}/icon-192.png"]:
        r = browser.get(path, headers={"cf-access-jwt-assertion": value})
        assert r.status_code == 403, path
    r = browser.post(f"{P}/mint-key", headers={"cf-access-jwt-assertion": value})
    assert r.status_code == 403
    assert editor.requests == []


def test_unsigned_email_header_is_ignored(browser, editor):
    r = browser.get(f"{P}/", headers={"cf-access-jwt-assertion": "", "cf-access-authenticated-user-email": EMAIL})
    assert r.status_code == 403 and editor.requests == []


def test_healthz_is_open_and_the_bare_prefix_redirects(browser):
    assert browser.get("/healthz", headers={"cf-access-jwt-assertion": ""}).text == "ok"
    r = browser.get(P)
    assert r.status_code == 308 and r.headers["location"] == f"{P}/"


# -- the prefix and headers ------------------------------------------------------


def test_the_prefix_is_stripped_and_the_path_kept_as_sent(browser, editor):
    browser.get(f"{P}/?folder=/work/agent-1")
    browser.get(f"{P}/vscode-remote-resource?path=/work/a%20b/c%2Fd.png&tkn=1", headers=IMAGE)
    assert [str(r.url) for r in editor.requests] == [
        "http://code-server-editor/?folder=/work/agent-1",
        "http://code-server-editor/vscode-remote-resource?path=/work/a%20b/c%2Fd.png&tkn=1",
    ]


def test_only_listed_request_headers_reach_the_editor(browser, editor):
    browser.get(
        f"{P}/",
        headers={
            "cookie": "CF_Authorization=secret-token; lettuce_session=s",
            "authorization": "Bearer x",
            "cf-connecting-ip": "203.0.113.9",
            "cf-ray": "abc",
            "x-forwarded-for": "203.0.113.9",
            "accept-language": "pt-PT",
            "if-none-match": '"etag-1"',
        },
    )
    sent = editor.requests[0].headers
    for name in ("cookie", "authorization", "cf-access-jwt-assertion", "cf-connecting-ip", "cf-ray", "x-forwarded-for"):
        assert name not in sent, name
    for name in ("sec-fetch-mode", "sec-fetch-dest", "sec-fetch-site"):
        assert name not in sent, name
    assert sent["accept-language"] == "pt-PT" and sent["if-none-match"] == '"etag-1"'
    # code-server checks WebSocket origins against this host.
    assert sent["x-forwarded-host"] == "alfred-garden.example.com"


def test_only_listed_response_headers_reach_the_browser(browser, editor):
    editor.responses["/"] = httpx.Response(
        200,
        text="<html>workbench</html>",
        headers={
            "content-type": "text/html",
            "content-security-policy": "default-src 'self'",
            "set-cookie": "lettuce_session=hijacked; Path=/",
            "cross-origin-opener-policy": "same-origin",
            "cross-origin-embedder-policy": "require-corp",
            "access-control-allow-origin": "*",
            "server": "code-server",
            "etag": '"1"',
        },
    )
    r = browser.get(f"{P}/")
    assert r.status_code == 200 and r.text == "<html>workbench</html>"
    for name in (
        "set-cookie",
        "cross-origin-opener-policy",
        "cross-origin-embedder-policy",
        "access-control-allow-origin",
    ):
        assert name not in r.headers, name
    assert "server" not in r.headers or r.headers["server"] != "code-server"
    # The workbench keeps code-server's own policy, not the sandbox.
    assert r.headers["content-security-policy"] == "default-src 'self'"
    assert r.headers["etag"] == '"1"' and r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    "sent, seen",
    [
        ("/login?to=%2F", f"{P}/login?to=%2F"),
        ("./?folder=%2Fwork", "./?folder=%2Fwork"),
        ("login", "login"),
        (f"{ORIGIN}/?folder=x", f"{ORIGIN}{P}/?folder=x"),
        ("http://code-server-editor/x", f"{ORIGIN}{P}/x"),
        (f"{ORIGIN}{P}/ok", f"{ORIGIN}{P}/ok"),
        ("https://example.org/elsewhere", "https://example.org/elsewhere"),
    ],
)
def test_redirects_keep_the_prefix(browser, editor, sent, seen):
    editor.responses["/"] = httpx.Response(302, headers={"location": sent})
    assert browser.get(f"{P}/").headers["location"] == seen


def test_a_service_worker_gets_the_prefix_at_most(browser, editor):
    path = "/_static/out/browser/serviceWorker.js"
    editor.responses[path] = httpx.Response(
        200, text="self.x=1", headers={"content-type": "text/javascript", "service-worker-allowed": "/"}
    )
    worker = {"sec-fetch-mode": "same-origin", "sec-fetch-dest": "serviceworker", "sec-fetch-site": "same-origin"}
    r = browser.get(P + path, headers=worker)
    assert r.headers["service-worker-allowed"] == f"{P}/"
    # A worker's own policy would apply inside it; it gets no sandbox.
    assert "content-security-policy" not in r.headers


# -- what may load as a page -----------------------------------------------------


@pytest.mark.parametrize(
    "path, dest",
    [
        ("/", "document"),
        ("/?folder=/work/agent-1", "document"),
        ("/vscode/", "document"),
        (WEBVIEW + "?id=1&parentId=2", "iframe"),
        (f"/{COMMIT}/static/out/vs/workbench/contrib/webview/browser/pre/fake.html?id=1", "iframe"),
        (f"/{COMMIT}/static/out/vs/workbench/services/extensions/worker/webWorkerExtensionHostIframe.html", "iframe"),
    ],
)
def test_the_workbench_and_its_own_frames_load_as_pages(browser, editor, path, dest):
    r = browser.get(P + path, headers={"sec-fetch-dest": dest})
    assert r.status_code == 200, path
    assert r.headers.get("content-security-policy") != "sandbox"
    assert "cross-origin-opener-policy" not in r.headers


@pytest.mark.parametrize(
    "path, dest",
    [
        (AGENT_FILE, "document"),
        (AGENT_FILE, "iframe"),
        (AGENT_FILE, "object"),
        (AGENT_FILE, "embed"),
        ("/manifest.json", "document"),
        (WEBVIEW, "document"),
        ("/", "iframe"),
        (f"/{COMMIT}/static/out/vs/workbench/contrib/webview/browser/pre/%2e%2e/%2e%2e/x.html", "iframe"),
        ("/vscode-remote-resource?path=" + WEBVIEW, "iframe"),
    ],
)
def test_nothing_else_loads_as_a_page(browser, editor, path, dest):
    r = browser.get(P + path, headers={"sec-fetch-dest": dest})
    assert r.status_code == 403, (path, dest)
    assert r.headers["cross-origin-opener-policy"] == "same-origin"
    assert editor.requests == []


def test_a_request_without_fetch_metadata_is_refused(browser, editor):
    r = browser.get(f"{P}/", headers={"sec-fetch-dest": "", "sec-fetch-mode": "", "sec-fetch-site": ""})
    assert r.status_code == 403 and editor.requests == []


def test_everything_else_is_served_sandboxed(browser, editor):
    editor.responses["/vscode-remote-resource"] = httpx.Response(
        200, text="<script>parent.steal()</script>", headers={"content-type": "text/html"}
    )
    for fetch in (IMAGE, SCRIPT):
        r = browser.get(P + AGENT_FILE, headers=fetch)
        assert r.status_code == 200
        assert r.headers["content-security-policy"] == "sandbox"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["cross-origin-resource-policy"] == "same-origin"


# -- bodies, errors, the landing page --------------------------------------------


def test_every_answer_varies_with_how_it_was_asked(browser, editor):
    # The gate decides by Sec-Fetch-Dest; a cached answer must never be reused for another kind of request.
    editor.responses["/cached.js"] = httpx.Response(200, text="x", headers={"cache-control": "max-age=3600"})
    editor.responses["/varied.js"] = httpx.Response(200, text="x", headers={"vary": "Accept-Encoding"})
    for path, fetch in (("/cached.js", IMAGE), ("/varied.js", SCRIPT), ("/", NAVIGATE)):
        r = browser.get(f"{P}{path}", headers=fetch)
        assert r.status_code == 200, path
        vary = [v.strip().lower() for v in r.headers["vary"].split(",")]
        assert "sec-fetch-dest" in vary, path
    assert "accept-encoding" in browser.get(f"{P}/varied.js", headers=SCRIPT).headers["vary"].lower()


@pytest.mark.parametrize("site", ["same-site", "cross-site", "none"])
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_only_this_origin_may_send_anything_but_a_read(browser, editor, method, site):
    # The Access cookie is SameSite=Lax: another stack under the same domain is the same site.
    r = browser.request(method, f"{P}/x", headers={**SCRIPT, "sec-fetch-site": site}, content=b"x")
    assert r.status_code == 403
    assert editor.requests == []
    r = browser.request(method, f"{P}/x", headers=SCRIPT, content=b"x")
    assert r.status_code == 200 and len(editor.requests) == 1


@pytest.mark.parametrize("path", ["/code-serv%65r/x", "/%63ode-server/x", "/code-server%2Fx", "/code-server%2fx"])
def test_a_prefix_that_is_not_spelled_out_is_refused(browser, editor, path):
    r = browser.get(path, headers=SCRIPT)
    assert r.status_code == 404
    assert editor.requests == []


def test_bodies_stream_both_ways(browser, editor):
    big = bytes(range(256)) * 4096
    editor.responses["/upload"] = httpx.Response(
        201, content=big[::-1], headers={"content-type": "application/octet-stream"}
    )
    r = browser.post(f"{P}/upload", content=big, headers={**SCRIPT, "content-type": "application/octet-stream"})
    assert r.status_code == 201 and r.content == big[::-1]
    assert editor.bodies == [big]


def test_an_editor_that_is_down_gets_a_page_not_a_crash(browser, editor):
    editor.down = True
    r = browser.get(f"{P}/")
    assert r.status_code == 502 and "isn&#39;t running" in r.text
    assert r.headers["cache-control"] == "no-store"


def test_the_gate_serves_its_own_assets_and_not_the_garden_landing_page(browser, editor):
    assert P == "/code-server"
    for path in ("/", "/garden", "/garden/"):
        assert browser.get(path).status_code == 404, path
    css = browser.get(f"{P}/garden.css", headers={"sec-fetch-dest": "style"})
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    icon = browser.get(f"{P}/icon-192.png", headers={"sec-fetch-dest": "image"})
    assert icon.status_code == 302 and icon.headers["location"].startswith(f"{P}/_static/")
    assert editor.requests == []


def test_the_gates_own_pages_link_to_the_garden_root(browser, editor):
    editor.down = True
    r = browser.get(f"{P}/")
    assert 'href="/">Lettuce Garden</a>' in r.text and f'href="{P}/garden.css"' in r.text
    assert "/garden/" not in r.text
    assert r.headers["cross-origin-opener-policy"] == "same-origin"


def test_settings_from_the_environment():
    base = {
        "CF_ACCESS_TEAM_DOMAIN": "team1",
        "CF_ACCESS_AUD": "aud1",
        "ALLOWED_USERS": "Alfred@Gmail.com, hal@example.com",
        "PUBLIC_ORIGIN": "https://alfred-garden.example.com/",
    }
    s = Settings.from_env(base)
    assert s.allowed_users == {"alfred@gmail.com", "hal@example.com"}
    assert s.public_origin == ORIGIN and s.public_host == "alfred-garden.example.com"
    assert not hasattr(s, "garden_profiles")
    assert s.upstream == "http://code-server-editor"
    for broken in (
        {"PUBLIC_ORIGIN": "http://alfred-garden.example.com"},
        {"PUBLIC_ORIGIN": f"{ORIGIN}/x"},
        {"ALLOWED_USERS": ""},
    ):
        with pytest.raises(ConfigError):
            Settings.from_env({**base, **broken})


# -- WebSockets ------------------------------------------------------------------


def socket_headers(signing_key, **over):
    headers = {"origin": ORIGIN, "cf-access-jwt-assertion": make_jwt(signing_key), "cookie": "CF_Authorization=t"}
    headers.update(over)
    return {k: v for k, v in headers.items() if v is not None}


def test_a_websocket_is_relayed_both_ways(settings, authenticator, editor, signing_key):
    with TestClient(gate(settings, authenticator, editor), base_url=ORIGIN) as c:
        with c.websocket_connect(
            f"{P}/?reconnectionToken=abc&skipWebSocketFrames=false",
            headers=socket_headers(signing_key),
            subprotocols=["vscode-remote"],
        ) as ws:
            assert ws.accepted_subprotocol == "vscode-remote"
            ws.send_bytes(b"\x00\x01binary")
            assert ws.receive_bytes() == b"\x00\x01binary"
            ws.send_text("text")
            assert ws.receive_text() == "text"
    peer = editor.sockets[0]
    assert peer.url == "ws://code-server-editor/?reconnectionToken=abc&skipWebSocketFrames=false"
    assert peer.received == [b"\x00\x01binary", "text"]
    sent = dict(peer.headers)
    assert "cookie" not in sent and "cf-access-jwt-assertion" not in sent
    assert sent["origin"] == ORIGIN and sent["x-forwarded-host"] == "alfred-garden.example.com"
    assert peer.closed


@pytest.mark.parametrize(
    "over",
    [
        {"cf-access-jwt-assertion": None},
        {"cf-access-jwt-assertion": "garbage"},
        {"origin": "https://evil.example.org"},
        {"origin": "https://alfred-garden.example.com:8443"},
        {"origin": None},
    ],
)
def test_a_websocket_needs_access_and_this_origin(settings, authenticator, editor, signing_key, over):
    with TestClient(gate(settings, authenticator, editor), base_url=ORIGIN) as c:
        with pytest.raises(WebSocketDisconnect) as e:
            with c.websocket_connect(f"{P}/", headers=socket_headers(signing_key, **over)):
                pass
    assert e.value.code == 1008
    assert editor.sockets == []


def test_a_websocket_to_a_prefix_that_is_not_spelled_out_is_refused(settings, authenticator, editor, signing_key):
    with TestClient(gate(settings, authenticator, editor), base_url=ORIGIN) as c:
        with pytest.raises(WebSocketDisconnect) as e:
            with c.websocket_connect("/code-server%2F", headers=socket_headers(signing_key)):
                pass
    assert e.value.code == 1008
    assert editor.sockets == []


def test_a_websocket_closes_when_the_sign_in_expires(settings, authenticator, editor, signing_key):
    expiry = int(time.time()) + 3600
    token = make_jwt(signing_key, exp=expiry)

    def clock():
        return expiry - 0.2  # the JWT has 0.2 s left

    with TestClient(gate(settings, authenticator, editor, clock=clock), base_url=ORIGIN) as c:
        with c.websocket_connect(
            f"{P}/", headers=socket_headers(signing_key, **{"cf-access-jwt-assertion": token})
        ) as ws:
            message = ws.receive()
    assert message["type"] == "websocket.close" and message["code"] == 1008
    assert editor.sockets[0].closed


def test_a_websocket_to_an_editor_that_is_down_closes(settings, authenticator, editor, signing_key):
    editor.down = True
    with TestClient(gate(settings, authenticator, editor), base_url=ORIGIN) as c:
        with pytest.raises(WebSocketDisconnect) as e:
            with c.websocket_connect(f"{P}/", headers=socket_headers(signing_key)):
                pass
    assert e.value.code == 1011
