from __future__ import annotations

import re

import pytest
from conftest import AGENT, EVENTS_JSON, NAVIGATE, ORIGIN, TOKEN, json_spec, make_jwt
from starlette.testclient import TestClient

from garden_monitors.app import create_app
from garden_monitors.store import Agent

B = "/monitors"
AUTH = {"authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def app(service, authenticator):
    return create_app(service, authenticator, TOKEN, run_loops=False)


@pytest.fixture
def client(app, signing_key):
    with TestClient(app, base_url=ORIGIN, headers=NAVIGATE, follow_redirects=False) as c:
        c.headers["cf-access-jwt-assertion"] = make_jwt(signing_key)
        yield c


@pytest.fixture
def internal(app):
    with TestClient(app, base_url="http://garden-monitors:8000", follow_redirects=False) as c:
        yield c


async def setup_monitor(service, web):
    web.add("https://agenda.example.org/api/events", EVENTS_JSON, "application/json")
    assert (await service.source_test(json_spec()))["passed"]
    service.source_put(json_spec())
    service.monitor_put(AGENT, "films", ["city-agenda"], schedule="0 9 * * 1-5")


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def test_the_page_needs_access(app):
    with TestClient(app, base_url=ORIGIN, headers=NAVIGATE) as c:
        r = c.get(f"{B}/")
        assert r.status_code == 403 and "films" not in r.text


@pytest.mark.anyio
async def test_the_page_lists_and_deletes(client, service, web, store):
    await setup_monitor(service, web)
    store.set_agents([Agent(AGENT, "lucius")])
    page = client.get(f"{B}/")
    assert page.status_code == 200
    assert "lucius" in page.text and "films" in page.text and "at 09:00, Monday to Friday" in page.text
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert "same-origin" in page.headers["cross-origin-opener-policy"]
    token = csrf_of(page.text)
    # A used source can't be deleted.
    r = client.post(f"{B}/s/city-agenda/delete", data={"csrf": token}, headers={"origin": ORIGIN})
    assert r.status_code == 400
    mid = store.monitors()[0].id
    r = client.post(f"{B}/m/{mid}/delete", data={"csrf": "wrong"}, headers={"origin": ORIGIN})
    assert r.status_code == 403 and store.monitors()
    r = client.post(f"{B}/m/{mid}/delete", data={"csrf": token}, headers={"origin": "https://evil.example.org"})
    assert r.status_code == 403 and store.monitors()
    r = client.post(f"{B}/m/{mid}/delete", data={"csrf": token}, headers={"origin": ORIGIN})
    assert r.status_code == 303 and not store.monitors()
    r = client.post(f"{B}/s/city-agenda/delete", data={"csrf": token}, headers={"origin": ORIGIN})
    assert r.status_code == 303 and store.source_spec("city-agenda") is None


@pytest.mark.anyio
async def test_the_page_warns_when_lettuce_is_not_picking_up(client, service, web):
    await setup_monitor(service, web)
    assert "isn&#39;t picking up reports" in client.get(f"{B}/").text


def test_the_executor_api_needs_the_token_and_no_browser(internal):
    assert internal.get("/executor/v1/requests").status_code == 401
    assert internal.get("/executor/v1/requests", headers={**AUTH, "origin": ORIGIN}).status_code == 403
    assert internal.get("/executor/v1/requests", headers={**AUTH, "cf-ray": "x"}).status_code == 403
    r = internal.get("/executor/v1/requests", headers=AUTH)
    assert r.status_code == 200 and r.json() == {"v": 1, "requests": [], "agents_wanted": True}


@pytest.mark.anyio
async def test_the_executor_claims_reports_and_posts_results(internal, service, web, store, clock):
    await setup_monitor(service, web)
    await service.fetch_source("city-agenda")
    clock.t += 60
    from datetime import datetime

    [rid] = service.fire(datetime.fromisoformat("2027-01-15T09:00+00:00"))
    claimed = internal.get("/executor/v1/requests", headers=AUTH).json()["requests"]
    assert [r["id"] for r in claimed] == [rid]
    assert internal.get("/executor/v1/requests", headers=AUTH).json()["requests"] == []  # once only
    r = internal.post("/executor/v1/results", headers=AUTH, json={"v": 1, "id": rid, "ok": True, "task_id": "t-1"})
    assert r.status_code == 204
    assert store.report(rid).status == "ok"
    r = internal.post("/executor/v1/heartbeat", headers=AUTH, json={"v": 1, "letta_ok": True})
    assert r.status_code == 204 and store.heartbeat().letta_ok


def test_mcp_refuses_browsers_and_the_tunnel_host(service, authenticator):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    with TestClient(
        create_app(service, authenticator, TOKEN, run_loops=False), base_url="http://garden-monitors:8000"
    ) as c:
        ok = c.post("/mcp", json=body, headers=headers)
        assert ok.status_code == 200
        names = {t["name"] for t in ok.json()["result"]["tools"]}
        assert {"source_test", "source_put", "monitor_put", "monitor_preview", "digest_more"} <= names
        assert c.post("/mcp", json=body, headers={**headers, "origin": ORIGIN}).status_code in (400, 403)
    with TestClient(create_app(service, authenticator, TOKEN, run_loops=False), base_url=ORIGIN) as c:
        assert c.post("/mcp", json=body, headers=headers).status_code in (400, 421)


def test_healthz(internal):
    assert internal.get("/healthz").text == "ok"
