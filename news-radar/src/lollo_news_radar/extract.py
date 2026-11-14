"""From a fetched page to items, one function per kind, then the same clean-up for
all: text made one line and capped, URLs absolute and canonical, dates parsed, a
stable key. Nothing here runs code from the page."""

from __future__ import annotations

import calendar
import hashlib
import html
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from itertools import zip_longest
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

import feedparser
import icalendar
import jmespath
import lxml.html
from lollo_common.text import one_line
from lxml import etree

from .config import MAX_FIELD, MAX_INDEX_FILES, MAX_SUMMARY, MAX_URL, SITEMAPS_READ
from .fetch import Page
from .spec import ITEM_FIELDS, Spec, split_selector

# Query parameters that only track a click.
TRACKING = re.compile(r"(utm_[a-z]+|fbclid|gclid|mc_cid|mc_eid|_hsenc|_hsmi|igshid)")
URL_FIELDS = ("url", "image")
DATE_FIELDS = ("start", "end", "published")
ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?")
OUTLET_SUFFIX = re.compile(r"\s+[-|\u2013\u2014]\s+([^-|\u2013\u2014]{2,60})$")


class ExtractError(Exception):
    pass


@dataclass
class Item:
    key: str
    fields: dict[str, str]
    extra: dict[str, str] = field(default_factory=dict)

    def watched(self, names: tuple[str, ...]) -> str:
        """The watched fields, as one string to compare across runs."""
        values = {n: self.fields.get(n) or self.extra.get(n, "") for n in names}
        return json.dumps(values, sort_keys=True)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {k: v for k, v in self.fields.items() if v}
        if self.extra:
            out["extra"] = {k: v for k, v in self.extra.items() if v}
        return out


# -- values ---------------------------------------------------------------------
def _scalar(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, str | int | float):
        return str(value)
    if isinstance(value, dict):
        # A schema.org object (a Place, an ImageObject...): its name or URL.
        for k in ("name", "url", "@id", "text"):
            if isinstance(value.get(k), str):
                return value[k]
        return ""
    if isinstance(value, list):
        return next((s for s in (_scalar(v) for v in value) if s), "")
    return ""


def canonical_url(href: str, base: str) -> str:
    """Absolute, without fragment or tracking parameters; "" unless http(s)."""
    try:
        parts = urlsplit(urljoin(base, href.strip()))
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING.fullmatch(k)])
    url = urlunsplit((parts.scheme, parts.netloc.lower(), parts.path or "/", query, ""))
    return url if len(url) <= MAX_URL else ""


# A date written without a year is the one in the year from YEARLESS_PAST_DAYS back
# to the rest of a year ahead: a listing's yearless dates are upcoming, or ongoing
# since a little while.
YEARLESS_PAST_DAYS = 90
YEAR = re.compile(r"\b\d{4}\b")
# Read again from this much earlier, a yearless date keeps its day and month and a
# relative one ("2 hours ago", "há 4 meses") does not: not a whole number of years.
REREAD_SHIFT = timedelta(days=400)


def parse_date(value: str, languages: tuple[str, ...], now: datetime | None = None) -> str:
    """ISO 8601 ("2026-11-08" or "2026-11-08T21:00", with the offset when given),
    or "" when the text is not a date this can read. Relative dates count from
    `now` (default: the real now). A date without a year ("27 Outubro", "sáb 14
    nov") is the nearest from YEARLESS_PAST_DAYS before `now` onwards."""
    text = value.strip()
    if not text:
        return ""
    if ISO_DATE.fullmatch(text):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if len(text) == 10:
                return parsed.date().isoformat()
            return parsed.isoformat(timespec="minutes")
    now = now or datetime.now()
    parsed = _dateparser(text, languages, now)
    if parsed is None:
        return ""
    if not YEAR.search(text):
        earlier = _dateparser(text, languages, now - REREAD_SHIFT)
        if earlier is not None and earlier != parsed and earlier.timetuple()[1:6] == parsed.timetuple()[1:6]:
            parsed = _nearest_year(parsed, now.date())
    if parsed.hour == 0 and parsed.minute == 0 and not re.search(r"\d:\d", text):
        return parsed.date().isoformat()
    return parsed.isoformat(timespec="minutes")


def _dateparser(text: str, languages: tuple[str, ...], base: datetime) -> datetime | None:
    import dateparser  # slow to import; only text dates need it

    settings = {"RETURN_AS_TIMEZONE_AWARE": False, "PREFER_DAY_OF_MONTH": "first", "RELATIVE_BASE": base}
    return dateparser.parse(text[:120], languages=list(languages) or None, settings=settings)


def _nearest_year(parsed: datetime, today: date) -> datetime:
    """A yearless date moved to the year that puts it from YEARLESS_PAST_DAYS before
    today to the rest of a year after."""
    years = 0
    if parsed.date() < today - timedelta(days=YEARLESS_PAST_DAYS):
        years = 1
    elif parsed.date() >= today + timedelta(days=365 - YEARLESS_PAST_DAYS):
        years = -1
    try:
        return parsed.replace(year=parsed.year + years)
    except ValueError:  # 29 February
        return parsed


def _extra_scalar(value: Any) -> str:
    """An extra's text. True and false are kept, as "true" and "false", so a flag
    such as "in theatres" can be watched and matched; item fields still drop them
    (a flag is never a title, a date or a link)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return _scalar(value)


def _date_value(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="minutes")
    if isinstance(value, date):
        return value.isoformat()
    return _scalar(value)


def fold(text: str) -> str:
    """Text compared without case or accents: "Cuarón" and "CUARON" are the same."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def outlet_of(title: str) -> tuple[str, str]:
    """A news title "Headline - Outlet" as (headline, outlet); outlet "" when there is none."""
    m = OUTLET_SUFFIX.search(title)
    if not m:
        return title, ""
    return title[: m.start()].strip(), m.group(1).strip()


# Kinds whose text comes as the site stored it, HTML codes and tags included (JSON
# APIs, JSON-LD); html, rss and ical text is already decoded.
ENCODED_KINDS = ("json", "schema_org", "sitemap")
MARKUP = re.compile(r"<[A-Za-z/!][^>]*>")


def _plain(text: str) -> str:
    """Text without HTML tags or codes: "<p>Doors &amp; more</p>" is "Doors & more"."""
    if MARKUP.search(text):
        try:
            return lxml.html.fromstring(text).text_content()
        except (etree.ParserError, ValueError):
            pass
    return html.unescape(text)


def normalize(raw: dict[str, Any], extra: dict[str, Any], spec: Spec, base: str) -> Item | None:
    """One item, cleaned; None without a title."""
    decode = _plain if spec.kind in ENCODED_KINDS else (lambda text: text)
    fields: dict[str, str] = {}
    for name in ITEM_FIELDS:
        value = raw.get(name)
        if name in DATE_FIELDS:
            text = _date_value(value)
            fields[name] = parse_date(text, spec.languages) if text else ""
        elif name in URL_FIELDS:
            fields[name] = canonical_url(_scalar(value), base) if _scalar(value) else ""
        else:
            fields[name] = one_line(decode(_scalar(value)), MAX_SUMMARY if name == "summary" else MAX_FIELD)
    if not fields["title"]:
        return None
    if spec.item_kind == "article" and not fields["outlet"]:
        headline, outlet = outlet_of(fields["title"])
        if outlet:
            fields["outlet"] = outlet
            fields["title"] = headline
    extra_out = {k: one_line(decode(_extra_scalar(v)), MAX_FIELD) for k, v in extra.items()}
    key_raw = raw.get("_key")
    if spec.key:
        parts = [fields.get(k) or extra_out.get(k, "") for k in spec.key]
        key_text = "\x1f".join(parts)
    elif isinstance(key_raw, str) and key_raw.strip():
        key_text = key_raw.strip()
    elif fields["url"]:
        key_text = fields["url"]
    else:
        key_text = fields["title"] + "\x1f" + fields["start"]
    key = hashlib.sha256(key_text.encode("utf-8", errors="replace")).hexdigest()[:32]
    return Item(key, fields, extra_out)


# -- kinds ----------------------------------------------------------------------
def _json(spec: Spec, page: Page) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    try:
        data = json.loads(page.body)
    except (ValueError, RecursionError) as e:
        raise ExtractError("the response is not JSON (a challenge page, or the wrong URL?)") from e
    found = jmespath.search(spec.extract["items"], data)
    if found is None:
        return []
    if not isinstance(found, list):
        raise ExtractError("extract.items did not select a list")
    out = []
    for entry in found:
        raw = {name: jmespath.search(expr, entry) for name, expr in spec.extract["fields"].items()}
        extra = {name: jmespath.search(expr, entry) for name, expr in spec.extract["extra"].items()}
        out.append((raw, extra))
    return out


XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>")


def html_document(page: Page) -> Any:
    """The page parsed in its encoding: the header's charset, else UTF-8 when the
    bytes are UTF-8, else what the page itself declares (lxml reads its meta tag).
    Python decodes, not libxml2, which lacks some of Python's codecs; lxml refuses
    decoded text with an XML declaration, so one is dropped. Raises
    etree.ParserError or ValueError for what is not HTML."""
    if page.charset:
        text = page.body.decode(page.charset, errors="replace")
    else:
        try:
            text = page.body.decode("utf-8")
        except UnicodeDecodeError:
            return lxml.html.document_fromstring(page.body, base_url=page.url)
    return lxml.html.document_fromstring(XML_DECLARATION.sub("", text, count=1), base_url=page.url)


def _html_root(page: Page) -> Any:
    if not page.body.strip():
        raise ExtractError("the page is empty")
    try:
        return html_document(page)
    except (etree.ParserError, ValueError) as e:
        raise ExtractError("the page is not HTML") from e


def _select(element: Any, selector: str) -> str:
    css, attr = split_selector(selector)
    targets = element.cssselect(css) if css else [element]
    if not targets:
        return ""
    target = targets[0]
    if attr:
        return target.get(attr, "") or ""
    return " ".join(target.text_content().split())


def _html(spec: Spec, page: Page) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    root = _html_root(page)
    out = []
    for element in root.cssselect(spec.extract["items"]):
        raw = {name: _select(element, sel) for name, sel in spec.extract["fields"].items()}
        extra = {name: _select(element, sel) for name, sel in spec.extract["extra"].items()}
        out.append((raw, extra))
    return out


def _types(node: dict[str, Any]) -> list[str]:
    t = node.get("@type")
    names = t if isinstance(t, list) else [t]
    return [n.rsplit("/", 1)[-1] for n in names if isinstance(n, str)]


def _walk_jsonld(node: Any, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 6:
        return []
    if isinstance(node, list):
        return [n for child in node for n in _walk_jsonld(child, depth + 1)]
    if not isinstance(node, dict):
        return []
    out = [node] if _types(node) else []
    for key in ("@graph", "itemListElement", "item", "subEvent", "mainEntity"):
        if key in node:
            out += _walk_jsonld(node[key], depth + 1)
    return out


def _schema_fields(node: dict[str, Any]) -> dict[str, Any]:
    location = node.get("location")
    if isinstance(location, dict) and not location.get("name") and isinstance(location.get("address"), dict | str):
        location = location["address"]
    url = node.get("url") or (node.get("@id") if str(node.get("@id", "")).startswith("http") else "")
    return {
        "title": node.get("name") or node.get("headline"),
        "url": url,
        "start": node.get("startDate") or node.get("releaseDate"),
        "end": node.get("endDate"),
        "venue": location,
        "summary": node.get("description"),
        "image": node.get("image"),
        "published": node.get("datePublished"),
        "outlet": node.get("publisher"),
        "_key": node.get("@id") if isinstance(node.get("@id"), str) else None,
    }


def _jsonld_nodes(root: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for script in root.xpath('//script[@type="application/ld+json"]'):
        text = (script.text or "").strip()
        text = re.sub(r"^\s*(<!--|<!\[CDATA\[)|(-->|\]\]>)\s*$", "", text)
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            continue
        nodes += _walk_jsonld(data)
    return nodes


def _microdata_nodes(root: Any) -> list[dict[str, Any]]:
    nodes = []
    for scope in root.xpath("//*[@itemscope][@itemtype]"):
        node: dict[str, Any] = {"@type": scope.get("itemtype", "").split()[0] if scope.get("itemtype") else ""}
        for prop in scope.xpath(".//*[@itemprop]"):
            # Only the properties of this scope, not of one nested in it.
            owner = prop.getparent()
            while owner is not None and owner is not scope and owner.get("itemscope") is None:
                owner = owner.getparent()
            if owner is not scope:
                continue
            tag = prop.tag if isinstance(prop.tag, str) else ""
            if prop.get("itemscope") is not None:
                value: Any = {"name": " ".join(prop.text_content().split())}
            elif prop.get("content") is not None:
                value = prop.get("content")
            elif tag in ("a", "link", "area"):
                value = prop.get("href", "")
            elif tag in ("img", "audio", "video", "source", "iframe", "embed"):
                value = prop.get("src", "")
            elif tag == "time":
                value = prop.get("datetime") or prop.text_content()
            elif tag in ("meta", "data", "meter"):
                value = prop.get("value", "")
            else:
                value = " ".join(prop.text_content().split())
            for name in (prop.get("itemprop") or "").split():
                node.setdefault(name, value)
        nodes.append(node)
    return nodes


def schema_items(root: Any, types: list[str]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    wanted = set(types)
    out = []
    for node in _jsonld_nodes(root) + _microdata_nodes(root):
        if wanted & set(_types(node)):
            out.append((_schema_fields(node), {}))
    return out


def schema_types(root: Any) -> list[str]:
    """Every schema.org type a page describes (for source_inspect)."""
    found: dict[str, None] = {}
    for node in _jsonld_nodes(root) + _microdata_nodes(root):
        for t in _types(node):
            found[t] = None
    return list(found)


def _schema_org(spec: Spec, page: Page) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return schema_items(_html_root(page), spec.extract["types"])


# -- sitemaps ----------------------------------------------------------------------
# The newest entries of a sitemap kept per run: a site's sitemap can list tens of thousands.
MAX_SITEMAP_ITEMS = 200
# Parsing XML from the web: no entities, no DTD, no network.
SAFE_XML = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)


def _sitemap_root(page: Page) -> Any:
    if not page.body.strip():
        raise ExtractError("the sitemap is empty")
    try:
        return etree.fromstring(page.body, SAFE_XML)
    except (etree.XMLSyntaxError, ValueError) as e:
        raise ExtractError("the response is not a sitemap (gzipped sitemaps are not read)") from e


def _local(element: Any, name: str) -> str:
    found = element.xpath(f"./*[local-name()='{name}']/text()")
    return str(found[0]).strip() if found else ""


@dataclass
class Sitemap:
    # A sitemap index's sitemaps; empty for a sitemap of pages.
    children: list[str] = field(default_factory=list)
    # (url, lastmod) of the newest pages, at most MAX_SITEMAP_ITEMS.
    entries: list[tuple[str, str]] = field(default_factory=list)
    # How many pages it lists (that hold one of extract.match).
    total: int = 0


def read_sitemap(spec: Spec | None, page: Page) -> Sitemap:
    """A sitemap index's sitemaps, or the pages of a sitemap whose URL holds one of
    extract.match: newest first (undated ones last, in the sitemap's order), cut to
    MAX_SITEMAP_ITEMS before any date is parsed, since a sitemap can list tens of
    thousands. W3C dates sort as text."""
    root = _sitemap_root(page)
    name = etree.QName(root).localname
    if name == "sitemapindex":
        children = [u for u in (_local(e, "loc") for e in root.xpath("./*[local-name()='sitemap']")) if u]
        return Sitemap(children=children[:MAX_INDEX_FILES])
    if name != "urlset":
        raise ExtractError("the response is not a sitemap")
    match = (spec.extract.get("match") if spec else None) or []
    found = []
    for element in root.xpath("./*[local-name()='url']"):
        loc = _local(element, "loc")
        if loc and (not match or any(m in loc for m in match)):
            found.append((loc, _local(element, "lastmod"), len(found)))
    # Newest first: by lastmod, then, among equal or missing ones, the last listed
    # (sitemaps add pages at the end).
    found.sort(key=lambda e: (e[1], e[2]), reverse=True)
    entries = []
    for loc, lastmod, _ in found[:MAX_SITEMAP_ITEMS]:
        url = canonical_url(loc, page.url)
        if url:
            entries.append((url, parse_date(lastmod, ())))
    return Sitemap(entries=entries, total=len(found))


def sitemap_entries(spec: Spec | None, page: Page) -> list[tuple[str, str]]:
    """A sitemap's newest pages (read_sitemap); an index is an error that says what to do."""
    sitemap = read_sitemap(spec, page)
    if sitemap.children:
        raise ExtractError(
            "a sitemap index, not a sitemap: point the source at it with extract.sitemaps naming the files to "
            "read (source_inspect lists them as sitemap_families), or use one of its sitemaps as the url"
        )
    return sitemap.entries


GZIPPED = re.compile(r"\.gz$", re.I)
SITEMAP_FILE = re.compile(r"\.xml(\.gz)?$", re.I)
NUMBER = re.compile(r"[-_.]?(\d+)")


def sitemap_family(url: str) -> tuple[str, tuple[int, ...]]:
    """A sitemap file's family and its place in it, from its name: "events-sitemap2.xml"
    is ("events-sitemap", (2,)), "post-sitemap-2026-10.xml" ("post-sitemap", (2026, 10)),
    "events-sitemap.xml" ("events-sitemap", ()), the first of its family."""
    name = SITEMAP_FILE.sub("", urlsplit(url).path.rsplit("/", 1)[-1])
    return NUMBER.sub("", name), tuple(int(n) for n in NUMBER.findall(name))


def pick_sitemaps(spec: Spec, page: Page) -> list[list[str]]:
    """For each family in extract.sitemaps found in the index, its SITEMAPS_READ
    highest-numbered files (gzipped ones left out), newest first."""
    sitemap = read_sitemap(spec, page)
    if not sitemap.children:
        raise ExtractError("extract.sitemaps is for a sitemap index; this is a sitemap: drop it, or use the index")
    names = spec.extract["sitemaps"]
    readable = [u for u in sitemap.children if not GZIPPED.search(urlsplit(u).path)]
    chosen = [u for u in readable if sitemap_family(u)[0] in names]
    if not chosen:
        families = ", ".join(dict.fromkeys(sitemap_family(u)[0] for u in readable[:200]))
        raise ExtractError(f"no sitemap family {', '.join(names)} in the index; its families are {families}")
    chosen.sort(key=lambda u: sitemap_family(u)[1])  # stable: the index's order among equals
    families = []
    for name in dict.fromkeys(names):
        files = [u for u in chosen if sitemap_family(u)[0] == name]
        if files:
            families.append(files[-SITEMAPS_READ:][::-1])
    return families


def combine_entries(families: list[list[list[tuple[str, str]]]]) -> list[tuple[str, str]]:
    """Sitemaps' entries (each file's newest first; each family's files newest first)
    as one list, each page once, at most MAX_SITEMAP_ITEMS: a family's newer file
    before its older one, whatever their lastmods (a new file may have none), and
    the families in turn, so that none crowds out another."""
    queues = [[entry for entries in files for entry in entries] for files in families]
    out: dict[str, str] = {}
    for row in zip_longest(*queues):
        for entry in row:
            if entry is not None:
                out.setdefault(*entry)
    return list(out.items())[:MAX_SITEMAP_ITEMS]


def sitemap_items(entries: list[tuple[str, str]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """A sitemap's pages as raw items: titled from their URL, dated by their lastmod."""
    return [({"title": slug_title(url), "url": url, "published": lastmod}, {}) for url, lastmod in entries]


def slug_title(url: str) -> str:
    """A page's title guessed from its URL: "/e/the-band-tour-2027/" is "the band tour 2027"."""
    segments = [p for p in urlsplit(url).path.split("/") if p]
    if not segments:
        return ""
    slug = re.sub(r"\.(html?|php|aspx?)$", "", unquote(segments[-1]), flags=re.I)
    return " ".join(re.split(r"[-_+]+", slug)).strip()


def _sitemap(spec: Spec, page: Page) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return sitemap_items(sitemap_entries(spec, page))


def follow_links(spec: Spec, page: Page) -> list[str]:
    """For follow: the item pages a listing links to, or a sitemap lists, in order."""
    assert spec.follow is not None
    if spec.kind == "sitemap":
        return [url for url, _ in sitemap_entries(spec, page)]
    css, attr = split_selector(spec.follow["links"])
    root = _html_root(page)
    urls: dict[str, None] = {}
    for element in root.cssselect(css):
        url = canonical_url(element.get(attr or "href", ""), page.url)
        if url:
            urls[url] = None
    return list(urls)


def _struct_time(value: Any) -> str:
    if not value:
        return ""
    return datetime.fromtimestamp(calendar.timegm(value), tz=UTC).isoformat(timespec="minutes")


def _rss(spec: Spec, page: Page) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    # The header's charset wins over the feed's own declaration, as it does for a browser.
    headers = {"content-type": f"{page.content_type or 'application/xml'}; charset={page.charset}"}
    feed = feedparser.parse(
        page.body, sanitize_html=True, resolve_relative_uris=False, response_headers=headers if page.charset else {}
    )
    if feed.bozo and not feed.entries:
        raise ExtractError("the response is not an RSS or Atom feed")
    out = []
    for entry in feed.entries:
        summary = entry.get("summary", "")
        if summary and "<" in summary:
            try:
                summary = lxml.html.fromstring(summary).text_content()
            except (etree.ParserError, ValueError):
                summary = ""
        source = entry.get("source") or {}
        raw = {
            "title": entry.get("title", ""),
            "url": entry.get("link", ""),
            "summary": summary,
            "published": _struct_time(entry.get("published_parsed") or entry.get("updated_parsed")),
            "outlet": source.get("title", "") if isinstance(source, dict) else "",
            "_key": entry.get("id") or entry.get("guid"),
        }
        if spec.item_kind == "event":
            raw["start"] = raw["published"]
        out.append((raw, {}))
    return out


def _ical_time(value: Any) -> Any:
    return getattr(value, "dt", value) if value is not None else None


def _ical(spec: Spec, page: Page) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    try:
        cal = icalendar.Calendar.from_ical(page.body)
    except ValueError as e:
        raise ExtractError("the response is not an iCalendar feed") from e
    out = []
    for event in cal.walk("VEVENT"):
        raw = {
            "title": str(event.get("SUMMARY", "")),
            "url": str(event.get("URL", "")),
            "start": _ical_time(event.get("DTSTART")),
            "end": _ical_time(event.get("DTEND")),
            "venue": str(event.get("LOCATION", "")),
            "summary": str(event.get("DESCRIPTION", "")),
            "_key": str(event.get("UID", "")) or None,
        }
        out.append((raw, {}))
    return out


KIND_EXTRACTORS = {
    "json": _json,
    "html": _html,
    "schema_org": _schema_org,
    "rss": _rss,
    "ical": _ical,
    "sitemap": _sitemap,
}


def items_of(raws: list[tuple[dict[str, Any], dict[str, Any]]], spec: Spec, base: str) -> list[Item]:
    """Raw items cleaned, in order; a repeated key counts once."""
    items: dict[str, Item] = {}
    for raw, extra in raws:
        item = normalize(raw, extra, spec, base)
        if item is not None and item.key not in items:
            items[item.key] = item
    return list(items.values())


def extract(spec: Spec, page: Page) -> list[Item]:
    """Items of one page, in page order; a repeated key counts once."""
    return items_of(KIND_EXTRACTORS[spec.kind](spec, page), spec, page.url)


def fill_rates(items: list[Item], spec: Spec) -> dict[str, float]:
    """For each field the spec yields, the share of items that have it."""
    names = [n for n in ITEM_FIELDS if any(i.fields.get(n) for i in items)]
    if spec.kind in ("json", "html"):
        names = list(dict.fromkeys([*spec.extract["fields"], *spec.extra_names]))
    if not items:
        return {n: 0.0 for n in names}
    return {n: round(sum(1 for i in items if i.fields.get(n) or i.extra.get(n)) / len(items), 2) for n in names}


# -- what a run's items say about the source ----------------------------------------
# Upcoming events spanning fewer days than this, in a listing this long, look cut short.
SHORT_WINDOW_DAYS = 14
SHORT_WINDOW_ITEMS = 10


def _required(spec: Spec) -> tuple[str, ...]:
    return ("title", "start") if spec.item_kind == "event" else ("title",)


def _dated(spec: Spec) -> bool:
    """Whether start is when the event happens: an RSS item's start is when it was posted."""
    return spec.item_kind == "event" and spec.kind != "rss"


def _whole_listing(spec: Spec) -> bool:
    """Whether a run's items are the whole listing: a followed one's are only the
    pages read in that run, which can be old ones read again."""
    return spec.follow is None


def run_problem(items: list[Item], spec: Spec, today: date) -> str:
    """Why a run's items would make its source unhealthy, or "": the same rule for
    source_test and the scheduled runs. An empty run is judged against earlier runs
    (Store.record_run), not here."""
    if not items:
        return ""
    for f in _required(spec):
        missing = sum(1 for i in items if not i.fields.get(f))
        if missing * 2 > len(items):
            return f"{missing} of {len(items)} items have no {f}"
    if _dated(spec) and _whole_listing(spec):
        ends = [(i.fields.get("end") or i.fields["start"])[:10] for i in items if i.fields.get("start")]
        if ends and max(ends) < today.isoformat():
            return (
                f"every event is in the past (the latest on {max(ends)}): a date fixed in the URL, "
                "or a listing the site moved"
            )
    return ""


def window_warning(items: list[Item], spec: Spec, today: date) -> str:
    """A listing whose upcoming events span only a few days is often cut by a page
    size; "" when it looks whole (or is too short to tell, or is only the pages a
    followed listing's run read)."""
    if not _dated(spec) or not _whole_listing(spec):
        return ""
    starts = sorted(s[:10] for i in items if (s := i.fields.get("start", "")) and s[:10] >= today.isoformat())
    if len(starts) < SHORT_WINDOW_ITEMS:
        return ""
    try:
        span = (date.fromisoformat(starts[-1]) - date.fromisoformat(starts[0]) + timedelta(days=1)).days
    except ValueError:
        return ""
    if span >= SHORT_WINDOW_DAYS:
        return ""
    return (
        f"the upcoming events span only {span} days ({starts[0]} to {starts[-1]}): if the site lists further "
        "ahead, this request cuts its list. Add pages, ask for more per page, or a narrower list (one category)"
    )
