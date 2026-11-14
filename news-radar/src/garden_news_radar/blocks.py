"""Repeated blocks in a page: elements repeated with one class, each with a link,
the likely cards of a listing. source_inspect offers them as extract.items for
kind html, and source_test compares them with the items a spec reads."""

from __future__ import annotations

import re
from typing import Any

from garden_common.text import one_line

from .extract import fold

# Repeated blocks offered for kind html.
MAX_BLOCKS = 5
MIN_BLOCK_REPEATS = 3
CLASS_NAME = re.compile(r"-?[_a-zA-Z][_a-zA-Z0-9-]*")
# Month names, full and short, in the languages listings are most often in (pt, en,
# es, fr, de, it), accents left out: text is folded before it is matched.
MONTHS = (
    "janeiro|fevereiro|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro|"
    "january|february|march|april|may|june|july|august|september|october|november|december|"
    "enero|febrero|marzo|mayo|junio|julio|septiembre|setiembre|octubre|noviembre|diciembre|"
    "janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre|novembre|decembre|"
    "januar|februar|marz|juni|juli|oktober|dezember|"
    "gennaio|febbraio|aprile|maggio|giugno|luglio|settembre|ottobre|dicembre|"
    "jan|fev|feb|mar|abr|apr|mai|may|jun|jul|ago|aug|set|sep|sept|out|oct|okt|nov|dez|dec|dic|ene|avr|gen|giu|lug"
)
DATE_TEXT = re.compile(
    r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b|\b\d{4}-\d{2}-\d{2}\b"
    rf"|\b\d{{1,2}}\.?(?:\s+de)?\s+(?:{MONTHS})\b\.?"
    rf"|\b(?:{MONTHS})\.?\s+\d{{1,2}}\b"
)
OUTSIDE_CONTENT = {"nav", "header", "footer", "script", "style", "noscript", "template", "select", "form"}


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
    text and a heading, how many different pages they link to (a calendar repeats
    one event in every day's cell: count is cells, links is events), and a sample.
    Most dated first, then the most links."""
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
        targets = {e.get("href") if e.tag == "a" and e.get("href") else e.xpath(".//a/@href")[0] for e in linked}
        if len(linked) * 2 < len(elements) or not any(10 <= len(t) <= 2000 for t in texts):
            continue
        dated = sum(1 for e, t in zip(elements, texts, strict=True) if DATE_TEXT.search(fold(t)) or e.xpath(".//time"))
        headings = [h.tag for e in elements[:1] for h in e.xpath(".//h1|.//h2|.//h3|.//h4|.//h5|.//h6")]
        found.append(
            {
                "items": selector,
                "count": len(elements),
                "links": len(targets),
                "with_link": round(len(linked) / len(elements), 2),
                "with_date": round(dated / len(elements), 2),
                **({"heading": headings[0]} if headings else {}),
                "sample": one_line(texts[0], 160),
            }
        )
    found.sort(key=lambda b: (b["with_date"] >= 0.5, b["with_link"], b["links"], b["count"]), reverse=True)
    return found[:MAX_BLOCKS]
