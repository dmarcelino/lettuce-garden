from __future__ import annotations

from typing import Any

import pytest
from conftest import EVENTS_JSON, json_spec

from garden_monitors.extract import canonical_url, extract, outlet_of, parse_date
from garden_monitors.fetch import Page
from garden_monitors.runner import run_source
from garden_monitors.spec import SpecError, parse_spec

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
    ],
)
def test_bad_specs_are_refused(obj, message):
    with pytest.raises(SpecError, match=message):
        spec(obj)


def test_fingerprint_ignores_key_order():
    a = spec(json_spec())
    reordered = dict(reversed(list(json_spec().items())))
    assert spec(reordered).fingerprint == a.fingerprint
    assert spec(json_spec(every="12h")).fingerprint != a.fingerprint


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
    from garden_monitors.inspect import MAX_CANDIDATES, inspect

    attrs = "".join(f'<div data-src="/api/item/{i}.json"></div>' for i in range(200))
    web.add("https://site.example.org/", f"<html><body>{attrs}</body></html>")
    out = await inspect(fetcher, "https://site.example.org/")
    assert len(out["data_urls"]) == MAX_CANDIDATES
