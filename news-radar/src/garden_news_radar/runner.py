"""One run of a source: its pages fetched (and, for follow, the item pages it
links to), its items extracted. Used by the scheduler and by source_test alike,
so a test runs exactly what the schedule will."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from garden_common.text import one_line
from lxml import etree

from .extract import (
    ExtractError,
    Item,
    extract,
    follow_links,
    html_document,
    normalize,
    schema_items,
    slug_title,
)
from .fetch import Fetcher, FetchError, Page
from .spec import Spec

ACCEPT = {
    "rss": "application/rss+xml, application/atom+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5",
    "json": "application/json, */*;q=0.5",
    "html": "text/html, application/xhtml+xml, */*;q=0.5",
    "schema_org": "text/html, application/xhtml+xml, */*;q=0.5",
    "ical": "text/calendar, */*;q=0.5",
    "sitemap": "application/xml, text/xml;q=0.9, */*;q=0.5",
}
SAMPLE = 600


@dataclass
class RunResult:
    items: list[Item] = field(default_factory=list)
    error: str = ""
    not_modified: bool = False
    etag: str = ""
    last_modified: str = ""
    sample: str = ""
    # Seconds the site asked to be left alone (a 429 or 503, or a pause still running).
    retry_after: int = 0
    pages: int = 0
    followed: int = 0
    follow_errors: int = 0

    @property
    def ok(self) -> bool:
        return not self.error


def page_url(url: str, param: str, value: int) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != param]
    query.append((param, str(value)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def sample_of(page: Page) -> str:
    """A short look at what was fetched, for an agent repairing a source."""
    if page.content_type in ("text/html", "application/xhtml+xml"):
        try:
            root = html_document(page)
            for bad in root.xpath("//script|//style|//noscript"):
                bad.drop_tree()
            return one_line(root.text_content(), SAMPLE)
        except (etree.ParserError, ValueError):
            pass
    return one_line(page.text(), SAMPLE)


async def run_source(
    spec: Spec,
    fetcher: Fetcher,
    *,
    etag: str = "",
    last_modified: str = "",
    known_url: Callable[[str], bool] = lambda url: False,
    max_pages: int | None = None,
    max_follow: int | None = None,
    max_wait: float | None = None,
) -> RunResult:
    """Never raises for what a site does: the result's error says why the run failed."""
    result = RunResult()
    accept = ACCEPT[spec.kind]
    # Conditional requests only make sense for a source that is a single request.
    conditional = spec.pages is None and spec.follow is None
    urls = [spec.url]
    if spec.pages:
        count = min(spec.pages["max"], max_pages or spec.pages["max"])
        start, step, param = spec.pages["start"], spec.pages["step"], str(spec.pages["param"])
        urls = [page_url(spec.url, param, int(start) + i * int(step)) for i in range(count)]
    items: dict[str, Item] = {}
    first: Page | None = None
    sitemap_links: list[str] = []
    for i, url in enumerate(urls):
        try:
            page = await fetcher.get(
                url,
                accept=accept,
                etag=etag if conditional else "",
                last_modified=last_modified if conditional else "",
                max_wait=max_wait,
            )
        except FetchError as e:
            if i == 0:
                result.error = str(e)
                result.retry_after = e.retry_after
                return result
            break  # a later page failing ends the paging, not the run
        result.pages += 1
        if page.not_modified:
            result.not_modified = True
            result.etag, result.last_modified = etag, last_modified
            return result
        if first is None:
            first = page
            result.sample = sample_of(page)
            result.etag, result.last_modified = page.etag, page.last_modified
        try:
            # A followed sitemap's items come from its pages; its own entries are only links,
            # read here once so that a page that is not a sitemap fails the run.
            if spec.kind == "sitemap" and spec.follow:
                found, sitemap_links = [], follow_links(spec, page)
            else:
                found = extract(spec, page)
        except ExtractError as e:
            if i == 0:
                result.error = str(e)
                return result
            break
        fresh = [it for it in found if it.key not in items]
        for it in fresh:
            items[it.key] = it
        if spec.pages and not fresh:
            break  # past the last page: nothing new on this one
    if spec.follow and first is not None:
        most = min(spec.follow["max"], max_follow or spec.follow["max"])
        try:
            listed = sitemap_links if spec.kind == "sitemap" else follow_links(spec, first)
            links = [u for u in listed if not known_url(u)][:most]
        except ExtractError:
            links = []
        for link in links:
            try:
                page = await fetcher.get(link, accept=ACCEPT["html"], max_wait=max_wait)
                root = html_document(page)
            except (FetchError, etree.ParserError, ValueError):
                result.follow_errors += 1
                continue
            result.followed += 1
            described = schema_items(root, spec.extract["types"])
            if not described and spec.kind == "sitemap":
                # A page without schema.org data still counts as seen: its title, or its URL's words.
                title = one_line(" ".join(root.xpath("//title//text()")), 300) or slug_title(link)
                described = [({"title": title, "url": link}, {})]
            for raw, extra in described:
                if not raw.get("url"):
                    raw["url"] = link
                item = normalize(raw, extra, spec, page.url)
                if item is not None and item.key not in items:
                    items[item.key] = item
    result.items = list(items.values())
    return result
