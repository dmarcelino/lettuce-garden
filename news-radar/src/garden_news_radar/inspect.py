"""source_inspect: what a page offers to read it with, found without a model. The
part an agent's normal page fetch misses: declared feeds, WordPress REST routes,
schema.org types, calendars, and the JSON endpoints a page's scripts call."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

import lxml.html
from garden_common.text import one_line
from lxml import etree

from .extract import schema_types
from .fetch import Fetcher, FetchError, Page, host_of
from .runner import sample_of

# Same-site scripts read for data URLs, after the library and tracking ones are
# left out; at the host's pacing (2 s apart) they fit well inside a tool call.
MAX_SCRIPTS = 6
MAX_SCRIPT_BYTES = 2 * 1024 * 1024
MAX_SCRIPTS_BYTES = 8 * 1024 * 1024
# Script paths that are libraries, consent banners or tracking, never a site's own
# data calls: skipped so the app's bundle is among those read.
LIBRARY_SCRIPT = re.compile(
    r"/\.rum/|jquery|csrf|granite|consent|cookie|analytics|gtm|gtag|tracking|polyfill|recaptcha|matomo|hotjar",
    re.I,
)
MAX_CANDIDATES = 40
MAX_ROUTES = 40
# URLs in script text that look like data: JSON files, APIs, GraphQL (persisted queries too).
# A root-relative one is matched from its first "/", so "/api/..." counts too.
CANDIDATE = re.compile(
    r"""["'`]((?:https?://[^"'`\s]{1,300}|(?=/))[^"'`\s]{0,300}?(?:\.json|/api/|/graphql|/wp-json/|/feed)[^"'`\s]{0,200})["'`]"""
)
ROUTE_HINT = re.compile(r"event|agenda|calend|concert|show|movie|film|session|program|ticket|news|post", re.I)
FEED_TYPES = (
    "application/rss+xml",
    "application/atom+xml",
    "application/feed+json",
    "application/json",
    "text/calendar",
)


def _shape(value: Any, depth: int = 0) -> Any:
    """A JSON value's outline: keys and list lengths, a few levels deep."""
    if depth > 3:
        return "..."
    if isinstance(value, dict):
        return {k: _shape(v, depth + 1) for k, v in list(value.items())[:25]}
    if isinstance(value, list):
        return [f"{len(value)} items", _shape(value[0], depth + 1)] if value else []
    if isinstance(value, str):
        return one_line(value, 60)
    return value


async def inspect(fetcher: Fetcher, url: str, max_wait: float | None = None) -> dict[str, Any]:
    try:
        page = await fetcher.get(url, accept="text/html, application/json;q=0.9, */*;q=0.5", max_wait=max_wait)
    except FetchError as e:
        return {"url": url, "error": str(e)}
    out: dict[str, Any] = {"url": page.url, "status": page.status, "content_type": page.content_type}
    if page.content_type in ("application/json", "application/ld+json") or page.content_type.endswith("+json"):
        try:
            out["json_shape"] = _shape(json.loads(page.body))
            out["suggested_kind"] = "json"
        except (ValueError, RecursionError):
            out["sample"] = sample_of(page)
        return out
    head = page.body[:2048].lstrip().lower()
    if page.content_type.endswith("xml") or head.startswith(b"<?xml") or b"<rss" in head or b"<feed" in head:
        out["suggested_kind"] = "rss"
        out["sample"] = sample_of(page)
        return out
    if page.content_type == "text/calendar" or head.startswith(b"begin:vcalendar"):
        out["suggested_kind"] = "ical"
        out["sample"] = sample_of(page)
        return out
    try:
        root = lxml.html.document_fromstring(page.body, base_url=page.url)
    except (etree.ParserError, ValueError):
        out["sample"] = sample_of(page)
        return out
    out["title"] = one_line(" ".join(root.xpath("//title//text()")), 200)
    out["feeds"] = _feeds(root, page)
    out["calendars"] = sorted(
        {urljoin(page.url, h) for h in root.xpath("//a/@href") if h.lower().split("?")[0].endswith(".ics")}
    )[:10]
    out["schema_org_types"] = schema_types(root)
    out["data_urls"], out["scripts_read"], out["scripts_skipped"] = await _data_urls(fetcher, root, page, max_wait)
    out["wordpress_routes"] = await _wordpress(fetcher, page, max_wait)
    out["sample"] = sample_of(page)
    return out


def _feeds(root: Any, page: Page) -> list[dict[str, str]]:
    feeds = []
    for link in root.xpath("//link[@rel]"):
        rels = (link.get("rel") or "").lower().split()
        kind = (link.get("type") or "").lower()
        if "alternate" in rels and kind in FEED_TYPES and link.get("href"):
            feeds.append({"type": kind, "url": urljoin(page.url, link.get("href")), "title": link.get("title") or ""})
    return feeds[:20]


async def _data_urls(fetcher: Fetcher, root: Any, page: Page, max_wait: float | None) -> tuple[list[str], int, int]:
    """Data URLs in the page and its same-site scripts; how many scripts were read
    and how many were left out (libraries, tracking, or past the cap)."""
    texts = [s.text or "" for s in root.xpath("//script[not(@src)]")]
    for el in root.xpath("//*"):
        for name, value in el.attrib.items():
            if name.startswith("data-") and ("/" in value) and len(value) < 500:
                texts.append(f'"{value}"')
    site = urlsplit(page.url).hostname or ""
    scripts = [urljoin(page.url, s) for s in root.xpath("//script/@src")]
    same_site = [s for s in scripts if (urlsplit(s).hostname or "").endswith(site.removeprefix("www."))]
    candidates = [s for s in dict.fromkeys(same_site) if not LIBRARY_SCRIPT.search(urlsplit(s).path)]
    skipped = len(set(same_site)) - len(candidates)
    read = budget = 0
    for i, src in enumerate(candidates):
        if read >= MAX_SCRIPTS or budget >= MAX_SCRIPTS_BYTES:
            skipped += len(candidates) - i
            break
        try:
            js = await fetcher.get(src, accept="*/*", max_bytes=MAX_SCRIPT_BYTES, max_wait=max_wait)
        except FetchError:
            continue
        read += 1
        budget += len(js.body)
        texts.append(js.text())
    found: dict[str, None] = {}
    for text in texts:
        for m in CANDIDATE.finditer(text):
            if len(found) >= MAX_CANDIDATES:
                return list(found), read, skipped
            candidate = m.group(1)
            if candidate.startswith("/") and not candidate.startswith("//"):
                candidate = urljoin(page.url, candidate)
            if candidate.startswith("http"):
                found[candidate] = None
    return list(found), read, skipped


async def _wordpress(fetcher: Fetcher, page: Page, max_wait: float | None) -> list[str]:
    """When /wp-json/ answers: its routes that look like events or posts."""
    try:
        index = await fetcher.get(host_of(page.url) + "/wp-json/", accept="application/json", max_wait=max_wait)
        data = json.loads(index.body)
    except (FetchError, ValueError, RecursionError):
        return []
    routes = data.get("routes") if isinstance(data, dict) else None
    if not isinstance(routes, dict):
        return []
    hinted = [r for r in routes if isinstance(r, str) and ROUTE_HINT.search(r) and "(?P<" not in r]
    return [host_of(page.url) + "/wp-json" + r for r in hinted[:MAX_ROUTES]]
