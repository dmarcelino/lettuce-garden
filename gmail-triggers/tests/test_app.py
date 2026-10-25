import json
import logging
import re
import time

import pytest
from starlette.testclient import TestClient

from gmail_triggers.app import EXAMPLES, create_app, gap_text
from gmail_triggers.config import MAX_LISTED_MESSAGES, PREFIX
from gmail_triggers.store import GAP_DROPPED, Held, Rule

from .conftest import AGENT, BODY, ORIGIN, OTHER_AGENT, TOKEN, make_jwt, write_creds, write_policy

B = PREFIX


@pytest.fixture
def client(env, authenticator, signing_key):
    app = create_app(env.store, env.google, authenticator, TOKEN)
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as c:
        c.headers["cf-access-jwt-assertion"] = make_jwt(signing_key)
        yield c


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([0-9a-f]+)"', html).group(1)


def csrf(client) -> str:
    return csrf_of(client.get(f"{B}/new").text)


def post(client, path, data=None, token=None, origin=ORIGIN):
    form = dict(data or {})
    if token is not False:
        form["csrf"] = token if token is not None else csrf(client)
    return client.post(path, data=form, headers={"origin": origin} if origin else {})


def form(**over):
    data = {
        "name": "Bills",
        "labels": ["INBOX", "Label_1"],
        "senders": "Hal@Example.com\nbilling@example.org",
        "verified": "on",
        "agent_id": AGENT,
        "conversation": "new",
        "prompt": "Note the amount.",
        "max_per_hour": "4",
    }
    data.update(over)
    return data


def saved_rule(env, **over) -> Rule:
    fields = dict(
        name="Bills",
        label_ids=["INBOX"],
        label_names={"INBOX": "Inbox"},
        agent_id=AGENT,
        agent_name="lucius",
        prompt="p",
    )
    fields.update(over)
    rule = Rule(**fields)
    rule.id = env.store.save_rule(rule)
    return rule


# -- authentication -----------------------------------------------------------


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
def test_every_route_requires_a_valid_access_jwt(client, env, signing_key, other_key, token):
    rule = saved_rule(env)
    if token is None:
        value = ""
    elif token == "other-key":
        value = make_jwt(other_key)
    elif isinstance(token, dict):
        value = make_jwt(signing_key, **token)
    else:
        value = token
    headers = {"cf-access-jwt-assertion": value, "origin": ORIGIN}
    gets = ["/", "/new", f"/r/{rule.id}", f"/r/{rule.id}/history", "/garden.css", "/app.css", "/icon-192.png"]
    for path in gets:
        r = client.get(B + path, headers=headers)
        assert r.status_code == 403, path
        assert "Bills" not in r.text
    for path in ["/rules", f"/r/{rule.id}", f"/r/{rule.id}/toggle", f"/r/{rule.id}/delete", f"/r/{rule.id}/test"]:
        assert client.post(B + path, data={"csrf": "x"}, headers=headers).status_code == 403, path
    assert env.store.rule(rule.id) is not None and env.requests() == []


def test_posts_need_csrf_and_this_origin(client, env):
    rule = saved_rule(env)
    path = f"{B}/r/{rule.id}/delete"
    assert post(client, path, token=False).status_code == 403
    assert post(client, path, token="0" * 64).status_code == 403
    assert post(client, path, origin="https://evil.example.org").status_code == 403
    assert post(client, path, origin=None).status_code == 403
    assert env.store.rule(rule.id) is not None
    assert post(client, path).status_code == 303
    assert env.store.rule(rule.id) is None


def test_healthz_is_open_and_root_redirects(client):
    assert client.get("/healthz", headers={"cf-access-jwt-assertion": ""}).text == "ok"
    r = client.get(B)
    assert r.status_code == 308 and r.headers["location"] == f"{B}/"


def test_pages_carry_the_security_headers_and_no_script(client, env):
    env.write_agents()
    rule = saved_rule(env)
    for path in ["/", "/new", f"/r/{rule.id}", f"/r/{rule.id}/history"]:
        r = client.get(B + path)
        assert r.status_code == 200, path
        assert "script-src 'none'" in r.headers["content-security-policy"]
        assert r.headers["x-frame-options"] == "DENY"
        assert "<script" not in r.text and "style=" not in r.text
    css = client.get(f"{B}/garden.css")
    assert css.status_code == 200 and "--accent: #0b1020" in css.text
    icon = client.get(f"{B}/icon-512.png")
    assert icon.headers["content-type"] == "image/png" and icon.content.startswith(b"\x89PNG")


# -- rules --------------------------------------------------------------------


def test_create_edit_toggle_delete(client, env):
    env.write_agents()
    r = post(client, f"{B}/rules", form())
    assert r.status_code == 303, r.text
    assert r.headers["location"] == f"{B}/?done=saved"
    [rule] = env.store.rules()
    assert rule.label_ids == ["INBOX", "Label_1"] and rule.label_names == {"INBOX": "Inbox", "Label_1": "Bills"}
    assert rule.senders == ["hal@example.com", "billing@example.org"]
    assert rule.verified_only and rule.agent_name == "lucius" and rule.max_per_hour == 4 and rule.enabled
    page = client.get(r.headers["location"]).text
    assert "Trigger saved." in page and "Inbox, Bills" in page and "asks lucius" in page

    r = post(
        client, f"{B}/r/{rule.id}", form(name="Bills 2", verified="", agent_id=OTHER_AGENT, conversation="default")
    )
    assert r.status_code == 303
    rule = env.store.rule(rule.id)
    assert rule.name == "Bills 2" and not rule.verified_only and rule.agent_id == OTHER_AGENT
    assert rule.conversation == "default" and rule.agent_name == "hal"

    assert post(client, f"{B}/r/{rule.id}/toggle", {"enabled": "0"}).status_code == 303
    assert not env.store.rule(rule.id).enabled
    assert post(client, f"{B}/r/{rule.id}/toggle", {"enabled": "1"}).status_code == 303
    assert env.store.rule(rule.id).enabled

    assert post(client, f"{B}/r/{rule.id}/delete").status_code == 303
    assert env.store.rules() == []


@pytest.mark.parametrize(
    "over, problem",
    [
        ({"agent_id": "agent-not-in-the-list"}, "Choose an agent."),
        ({"agent_id": "../../etc"}, "Choose an agent."),
        ({"labels": ["Label_99"]}, "Choose at least one label."),
        ({"labels": []}, "Choose at least one label."),
        ({"senders": "hal at example"}, "Not an email address"),
        ({"prompt": "  "}, "Say what the agent should do."),
        ({"max_per_hour": "0"}, "Runs per hour"),
        ({"max_per_hour": "lots"}, "Runs per hour"),
        ({"conversation": "conv-123"}, "Choose where the agent answers."),
        ({"name": ""}, "Give the trigger a name."),
    ],
)
def test_invalid_forms_are_refused_and_redisplayed(client, env, over, problem):
    env.write_agents()
    r = post(client, f"{B}/rules", form(**over))
    assert r.status_code == 400
    assert problem in r.text
    assert env.store.rules() == []


def test_no_agent_list_means_no_new_rule(client, env):
    r = post(client, f"{B}/rules", form())
    assert r.status_code == 400 and "hasn't sent its list of agents" in r.text


def test_edit_keeps_its_agent_and_labels_while_lettuce_and_gmail_are_away(client, env, settings):
    rule = saved_rule(env, label_ids=["Label_1"], label_names={"Label_1": "Bills"})
    write_policy(settings.policy_dir, enabled=False)
    page = client.get(f"{B}/r/{rule.id}").text
    assert "can't be loaded right now" in page and "Bills" in page
    r = post(client, f"{B}/r/{rule.id}", form(labels=["Label_1"]))
    assert r.status_code == 303, r.text
    assert env.store.rule(rule.id).label_ids == ["Label_1"]


def test_agent_names_are_escaped(client, env):
    env.write_agents([(AGENT, "<img src=x onerror=alert(1)>")])
    page = client.get(f"{B}/new").text
    assert "<img src=x" not in page and "&lt;img src=x" in page


def test_example_links_fill_the_prompt(client, env):
    env.write_agents()
    page = client.get(f"{B}/new?example=2").text
    assert EXAMPLES[1][1] in page
    rule = saved_rule(env)
    page = client.get(f"{B}/r/{rule.id}?example=3").text
    assert EXAMPLES[2][1].replace("'", "&#39;") in page
    assert f'href="{B}/r/{rule.id}?example=1#prompt"' in page
    assert client.get(f"{B}/new?example=9").status_code == 200


def test_delete_confirmation_is_a_bottom_sheet(client, env):
    rule = saved_rule(env)
    page = client.get(f"{B}/r/{rule.id}").text
    assert 'href="#delete"' in page and 'id="delete" class="sheet"' in page.replace(
        'class="sheet" id="delete"', 'id="delete" class="sheet"'
    )
    assert f'action="{B}/r/{rule.id}/delete"' in page


def test_unknown_rule(client):
    assert client.get(f"{B}/r/0123456789abcdef").status_code == 400
    assert client.get(f"{B}/r/..%2f..").status_code in (400, 404)


# -- history and test runs ----------------------------------------------------


def test_send_a_test_and_see_it_in_history(client, env):
    rule = saved_rule(env)
    r = post(client, f"{B}/r/{rule.id}/test")
    assert r.status_code == 303 and r.headers["location"] == f"{B}/r/{rule.id}/history?done=test"
    [req] = env.requests()
    assert "(test run, no email)" in req["prompt"]
    page = client.get(r.headers["location"]).text
    assert "Test request sent" in page and "Waiting for Lettuce" in page
    auth = {"authorization": f"Bearer {TOKEN}"}
    with TestClient(client.app, base_url="http://gmail-triggers:8000") as api:
        claimed = api.get("/executor/v1/requests", headers=auth).json()["requests"]
        assert [c["id"] for c in claimed] == [req["id"]]
        assert "Sent to Lettuce" in client.get(f"{B}/r/{rule.id}/history").text
        result = {"v": 1, "id": req["id"], "ok": False, "error": "letta: <b>agent not found</b>"}
        assert api.post("/executor/v1/results", json=result, headers=auth).status_code == 204
    page = client.get(f"{B}/r/{rule.id}/history").text
    assert "&lt;b&gt;agent not found" in page
    assert "1 request failed in the last day" in client.get(f"{B}/").text


# -- banners ------------------------------------------------------------------


def test_missing_or_stale_heartbeat_shows_the_banner(client, env):
    assert "Lettuce isn&#39;t picking up requests" in client.get(f"{B}/").text
    env.write_heartbeat(age=30)
    assert "picking up requests" not in client.get(f"{B}/").text
    env.write_heartbeat(age=181)
    assert "Lettuce last checked for requests" in client.get(f"{B}/").text
    env.write_heartbeat(age=10, letta_ok=False, error="letta: not logged in")
    page = client.get(f"{B}/").text
    assert "Lettuce reported a problem" in page and "letta: not logged in" in page


def test_google_banners(client, env, settings):
    env.write_heartbeat()
    assert "banner bad" not in client.get(f"{B}/").text
    write_policy(settings.policy_dir, enabled=False)
    assert "Google is switched off" in client.get(f"{B}/").text
    write_policy(settings.policy_dir, permissions=("calendar:readonly",))
    assert "Gmail isn&#39;t allowed" in client.get(f"{B}/").text


@pytest.mark.anyio
async def test_reconnect_banner(env, authenticator, signing_key):
    env.fake.token_error = "invalid_grant"
    await env.poller.run_once()
    app = create_app(env.store, env.google, authenticator, TOKEN)
    with TestClient(app, base_url=ORIGIN) as c:
        page = c.get(f"{B}/", headers={"cf-access-jwt-assertion": make_jwt(signing_key)}).text
    assert "Google needs reconnecting" in page
    write_creds(env.settings.creds_dir, refresh_token="refresh-token-2")
    with TestClient(app, base_url=ORIGIN) as c:
        page = c.get(f"{B}/", headers={"cf-access-jwt-assertion": make_jwt(signing_key)}).text
    assert "Google needs reconnecting" not in page


@pytest.mark.anyio
async def test_gap_banner_and_body_never_on_pages(env, authenticator, signing_key, caplog):
    caplog.set_level(logging.DEBUG)
    rule = saved_rule(env, verified_only=False)
    await env.poller.run_once()
    env.fake.add(subject="Your invoice")
    await env.poller.run_once()
    env.fake.expired = True
    env.clock.t += 3600
    await env.poller.run_once()
    app = create_app(env.store, env.google, authenticator, TOKEN)
    with TestClient(app, base_url=ORIGIN) as c:
        c.headers["cf-access-jwt-assertion"] = make_jwt(signing_key)
        pages = [c.get(f"{B}{p}").text for p in ("/", f"/r/{rule.id}", f"/r/{rule.id}/history")]
    assert "Some mail wasn&#39;t checked" in pages[0]
    assert "Your invoice" in pages[2] and "hal@example.com" in pages[2]
    for text in pages + [caplog.text, env.db_text(), json.dumps(env.requests())]:
        assert BODY not in text


def test_dropped_matches_get_their_own_notice():
    title, text = gap_text({"start": 0, "end": 0, "kind": GAP_DROPPED, "skipped": 2})
    assert title == "Some matched mail wasn't sent"
    assert text.startswith("2 matched emails around ") and "more than 500 were waiting" in text


def test_waiting_matches_are_not_blamed_on_the_hourly_limit(client, env):
    rule = saved_rule(env, max_per_hour=6)
    env.store.record_poll(handled=[], history_id="1", held={rule.id: [Held("18c0000000000001", "", "")]}, sent=[])
    index = client.get(f"{B}/").text
    history = client.get(f"{B}/r/{rule.id}/history").text
    assert "1 waiting" in index
    assert f"1 matched email waits. Up to {MAX_LISTED_MESSAGES} go in each run, within" in history
    assert "6 per hour" in history
    for page in (index, history):
        assert "for the hourly limit" not in page and "sent together" not in page


def test_verified_sender_help_mentions_unchecked_workspace_mail(client, env):
    env.write_agents()
    page = client.get(f"{B}/new").text
    assert "same Google Workspace" in page and "turn this off" in page
