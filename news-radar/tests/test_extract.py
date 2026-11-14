from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import EVENTS_JSON, json_spec

from lollo_news_radar.extract import canonical_url, extract, outlet_of, parse_date
from lollo_news_radar.fetch import Page
from lollo_news_radar.runner import run_source
from lollo_news_radar.spec import SpecError, parse_spec

pytestmark = pytest.mark.anyio


def spec(obj: dict[str, Any]):
    return parse_spec(obj, 300)


def page(url: str, body: str, content_type: str = "text/html") -> Page:
    return Page(url, 200, content_type, body.encode())


def test_json_with_jmespath():
    s = spec(json_spec())
    items = extract(s, page("https://agenda.example.org/api/events", EVENTS_JSON, "application/json"))
    assert [i.fields["title"] for i in items] == ["The Long Night", "Morning Light"]  # no title, no item
    first = items[0].fields
    assert first["url"] == "https://agenda.example.org/e/1"  # absolute, tracking dropped
    assert first["start"] == "2027-02-01"
    assert first["venue"] == "Hall A"
    assert items[1].fields["start"] == "2027-02-03T21:00"


def test_html_with_css_and_attributes():
    html = """<html><body><ul>
      <li class="ev"><a class="t" href="/show/9">Quiet Hours</a><span class="d">12 de fevereiro de 2027</span>
        <span class="v">Room 1</span><img src="/i/9.jpg"></li>
      <li class="ev"><a class="t" href="/show/10">Late Set</a><span class="d">2027-02-20</span></li>
    </ul></body></html>"""
    s = spec(
        {
            "name": "hall",
            "url": "https://tickets.example.org/search",
            "kind": "html",
            "extract": {
                "items": "li.ev",
                "fields": {"title": "a.t", "url": "a.t@href", "start": "span.d", "venue": "span.v", "image": "img@src"},
            },
            "dates": {"languages": ["pt"]},
        }
    )
    items = extract(s, page("https://tickets.example.org/search", html))
    assert items[0].fields["url"] == "https://tickets.example.org/show/9"
    assert items[0].fields["start"] == "2027-02-12"
    assert items[0].fields["image"] == "https://tickets.example.org/i/9.jpg"
    assert items[1].fields["venue"] == ""


def test_schema_org_json_ld_and_microdata():
    html = """<html><head><script type="application/ld+json">
      {"@context": "https://schema.org", "@graph": [
        {"@type": "WebSite", "name": "Not an event"},
        {"@type": "MusicEvent", "@id": "https://venue.example.org/e/1", "name": "Band Night",
         "startDate": "2027-03-01T20:00:00+00:00", "location": {"@type": "Place", "name": "Big Room"},
         "description": "Doors at 19:00", "image": ["https://venue.example.org/1.jpg"]}
      ]}</script></head><body>
      <div itemscope itemtype="https://schema.org/TheaterEvent">
        <span itemprop="name">A Play</span><time itemprop="startDate" datetime="2027-03-05">5 March</time>
        <div itemprop="location" itemscope itemtype="https://schema.org/Place">
          <span itemprop="name">Stage 2</span></div>
        <a itemprop="url" href="/plays/2">more</a>
      </div></body></html>"""
    s = spec({"name": "venue", "url": "https://venue.example.org/", "kind": "schema_org"})
    items = extract(s, page("https://venue.example.org/", html))
    by_title = {i.fields["title"]: i.fields for i in items}
    assert set(by_title) == {"Band Night", "A Play"}
    assert by_title["Band Night"]["venue"] == "Big Room"
    assert by_title["Band Night"]["start"] == "2027-03-01T20:00+00:00"
    assert by_title["A Play"]["url"] == "https://venue.example.org/plays/2"
    assert by_title["A Play"]["venue"] == "Stage 2"


def test_rss_articles_take_their_outlet():
    feed = """<?xml version="1.0"?><rss version="2.0"><channel><title>Search</title>
      <item><title>Talks resume in the capital - Daily Example</title><link>https://news.example.org/a</link>
        <guid>tag:1</guid><pubDate>Fri, 15 Jan 2027 09:00:00 GMT</pubDate>
        <description>&lt;p&gt;Text&lt;/p&gt;</description></item>
      <item><title>Second</title><link>https://news.example.org/b</link><guid>tag:2</guid>
        <source url="https://other.example.org">Other Paper</source></item>
    </channel></rss>"""
    s = spec({"name": "topic", "url": "https://news.example.org/rss?q=x", "kind": "rss"})
    items = extract(s, page("https://news.example.org/rss?q=x", feed, "application/rss+xml"))
    assert items[0].fields["title"] == "Talks resume in the capital"
    assert items[0].fields["outlet"] == "Daily Example"
    assert items[0].fields["published"] == "2027-01-15T09:00+00:00"
    assert items[0].fields["summary"] == "Text"
    assert items[1].fields["outlet"] == "Other Paper"


def test_ical_events():
    cal = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//t//EN\r\nBEGIN:VEVENT\r\nUID:ev-1@example.org\r\n"
        "DTSTAMP:20270101T000000Z\r\nDTSTART:20270210T190000Z\r\nDTEND:20270210T210000Z\r\nSUMMARY:Lecture\r\n"
        "LOCATION:Auditorium\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    s = spec({"name": "cal", "url": "https://uni.example.org/events.ics", "kind": "ical"})
    items = extract(s, page("https://uni.example.org/events.ics", cal, "text/calendar"))
    assert items[0].fields["title"] == "Lecture"
    assert items[0].fields["start"].startswith("2027-02-10T19:00")
    assert items[0].fields["venue"] == "Auditorium"


def test_keys_are_stable_and_watch_follows_fields():
    s = spec(json_spec())
    a = extract(s, page("https://agenda.example.org/api/events", EVENTS_JSON, "application/json"))
    b = extract(s, page("https://agenda.example.org/api/events", EVENTS_JSON, "application/json"))
    assert [i.key for i in a] == [i.key for i in b]
    moved = EVENTS_JSON.replace("2027-02-01", "2027-02-08")
    c = extract(s, page("https://agenda.example.org/api/events", moved, "application/json"))
    assert c[0].key == a[0].key and c[0].watched(s.watch) != a[0].watched(s.watch)


def test_helpers():
    assert canonical_url("/x?utm_source=a&id=3#frag", "https://example.org/y") == "https://example.org/x?id=3"
    assert canonical_url("javascript:alert(1)", "https://example.org/") == ""
    assert parse_date("2027-02-01T21:00:00Z", ()) == "2027-02-01T21:00+00:00"
    assert parse_date("3 March 2027", ("en",)) == "2027-03-03"
    assert parse_date("sometime soon", ()) == ""
    assert outlet_of("Headline here - The Paper") == ("Headline here", "The Paper")


@pytest.mark.parametrize(
    "obj, message",
    [
        ({**json_spec(), "kind": "python"}, "kind"),
        ({**json_spec(), "url": "http://10.0.0.1/"}, "public"),
        ({**json_spec(), "every": "1m"}, "at least"),
        ({**json_spec(), "extract": {"items": "a[", "fields": {"title": "t"}}}, "extract.items"),
        ({**json_spec(), "extract": {"items": "a", "fields": {"url": "u"}}}, "title"),
        ({**json_spec(), "code": "import os"}, "unknown"),
        ({**json_spec(), "follow": {"links": "a"}}, "schema_org"),
        ({**json_spec(kind="html"), "extract": {"items": "li", "fields": {"title": "[["}}}, "extract.fields.title"),
        (
            {**json_spec(kind="schema_org"), "extract": {}, "types": ["Event"]},
            r"types \(it goes inside extract: extract.types\)",
        ),
        ({**json_spec(), "languages": ["pt"]}, r"dates.languages"),
    ],
)
def test_bad_specs_are_refused(obj, message):
    with pytest.raises(SpecError, match=message):
        spec(obj)


def test_fingerprint_ignores_key_order():
    a = spec(json_spec())
    reordered = dict(reversed(list(json_spec().items())))
    assert spec(reordered).fingerprint == a.fingerprint
    # How often it runs is not what a test checked: changing it needs no new test.
    assert spec(json_spec(every="12h")).fingerprint == a.fingerprint
    assert spec(json_spec(url="https://agenda.example.org/api/other")).fingerprint != a.fingerprint


async def test_pages_stop_when_nothing_new(fetcher, web):
    base = "https://agenda.example.org/api/events"
    web.add(base + "?page=1", EVENTS_JSON, "application/json")
    web.add(base + "?page=2", EVENTS_JSON, "application/json")
    s = spec(json_spec(pages={"param": "page", "start": 1, "max": 5}))
    result = await run_source(s, fetcher)
    assert result.ok and len(result.items) == 2
    assert web.fetched() == [base + "?page=1", base + "?page=2"]


async def test_follow_reads_unseen_item_pages(fetcher, web):
    listing = '<html><body><a class="e" href="/e/1">1</a><a class="e" href="/e/2">2</a></body></html>'
    detail = (
        '<html><script type="application/ld+json">'
        '{"@type": "Event", "name": "E%s", "startDate": "2027-04-0%s"}</script></html>'
    )
    web.add("https://venue.example.org/list", listing)
    web.add("https://venue.example.org/e/1", detail % (1, 1))
    web.add("https://venue.example.org/e/2", detail % (2, 2))
    s = spec({"name": "v", "url": "https://venue.example.org/list", "kind": "schema_org", "follow": {"links": "a.e"}})
    result = await run_source(s, fetcher, known_url=lambda u: u.endswith("/e/1"))
    assert [i.fields["title"] for i in result.items] == ["E2"]
    assert result.items[0].fields["url"] == "https://venue.example.org/e/2"


async def test_not_json_is_an_error_with_a_sample(fetcher, web):
    web.add("https://agenda.example.org/api/events", "<html><body>Please sign in</body></html>")
    result = await run_source(spec(json_spec()), fetcher)
    assert "not JSON" in result.error
    assert "Please sign in" in result.sample


async def test_inspect_keeps_to_its_cap_of_data_urls(fetcher, web):
    from lollo_news_radar.inspect import MAX_CANDIDATES, inspect

    attrs = "".join(f'<div data-src="/api/item/{i}.json"></div>' for i in range(200))
    web.add("https://site.example.org/", f"<html><body>{attrs}</body></html>")
    out = await inspect(fetcher, "https://site.example.org/")
    assert len(out["data_urls"]) == MAX_CANDIDATES


async def test_inspect_reads_the_app_script_behind_library_and_tracking_ones(fetcher, web):
    from lollo_news_radar.inspect import inspect

    site = "https://cinema.example.org"
    scripts = ["/.rum/micro.js", "/libs/jquery.min.js", "/libs/granite/csrf.js", "/clientlibs/site.min.js"]
    tags = "".join(f'<script src="{s}"></script>' for s in scripts)
    web.add(f"{site}/", f"<html><head>{tags}</head><body>Films</body></html>")
    for s in scripts[:3]:
        web.add(site + s, 'var x = "/libs/token.json";', "application/javascript")
    web.add(site + scripts[3], 'fetch("/graphql/execute.json/site/getAllMovies")', "application/javascript")
    out = await inspect(fetcher, f"{site}/")
    assert f"{site}/graphql/execute.json/site/getAllMovies" in out["data_urls"]
    assert out["scripts_read"] == 1 and out["scripts_skipped"] == 3
    assert f"{site}/libs/jquery.min.js" not in web.fetched()


async def test_inspect_reads_at_most_its_cap_of_scripts(fetcher, web):
    from lollo_news_radar.inspect import MAX_SCRIPTS, inspect

    site = "https://many.example.org"
    tags = "".join(f'<script src="/app/part-{i}.js"></script>' for i in range(MAX_SCRIPTS + 3))
    web.add(f"{site}/", f"<html><head>{tags}</head><body>x</body></html>")
    for i in range(MAX_SCRIPTS + 3):
        web.add(f"{site}/app/part-{i}.js", f'get("/api/part/{i}")', "application/javascript")
    out = await inspect(fetcher, f"{site}/")
    assert out["scripts_read"] == MAX_SCRIPTS and out["scripts_skipped"] == 3
    assert len(out["data_urls"]) == MAX_SCRIPTS


# -- text encodings -------------------------------------------------------------------
CARDS = "<html><body><h3>Programação</h3><h3>Música clássica</h3></body></html>"


def html_spec(**over: Any) -> dict[str, Any]:
    return {
        "name": "venue",
        "url": "https://venue.example.org/agenda",
        "kind": "html",
        "item_kind": "article",
        "extract": {"items": "h3", "fields": {"title": "*"}},
        **over,
    }


@pytest.mark.parametrize(
    "body, content_type",
    [
        (CARDS.encode("utf-8"), "text/html; charset=utf-8"),  # the charset only in the header
        (CARDS.encode("utf-8"), "text/html"),  # no charset anywhere: UTF-8 when it decodes as such
        (CARDS.encode("iso-8859-1"), 'text/html; charset="ISO-8859-1"'),
    ],
)
async def test_pages_are_read_in_their_encoding(fetcher, web, body, content_type):
    web.add("https://venue.example.org/agenda", body, content_type)
    result = await run_source(spec(html_spec()), fetcher)
    assert [i.fields["title"] for i in result.items] == ["Programação", "Música clássica"]
    assert "Programação" in result.sample


async def test_inspect_reads_the_header_charset(fetcher, web):
    from lollo_news_radar.inspect import inspect

    web.add("https://venue.example.org/", CARDS.encode("utf-8"), "text/html; charset=utf-8")
    out = await inspect(fetcher, "https://venue.example.org/")
    assert "Programação" in out["sample"]


# -- sitemaps -------------------------------------------------------------------------
SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://venue.example.org/events/old-band-northtown/</loc><lastmod>2027-01-02</lastmod></url>
  <url><loc>https://venue.example.org/events/the-new-band-northtown-2027/</loc><lastmod>2027-01-14T10:00:00Z</lastmod></url>
  <url><loc>https://venue.example.org/about/</loc><lastmod>2027-01-15</lastmod></url>
</urlset>"""


async def test_a_sitemap_lists_its_newest_pages(fetcher, web):
    web.add("https://venue.example.org/sitemap.xml", SITEMAP, "application/xml")
    s = spec(
        {
            "name": "v",
            "url": "https://venue.example.org/sitemap.xml",
            "kind": "sitemap",
            "extract": {"match": ["/events/"]},
        }
    )
    assert s.item_kind == "article"
    result = await run_source(s, fetcher)
    assert [i.fields["title"] for i in result.items] == ["the new band northtown 2027", "old band northtown"]
    assert result.items[0].fields["published"] == "2027-01-14T10:00+00:00"


async def test_a_followed_sitemap_reads_its_pages(fetcher, web):
    web.add("https://venue.example.org/sitemap.xml", SITEMAP, "application/xml")
    web.add(
        "https://venue.example.org/events/the-new-band-northtown-2027/",
        '<html><script type="application/ld+json">{"@type": "MusicEvent", "name": "The New Band", '
        '"startDate": "2027-04-01", "location": {"name": "Big Room"}}</script></html>',
    )
    web.add("https://venue.example.org/events/old-band-northtown/", "<html><title>Old Band</title></html>")
    s = spec(
        {
            "name": "v",
            "url": "https://venue.example.org/sitemap.xml",
            "kind": "sitemap",
            "extract": {"match": ["/events/"]},
            "follow": {"max": 5},
        }
    )
    assert s.item_kind == "event"
    result = await run_source(s, fetcher, known_url=lambda u: False)
    by_title = {i.fields["title"]: i.fields for i in result.items}
    assert by_title["The New Band"]["start"] == "2027-04-01" and by_title["The New Band"]["venue"] == "Big Room"
    assert "Old Band" in by_title  # no schema.org data: still seen, by its title
    assert "https://venue.example.org/about/" not in web.fetched()


async def test_a_sitemap_index_and_unsafe_xml_are_refused(fetcher, web):
    index = (
        '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<sitemap><loc>https://venue.example.org/events-sitemap.xml</loc></sitemap></sitemapindex>"
    )
    web.add("https://venue.example.org/sitemap.xml", index, "application/xml")
    s = spec({"name": "v", "url": "https://venue.example.org/sitemap.xml", "kind": "sitemap"})
    assert "sitemap index" in (await run_source(s, fetcher)).error
    entity = (
        '<?xml version="1.0"?><!DOCTYPE u [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>&x;</loc></url></urlset>'
    )
    web.add("https://venue.example.org/sitemap.xml", entity, "application/xml")
    result = await run_source(s, fetcher)
    assert not any("root:" in i.fields["title"] for i in result.items)


@pytest.mark.parametrize(
    "obj, message",
    [
        ({"name": "v", "url": "https://v.example.org/s.xml", "kind": "sitemap", "extract": {"match": "/e/"}}, "match"),
        ({"name": "v", "url": "https://v.example.org/s.xml", "kind": "sitemap", "follow": {"links": "a"}}, "only max"),
        ({"name": "v", "url": "https://v.example.org/s.xml", "kind": "sitemap", "pages": {"param": "p"}}, "pages"),
    ],
)
def test_bad_sitemap_specs_are_refused(obj, message):
    with pytest.raises(SpecError, match=message):
        spec(obj)


# -- source_inspect -------------------------------------------------------------------
async def test_inspect_offers_sitemaps_and_reads_them(fetcher, web):
    from lollo_news_radar.inspect import inspect

    web.add(
        "https://venue.example.org/robots.txt",
        "User-agent: *\nAllow: /\nSitemap: https://venue.example.org/sitemap.xml\n",
        "text/plain",
    )
    web.add("https://venue.example.org/", "<html><body>Home</body></html>")
    web.add("https://venue.example.org/sitemap.xml", SITEMAP, "application/xml")
    out = await inspect(fetcher, "https://venue.example.org/")
    assert out["sitemaps"] == ["https://venue.example.org/sitemap.xml"]
    sm = await inspect(fetcher, "https://venue.example.org/sitemap.xml")
    assert sm["suggested_kind"] == "sitemap" and sm["sitemap_pages"] == 3
    assert sm["newest"][0]["url"] == "https://venue.example.org/about/"


async def test_inspect_cleans_and_ranks_data_urls(fetcher, web):
    from lollo_news_radar.inspect import inspect

    script = (
        'a("https://site.example.org/api/account/delete\\\\"); b(`https://${host}/api/oembed.json`);'
        'c("https://site.example.org/api/user/login"); d("https://site.example.org/api/list/latest");'
        'e("https://site.example.org/api/events?from=today")'
    )
    web.add("https://site.example.org/", f"<html><body><script>{script}</script></body></html>")
    out = await inspect(fetcher, "https://site.example.org/")
    assert out["data_urls"] == [
        "https://site.example.org/api/events?from=today",
        "https://site.example.org/api/list/latest",
    ]
    assert out["data_urls_left_out"] == 2


async def test_inspect_offers_repeated_blocks_for_html(fetcher, web):
    from lollo_news_radar.inspect import inspect

    cards = "".join(
        f'<div class="event-card big"><h3>Band {i}</h3><p class="when">2{i}.11.2027</p><a href="/e/{i}">more</a></div>'
        for i in range(4)
    )
    nav = "".join(f'<li class="menu-item"><a href="/m/{i}">Menu entry {i}</a></li>' for i in range(5))
    web.add("https://venue.example.org/agenda", f"<html><body><nav><ul>{nav}</ul></nav>{cards}</body></html>")
    out = await inspect(fetcher, "https://venue.example.org/agenda")
    first = out["repeated_blocks"][0]
    assert first["items"] == "div.event-card" and first["count"] == 4
    assert first["with_date"] == 1.0 and first["heading"] == "h3"
    assert not any(b["items"] == "li.menu-item" for b in out["repeated_blocks"])


@pytest.mark.parametrize("charset", ["EUC-JP", "base64", "rot13", "x-unknown"])
async def test_any_declared_charset_is_read_or_ignored_without_crashing(fetcher, web, charset):
    from lollo_news_radar.fetch import charset_of
    from lollo_news_radar.inspect import inspect

    body = '<?xml version="1.0" encoding="EUC-JP"?><html><body><h3>日本の夜</h3><h3>Late Set</h3></body></html>'
    data = body.encode("euc_jp")
    web.add("https://venue.example.org/agenda", data, f"text/html; charset={charset}")
    result = await run_source(spec(html_spec()), fetcher)
    assert result.ok and len(result.items) == 2
    if charset == "EUC-JP":
        assert result.items[0].fields["title"] == "日本の夜"
    else:
        assert charset_of(f"text/html; charset={charset}") == ""  # not a text encoding Python knows
    assert "error" not in await inspect(fetcher, "https://venue.example.org/agenda")


def test_only_unfollowed_listings_are_judged_by_past_events():
    from datetime import date

    from lollo_news_radar.extract import Item, run_problem

    past = [Item("k", {"title": "Old", "start": "2027-01-01"})]
    today = date(2027, 1, 15)
    assert "in the past" in run_problem(past, spec(json_spec()), today)
    # A followed listing's run holds only the pages read in it, which can be old ones read again.
    followed = spec({"name": "v", "url": "https://v.example.org/list", "kind": "schema_org", "follow": {"links": "a"}})
    assert run_problem(past, followed, today) == ""


async def test_inspect_counts_a_whole_sitemap(fetcher, web):
    from lollo_news_radar.extract import MAX_SITEMAP_ITEMS
    from lollo_news_radar.inspect import inspect

    urls = "".join(f"<url><loc>https://big.example.org/e/{i}</loc></url>" for i in range(MAX_SITEMAP_ITEMS + 50))
    web.add(
        "https://big.example.org/sitemap.xml",
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
        "application/xml",
    )
    out = await inspect(fetcher, "https://big.example.org/sitemap.xml")
    assert out["sitemap_pages"] == MAX_SITEMAP_ITEMS + 50 and len(out["newest"]) == 10


async def test_inspect_keeps_listing_urls_that_share_a_word_with_account_ones(fetcher, web):
    from lollo_news_radar.inspect import inspect

    script = (
        'a("https://site.example.org/api/events?category=piano"); b("https://site.example.org/api/artists/profile.json");'
        'c("https://site.example.org/api/piano/payment/get"); d("https://[cdn/api/events.json")'
    )
    web.add("https://site.example.org/", f"<html><body><script>{script}</script></body></html>")
    out = await inspect(fetcher, "https://site.example.org/")
    assert out["data_urls"] == [
        "https://site.example.org/api/events?category=piano",
        "https://site.example.org/api/artists/profile.json",
    ]
    assert out["data_urls_left_out"] == 1


# -- rc.2: text, dates and listings -----------------------------------------------------
def test_text_from_json_and_json_ld_is_decoded():
    body = json.dumps(
        {
            "data": {
                "list": {
                    "items": [
                        {
                            "name": "Exposição: &#8220;Ecos&#8221; &amp; Co",
                            "date": "2027-02-01",
                            "link": "/e/1",
                            "place": {"name": "Hall &amp; Bar"},
                            "about": "<p>Doors at 19:00 &amp; more</p>",
                        }
                    ]
                }
            }
        }
    )
    s = spec(
        json_spec(
            extract={
                "items": "data.list.items",
                "fields": {"title": "name", "start": "date", "url": "link", "venue": "place.name", "summary": "about"},
            }
        )
    )
    [item] = extract(s, page("https://agenda.example.org/api/events", body, "application/json"))
    assert item.fields["title"] == "Exposição: “Ecos” & Co"
    assert item.fields["venue"] == "Hall & Bar"
    assert item.fields["summary"] == "Doors at 19:00 & more"
    ld = (
        '<html><script type="application/ld+json">'
        '{"@type": "Event", "name": "&#8220;Quietude&#8221;", "startDate": "2027-03-01"}</script></html>'
    )
    [event] = extract(
        spec({"name": "v", "url": "https://v.example.org/", "kind": "schema_org"}), page("https://v.example.org/", ld)
    )
    assert event.fields["title"] == "“Quietude”"


@pytest.mark.parametrize(
    "text, languages, expected",
    [
        ("27 Outubro", ("pt",), "2026-10-27"),  # started recently: this year
        ("sáb 14 nov", ("pt",), "2026-11-14"),
        ("15 Janeiro", ("pt",), "2027-01-15"),  # long past this year: next year's
        ("Nov 20", ("en",), "2026-11-20"),
        ("12 de fevereiro de 2026", ("pt",), "2026-02-12"),  # a year given is kept
    ],
)
def test_dates_without_a_year_take_the_nearest_upcoming_year(text, languages, expected):
    from datetime import datetime

    assert parse_date(text, languages, now=datetime(2026, 11, 13, 12, 0)) == expected


def test_blocks_see_dates_written_without_a_year():
    import lxml.html

    from lollo_news_radar.blocks import repeated_blocks

    cards = "".join(f'<article class="card"><a href="/e/{i}">Show {i}</a> sáb {i + 10} nov</article>' for i in range(4))
    cards += "".join(f'<div class="tile"><a href="/x/{i}">Show {i}</a> 2{i} de Outubro</div>' for i in range(4))
    blocks = {
        b["items"]: b for b in repeated_blocks(lxml.html.document_fromstring(f"<html><body>{cards}</body></html>"))
    }
    assert blocks["article.card"]["with_date"] == 1.0 and blocks["div.tile"]["with_date"] == 1.0


async def test_a_sitemap_follows_only_its_newest_pages(fetcher, web):
    urls = "".join(
        f"<url><loc>https://v.example.org/events/e{i}/</loc><lastmod>2027-01-{i + 1:02d}</lastmod></url>"
        for i in range(30)
    )
    web.add(
        "https://v.example.org/sitemap.xml",
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
        "application/xml",
    )
    s = spec({"name": "v", "url": "https://v.example.org/sitemap.xml", "kind": "sitemap", "follow": {"max": 5}})
    newest = {f"https://v.example.org/events/e{i}/" for i in range(25, 30)}
    result = await run_source(s, fetcher, known_url=lambda u: u in newest)
    assert result.followed == 0 and web.fetched() == ["https://v.example.org/sitemap.xml"]  # the backlog is never read
    result = await run_source(s, fetcher, known_url=lambda u: u in newest - {"https://v.example.org/events/e29/"})
    assert web.fetched()[-1] == "https://v.example.org/events/e29/"


# -- rc.3: a sitemap source that follows a sitemap index ----------------------------------
SM = "https://site.example.org"
NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


def urlset(entries):
    rows = "".join(f"<url><loc>{SM}/events/{slug}/</loc><lastmod>{day}</lastmod></url>" for slug, day in entries)
    return f"<urlset {NS}>{rows}</urlset>"


def add_index(web, files=("events-sitemap.xml", "events-sitemap2.xml", "events-sitemap3.xml", "post-sitemap.xml")):
    rows = "".join(f"<sitemap><loc>{SM}/{f}</loc><lastmod>2027-01-14</lastmod></sitemap>" for f in files)
    web.add(f"{SM}/sitemap_index.xml", f"<sitemapindex {NS}>{rows}</sitemapindex>", "application/xml")
    web.add(f"{SM}/events-sitemap.xml", urlset([("ancient", "2025-01-01")]), "application/xml")
    web.add(f"{SM}/post-sitemap.xml", urlset([("a-post", "2027-01-14")]), "application/xml")
    # The newest pages straddle the files: a new file has just begun.
    web.add(f"{SM}/events-sitemap2.xml", urlset([("older", "2027-01-10"), ("newer", "2027-01-12")]), "application/xml")
    web.add(f"{SM}/events-sitemap3.xml", urlset([("newest", "2027-01-14")]), "application/xml")


def index_spec(**over: Any) -> dict[str, Any]:
    return {
        "name": "site",
        "url": f"{SM}/sitemap_index.xml",
        "kind": "sitemap",
        "extract": {"sitemaps": ["events-sitemap"], "match": ["/events/"]},
        **over,
    }


async def test_an_index_source_reads_the_last_two_files_of_its_family(fetcher, web):
    add_index(web)
    result = await run_source(spec(index_spec()), fetcher)
    assert result.ok
    assert [i.fields["title"] for i in result.items] == ["newest", "newer", "older"]
    assert f"{SM}/events-sitemap.xml" not in web.fetched() and f"{SM}/post-sitemap.xml" not in web.fetched()


async def test_a_followed_index_reads_the_newest_pages_across_its_files(fetcher, web):
    add_index(web)
    for slug in ("newest", "newer", "older"):
        web.add(f"{SM}/events/{slug}/", f"<html><title>{slug.title()}</title></html>")
    result = await run_source(spec(index_spec(follow={"max": 2})), fetcher)
    assert {i.fields["title"] for i in result.items} == {"Newest", "Newer"}
    assert f"{SM}/events/older/" not in web.fetched()


async def test_an_index_source_survives_its_older_file_unreadable(fetcher, web):
    add_index(web)
    web.add(f"{SM}/events-sitemap2.xml", "boom", "text/plain", status=500)
    result = await run_source(spec(index_spec()), fetcher)
    assert result.ok and [i.fields["title"] for i in result.items] == ["newest"]


async def test_index_mistakes_say_what_to_do(fetcher, web):
    add_index(web)
    plain = await run_source(spec(index_spec(extract={"match": ["/events/"]})), fetcher)
    assert "extract.sitemaps" in plain.error  # an index without it
    web.add(f"{SM}/one.xml", urlset([("x", "2027-01-01")]), "application/xml")
    wrong = await run_source(spec(index_spec(url=f"{SM}/one.xml")), fetcher)
    assert "is for a sitemap index" in wrong.error
    none = await run_source(spec(index_spec(extract={"sitemaps": ["film-sitemap"]})), fetcher)
    assert "no sitemap family film-sitemap" in none.error and "events-sitemap" in none.error


@pytest.mark.parametrize(
    "extract_in, message",
    [({"sitemaps": "events"}, "extract.sitemaps"), ({"sitemaps": [""]}, "extract.sitemaps")],
)
def test_bad_sitemaps_are_refused(extract_in, message):
    with pytest.raises(SpecError, match=message):
        spec(index_spec(extract=extract_in))
    with pytest.raises(SpecError, match="sitemaps"):
        spec({**json_spec(), "extract": {"items": "a", "fields": {"title": "t"}, "sitemaps": ["x"]}})


async def test_inspect_groups_an_index_into_families(fetcher, web):
    from lollo_news_radar.inspect import inspect

    add_index(web)
    out = await inspect(fetcher, f"{SM}/sitemap_index.xml")
    assert out["sitemap_families"] == {"events-sitemap": 3, "post-sitemap": 1}
    assert "extract.sitemaps" in out["hint"]


# -- rc.3 review: families, order, undated files, pauses ----------------------------------
def test_sitemap_families_ignore_numbers_dates_and_order():
    from lollo_news_radar.extract import sitemap_family

    assert sitemap_family(f"{SM}/events-sitemap2.xml") == ("events-sitemap", (2,))
    assert sitemap_family(f"{SM}/events-sitemap.xml") == ("events-sitemap", ())
    assert sitemap_family(f"{SM}/post-sitemap-2026-10.xml") == ("post-sitemap", (2026, 10))
    assert sitemap_family(f"{SM}/tribe_events-sitemap.xml")[0] == "tribe_events-sitemap"


async def test_an_index_source_picks_files_by_number_not_by_their_place(fetcher, web):
    files = [f"sitemap-{n}.xml" for n in (1, 10, 11, 2, 3)] + ["tribe_events-sitemap.xml"]  # listed as text sorts
    rows = "".join(f"<sitemap><loc>{SM}/{f}</loc></sitemap>" for f in files)
    web.add(f"{SM}/sitemap_index.xml", f"<sitemapindex {NS}>{rows}</sitemapindex>", "application/xml")
    for n in (1, 10, 11, 2, 3):
        web.add(f"{SM}/sitemap-{n}.xml", urlset([(f"page-{n}", f"2027-01-{n:02d}")]), "application/xml")
    result = await run_source(spec(index_spec(extract={"sitemaps": ["sitemap"]})), fetcher)
    assert [i.fields["title"] for i in result.items] == ["page 11", "page 10"]


async def test_an_index_reads_files_beyond_its_fiftieth(fetcher, web):
    rows = "".join(f"<sitemap><loc>{SM}/post-sitemap{n}.xml</loc></sitemap>" for n in range(1, 81))
    web.add(f"{SM}/sitemap_index.xml", f"<sitemapindex {NS}>{rows}</sitemapindex>", "application/xml")
    web.add(f"{SM}/post-sitemap79.xml", urlset([("a", "2027-01-01")]), "application/xml")
    web.add(f"{SM}/post-sitemap80.xml", urlset([("b", "2027-01-02")]), "application/xml")
    result = await run_source(spec(index_spec(extract={"sitemaps": ["post-sitemap"]})), fetcher)
    assert [i.fields["title"] for i in result.items] == ["b", "a"]


async def test_undated_files_keep_their_newest_pages_last_listed(fetcher, web):
    rows = "".join(f"<sitemap><loc>{SM}/{f}</loc></sitemap>" for f in ("events-sitemap2.xml", "events-sitemap3.xml"))
    web.add(f"{SM}/sitemap_index.xml", f"<sitemapindex {NS}>{rows}</sitemapindex>", "application/xml")
    old = "".join(f"<url><loc>{SM}/events/old-{i}/</loc></url>" for i in range(1000))
    web.add(f"{SM}/events-sitemap2.xml", f"<urlset {NS}>{old}</urlset>", "application/xml")
    new = "".join(f"<url><loc>{SM}/events/new-{i}/</loc></url>" for i in range(5))
    web.add(f"{SM}/events-sitemap3.xml", f"<urlset {NS}>{new}</urlset>", "application/xml")
    result = await run_source(spec(index_spec(extract={"sitemaps": ["events-sitemap"]})), fetcher)
    titles = [i.fields["title"] for i in result.items]
    assert titles[:6] == ["new 4", "new 3", "new 2", "new 1", "new 0", "old 999"]


async def test_the_newest_file_of_a_family_must_be_read(fetcher, web):
    add_index(web)
    web.add(f"{SM}/events-sitemap2.xml", "boom", "text/plain", status=500)
    assert (await run_source(spec(index_spec()), fetcher)).ok  # the older one may fail
    add_index(web)
    web.add(f"{SM}/events-sitemap3.xml", "slow down", "text/plain", status=429, **{"retry-after": "600"})
    result = await run_source(spec(index_spec()), fetcher)
    assert "HTTP 429" in result.error and result.retry_after == 600


async def test_inspect_leaves_out_gzipped_files_and_says_so(fetcher, web):
    from lollo_news_radar.inspect import inspect

    files = ("events-sitemap1.xml.gz", "events-sitemap2.xml.gz", "post-sitemap.xml")
    rows = "".join(f"<sitemap><loc>{SM}/{f}</loc></sitemap>" for f in files)
    web.add(f"{SM}/sitemap_index.xml", f"<sitemapindex {NS}>{rows}</sitemapindex>", "application/xml")
    out = await inspect(fetcher, f"{SM}/sitemap_index.xml")
    assert out["sitemap_families"] == {"post-sitemap": 1}
    assert out["gzipped_left_out"] == 2


# -- rc.4 review: families, undated files, relative dates, escaped URLs ------------------
def add_families(web, events_newest="events-sitemap5.xml"):
    files = ("events-sitemap4.xml", "events-sitemap5.xml", "post-sitemap-2026-09.xml", "post-sitemap-2026-10.xml")
    rows = "".join(f"<sitemap><loc>{SM}/{f}</loc></sitemap>" for f in files)
    web.add(f"{SM}/sitemap_index.xml", f"<sitemapindex {NS}>{rows}</sitemapindex>", "application/xml")
    web.add(f"{SM}/events-sitemap4.xml", urlset([("ev-4", "2027-01-04")]), "application/xml")
    web.add(f"{SM}/events-sitemap5.xml", urlset([("ev-5", "2027-01-05")]), "application/xml")
    web.add(f"{SM}/post-sitemap-2026-09.xml", urlset([("post-9", "2026-09-30")]), "application/xml")
    web.add(f"{SM}/post-sitemap-2026-10.xml", urlset([("post-10", "2026-10-31")]), "application/xml")


async def test_an_index_source_reads_the_newest_files_of_each_family(fetcher, web):
    add_families(web)
    two = index_spec(extract={"sitemaps": ["events-sitemap", "post-sitemap"], "match": ["/events/"]})
    result = await run_source(spec(two), fetcher)
    assert result.ok
    assert {i.fields["title"] for i in result.items} == {"ev 5", "ev 4", "post 10", "post 9"}
    # Each family's newest file must be read, not only the one that numbers highest overall.
    web.add(f"{SM}/events-sitemap5.xml", "boom", "text/plain", status=500)
    assert "HTTP 500" in (await run_source(spec(two), fetcher)).error


async def test_an_undated_newest_file_comes_before_a_dated_older_one(fetcher, web):
    from lollo_news_radar.extract import MAX_SITEMAP_ITEMS

    rows = "".join(f"<sitemap><loc>{SM}/{f}</loc></sitemap>" for f in ("events-sitemap2.xml", "events-sitemap3.xml"))
    web.add(f"{SM}/sitemap_index.xml", f"<sitemapindex {NS}>{rows}</sitemapindex>", "application/xml")
    dated = [(f"old-{i}", f"2027-01-{1 + i % 28:02d}") for i in range(MAX_SITEMAP_ITEMS + 50)]
    web.add(f"{SM}/events-sitemap2.xml", urlset(dated), "application/xml")
    new = "".join(f"<url><loc>{SM}/events/new-{i}/</loc></url>" for i in range(3))
    web.add(f"{SM}/events-sitemap3.xml", f"<urlset {NS}>{new}</urlset>", "application/xml")
    result = await run_source(spec(index_spec()), fetcher)
    assert [i.fields["title"] for i in result.items][:3] == ["new 2", "new 1", "new 0"]


@pytest.mark.parametrize(
    "text, languages, expected",
    [
        ("2 hours ago", ("en",), "2026-11-13T13:00"),  # from now, not from midnight
        ("há 2 horas", ("pt",), "2026-11-13T13:00"),
        ("4 months ago", ("en",), "2026-07-13T15:00"),  # relative: never moved a year on
        ("há 4 meses", ("pt",), "2026-07-13T15:00"),
        ("01.03.26", ("de",), "2026-03-01"),  # a two-digit year is a year given
    ],
)
def test_relative_dates_and_short_years_are_kept_as_written(text, languages, expected):
    from datetime import datetime

    assert parse_date(text, languages, now=datetime(2026, 11, 13, 15, 0)) == expected


def test_a_yearless_date_far_ahead_is_last_years():
    from datetime import datetime

    january = datetime(2027, 1, 10, 12, 0)
    assert parse_date("28 Dez", ("de",), now=january) == "2026-12-28"  # began last month
    assert parse_date("20 Jan", ("de",), now=january) == "2027-01-20"


def test_followed_listings_are_not_judged_by_their_span():
    from datetime import date

    from lollo_news_radar.extract import Item, window_warning

    items = [Item(f"k{i}", {"title": f"E{i}", "start": f"2027-01-{16 + i % 3:02d}"}) for i in range(12)]
    today = date(2027, 1, 15)
    assert "span only" in window_warning(items, spec(json_spec()), today)
    # A followed listing's items are only the pages a run read, never the whole listing.
    followed = spec({"name": "v", "url": "https://v.example.org/list", "kind": "schema_org", "follow": {"links": "a"}})
    assert window_warning(items, followed, today) == ""


async def test_inspect_keeps_json_escaped_data_urls(fetcher, web):
    from lollo_news_radar.inspect import inspect

    script = (
        'var a = "https://site.example.org/api/events?from=1\\u0026size=50";'
        ' var b = "https://site.example.org\\u002Fapi\\u002Fshows.json"; var c = `/api/x/${id}.json`;'
    )
    web.add("https://site.example.org/", f"<html><body><script>{script}</script></body></html>")
    out = await inspect(fetcher, "https://site.example.org/")
    assert out["data_urls"] == [
        "https://site.example.org/api/events?from=1&size=50",
        "https://site.example.org/api/shows.json",
    ]
