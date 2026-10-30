import json

import pytest
from garden_common.executor import AGENTS_FRESH_SECONDS

from garden_landing.config import ConfigError, Settings

from .conftest import API, AUTH, LETTUCE, ORIGIN, TOKEN, make_jwt, raw_get

AGENT_1 = "agent-11111111-2222-3333-4444-555555555555"
AGENT_2 = "agent-66666666-7777-8888-9999-000000000000"


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
def test_the_token_is_required(garden, headers):
    r = garden.api.get(f"{API}/status", headers=headers)
    assert r.status_code == 401 and r.json() == {"error": "unauthorized"}
    r = garden.api.put(f"{API}/agents", json={"v": 1, "agents": [{"id": AGENT_1, "name": "x"}]}, headers=headers)
    assert r.status_code == 401
    assert garden.agents.agents() is None


@pytest.mark.parametrize(
    "extra",
    [
        {"origin": ORIGIN},
        {"origin": "null"},
        {"cf-access-jwt-assertion": "x"},
        {"cf-connecting-ip": "192.0.2.1"},
        {"cf-ray": "1-AMS"},
    ],
)
def test_browser_and_tunnel_requests_are_refused(garden, extra):
    assert garden.api.get(f"{API}/status", headers={**AUTH, **extra}).status_code == 403
    assert garden.put_agents([{"id": AGENT_1, "name": "x"}], **extra).status_code == 403
    assert garden.agents.agents() is None


def test_the_api_answers_only_at_its_own_path(garden, signing_key):
    jwt_headers = {**AUTH, "cf-access-jwt-assertion": make_jwt(signing_key)}
    for path in [
        "/%65xecutor/v1/status",
        "/executor%2Fv1/status",
        f"/x/..{API}/status",
        f"/%2e%2e{API}/status",
    ]:
        for headers in (AUTH, jwt_headers):
            status = raw_get(garden.app, path, headers)
            assert status in (400, 404), (path, status)
    assert raw_get(garden.app, f"{API}/status", AUTH) == 200


def test_pages_do_not_take_the_token(garden):
    assert garden.api.get("/", headers=AUTH).status_code == 403


def test_status_does_not_change_anything(garden):
    for method in ("POST", "PUT", "DELETE"):
        assert garden.api.request(method, f"{API}/status", headers=AUTH).status_code == 405
    r = garden.api.get(f"{API}/status", headers=AUTH)
    assert r.headers["cache-control"] == "no-store"
    assert r.json() == {"v": 1, "agents_wanted": True}


# -- when a list is wanted ----------------------------------------------------------


def test_a_list_is_wanted_until_one_arrives_then_again_once_stale(garden, clock):
    assert garden.wanted()
    assert garden.put_agents([{"id": AGENT_1, "name": "lucius"}]).status_code == 204
    assert not garden.wanted()
    clock.t += AGENTS_FRESH_SECONDS
    assert not garden.wanted()
    clock.t += 1
    assert garden.wanted()


def test_without_the_editor_no_list_is_ever_wanted(garden_for, clock):
    garden = garden_for("garden-vault,garden-gmail-triggers")
    assert not garden.wanted()
    clock.t += 10 * AGENTS_FRESH_SECONDS
    assert not garden.wanted()
    # The executor may still send one (Gmail triggers asked for it); it is kept, unused.
    assert garden.put_agents([{"id": AGENT_1, "name": "lucius"}]).status_code == 204


# -- the list ---------------------------------------------------------------------


def test_the_list_is_cleaned_and_replaces_the_last_one(garden):
    assert garden.put_agents([{"id": AGENT_2, "name": "old"}]).status_code == 204
    agents = [
        {"id": AGENT_1, "name": "  lucius\n\tthe\x1b[31m  second  "},
        {"id": AGENT_1, "name": "duplicate id"},
        {"id": AGENT_2, "name": "n" * 201},
        {"id": "agent-x;rm", "name": "bad id"},
        {"id": "--help", "name": "bad id"},
        {"id": 5, "name": "number"},
        "text",
        None,
        {"id": AGENT_2, "name": "hal", "system": "ignored"},
    ]
    assert garden.put_agents(agents).status_code == 204
    assert garden.agents.agents() == [(AGENT_2, "hal"), (AGENT_1, "lucius the [31m second")]


def test_agents_without_a_usable_name_are_listed_by_id(garden):
    agents = [{"id": "agent-1", "name": ""}, {"id": "agent-2", "name": "\x00\x01 "}, {"id": "agent-3"}]
    assert garden.put_agents(agents).status_code == 204
    assert garden.agents.agents() == [("agent-1", "agent-1"), ("agent-2", "agent-2"), ("agent-3", "agent-3")]


def test_the_list_is_capped(garden):
    agents = [{"id": f"agent-{i}", "name": f"a{i}"} for i in range(1200)]
    assert garden.put_agents(agents).status_code == 204
    assert len(garden.agents.agents()) == 1000


@pytest.mark.parametrize(
    "body, content_type, status",
    [
        ({"v": 1, "agents": {}}, "application/json", 400),
        ({"v": 2, "agents": []}, "application/json", 400),
        ({"agents": []}, "application/json", 400),
        ([], "application/json", 400),
        ({"v": 1, "agents": []}, "text/plain", 400),
        ("{not json", "application/json", 400),
        ({"v": 1, "agents": [], "pad": "p" * (1024 * 1024)}, "application/json", 413),
    ],
)
def test_bad_bodies_are_refused_and_keep_the_last_list(garden, body, content_type, status):
    assert garden.put_agents([{"id": AGENT_1, "name": "lucius"}]).status_code == 204
    raw = body if isinstance(body, str) else json.dumps(body)
    r = garden.api.put(f"{API}/agents", content=raw, headers={**AUTH, "content-type": content_type})
    assert r.status_code == status
    assert garden.agents.agents() == [(AGENT_1, "lucius")]


# -- settings ---------------------------------------------------------------------

BASE = {
    "CF_ACCESS_TEAM_DOMAIN": "team1",
    "CF_ACCESS_AUD": "aud1",
    "ALLOWED_USERS": "Alfred@Gmail.com, hal@example.com",
    "PUBLIC_ORIGIN": f"{ORIGIN}/",
    "LETTUCE_ORIGIN": LETTUCE,
    "GARDEN_PROFILES": "garden-vault, garden-gmail-triggers,../x,,Bad!,garden-code-server",
    "GARDEN_EXECUTOR_TOKEN": TOKEN,
}


def test_settings_from_the_environment():
    s = Settings.from_env(BASE)
    assert s.allowed_users == {"alfred@gmail.com", "hal@example.com"}
    assert s.public_origin == ORIGIN and s.lettuce_origin == LETTUCE
    assert s.issuer == "https://team1.cloudflareaccess.com"
    assert s.garden_profiles == ("garden-vault", "garden-gmail-triggers", "garden-code-server")
    assert s.executor_token == TOKEN and TOKEN not in repr(s)
    assert Settings.from_env({**BASE, "GARDEN_PROFILES": ""}).garden_profiles == ()


@pytest.mark.parametrize(
    "broken",
    [
        {"CF_ACCESS_TEAM_DOMAIN": ""},
        {"CF_ACCESS_AUD": ""},
        {"ALLOWED_USERS": ""},
        {"PUBLIC_ORIGIN": ""},
        {"PUBLIC_ORIGIN": "http://alfred-garden.example.com"},
        {"PUBLIC_ORIGIN": f"{ORIGIN}/x"},
        {"LETTUCE_ORIGIN": ""},
        {"LETTUCE_ORIGIN": "http://alfred.example.com"},
        {"LETTUCE_ORIGIN": "https://alfred.example.com/x"},
        {"LETTUCE_ORIGIN": "https://"},
        {"LETTUCE_ORIGIN": "https://alfred.example.com?x"},
        {"GARDEN_EXECUTOR_TOKEN": ""},
        {"GARDEN_EXECUTOR_TOKEN": TOKEN.upper()},
        {"GARDEN_EXECUTOR_TOKEN": TOKEN + "0"},
        {"GARDEN_EXECUTOR_TOKEN": "g" * 64},
    ],
)
def test_settings_refuse_what_is_missing_or_malformed(broken):
    with pytest.raises(ConfigError):
        Settings.from_env({**BASE, **broken})
