"""source_inspect: what a page offers to read it with, found without a model. The
part an agent's normal page fetch misses: declared feeds, WordPress REST routes,
schema.org types, calendars, and the JSON endpoints a page's scripts call."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from garden_common.text import one_line
from lxml import etree

from .extract import ExtractError, html_document, read_sitemap, schema_types
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
# URLs gathered before the account-like ones are left out and the rest ranked.
MAX_GATHERED = 200
MAX_ROUTES = 40
# URLs in script text that look like data: JSON files, APIs, GraphQL (persisted queries too).
# A root-relative one is matched from its first "/", so "/api/..." counts too.
CANDIDATE = re.compile(
    r"""["'`]((?:https?://[^"'`\s]{1,300}|(?=/))[^"'`\s]{0,300}?(?:\.json|/api/|/graphql|/wp-json/|/feed)[^"'`\s]{0,200})["'`]"""
)
ROUTE_HINT = re.compile(r"event|agenda|calend|concert|show|movie|film|session|program|ticket|news|post", re.I)
# Path segments of data URLs about a visitor, never a site's listings: left out of
# data_urls, and counted. Whole segments only: /api/account/... goes, while
# ?category=piano or /artists/profile.json stays. "piano" is a paywall vendor's API.
ACCOUNT_SEGMENTS = frozenset(
    "account accounts login logout signin sign-in signup sign-up register password user users auth oauth token "
    "payment payments checkout cart basket subscription subscriptions newsletter newsletters profile favourites "
    "favorites consent captcha tracking analytics tagmanager oembed piano".split()
)
# Script code, not a URL: template literals and placeholders.
NOT_A_URL = re.compile(r"\$\{|\{\{|<%|\\")
# Repeated blocks offered for kind html.
MAX_BLOCKS = 5
MIN_BLOCK_REPEATS = 3
CLASS_NAME = re.compile(r"-?[_a-zA-Z][_a-zA-Z0-9-]*")
DATE_TEXT = re.compile(
    r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{1,2}(?:\s+de)?\s+[^\W\d_]{3,}\.?(?:\s+de)?\s+\d{4}\b"
)
OUTSIDE_CONTENT = {"nav", "header", "footer", "script", "style", "noscript", "template", "select", "form"}
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
        out_error: dict[str, Any] = {"url": url, "error": str(e)}
        if e.retry_after:
            out_error["retry_after"] = e.retry_after
        return out_error
    out: dict[str, Any] = {"url": page.url, "status": page.status, "content_type": page.content_type}
    if page.content_type in ("application/json", "application/ld+json") or page.content_type.endswith("+json"):
        try:
            out["json_shape"] = _shape(json.loads(page.body))
            out["suggested_kind"] = "json"
        except (ValueError, RecursionError):
            out["sample"] = sample_of(page)
        return out
    head = page.body[:2048].lstrip().lower()
    if b"<urlset" in head or b"<sitemapindex" in head:
        out["suggested_kind"] = "sitemap"
        out.update(_sitemap(page))
        return out
    if page.content_type.endswith("xml") or head.startswith(b"<?xml") or b"<rss" in head or b"<feed" in head:
        out["suggested_kind"] = "rss"
        out["sample"] = sample_of(page)
        return out
    if page.content_type == "text/calendar" or head.startswith(b"begin:vcalendar"):
        out["suggested_kind"] = "ical"
        out["sample"] = sample_of(page)
        return out
    try:
        root = html_document(page)
    except (etree.ParserError, ValueError):
        out["sample"] = sample_of(page)
        return out
    out["title"] = one_line(" ".join(root.xpath("//title//text()")), 200)
    out["feeds"] = _feeds(root, page)
    out["calendars"] = sorted(
        {urljoin(page.url, h) for h in root.xpath("//a/@href") if h.lower().split("?")[0].endswith(".ics")}
    )[:10]
    out["schema_org_types"] = schema_types(root)
    out["repeated_blocks"] = repeated_blocks(root)
    urls, out["scripts_read"], out["scripts_skipped"] = await _data_urls(fetcher, root, page, max_wait)
    out["data_urls"], out["data_urls_left_out"] = rank_data_urls(urls)
    out["wordpress_routes"] = await _wordpress(fetcher, page, max_wait)
    out["sitemaps"] = fetcher.sitemaps(page.url)
    out["sample"] = sample_of(page)
    return out


def _sitemap(page: Page) -> dict[str, Any]:
    """A sitemap's size and newest pages, or an index's sitemaps."""
    try:
        sitemap = read_sitemap(None, page)
    except ExtractError as e:
        return {"error": str(e)}
    if sitemap.children:
        return {"sitemap_index": sitemap.children}
    return {"sitemap_pages": sitemap.total, "newest": [{"url": u, "lastmod": m} for u, m in sitemap.entries[:10]]}


def rank_data_urls(urls: list[str]) -> tuple[list[str], int]:
    """Data URLs as found in script text, cleaned (JSON-escaped slashes, trailing
    backslashes), without script code or account-like ones, those that look like
    listings first; and how many were left out."""
    kept: dict[str, None] = {}
    left_out = 0
    for raw in urls:
        url = raw.replace("\\/", "/").rstrip("\\")
        if NOT_A_URL.search(url):
            continue
        try:
            path = urlsplit(url).path
        except ValueError:
            continue  # not a URL after all, such as a broken [host]
        if ACCOUNT_SEGMENTS & set(path.lower().split("/")):
            left_out += 1
            continue
        kept[url] = None
    ranked = sorted(kept, key=lambda u: 0 if ROUTE_HINT.search(urlsplit(u).path) else 1)
    return ranked[:MAX_CANDIDATES], left_out


def _first_class(element: Any) -> str:
    for name in (element.get("class") or "").split():
        if CLASS_NAME.fullmatch(name):
            return name
    return ""


def _outside_content(element: Any) -> bool:
    return any(a.tag in OUTSIDE_CONTENT for a in element.iterancestors() if isinstance(a.tag, str))


def repeated_blocks(root: Any) -> list[dict[str, Any]]:
    """Elements that repeat with one class, each with a link: the cards of a listing,
    offered as extract.items for kind html, with how many have a link, a date-like
    text and a heading, and a sample. Most dated first."""
    groups: dict[str, list[Any]] = {}
    for element in root.iter():
        if not isinstance(element.tag, str) or element.tag in OUTSIDE_CONTENT:
            continue
        name = _first_class(element)
        if name:
            groups.setdefault(f"{element.tag}.{name}", []).append(element)
    found = []
    for selector, elements in groups.items():
        if len(elements) < MIN_BLOCK_REPEATS or _outside_content(elements[0]):
            continue
        # Text nodes joined with spaces: "<h3>Band</h3><p>21.11</p>" reads "Band 21.11".
        texts = [" ".join(" ".join(e.itertext()).split()) for e in elements]
        linked = [e for e in elements if e.tag == "a" and e.get("href") or e.xpath(".//a[@href]")]
        if len(linked) * 2 < len(elements) or not any(10 <= len(t) <= 2000 for t in texts):
            continue
        dated = sum(1 for e, t in zip(elements, texts, strict=True) if DATE_TEXT.search(t) or e.xpath(".//time"))
        headings = [h.tag for e in elements[:1] for h in e.xpath(".//h1|.//h2|.//h3|.//h4|.//h5|.//h6")]
        found.append(
            {
                "items": selector,
                "count": len(elements),
                "with_link": round(len(linked) / len(elements), 2),
                "with_date": round(dated / len(elements), 2),
                **({"heading": headings[0]} if headings else {}),
                "sample": one_line(texts[0], 160),
            }
        )
    found.sort(key=lambda b: (b["with_date"] >= 0.5, b["with_link"], b["count"]), reverse=True)
    return found[:MAX_BLOCKS]


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
            if len(found) >= MAX_GATHERED:
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
