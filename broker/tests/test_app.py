import json
import logging
import re
import time

import jwt
import pytest
from garden_common.auth import Authenticator
from starlette.testclient import TestClient

from secret_broker.app import create_app
from secret_broker.config import AUTO_LOCK_SECONDS, PREFIX, Settings
from secret_broker.vault import Vault

from .conftest import (
    ADDRESS_TEXT,
    CARD,
    EMAIL,
    FAST_KDF,
    ORIGIN,
    PASSPHRASE,
    SECRET,
    STREET,
    FakeSender,
    make_broker,
    make_jwt,
)

MCP = "http://secret-broker:8000/mcp"
B = PREFIX


@pytest.fixture
def env(settings, authenticator, signing_key, vault_path, clock):
    vault = Vault(vault_path, clock=clock, kdf=FAST_KDF)
    sender = FakeSender()
    broker = make_broker(settings, vault, sender)
    app = create_app(broker, authenticator, settings.vapid_public_key)
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        client.headers["cf-access-jwt-assertion"] = make_jwt(signing_key)
        yield client, vault, sender


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" content="([0-9a-f]+)"', html).group(1)


def post(client, path, data, csrf=None, origin=ORIGIN):
    form = dict(data)
    form["csrf"] = csrf if csrf is not None else csrf_of(client.get(f"{B}/notifications").text)
    return client.post(path, data=form, headers={"origin": origin} if origin else {})


def setup_vault(client):
    r = post(client, f"{B}/setup", {"passphrase": PASSPHRASE, "confirm": PASSPHRASE})
    assert r.status_code == 303, r.text


ADDRESS = {"kind": "address", "name": "Home address", "f_text": ADDRESS_TEXT, "share_mode": "ask"}
CARD_FORM = {
    "kind": "card",
    "name": "Visa",
    "f_cardholder": "Alfred Test",
    "f_number": CARD,
    "f_expiry": "12/30",
    "f_security_code": "123",
    "f_notes": SECRET,
}


def add(client, form) -> str:
    r = post(client, f"{B}/vault", form)
    assert r.status_code == 303, r.text
    return r.headers["location"].rsplit("/", 1)[1]


def mcp(client, method, params=None, host_url=MCP):
    return client.post(
        host_url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers={
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2025-06-18",
            "cf-access-jwt-assertion": "",
        },
    )


def call_tool(client, name, args):
    r = mcp(client, "tools/call", {"name": name, "arguments": args})
    assert r.status_code == 200, r.text
    return r.json()["result"]["structuredContent"], r.text


# ── authentication ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "token",
    [
        None,
        "not-a-jwt",
        "other-key",
        {"aud": ["other-app"]},
        {"iss": "https://evil.cloudflareaccess.com"},
        {"email": "mallory@example.com"},
        {"email": None},
        {"exp": int(time.time()) - 10},
    ],
)
def test_ui_requires_valid_access_jwt(env, signing_key, other_key, token):
    client = env[0]
    if token is None:
        value = ""
    elif token == "other-key":
        value = make_jwt(other_key)
    elif isinstance(token, dict):
        value = make_jwt(signing_key, **token)
    else:
        value = token
    for path in [f"{B}/", f"{B}/vault", f"{B}/audit", f"{B}/sw.js", f"{B}/notifications"]:
        assert client.get(path, headers={"cf-access-jwt-assertion": value}).status_code == 403, path


def test_unsigned_email_header_is_ignored(env):
    r = env[0].get(f"{B}/", headers={"cf-access-jwt-assertion": "", "cf-access-authenticated-user-email": EMAIL})
    assert r.status_code == 403


def test_jwks_unavailable_fails_closed(settings, signing_key, vault_path, clock):
    def broken(token):
        raise jwt.PyJWKClientError("unreachable")

    vault = Vault(vault_path, clock=clock, kdf=FAST_KDF)
    app = create_app(make_broker(settings, vault), Authenticator(settings, key_resolver=broken), "k")
    with TestClient(app, base_url=ORIGIN) as client:
        r = client.get(f"{B}/", headers={"cf-access-jwt-assertion": make_jwt(signing_key)})
    assert r.status_code == 503


def test_security_headers_and_static(env):
    client = env[0]
    r = client.get(f"{B}/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    # Regression: with "no-referrer" browsers send `Origin: null` on form posts,
    # and check_origin refused every form ("cross-site request refused").
    assert r.headers["referrer-policy"] == "same-origin"
    assert client.get(f"{B}/sw.js").headers["content-type"].startswith("text/javascript")
    assert client.get(f"{B}/manifest.webmanifest").json()["name"] == "Lettuce Vault"
    assert client.get(f"{B}/icon-192.png").content.startswith(b"\x89PNG")


def test_pages_use_the_garden_look_and_link_to_the_garden(env):
    client = env[0]
    page = client.get(f"{B}/").text
    assert f'href="{B}/garden.css"' in page and 'href="/garden/">Lettuce Garden</a>' in page
    # app.js and the manifest take the prefix from the page and their own URL.
    assert f'<meta name="base" content="{B}">' in page
    assert client.get(f"{B}/manifest.webmanifest").json()["scope"] == "./"
    css = client.get(f"{B}/garden.css")
    assert css.status_code == 200 and "--accent: #0b1020" in css.text


def test_garden_landing_needs_access_and_lists_the_features_that_are_on(
    settings, authenticator, signing_key, vault_path, clock
):
    vault = Vault(vault_path, clock=clock, kdf=FAST_KDF)
    app = create_app(make_broker(settings, vault), authenticator, "k", garden_profiles="secrets")
    jwt_header = {"cf-access-jwt-assertion": make_jwt(signing_key)}
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        for path in ("/garden", "/garden/"):
            assert client.get(path).status_code == 403, path
            # Open while the vault is not even set up: it holds no vault data.
            r = client.get(path, headers=jwt_header)
            assert r.status_code == 200, path
            assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
            assert f'href="{B}/"' in r.text and f'href="{B}/garden.css"' in r.text
            assert 'href="/garden/gmail-triggers/"' not in r.text
    app = create_app(make_broker(settings, vault), authenticator, "k", garden_profiles="secrets,gmail-triggers")
    with TestClient(app, base_url=ORIGIN) as client:
        page = client.get("/garden/", headers=jwt_header).text
    assert 'href="/garden/gmail-triggers/"' in page and f'href="{B}/"' in page


def test_settings_read_the_garden_profiles():
    base = {
        "CF_ACCESS_TEAM_DOMAIN": "team1",
        "CF_ACCESS_AUD": "aud1",
        "ALLOWED_USERS": EMAIL,
        "PUBLIC_ORIGIN": ORIGIN,
        "BROKER_VAPID_PUBLIC_KEY": "pub",
        "BROKER_VAPID_PRIVATE_KEY": "priv",
        "PUSH_VAPID_CONTACT_EMAIL": "ops@example.com",
    }
    assert Settings.from_env(base).garden_profiles == ""
    settings = Settings.from_env({**base, "GARDEN_PROFILES": "secrets, gmail-triggers,../x,"})
    assert settings.garden_profiles == "secrets,gmail-triggers"


def test_garden_landing_does_not_hold_the_vault_open(env, clock):
    client, vault = env[0], env[1]
    setup_vault(client)
    assert vault.is_unlocked()
    clock.t += AUTO_LOCK_SECONDS - 60
    assert client.get("/garden/").status_code == 200
    clock.t += 120
    assert not vault.is_unlocked()


# ── setup, lock, unlock ──────────────────────────────────────────────────────


def test_first_visit_asks_to_create_the_vault(env):
    client, vault = env[0], env[1]
    assert "Create your vault" in client.get(f"{B}/vault").text
    r = post(client, f"{B}/setup", {"passphrase": PASSPHRASE, "confirm": "something else entirely"})
    assert r.status_code == 400 and not vault.is_setup()
    r = post(client, f"{B}/setup", {"passphrase": "short", "confirm": "short"})
    assert r.status_code == 400 and not vault.is_setup()
    setup_vault(client)
    assert vault.is_unlocked()
    assert "Add a secret" in client.get(f"{B}/vault").text


def test_locked_pages_ask_to_unlock_and_return(env):
    client, vault = env[0], env[1]
    setup_vault(client)
    secret = add(client, ADDRESS)
    post(client, f"{B}/lock", {})
    page = client.get(f"{B}/vault/{secret}")
    assert "Your vault is locked" in page.text and STREET not in page.text
    assert f'value="{B}/vault/{secret}"' in page.text
    r = post(client, f"{B}/unlock", {"passphrase": "wrong passphrase!!", "next": f"{B}/vault/{secret}"})
    assert r.status_code == 403 and not vault.is_unlocked()
    r = post(client, f"{B}/unlock", {"passphrase": PASSPHRASE, "next": f"{B}/vault/{secret}"})
    assert r.status_code == 303 and r.headers["location"] == f"{B}/vault/{secret}"
    post(client, f"{B}/lock", {})
    r = post(client, f"{B}/unlock", {"passphrase": PASSPHRASE, "next": "https://evil.example/"})
    assert r.headers["location"] == f"{B}/"


def test_unlock_is_rate_limited(env):
    client, vault = env[0], env[1]
    setup_vault(client)
    post(client, f"{B}/lock", {})
    codes = [post(client, f"{B}/unlock", {"passphrase": "nope nope nope"}).status_code for _ in range(5)]
    assert codes == [403] * 5
    r = post(client, f"{B}/unlock", {"passphrase": PASSPHRASE})
    assert r.status_code == 429 and not vault.is_unlocked()


def test_reset_needs_typed_confirmation(env):
    client, vault = env[0], env[1]
    setup_vault(client)
    add(client, ADDRESS)
    assert post(client, f"{B}/settings/reset", {"confirm": "delete"}).status_code == 400
    assert post(client, f"{B}/settings/reset", {"confirm": "DELETE"}).status_code == 303
    assert not vault.is_setup()


# ── CSRF and origin ──────────────────────────────────────────────────────────


def test_post_needs_csrf_and_same_origin(env):
    client, vault = env[0], env[1]
    form = {"passphrase": PASSPHRASE, "confirm": PASSPHRASE}
    assert post(client, f"{B}/setup", form, csrf="0" * 64).status_code == 403
    assert post(client, f"{B}/setup", form, origin="https://evil.example").status_code == 403
    assert post(client, f"{B}/setup", form, origin=None).status_code == 403
    assert not vault.is_setup()


# ── secrets ────────────────────────────────────────────────────────────────────


def test_private_fields_hidden_until_show(env):
    client = env[0]
    setup_vault(client)
    secret = add(client, CARD_FORM)
    page = client.get(f"{B}/vault/{secret}").text
    assert "Visa" in page and "ending 4242" in page and "12/30" in page
    assert CARD not in page and SECRET not in page
    shown = post(client, f"{B}/vault/{secret}/show", {})
    assert CARD in shown.text and SECRET in shown.text
    assert any(e.action == "viewed private fields" for e in env[1].audit_log())
    edit = client.get(f"{B}/vault/{secret}/edit").text
    assert CARD not in edit and SECRET not in edit


def test_editing_keeps_private_values_left_blank(env):
    client, vault = env[0], env[1]
    setup_vault(client)
    secret = add(client, CARD_FORM)
    form = {
        "kind": "card",
        "name": "Visa (work)",
        "f_cardholder": "Alfred Test",
        "f_number": "",
        "clear_number": "on",  # required: cannot be removed, the blank keeps it
        "f_expiry": "01/31",
        "f_security_code": "",
        "clear_security_code": "on",
        "f_notes": "",
        "share_mode": "ask",
    }
    assert post(client, f"{B}/vault/{secret}", form).status_code == 303
    saved = vault.secret(secret)
    assert saved.name == "Visa (work)" and saved.field("number").value == CARD
    assert saved.field("expiry").value == "01/31" and saved.field("security_code") is None
    assert saved.field("notes").value == SECRET


def test_card_cannot_be_shared_without_asking(env):
    client, vault = env[0], env[1]
    setup_vault(client)
    assert post(client, f"{B}/vault", {**CARD_FORM, "share_mode": "auto"}).status_code == 400
    assert vault.secrets() == []


def test_delete_item(env):
    client, vault = env[0], env[1]
    setup_vault(client)
    secret = add(client, ADDRESS)
    assert post(client, f"{B}/vault/{secret}/delete", {}).status_code == 303
    assert vault.secrets() == []


def test_values_never_in_history_page(env):
    client = env[0]
    setup_vault(client)
    secret = add(client, CARD_FORM)
    post(client, f"{B}/vault/{secret}/show", {})
    page = client.get(f"{B}/audit").text
    assert "viewed private fields" in page and CARD not in page and SECRET not in page


# ── MCP ──────────────────────────────────────────────────────────────────────


def test_mcp_tools(env):
    r = mcp(env[0], "tools/list")
    assert r.status_code == 200, r.text
    tools = {t["name"]: t for t in r.json()["result"]["tools"]}
    assert set(tools) == {"list_secrets", "request_secret", "get_result"}
    assert tools["list_secrets"]["annotations"]["readOnlyHint"] is True
    assert tools["get_result"]["annotations"]["readOnlyHint"] is True
    assert tools["request_secret"]["annotations"].get("readOnlyHint") is not True
    assert tools["request_secret"]["inputSchema"]["required"] == ["secret_id", "purpose"]
    delivery = tools["request_secret"]["inputSchema"]["properties"]["delivery"]
    assert delivery["enum"] == ["text", "env"] and delivery["default"] == "text"
    for name in ("request_secret", "get_result"):
        assert "never repeat" in tools[name]["description"].lower(), name


def test_mcp_refuses_public_host(env):
    assert mcp(env[0], "tools/list", host_url=f"{ORIGIN}/mcp").status_code in (403, 421)


def test_end_to_end_through_mcp_and_ui(env, caplog):
    caplog.set_level(logging.DEBUG)
    client, vault, sender = env
    setup_vault(client)
    card = add(client, CARD_FORM)
    vault.add_subscription("https://push.example/1", "k", "a", EMAIL)

    listed, raw = call_tool(client, "list_secrets", {})
    assert listed["secrets"][0]["name"] == "Visa"
    assert CARD not in raw and SECRET not in raw

    args = {"secret_id": card, "fields": ["number", "security_code"], "purpose": "Pay for the train"}
    sub, _ = call_tool(client, "request_secret", args)
    assert sub["status"] == "pending"
    rid = sub["id"]

    page = client.get(f"{B}/r/{rid}").text
    assert "Pay for the train" in page and "Card number" in page and "ending 4242" in page and CARD not in page
    assert "Expiry (MM/YY): 12/30" in page
    assert 'class="badge">1<' in client.get(f"{B}/vault").text
    assert post(client, f"{B}/r/{rid}/decide", {"action": "share", "fields": ["number"]}).status_code == 303

    res, _ = call_tool(client, "get_result", {"request_id": rid, "wait_seconds": 5})
    assert res["status"] == "done" and res["values"] == {"number": CARD} and res["withheld"] == ["Security code"]
    again, _ = call_tool(client, "get_result", {"request_id": rid})
    assert "values" not in again
    assert CARD not in client.get(f"{B}/r/{rid}").text
    assert CARD not in json.dumps([c["data"] for c in sender.calls])
    assert CARD not in caplog.text and SECRET not in caplog.text


def test_mcp_cannot_approve(env):
    """An agent can reach every internal route, but has no Access JWT."""
    client = env[0]
    setup_vault(client)
    secret = add(client, ADDRESS)
    sub, _ = call_tool(client, "request_secret", {"secret_id": secret, "purpose": "x"})
    r = client.post(
        f"http://secret-broker:8000{B}/r/{sub['id']}/decide",
        data={"action": "share", "fields": "text", "csrf": "x"},
        headers={"cf-access-jwt-assertion": "", "origin": ORIGIN},
    )
    assert r.status_code == 403
    res, _ = call_tool(client, "get_result", {"request_id": sub["id"]})
    assert res["status"] == "pending"


def test_request_page_escapes_agent_input(env):
    client = env[0]
    setup_vault(client)
    secret = add(client, ADDRESS)
    args = {"secret_id": secret, "purpose": "<script>alert(1)</script>"}
    sub, _ = call_tool(client, "request_secret", args)
    page = client.get(f"{B}/r/{sub['id']}").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_push_subscription_endpoints_work_while_locked(env):
    client, vault, _ = env
    csrf = csrf_of(client.get(f"{B}/notifications").text)
    headers = {"x-csrf-token": csrf, "origin": ORIGIN}
    sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "key", "auth": "auth"}}
    assert client.post(f"{B}/push/subscribe", json=sub, headers=headers).status_code == 200
    assert [s["email"] for s in vault.subscriptions()] == [EMAIL]
    assert client.post(f"{B}/push/subscribe", json=sub, headers={"origin": ORIGIN}).status_code == 403
    bad = {"endpoint": "http://internal/x", "keys": {"p256dh": "k", "auth": "a"}}
    assert client.post(f"{B}/push/subscribe", json=bad, headers=headers).status_code == 400
    assert client.post(f"{B}/push/test", json={}, headers=headers).json() == {"delivered": 1}
    unsub = {"endpoint": sub["endpoint"]}
    assert client.post(f"{B}/push/unsubscribe", json=unsub, headers=headers).status_code == 200
    assert vault.subscriptions() == []


def test_locked_vault_answers_agents_and_notifies(env):
    """Regression: the list tool ran in a worker thread and could not schedule the unlock push."""
    client, vault, sender = env
    setup_vault(client)
    vault.add_subscription("https://push.example/1", "k", "a", EMAIL)
    post(client, f"{B}/lock", {})
    listed, _ = call_tool(client, "list_secrets", {})
    assert listed["locked"] is True
    for _ in range(100):  # the push is sent in the background
        if sender.calls:
            break
        time.sleep(0.02)
    assert len(sender.calls) == 1


# ── env delivery and /redeem ─────────────────────────────────────────────────

REDEEM = "http://secret-broker:8000/redeem"


def redeem(client, handle, url=REDEEM, **headers):
    return client.post(url, json={"handle": handle}, headers={"cf-access-jwt-assertion": "", **headers})


def test_env_delivery_end_to_end(env, caplog):
    caplog.set_level(logging.DEBUG)
    client, vault, sender = env
    setup_vault(client)
    card = add(client, CARD_FORM)
    vault.add_subscription("https://push.example/1", "k", "a", EMAIL)

    args = {"secret_id": card, "fields": ["number"], "purpose": "Pay for the train", "delivery": "env"}
    sub, _ = call_tool(client, "request_secret", args)
    page = client.get(f"{B}/r/{sub['id']}").text
    assert "pass these to a command" in page and CARD not in page
    assert post(client, f"{B}/r/{sub['id']}/decide", {"action": "share", "fields": ["number"]}).status_code == 303

    res, raw = call_tool(client, "get_result", {"request_id": sub["id"]})
    assert res["status"] == "done" and res["env"] == {"VAULT_NUMBER": "Card number"} and CARD not in raw

    r = redeem(client, res["handle"])
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json() == {"secret": "Visa", "env": {"VAULT_NUMBER": CARD}}
    again = redeem(client, res["handle"])
    assert again.status_code == 404 and CARD not in again.text
    assert CARD not in client.get(f"{B}/r/{sub['id']}").text
    assert CARD not in json.dumps([c["data"] for c in sender.calls])
    assert CARD not in caplog.text and res["handle"] not in caplog.text


def test_redeem_is_internal_only(env):
    client = env[0]
    setup_vault(client)
    secret = add(client, {**ADDRESS, "share_mode": "auto"})
    res, _ = call_tool(client, "request_secret", {"secret_id": secret, "purpose": "x", "delivery": "env"})
    handle = res["handle"]
    for r in (
        redeem(client, handle, url=f"{ORIGIN}/redeem"),
        redeem(client, handle, url="http://evil.example:8000/redeem"),
        redeem(client, handle, origin="http://secret-broker:8000"),
    ):
        assert r.status_code == 403 and STREET not in r.text
    get = client.get(f"{REDEEM}?handle={handle}", headers={"cf-access-jwt-assertion": ""})
    assert get.status_code != 200 and STREET not in get.text
    for body in ("not json", None):
        r = client.post(REDEEM, content=body or b"[]", headers={"content-type": "application/json"})
        assert r.status_code in (400, 404)
    # None of that used up the handle.
    assert redeem(client, handle).json()["env"] == {"VAULT_TEXT": ADDRESS_TEXT}
