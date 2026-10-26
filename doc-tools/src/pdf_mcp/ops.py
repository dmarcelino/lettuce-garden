"""PDF operations done in-process with PyMuPDF and pypdf. Each takes the bytes
of an input the jail read and returns bytes (or named outputs) for it to write;
none of them touches the filesystem."""

from __future__ import annotations

import html
import io
import re
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pymupdf
from pypdf import PdfReader, PdfWriter
from pypdf.constants import UserAccessPermissions

pymupdf.TOOLS.mupdf_display_errors(False)
pymupdf.TOOLS.mupdf_display_warnings(False)


class OpError(Exception):
    """An operation that cannot be done on this input; safe to show the agent."""


PAGE_SIZES = {"A4": (595, 842), "LETTER": (612, 792), "A3": (842, 1191), "LEGAL": (612, 1008)}


@dataclass(frozen=True)
class Limits:
    max_pages: int
    max_render_dpi: int
    max_text_chars: int
    max_output_files: int


def open_pdf(data: bytes, password: str | None, limits: Limits) -> pymupdf.Document:
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception:
        raise OpError("not a readable PDF") from None
    if not doc.is_pdf:
        raise OpError("not a PDF")
    if doc.needs_pass:
        if not password:
            raise OpError("the PDF is encrypted; pass its password")
        if not doc.authenticate(password):
            raise OpError("wrong password for the PDF")
    if doc.page_count > limits.max_pages:
        raise OpError(f"the PDF has {doc.page_count} pages, more than the {limits.max_pages} limit")
    return doc


def page_numbers(spec: str | None, count: int) -> list[int]:
    """'1-3,5' (1-based, as people count) -> [0, 1, 2, 4]. None or 'all' -> every page."""
    if spec is None or spec.strip().lower() in ("", "all"):
        return list(range(count))
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+|end))?", part)
        if not m:
            raise OpError(f"bad page range {part!r}; use e.g. '1-3,5' or '2-end'")
        a = int(m.group(1))
        b = a if m.group(2) is None else (count if m.group(2) == "end" else int(m.group(2)))
        if not 1 <= a <= b <= count:
            raise OpError(f"page range {part!r} is outside 1-{count}")
        out.extend(range(a - 1, b))
    return list(dict.fromkeys(out))


def info(data: bytes, password: str | None, limits: Limits) -> dict[str, Any]:
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception:
        raise OpError("not a readable PDF") from None
    with doc:
        out: dict[str, Any] = {"encrypted": bool(doc.is_encrypted), "needs_password": bool(doc.needs_pass)}
        if doc.needs_pass and not (password and doc.authenticate(password)):
            out["page_count"] = None
            return out
        sizes: dict[str, int] = {}
        chars = images = 0
        sample = min(doc.page_count, 50)
        for i in range(doc.page_count):
            p = doc[i]
            key = f"{round(p.rect.width)}x{round(p.rect.height)}pt"
            sizes[key] = sizes.get(key, 0) + 1
            if i < sample:
                chars += len(p.get_text().strip())
                images += len(p.get_images())
        meta = {k: v for k, v in (doc.metadata or {}).items() if v}
        out.update(
            page_count=doc.page_count,
            page_sizes=sizes,
            metadata=meta,
            pdf_version=meta.get("format"),
            has_form=doc.is_form_pdf is not False and bool(doc.is_form_pdf),
            has_text=chars > 0,
        )
        if chars == 0 and images > 0:
            out["note"] = "no embedded text, only images: the pages are probably scanned; text extraction needs OCR, which these tools do not do"
        return out


def extract_text(data: bytes, pages: str | None, password: str | None, limits: Limits) -> tuple[str, bool, list[int]]:
    with open_pdf(data, password, limits) as doc:
        nums = page_numbers(pages, doc.page_count)
        parts, total, truncated = [], 0, False
        for i in nums:
            t = f"--- page {i + 1} ---\n{doc[i].get_text()}"
            if total + len(t) > limits.max_text_chars:
                parts.append(t[: limits.max_text_chars - total])
                truncated = True
                break
            parts.append(t)
            total += len(t)
        return "".join(parts), truncated, [n + 1 for n in nums]


def extract_images(data: bytes, pages: str | None, password: str | None, limits: Limits) -> Iterator[tuple[str, bytes]]:
    """(suggested name, bytes) per distinct embedded image, one at a time, so
    the caller writes each before the next is extracted. Refuses before
    extracting anything when there are more than the output cap."""
    with open_pdf(data, password, limits) as doc:
        found: list[tuple[int, int, int]] = []
        seen: set[int] = set()
        for i in page_numbers(pages, doc.page_count):
            for n, img in enumerate(doc[i].get_images(full=True), 1):
                if img[0] not in seen:
                    seen.add(img[0])
                    found.append((i, n, img[0]))
        if len(found) > limits.max_output_files:
            raise OpError(
                f"the pages hold {len(found)} images, more than the {limits.max_output_files} output files allowed; "
                "narrow the page range"
            )
        for i, n, xref in found:
            x = doc.extract_image(xref)
            if not x or not x.get("image"):
                continue
            ext = re.sub(r"[^a-z0-9]", "", str(x.get("ext", "bin")).lower())[:8] or "bin"
            yield f"p{i + 1}-{n}.{ext}", x["image"]


def render_pages(
    data: bytes, pages: str | None, dpi: int, password: str | None, limits: Limits
) -> Iterator[tuple[str, bytes]]:
    """A PNG per page, one at a time, so the caller writes each before the next
    is rendered. Refuses before rendering anything when there are more pages
    than the output cap."""
    if not 36 <= dpi <= limits.max_render_dpi:
        raise OpError(f"dpi must be between 36 and {limits.max_render_dpi}")
    with open_pdf(data, password, limits) as doc:
        nums = page_numbers(pages, doc.page_count)
        if len(nums) > limits.max_output_files:
            raise OpError(
                f"{len(nums)} pages is more than the {limits.max_output_files} output files allowed; "
                "narrow the page range"
            )
        for i in nums:
            yield f"p{i + 1}.png", doc[i].get_pixmap(dpi=dpi, alpha=False).tobytes("png")


def pick_pages(data: bytes, parts: list[list[int]], limits: Limits) -> Iterator[bytes]:
    """A PDF per list of page indexes, in that order, one at a time."""
    with open_pdf(data, None, limits) as doc:
        for nums in parts:
            with pymupdf.open() as part:
                for i in nums:
                    part.insert_pdf(doc, from_page=i, to_page=i)
                yield part.tobytes(garbage=3, deflate=True)


def create(text: str | None, html_body: str | None, page_size: str, title: str | None) -> bytes:
    """A PDF from plain text or simple HTML, laid out by PyMuPDF's Story (no
    network, no scripts)."""
    if (text is None) == (html_body is None):
        raise OpError("give exactly one of text or html")
    size = PAGE_SIZES.get(page_size.upper())
    if not size:
        raise OpError(f"page_size must be one of {', '.join(PAGE_SIZES)}")
    if text is not None:
        paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
        body = "".join(f"<p>{html.escape(p).replace(chr(10), '<br/>')}</p>" for p in paras) or "<p></p>"
    else:
        body = html_body or ""
    css = "body { font-family: sans-serif; font-size: 11pt; } p { margin-bottom: 6pt; }"
    story = pymupdf.Story(html=body, user_css=css)
    rect = pymupdf.Rect(0, 0, *size)
    where = rect + (54, 54, -54, -54)
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    more = True
    pages = 0
    while more:
        dev = writer.begin_page(rect)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
        pages += 1
        if pages > 5000:
            raise OpError("the content is too long")
    writer.close()
    if title:
        with pymupdf.open(stream=buf.getvalue(), filetype="pdf") as doc:
            doc.set_metadata({"title": title})
            return doc.tobytes(garbage=3, deflate=True)
    return buf.getvalue()


ANNOTATIONS = ("highlight", "underline", "strikeout", "note", "freetext", "rectangle")


def annotate(data: bytes, items: list[dict[str, Any]], password: str | None, limits: Limits) -> tuple[bytes, int]:
    """Adds annotations; text-markup ones find their text with `search` or take a `rect`."""
    if not items:
        raise OpError("annotations is empty")
    added = 0
    with open_pdf(data, password, limits) as doc:
        for n, a in enumerate(items, 1):
            kind = a.get("type")
            if kind not in ANNOTATIONS:
                raise OpError(f"annotation {n}: type must be one of {', '.join(ANNOTATIONS)}")
            page = _page(doc, a.get("page"), n)
            content = a.get("text")
            rect = _rect(a.get("rect"), page, n) if a.get("rect") is not None else None
            if kind in ("highlight", "underline", "strikeout"):
                quads = page.search_for(a["search"], quads=True) if a.get("search") else None
                if not quads and rect is None:
                    raise OpError(f"annotation {n}: give a rect, or search text that is on page {a.get('page')}")
                target = quads or rect
                annot = {
                    "highlight": page.add_highlight_annot,
                    "underline": page.add_underline_annot,
                    "strikeout": page.add_strikeout_annot,
                }[kind](target)
            elif kind == "note":
                point = rect.tl if rect is not None else pymupdf.Point(36, 36)
                annot = page.add_text_annot(point, str(content or ""))
            elif kind == "freetext":
                if rect is None:
                    raise OpError(f"annotation {n}: freetext needs a rect")
                annot = page.add_freetext_annot(rect, str(content or ""), fontsize=float(a.get("fontsize", 11)))
            else:
                if rect is None:
                    raise OpError(f"annotation {n}: rectangle needs a rect")
                annot = page.add_rect_annot(rect)
            if content and kind not in ("note", "freetext"):
                annot.set_info(content=str(content))
            annot.update()
            added += 1
        return doc.tobytes(garbage=3, deflate=True), added


def redact(
    data: bytes,
    terms: list[str],
    areas: list[dict[str, Any]],
    pages: str | None,
    remove_metadata: bool,
    password: str | None,
    limits: Limits,
) -> tuple[bytes, dict[str, Any]]:
    """Removes the matched text, and the image and drawing parts under each
    area, from the page content itself; deletes the annotations and clears the
    form values that hold a term; then checks the terms are gone from the
    pages, annotations, form values, document information, XMP metadata and
    bookmarks of the result."""
    terms = [t for t in terms if t.strip()]
    if not terms and not areas:
        raise OpError("give terms to find, areas to remove, or both")
    counts: dict[str, int] = {t: 0 for t in terms}
    removed = cleared = 0
    with open_pdf(data, password, limits) as doc:
        nums = page_numbers(pages, doc.page_count)
        marked: set[int] = set()
        for i in nums:
            page = doc[i]
            for xref in [a.xref for a in page.annots() if _has_term(terms, *_annot_texts(a))]:
                page.delete_annot(page.load_annot(xref))
                removed += 1
                marked.add(i)
            # PyMuPDF skips an empty field_value on update, so /V is set
            # directly, then the appearance is redrawn from it.
            for xref in [w.xref for w in page.widgets() if _has_term(terms, w.field_value)]:
                doc.xref_set_key(xref, "V", "()")
                page.load_widget(xref).update()
                cleared += 1
                marked.add(i)
            for t in terms:
                for q in page.search_for(t, quads=True):
                    page.add_redact_annot(q, fill=(0, 0, 0))
                    counts[t] += 1
                    marked.add(i)
        for n, a in enumerate(areas, 1):
            page = _page(doc, a.get("page"), n)
            page.add_redact_annot(_rect(a.get("rect"), page, n), fill=(0, 0, 0))
            marked.add(page.number)
        for i in marked:
            doc[i].apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                text=pymupdf.PDF_REDACT_TEXT_REMOVE,
            )
        if remove_metadata:
            doc.scrub(metadata=True, xml_metadata=True, attached_files=True, embedded_files=True,
                      javascript=True, reset_fields=False, reset_responses=True, thumbnails=True)
            doc.set_toc([])
        out = doc.tobytes(garbage=4, deflate=True, clean=True)
    # Verify on the bytes that will be written, not on the document in memory.
    with pymupdf.open(stream=out, filetype="pdf") as check:
        if check.needs_pass and not (password and check.authenticate(password)):
            raise OpError("the redacted PDF could not be checked; nothing was written")
        places = _places_with(check, terms, nums)
    if places:
        hint = ""
        if set(places) & {"document information", "XMP metadata", "bookmarks"}:
            hint = "; pass remove_metadata=true to clear the document information, XMP metadata and bookmarks"
        raise OpError(f"the terms are still in the {', '.join(places)} after redaction; nothing was written{hint}")
    summary = {"matches": counts, "areas": len(areas), "pages_changed": sorted(n + 1 for n in marked)}
    if removed:
        summary["annotations_removed"] = removed
    if cleared:
        summary["fields_cleared"] = cleared
    return out, summary


def _has_term(terms: list[str], *texts: Any) -> bool:
    low = [str(x or "").lower() for x in texts]
    return any(t.lower() in x for t in terms for x in low)


def _annot_texts(annot: pymupdf.Annot) -> list[Any]:
    info = annot.info
    return [info.get("content"), info.get("title"), info.get("subject")]


def _places_with(doc: pymupdf.Document, terms: list[str], nums: list[int]) -> list[str]:
    """Where in doc any of terms is: the pages nums (text, annotations, form
    values) and the whole document (information, XMP, bookmarks)."""
    places = []
    if any(_has_term(terms, doc[i].get_text()) for i in nums):
        places.append("page text")
    if any(_has_term(terms, *_annot_texts(a)) for i in nums for a in doc[i].annots()):
        places.append("annotations")
    if any(_has_term(terms, w.field_value) for i in nums for w in doc[i].widgets()):
        places.append("form fields")
    if _has_term(terms, *(doc.metadata or {}).values()):
        places.append("document information")
    if _has_term(terms, doc.get_xml_metadata()):
        places.append("XMP metadata")
    if _has_term(terms, *(entry[1] for entry in doc.get_toc(simple=True))):
        places.append("bookmarks")
    return places


def form_fields(data: bytes, password: str | None) -> list[dict[str, Any]]:
    reader = _reader(data, password)
    fields = reader.get_fields() or {}
    out = []
    for name, f in fields.items():
        kind = {"/Tx": "text", "/Btn": "button", "/Ch": "choice", "/Sig": "signature"}.get(str(f.get("/FT")), "unknown")
        item: dict[str, Any] = {"name": name, "type": kind}
        if f.get("/V") is not None:
            item["value"] = str(f.get("/V"))
        states = f.get("/_States_")
        if states:
            item["options"] = [str(s) for s in states]
        out.append(item)
    return out


def fill_form(data: bytes, values: dict[str, Any], flatten: bool, password: str | None) -> tuple[bytes, list[str]]:
    if not values:
        raise OpError("values is empty")
    reader = _reader(data, password)
    known = set((reader.get_fields() or {}).keys())
    if not known:
        raise OpError("the PDF has no form fields")
    unknown = sorted(set(values) - known)
    if unknown:
        raise OpError(f"no field named {', '.join(unknown[:10])}; list the fields first")
    writer = PdfWriter(clone_from=reader)
    vals = {k: str(v) for k, v in values.items()}
    for page in writer.pages:
        writer.update_page_form_field_values(page, vals, auto_regenerate=False, flatten=flatten)
    writer.set_need_appearances_writer(True)
    return _bytes(writer), sorted(vals)


PERMISSIONS = {
    "print": UserAccessPermissions.PRINT | UserAccessPermissions.PRINT_TO_REPRESENTATION,
    "modify": UserAccessPermissions.MODIFY | UserAccessPermissions.ASSEMBLE_DOC,
    "copy": UserAccessPermissions.EXTRACT | UserAccessPermissions.EXTRACT_TEXT_AND_GRAPHICS,
    "annotate": UserAccessPermissions.ADD_OR_MODIFY | UserAccessPermissions.FILL_FORM_FIELDS,
}


def encrypt(data: bytes, user_password: str, owner_password: str | None, allow: list[str], password: str | None) -> bytes:
    """Without an owner password, a random one nobody keeps: pypdf would use the
    user password, and anyone who can open the file could lift the restrictions."""
    if not user_password:
        raise OpError("user_password is required")
    if owner_password and owner_password == user_password:
        raise OpError("owner_password must differ from user_password, or the restrictions mean nothing")
    bad = sorted(set(allow) - set(PERMISSIONS))
    if bad:
        raise OpError(f"allow may contain only {', '.join(PERMISSIONS)}")
    reader = _reader(data, password)
    writer = PdfWriter(clone_from=reader)
    flags = UserAccessPermissions(0)
    for a in allow:
        flags |= PERMISSIONS[a]
    writer.encrypt(
        user_password=user_password,
        owner_password=owner_password or secrets.token_urlsafe(32),
        permissions_flag=flags,
        algorithm="AES-256",
    )
    return _bytes(writer)


METADATA_KEYS = {"title": "/Title", "author": "/Author", "subject": "/Subject", "keywords": "/Keywords", "creator": "/Creator"}


def set_metadata(data: bytes, values: dict[str, str], password: str | None) -> bytes:
    bad = sorted(set(values) - set(METADATA_KEYS))
    if bad or not values:
        raise OpError(f"metadata keys must be among {', '.join(METADATA_KEYS)}")
    reader = _reader(data, password)
    writer = PdfWriter(clone_from=reader)
    writer.add_metadata({METADATA_KEYS[k]: str(v) for k, v in values.items()})
    return _bytes(writer)


def page_count(data: bytes, password: str | None, limits: Limits) -> int:
    with open_pdf(data, password, limits) as doc:
        return doc.page_count


def result_pages(data: bytes) -> int:
    """The page count of a result about to be written. The page limit is for
    inputs: a merge may well end up past it."""
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            return doc.page_count
    except Exception:
        raise OpError("the result is not a readable PDF; nothing was written") from None


def needs_password(data: bytes) -> bool:
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            return bool(doc.needs_pass)
    except Exception:
        return False


def _reader(data: bytes, password: str | None) -> PdfReader:
    """A PdfReader of data, decrypted. A PDF with only an owner password (it
    restricts, but opens without one) needs no password: pypdf has already
    opened it with the empty one."""
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception:
        raise OpError("not a readable PDF") from None
    if reader.is_encrypted:
        if password:
            if not reader.decrypt(password):
                raise OpError("wrong password for the PDF")
        elif not reader.decrypt(""):
            raise OpError("the PDF is encrypted; pass its password")
    return reader


def _bytes(writer: PdfWriter) -> bytes:
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _page(doc: pymupdf.Document, number: Any, n: int) -> pymupdf.Page:
    if not isinstance(number, int) or not 1 <= number <= doc.page_count:
        raise OpError(f"item {n}: page must be a number from 1 to {doc.page_count}")
    return doc[number - 1]


def _rect(value: Any, page: pymupdf.Page, n: int) -> pymupdf.Rect:
    if not (isinstance(value, list) and len(value) == 4 and all(isinstance(v, (int, float)) for v in value)):
        raise OpError(f"item {n}: rect must be [x0, y0, x1, y1] in points from the page's top left")
    r = pymupdf.Rect(*value)
    if r.is_empty or not r.intersects(page.rect):
        raise OpError(f"item {n}: rect is empty or outside the page")
    return r
