import json

import pytest
from garden_common.executor import AGENTS_FRESH_SECONDS
from starlette.testclient import TestClient

from gmail_triggers.app import create_app
from gmail_triggers.config import LOST_AFTER_SECONDS, MAX_CLAIM, PREFIX, ConfigError, Settings
from gmail_triggers.store import Rule
from gmail_triggers.triggers import send_test

from .conftest import AGENT, NAVIGATE, ORIGIN, OTHER_AGENT, TOKEN, make_jwt, raw_get

API = "/executor/v1"
AUTH = {"authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def api(env, authenticator):
    app = create_app(env.store, env.google, authenticator, TOKEN)
    with TestClient(app, base_url="http://garden-gmail-triggers:8000") as c:
        yield c


def rule(env) -> Rule:
    r = Rule(name="Bills", label_ids=["INBOX"], agent_id=AGENT, agent_name="lucius", prompt="p")
    r.id = env.store.save_rule(r)
    return r


def claim(api) -> list[dict]:
    r = api.get(f"{API}/requests", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["v"] == 1
    return body["requests"]


def result(api, rid, **fields):
    return api.post(f"{API}/results", json={"v": 1, "id": rid, **fields}, headers=AUTH)


# -- who may call ---------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": TOKEN},
        {"authorization": "Bearer " + "0" * 64},
        {"authorization": f"Bearer {TOKEN}x"},
        {"authorization": f"bearer {TOKEN}"},
        {"authorization": "Basic " + TOKEN},
    ],
)
def test_the_token_is_required(api, env, headers):
    send_test(env.store, rule(env), env.store.now())
    for method, path, body in [
        ("GET", "/requests", None),
        ("POST", "/results", {"v": 1, "id": "0" * 32, "ok": True}),
        ("PUT", "/agents", {"v": 1, "agents": []}),
        ("POST", "/heartbeat", {"v": 1, "letta_ok": True}),
    ]:
        r = api.request(method, API + path, json=body, headers=headers)
        assert r.status_code == 401, path
        assert r.json() == {"error": "unauthorized"}
    assert len(env.requests()) == 1 and env.store.heartbeat() is None and env.store.agents() is None


@pytest.mark.parametrize(
    "extra",
    [{"origin": ORIGIN}, {"origin": "null"}, {"cf-access-jwt-assertion": "x"}, {"cf-connecting-ip": "192.0.2.1"}],
)
def test_browser_and_tunnel_requests_are_refused(api, env, extra):
    send_test(env.store, rule(env), env.store.now())
    assert api.get(f"{API}/requests", headers={**AUTH, **extra}).status_code == 403
    assert len(env.requests()) == 1


def test_the_api_is_not_reachable_under_the_page_prefix(env, authenticator, signing_key):
    send_test(env.store, rule(env), env.store.now())
    app = create_app(env.store, env.google, authenticator, TOKEN)
    jwt_headers = {**AUTH, "cf-access-jwt-assertion": make_jwt(signing_key)}
    for path in [
        f"{PREFIX}/executor/v1/requests",
        f"{PREFIX}{API}/requests",
        f"{PREFIX}/../executor/v1/requests",
        f"{PREFIX}/%2e%2e/executor/v1/requests",
        f"{PREFIX}/..%2fexecutor/v1/requests",
    ]:
        for headers in (AUTH, jwt_headers):
            status = raw_get(app, path, headers)
            assert status in (400, 404), (path, status)
    assert len(env.requests()) == 1


def test_healthz_and_pages_do_not_take_the_token(api):
    assert api.get(f"{PREFIX}/", headers=AUTH).status_code == 403


def test_settings_refuse_a_missing_or_malformed_token():
    base = {
        "CF_ACCESS_TEAM_DOMAIN": "team1",
        "CF_ACCESS_AUD": "aud1",
        "ALLOWED_USERS": "alfred@gmail.com",
        "PUBLIC_ORIGIN": "https://alfred-garden.example.com",
    }
    for token in ("", "abc", TOKEN.upper(), TOKEN + "0", "g" * 64):
        with pytest.raises(ConfigError):
            Settings.from_env({**base, "GARDEN_EXECUTOR_TOKEN": token})
    settings = Settings.from_env({**base, "GARDEN_EXECUTOR_TOKEN": TOKEN})
    assert settings.executor_token == TOKEN and TOKEN not in repr(settings)
    assert not hasattr(settings, "garden_profiles")


# -- requests and results ---------------------------------------------------------


def test_requests_are_claimed_at_most_once_oldest_first(api, env):
    r = rule(env)
    ids = []
    for _ in range(MAX_CLAIM + 3):
        ids.append(send_test(env.store, r, env.store.now()))
        env.clock.t += 1
    first = claim(api)
    assert [q["id"] for q in first] == ids[:MAX_CLAIM]
    assert set(first[0]) == {"id", "agent_id", "conversation", "name", "description", "prompt", "created_at"}
    assert [q["id"] for q in claim(api)] == ids[MAX_CLAIM:]
    assert claim(api) == []
    assert api.head(f"{API}/requests", headers=AUTH).status_code == 405


def test_results_are_recorded_once_for_claimed_requests(api, env):
    r = rule(env)
    rid = send_test(env.store, r, env.store.now())
    other = send_test(env.store, r, env.store.now())
    # Not claimed yet: refused.
    assert result(api, rid, ok=True, task_id="task-1").status_code == 404
    claim(api)
    assert result(api, rid, ok=True, task_id="task-1").status_code == 204
    assert result(api, rid, ok=False, error="spoofed").status_code == 404
    assert result(api, other, ok=False, error="letta:\nunknown agent").status_code == 204
    assert result(api, "f" * 32, ok=True).status_code == 404
    runs = {x.id: x for x in env.store.runs(r.id)}
    assert runs[rid].status == "ok" and runs[rid].task_id == "task-1"
    assert runs[other].status == "failed" and runs[other].error == "letta: unknown agent"


@pytest.mark.parametrize(
    "body",
    [
        {"v": 2, "ok": True},
        {"ok": "yes"},
        {"ok": True, "task_id": "x" * 201},
        {"ok": True, "task_id": "a\nb"},
        {"ok": True, "task_id": 5},
        {"ok": False, "error": "x" * 501},
        {"ok": False, "error": ["x"]},
    ],
)
def test_bad_results_are_refused(api, env, body):
    rid = send_test(env.store, rule(env), env.store.now())
    claim(api)
    r = api.post(f"{API}/results", json={"v": 1, "id": rid, **body}, headers=AUTH)
    assert r.status_code == 400
    assert env.store.run(rid).status == "claimed"


def test_body_rules(api, env):
    rid = send_test(env.store, rule(env), env.store.now())
    claim(api)
    url = f"{API}/results"
    assert api.post(url, content=b"{not json", headers={**AUTH, "content-type": "application/json"}).status_code == 400
    assert api.post(url, content=b'{"v":1}', headers={**AUTH, "content-type": "text/plain"}).status_code == 400
    big = json.dumps({"v": 1, "id": rid, "ok": False, "error": "x", "pad": "x" * 20000}).encode()
    assert api.post(url, content=big, headers={**AUTH, "content-type": "application/json"}).status_code == 413

    def chunks():
        yield b'{"v": 1, "pad": "'
        for _ in range(40):
            yield b"x" * 1024
        yield b'"}'

    # No Content-Length: the cap holds while reading.
    r = api.post(url, content=chunks(), headers={**AUTH, "content-type": "application/json"})
    assert r.status_code == 413
    assert env.store.run(rid).status == "claimed"


def test_a_claimed_request_without_result_is_lost(api, env):
    rid = send_test(env.store, rule(env), env.store.now())
    claim(api)
    env.store.expire_claims(LOST_AFTER_SECONDS)
    assert env.store.run(rid).status == "claimed"
    env.clock.t += LOST_AFTER_SECONDS + 1
    env.store.expire_claims(LOST_AFTER_SECONDS)
    assert env.store.run(rid).status == "lost"


def test_a_late_result_replaces_lost(api, env):
    # A claim of 20 requests can take longer than LOST_AFTER_SECONDS when letta is
    # slow; the task did run, so its result must not be refused.
    rid = send_test(env.store, rule(env), env.store.now())
    claim(api)
    env.clock.t += LOST_AFTER_SECONDS + 1
    env.store.expire_claims(LOST_AFTER_SECONDS)
    assert result(api, rid, ok=True, task_id="late").status_code == 204
    run = env.store.run(rid)
    assert run.status == "ok" and run.task_id == "late"
    assert result(api, rid, ok=True, task_id="again").status_code == 404


# -- agents and heartbeat ---------------------------------------------------------


def test_agents_are_validated_and_replaced(api, env):
    agents = [
        {"id": "agent-b", "name": "zed"},
        {"id": "agent-a", "name": "lucius\n<b>x</b>"},
        {"id": "../agent", "name": "bad"},
        {"id": "agent-a", "name": "duplicate"},
        "junk",
        {"id": "agent-c"},
        {"id": "agent-d", "name": "x" * 201},
        {"id": "agent-e", "name": "\ud800"},
    ]
    # By hand: a lone surrogate escape is valid JSON but can't go through json=.
    body = json.dumps({"v": 1, "agents": agents}).encode()
    r = api.put(f"{API}/agents", content=body, headers={**AUTH, "content-type": "application/json"})
    assert r.status_code == 204
    # A nameless agent is listed by its id.
    assert [(a.id, a.name) for a in env.store.agents()] == [
        ("agent-c", "agent-c"),
        ("agent-a", "lucius <b>x</b>"),
        ("agent-b", "zed"),
        ("agent-e", "\ufffd"),
    ]
    api.put(f"{API}/agents", json={"v": 1, "agents": [{"id": AGENT, "name": "lucius"}]}, headers=AUTH)
    assert [a.id for a in env.store.agents()] == [AGENT]
    many = [{"id": f"agent-{i}", "name": f"n{i:04d}"} for i in range(1200)]
    assert api.put(f"{API}/agents", json={"v": 1, "agents": many}, headers=AUTH).status_code == 204
    assert len(env.store.agents()) == 1000
    assert api.put(f"{API}/agents", json={"v": 1, "agents": {}}, headers=AUTH).status_code == 400
    assert len(env.store.agents()) == 1000


def test_heartbeat(api, env):
    assert env.store.heartbeat() is None
    r = api.post(f"{API}/heartbeat", json={"v": 1, "letta_ok": False, "error": "x\ny"}, headers=AUTH)
    assert r.status_code == 204
    beat = env.store.heartbeat()
    assert not beat.letta_ok and beat.error == "x y" and beat.at == env.store.now()
    assert api.post(f"{API}/heartbeat", json={"v": 1}, headers=AUTH).status_code == 400
    assert api.post(f"{API}/heartbeat", json={"v": 1, "letta_ok": True}, headers=AUTH).status_code == 204
    assert env.store.heartbeat().letta_ok


def test_the_executor_exactly_as_it_calls(api, env):
    """Headers and bodies as Node 22's fetch sends them from run.mjs: no Origin,
    but sec-fetch-mode, which must not be refused."""
    r_ = rule(env)
    ok_id = send_test(env.store, r_, env.store.now())
    bad_id = send_test(env.store, r_, env.store.now())
    node = {
        **AUTH,
        "content-type": "application/json",
        "accept": "*/*",
        "accept-language": "*",
        "sec-fetch-mode": "cors",
        "user-agent": "node",
        "accept-encoding": "gzip, deflate",
    }

    def send(method, path, body=None):
        content = json.dumps(body).encode() if body is not None else None
        return api.request(method, API + path, content=content, headers=node)

    got = send("GET", "/requests")
    assert got.status_code == 200 and [q["id"] for q in got.json()["requests"]] == [ok_id, bad_id]
    assert send("POST", "/results", {"v": 1, "id": ok_id, "ok": True, "task_id": "task-1"}).status_code == 204
    assert send("POST", "/results", {"v": 1, "id": bad_id, "ok": False, "error": "letta failed"}).status_code == 204
    agents = {"v": 1, "agents": [{"id": AGENT, "name": "lucius"}, {"id": OTHER_AGENT, "name": "hal"}]}
    assert send("PUT", "/agents", agents).status_code == 204
    assert send("POST", "/heartbeat", {"v": 1, "letta_ok": True}).status_code == 204
    assert send("POST", "/heartbeat", {"v": 1, "letta_ok": False, "error": "agents list failed"}).status_code == 204
    runs = {x.id: x.status for x in env.store.runs(r_.id)}
    assert runs == {ok_id: "ok", bad_id: "failed"}
    assert [a.name for a in env.store.agents()] == ["hal", "lucius"]
    assert env.store.heartbeat().error == "agents list failed"


# -- agents_wanted -----------------------------------------------------------------


def wanted(api) -> bool:
    r = api.get(f"{API}/requests", headers=AUTH)
    assert r.status_code == 200
    return r.json()["agents_wanted"]


def put_agents(api):
    body = {"v": 1, "agents": [{"id": AGENT, "name": "lucius"}]}
    assert api.put(f"{API}/agents", json=body, headers=AUTH).status_code == 204


def test_agents_are_wanted_until_a_list_arrives_and_again_when_it_is_old(api, env):
    assert wanted(api) is True
    put_agents(api)
    assert wanted(api) is False
    env.clock.t += AGENTS_FRESH_SECONDS
    assert wanted(api) is False
    env.clock.t += 1
    assert wanted(api) is True
    put_agents(api)
    assert wanted(api) is False


@pytest.mark.parametrize("form", ["new", "edit"])
def test_agents_are_wanted_after_the_form_is_shown(api, env, signing_key, form):
    r = rule(env)
    put_agents(api)
    assert wanted(api) is False
    path = f"{PREFIX}/new" if form == "new" else f"{PREFIX}/r/{r.id}"
    page = api.get(path, headers={**NAVIGATE, "cf-access-jwt-assertion": make_jwt(signing_key)})
    assert page.status_code == 200
    assert wanted(api) is True
    # Asking doesn't clear it; only a new list does.
    assert wanted(api) is True
    put_agents(api)
    assert wanted(api) is False


def test_a_form_shown_while_the_executor_lists_asks_again(api, env, signing_key):
    def form():
        headers = {**NAVIGATE, "cf-access-jwt-assertion": make_jwt(signing_key)}
        assert api.get(f"{PREFIX}/new", headers=headers).status_code == 200

    put_agents(api)
    form()
    assert wanted(api) is True
    # Shown after the executor read the status, so the list it sends may predate the agent.
    form()
    put_agents(api)
    assert wanted(api) is True
    put_agents(api)
    assert wanted(api) is False


def test_other_pages_do_not_ask_for_agents(api, env, signing_key):
    r = rule(env)
    put_agents(api)
    for path in ("/", f"/r/{r.id}/history"):
        assert (
            api.get(PREFIX + path, headers={**NAVIGATE, "cf-access-jwt-assertion": make_jwt(signing_key)}).status_code
            == 200
        )
    assert wanted(api) is False


def test_a_refused_form_view_does_not_ask_for_agents(api, env):
    put_agents(api)
    assert api.get(f"{PREFIX}/new").status_code == 403
    assert wanted(api) is False
