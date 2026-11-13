"""Source specs: declarative JSON an agent writes, validated here before anything
runs it. Never code: JMESPath expressions, CSS selectors and field names only."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

import jmespath
from cssselect import GenericTranslator, SelectorError

from .config import MAX_FOLLOW, MAX_PAGES, MAX_URL, NAME, parse_duration
from .fetch import FetchError, check_url

KINDS = ("rss", "json", "html", "schema_org", "ical", "sitemap")
ITEM_KINDS = ("event", "article")
ITEM_FIELDS = ("title", "url", "start", "end", "venue", "summary", "image", "published", "outlet")
EXTRA_NAME = re.compile(r"[a-z][a-z0-9_]{0,30}")
PARAM = re.compile(r"[A-Za-z0-9_.\[\]-]{1,40}")
LANGUAGE = re.compile(r"[a-z]{2,3}")
SCHEMA_TYPE = re.compile(r"[A-Za-z]{1,60}")
MAX_EXTRA = 10
MAX_MATCH = 10
MAX_MATCH_TEXT = 200
MAX_EXPRESSION = 300
MAX_EVERY = 7 * 86400

# schema.org's Event and its subtypes, then the other types kept by default.
EVENT_TYPES = (
    "Event",
    "BusinessEvent",
    "ChildrensEvent",
    "ComedyEvent",
    "CourseInstance",
    "DanceEvent",
    "DeliveryEvent",
    "EducationEvent",
    "EventSeries",
    "ExhibitionEvent",
    "Festival",
    "FoodEvent",
    "Hackathon",
    "LiteraryEvent",
    "MusicEvent",
    "PublicationEvent",
    "SaleEvent",
    "ScreeningEvent",
    "SocialEvent",
    "SportsEvent",
    "TheaterEvent",
    "VisualArtsEvent",
)
DEFAULT_SCHEMA_TYPES = (*EVENT_TYPES, "Movie", "NewsArticle")
DEFAULT_WATCH = ("title", "start", "venue")


class SpecError(ValueError):
    pass


# Fields that belong inside another one, for the error that says where.
PLACES = {
    **{f: f"extract.{f}" for f in ("types", "items", "fields", "extra", "match", "key")},
    "languages": "dates.languages",
    "links": "follow.links",
    "param": "pages.param",
}


@dataclass(frozen=True)
class Spec:
    name: str
    url: str
    kind: str
    item_kind: str
    every: int
    extract: dict[str, Any]
    pages: dict[str, int | str] | None = None
    follow: dict[str, Any] | None = None
    languages: tuple[str, ...] = ()
    watch: tuple[str, ...] = DEFAULT_WATCH
    key: tuple[str, ...] = ()
    # A field or extra whose value names one item seen several times (a film in
    # several formats): a report shows such items as one row with its variants.
    group_by: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def fingerprint(self) -> str:
        """The same for two specs that read the same items, whatever their key order
        and however often they run: a passing source_test covers a new interval too."""
        what = {k: v for k, v in self.raw.items() if k != "every"}
        return hashlib.sha256(json.dumps(what, sort_keys=True).encode()).hexdigest()

    @property
    def extra_names(self) -> tuple[str, ...]:
        return tuple((self.extract.get("extra") or {}).keys())


def _text(obj: dict[str, Any], key: str, limit: int, required: bool = True) -> str:
    value = obj.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise SpecError(f"{key} must be a non-empty string")
    if len(value) > limit:
        raise SpecError(f"{key} is over {limit} characters")
    return value.strip()


def _jmespath(expr: Any, where: str) -> str:
    if not isinstance(expr, str) or not expr.strip() or len(expr) > MAX_EXPRESSION:
        raise SpecError(f"{where} must be a JMESPath expression of at most {MAX_EXPRESSION} characters")
    try:
        jmespath.compile(expr)
    except jmespath.exceptions.JMESPathError as e:
        raise SpecError(f"{where}: {e}") from e
    return expr


def split_selector(text: str) -> tuple[str, str]:
    """ "css@attr" -> (css, attr); "@attr" -> ("", attr); "css" -> (css, "")."""
    if "@" in text:
        css, attr = text.rsplit("@", 1)
        return css.strip(), attr.strip()
    return text.strip(), ""


def _css(expr: Any, where: str, allow_empty: bool = False) -> str:
    if not isinstance(expr, str) or len(expr) > MAX_EXPRESSION:
        raise SpecError(f"{where} must be a CSS selector of at most {MAX_EXPRESSION} characters")
    css, attr = split_selector(expr)
    if attr and not re.fullmatch(r"[A-Za-z_:][A-Za-z0-9_:.-]{0,60}", attr):
        raise SpecError(f"{where}: {attr!r} is not an attribute name")
    if not css:
        if allow_empty:
            return expr
        raise SpecError(f"{where} needs a CSS selector")
    try:
        GenericTranslator().css_to_xpath(css)
    except SelectorError as e:
        raise SpecError(f"{where}: {e}") from e
    return expr


def _mapping(obj: Any, where: str, check: Any, names: tuple[str, ...] | None) -> dict[str, str]:
    if obj is None:
        return {}
    if not isinstance(obj, dict) or len(obj) > (len(names) if names else MAX_EXTRA):
        raise SpecError(f"{where} must be an object of at most {len(names) if names else MAX_EXTRA} fields")
    out: dict[str, str] = {}
    for name, expr in obj.items():
        if names is not None and name not in names:
            raise SpecError(f"{where}: {name!r} is not one of {', '.join(names)}")
        if names is None and (not isinstance(name, str) or not EXTRA_NAME.fullmatch(name) or name in ITEM_FIELDS):
            raise SpecError(f"{where}: {name!r} must be lowercase letters, digits and _, not an item field")
        out[name] = check(expr, f"{where}.{name}")
    return out


def parse_spec(obj: Any, min_interval: int) -> Spec:
    """A validated Spec, or SpecError saying what is wrong."""
    if not isinstance(obj, dict):
        raise SpecError("a spec is a JSON object")
    allowed = {"name", "url", "kind", "item_kind", "every", "extract", "pages", "follow", "dates", "watch", "group_by"}
    unknown = sorted(set(obj) - allowed)
    if unknown:
        hints = [f"{u} (it goes inside {PLACES[u].split('.')[0]}: {PLACES[u]})" if u in PLACES else u for u in unknown]
        raise SpecError(f"unknown field(s): {', '.join(hints)}")
    name = _text(obj, "name", 64)
    if not NAME.fullmatch(name):
        raise SpecError("name must be lowercase letters, digits and -, starting with a letter or digit")
    url = _text(obj, "url", MAX_URL)
    try:
        check_url(url)
    except FetchError as e:
        raise SpecError(f"url: {e}") from e
    kind = obj.get("kind")
    if kind not in KINDS:
        raise SpecError(f"kind must be one of {', '.join(KINDS)}")
    # A sitemap's own entries have no event dates; the pages it follows do.
    plain = kind == "rss" or (kind == "sitemap" and obj.get("follow") is None)
    item_kind = obj.get("item_kind", "article" if plain else "event")
    if item_kind not in ITEM_KINDS:
        raise SpecError(f"item_kind must be one of {', '.join(ITEM_KINDS)}")
    try:
        every = parse_duration(str(obj.get("every", "6h")))
    except ValueError as e:
        raise SpecError(f"every: {e}") from e
    if every < min_interval:
        raise SpecError(f"every must be at least {min_interval // 60} minutes (the admin's minimum)")
    if every > MAX_EVERY:
        raise SpecError("every must be at most 7d")

    extract_in = obj.get("extract") or {}
    if not isinstance(extract_in, dict):
        raise SpecError("extract must be an object")
    extract: dict[str, Any] = {}
    allowed_extract = {
        "rss": {"key"},
        "ical": {"key"},
        "schema_org": {"types", "key"},
        "sitemap": {"match", "types", "key"},
    }.get(kind, {"items", "fields", "extra", "key"})
    unknown = sorted(set(extract_in) - allowed_extract)
    if unknown:
        raise SpecError(f"extract: {', '.join(unknown)} not used by kind {kind}")
    if kind == "json":
        extract["items"] = _jmespath(extract_in.get("items"), "extract.items")
        extract["fields"] = _mapping(extract_in.get("fields"), "extract.fields", _jmespath, ITEM_FIELDS)
        extract["extra"] = _mapping(extract_in.get("extra"), "extract.extra", _jmespath, None)
    elif kind == "html":
        extract["items"] = _css(extract_in.get("items"), "extract.items")
        if split_selector(extract["items"])[1]:
            raise SpecError("extract.items selects elements, without @attr")
        check = lambda e, w: _css(e, w, allow_empty=True)  # noqa: E731
        extract["fields"] = _mapping(extract_in.get("fields"), "extract.fields", check, ITEM_FIELDS)
        extract["extra"] = _mapping(extract_in.get("extra"), "extract.extra", check, None)
    if kind in ("json", "html") and "title" not in extract["fields"]:
        raise SpecError("extract.fields needs at least title")
    if kind == "sitemap" and extract_in.get("match") is not None:
        match = extract_in["match"]
        if (
            not isinstance(match, list)
            or not 1 <= len(match) <= MAX_MATCH
            or not all(isinstance(m, str) and 0 < len(m.strip()) <= MAX_MATCH_TEXT for m in match)
        ):
            raise SpecError(
                f"extract.match is a list of 1 to {MAX_MATCH} pieces of URL text, such as /events/ (plain text, "
                "not a pattern): only the sitemap's URLs holding one are kept"
            )
        extract["match"] = [m.strip() for m in match]
    if kind in ("schema_org", "sitemap"):
        types = extract_in.get("types", list(DEFAULT_SCHEMA_TYPES))
        if not isinstance(types, list) or not 1 <= len(types) <= 40:
            raise SpecError("extract.types must be a list of 1 to 40 schema.org types")
        if not all(isinstance(t, str) and SCHEMA_TYPE.fullmatch(t) for t in types):
            raise SpecError("extract.types holds schema.org type names, such as MusicEvent")
        extract["types"] = list(dict.fromkeys(types))

    extra_names = tuple(extract.get("extra", {}).keys())
    key_in = extract_in.get("key")
    key: tuple[str, ...] = ()
    if key_in is not None:
        if not isinstance(key_in, list) or not 1 <= len(key_in) <= 3:
            raise SpecError("extract.key must be a list of 1 to 3 field names")
        for k in key_in:
            if k not in ITEM_FIELDS and k not in extra_names:
                raise SpecError(f"extract.key: {k!r} is not a field")
        key = tuple(key_in)
        extract["key"] = list(key)

    pages = None
    if obj.get("pages") is not None:
        p = obj["pages"]
        if not isinstance(p, dict) or set(p) - {"param", "start", "step", "max"}:
            raise SpecError("pages takes param, start, step and max")
        param = p.get("param")
        if not isinstance(param, str) or not PARAM.fullmatch(param):
            raise SpecError("pages.param is the query parameter that selects the page")
        start, step, most = p.get("start", 1), p.get("step", 1), p.get("max", 3)
        for label, value, low, high in (
            ("start", start, 0, 100000),
            ("step", step, 1, 1000),
            ("max", most, 1, MAX_PAGES),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise SpecError(f"pages.{label} must be a whole number from {low} to {high}")
        if kind in ("rss", "ical", "sitemap"):
            raise SpecError(f"pages is not used by kind {kind}")
        pages = {"param": param, "start": start, "step": step, "max": most}

    follow = None
    if obj.get("follow") is not None:
        f = obj["follow"]
        if kind not in ("schema_org", "sitemap"):
            raise SpecError(
                "follow is for kind schema_org (listings whose events are described on their own pages) and "
                "sitemap (its pages read for schema.org data)"
            )
        if kind == "sitemap":
            if not isinstance(f, dict) or set(f) - {"max"}:
                raise SpecError("a sitemap's follow takes only max: the pages are the sitemap's URLs")
        elif not isinstance(f, dict) or set(f) - {"links", "max"}:
            raise SpecError("follow takes links (a CSS selector, @href by default) and max")
        most = f.get("max", MAX_FOLLOW)
        if not isinstance(most, int) or isinstance(most, bool) or not 1 <= most <= MAX_FOLLOW:
            raise SpecError(f"follow.max must be a whole number from 1 to {MAX_FOLLOW}")
        follow = {"max": most} if kind == "sitemap" else {"links": _css(f.get("links"), "follow.links"), "max": most}

    languages: tuple[str, ...] = ()
    if obj.get("dates") is not None:
        d = obj["dates"]
        if not isinstance(d, dict) or set(d) - {"languages"}:
            raise SpecError("dates takes languages")
        langs = d.get("languages", [])
        if (
            not isinstance(langs, list)
            or len(langs) > 5
            or not all(isinstance(x, str) and LANGUAGE.fullmatch(x) for x in langs)
        ):
            raise SpecError("dates.languages is a list of up to 5 language codes, such as pt or en")
        languages = tuple(langs)

    watch = DEFAULT_WATCH
    if obj.get("watch") is not None:
        w = obj["watch"]
        if not isinstance(w, list) or not 1 <= len(w) <= 10:
            raise SpecError("watch must be a list of 1 to 10 field names")
        for name_ in w:
            if name_ not in ITEM_FIELDS and name_ not in extra_names:
                raise SpecError(f"watch: {name_!r} is not a field")
        watch = tuple(dict.fromkeys(w))

    group_by = obj.get("group_by")
    if group_by is not None and (
        not isinstance(group_by, str) or (group_by not in ITEM_FIELDS and group_by not in extra_names)
    ):
        raise SpecError(f"group_by: {group_by!r:.40} is not a field or extra of this source")

    raw: dict[str, Any] = {
        "name": name,
        "url": url,
        "kind": kind,
        "item_kind": item_kind,
        "every": str(obj.get("every", "6h")).strip(),
        "extract": extract,
    }
    if pages:
        raw["pages"] = pages
    if follow:
        raw["follow"] = follow
    if languages:
        raw["dates"] = {"languages": list(languages)}
    if watch != DEFAULT_WATCH:
        raw["watch"] = list(watch)
    if group_by:
        raw["group_by"] = group_by
    return Spec(name, url, kind, item_kind, every, extract, pages, follow, languages, watch, key, group_by or "", raw)
