import logging
import re

import pytest

from .conftest import EMAIL, LETTUCE, SCRIPT_FETCH, make_jwt

AGENT_1 = "agent-11111111-2222-3333-4444-555555555555"
AGENT_2 = "agent-66666666-7777-8888-9999-000000000000"
AGENT_3 = "agent-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
EDITOR_ROWS = re.compile(r'href="/code-server/\?folder=([^"]*)"')


def csp(r) -> dict[str, str]:
    return dict(d.strip().split(" ", 1) for d in r.headers["content-security-policy"].split(";"))


# -- who may open it --------------------------------------------------------------


@pytest.mark.parametrize("token", ["", "garbage", "other-key", {"email": "hal@example.com"}, {"exp": 1}, {"aud": "x"}])
def test_every_route_needs_a_valid_access_jwt(landing, signing_key, other_key, token, caplog):
    caplog.set_level(logging.INFO)
    if token == "other-key":
        value = make_jwt(other_key)
    elif isinstance(token, dict):
        value = make_jwt(signing_key, **token)
    else:
        value = token
    for path in ("/", "/assets/lollo.css", "/assets/icon-192.png"):
        r = landing.browser.get(path, headers={"cf-access-jwt-assertion": value})
        assert r.status_code == 403, path
        assert EMAIL not in r.text, path
        assert r.headers["cross-origin-opener-policy"] == "same-origin", path
    assert "refused GET / reason=auth" in caplog.text


def test_the_unsigned_email_header_is_ignored(landing):
    r = landing.page(**{"cf-access-jwt-assertion": "", "cf-access-authenticated-user-email": EMAIL})
    assert r.status_code == 403


@pytest.mark.parametrize(
    "fetch",
    [
        SCRIPT_FETCH,  # fetch() from a script on the same origin (the code editor's tab)
        {"sec-fetch-dest": "iframe"},
        {"sec-fetch-mode": "no-cors", "sec-fetch-dest": "empty"},
        {"sec-fetch-mode": "", "sec-fetch-dest": "", "sec-fetch-site": ""},  # no Fetch Metadata at all
    ],
)
def test_the_page_loads_only_as_a_top_level_document(landing, fetch, caplog):
    caplog.set_level(logging.INFO)
    landing.agents.set([(AGENT_1, "lucius")])
    r = landing.browser.get("/", headers=fetch)
    assert r.status_code == 403
    # The refusal carries neither the person nor the agents.
    assert EMAIL not in r.text and "lucius" not in r.text and AGENT_1 not in r.text
    assert r.headers["cross-origin-opener-policy"] == "same-origin"
    assert "refused GET / reason=fetch" in caplog.text


def test_the_page_opens_from_another_site_after_an_access_login(landing):
    for site in ("cross-site", "none", "same-site"):
        assert landing.page(**{"sec-fetch-site": site}).status_code == 200, site


def test_assets_load_as_subresources(landing):
    style = {"sec-fetch-mode": "no-cors", "sec-fetch-dest": "style", "sec-fetch-site": "same-origin"}
    css = landing.browser.get("/assets/lollo.css", headers=style)
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert "--accent: #0b1020" in css.text
    icon = landing.browser.get("/assets/icon-192.png", headers={**style, "sec-fetch-dest": "image"})
    assert icon.status_code == 200 and icon.headers["content-type"] == "image/png"
    assert icon.content.startswith(b"\x89PNG")
    for r in (css, icon):
        assert r.headers["cross-origin-opener-policy"] == "same-origin"
        assert r.headers["x-content-type-options"] == "nosniff"


def test_only_the_landing_page_and_its_assets_are_served(landing):
    paths = ("/index.html", "/vault/", "/gmail-triggers/", "/code-server/", "/lollo/", "/app.css")
    for path in (*paths, "/lollo.css", "/icon-192.png", "/assets/", "/assets/app.css"):
        assert landing.browser.get(path).status_code == 404, path
    assert landing.browser.get("/healthz", headers={"cf-access-jwt-assertion": ""}).text == "ok"


def test_every_page_route_is_the_root_or_under_assets(landing):
    # The tunnel routes only these to landing (LOLLO_LANDING_PATH in lib/cloudflare.sh):
    # an asset anywhere else would not load.
    paths = [r.path for r in landing.app.routes]
    pages = [p for p in paths if p != "/healthz" and not p.startswith("/executor/v1/")]
    assert "/" in pages and len(pages) > 1
    for path in pages:
        assert path == "/" or re.fullmatch(r"/assets/[^/]+", path), path


# -- what it shows ----------------------------------------------------------------


def test_the_page_has_no_script_and_strict_headers(landing):
    landing.agents.set([(AGENT_1, "lucius")])
    r = landing.page()
    assert r.status_code == 200
    assert "<script" not in r.text.lower()
    assert csp(r) == {
        "default-src": "'none'",
        "style-src": "'self'",
        "img-src": "'self'",
        "frame-ancestors": "'none'",
        "form-action": "'none'",
        "base-uri": "'none'",
        "object-src": "'none'",
    }
    assert r.headers["cross-origin-opener-policy"] == "same-origin"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "same-origin"
    assert f"Signed in as {EMAIL}" in r.text


def test_the_page_links_to_the_lollo_and_back_to_lettuce(landing):
    page = landing.page().text
    assert 'href="/">Lollo Rozzo</a>' in page
    assert f'href="{LETTUCE}/">Back to Lettuce</a>' in page
    assert 'href="/assets/lollo.css"' in page and 'href="/assets/icon-192.png"' in page
    assert 'src="/assets/icon-192.png"' in page
    assert "/lollo/" not in page


def test_the_page_links_to_the_lollo_pages_that_are_on(landing_for):
    page = landing_for("lollo-vault").page().text
    # Profile lollo-<page> serves /<page>/.
    assert 'href="/vault/"' in page and "Vault" in page and "/lollo-vault" not in page
    assert "/gmail-triggers/" not in page and "/code-server/" not in page and "Code editor" not in page

    page = landing_for("lollo-vault,lollo-gmail-triggers").page().text
    assert 'href="/vault/"' in page and 'href="/gmail-triggers/"' in page and "Gmail triggers" in page
    assert page.index('href="/vault/"') < page.index('href="/gmail-triggers/"')

    page = landing_for("").page().text
    assert "No lollo features are on" in page and 'href="/vault/"' not in page


def test_with_the_editor_on_and_no_list_yet_the_page_says_it_is_on_its_way(landing):
    r = landing.page()
    assert r.status_code == 200
    # One entry for the editor, the agent list, titled like the row it replaces.
    assert "<h2>Code editor</h2>" in r.text and "Your agents&#39; files in VS Code" not in r.text
    assert "on its way" in r.text
    assert r.text.count('href="/code-server/"') == 1 and "File -&gt; Open Folder" in r.text
    assert EDITOR_ROWS.findall(r.text) == []


def test_with_the_editor_on_the_page_lists_the_agents_by_name(landing):
    landing.agents.set([(AGENT_2, "zed"), (AGENT_1, "Lucius"), (AGENT_3, "alfred")])
    page = landing.page().text
    # Sorted by name, ignoring case; each opens the editor in its workspace.
    assert EDITOR_ROWS.findall(page) == [
        f"%2Fwork%2F{AGENT_3}",
        f"%2Fwork%2F{AGENT_1}",
        f"%2Fwork%2F{AGENT_2}",
    ]
    for aid, name in ((AGENT_3, "alfred"), (AGENT_1, "Lucius"), (AGENT_2, "zed")):
        assert f">{name}</span>" in page and f"/work/{aid}" in page
    assert "on its way" not in page and 'href="/code-server/"' not in page


def test_agent_names_are_text_only(landing):
    landing.agents.set([(AGENT_1, '<img src=x onerror=alert(1)> "q" &amp;')])
    page = landing.page().text
    assert "<img src=x" not in page
    assert "&lt;img src=x onerror=alert(1)&gt; &#34;q&#34; &amp;amp;" in page


def test_agents_sharing_a_name_are_told_apart(landing):
    landing.agents.set([(AGENT_2, "lucius"), (AGENT_1, "lucius"), (AGENT_3, "hal")])
    page = landing.page().text
    assert f">lucius ({AGENT_1})</span>" in page and f">lucius ({AGENT_2})</span>" in page
    assert ">hal</span>" in page
    assert EDITOR_ROWS.findall(page) == [f"%2Fwork%2F{a}" for a in (AGENT_3, AGENT_1, AGENT_2)]


def test_with_no_agents_the_page_says_so_and_links_to_the_editor(landing):
    landing.agents.set([])
    page = landing.page().text
    assert "no agents yet" in page and page.count('href="/code-server/"') == 1


def test_opening_the_page_asks_for_a_new_list(landing):
    landing.agents.set([(AGENT_1, "lucius")])
    assert not landing.wanted()
    landing.page()
    assert landing.wanted()
    landing.agents.set([(AGENT_1, "lucius")])
    assert not landing.wanted()
    # A refused request asks for nothing.
    landing.browser.get("/", headers=SCRIPT_FETCH)
    landing.browser.get("/", headers={"cf-access-jwt-assertion": ""})
    assert not landing.wanted()


def test_a_page_opened_while_the_executor_lists_asks_again(landing):
    landing.agents.set([(AGENT_1, "lucius")])
    landing.page()
    assert landing.wanted()
    # Opened after the executor read the status, so the list it sends may predate the agent.
    landing.page()
    landing.agents.set([(AGENT_1, "lucius")])
    assert landing.wanted()
    landing.agents.set([(AGENT_1, "lucius")])
    assert not landing.wanted()


def test_without_the_editor_no_list_is_asked_for_or_shown(landing_for):
    landing = landing_for("lollo-vault,lollo-gmail-triggers")
    assert not landing.wanted()
    landing.agents.set([(AGENT_1, "lucius")])
    page = landing.page().text
    assert "lucius" not in page and "Code editor" not in page
    assert not landing.wanted()
