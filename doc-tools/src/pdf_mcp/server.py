"""The MCP tools. Each validates its paths through the jail, reads its inputs,
runs one operation (in-process or on the shared Gotenberg) and writes its
outputs into the workspace; results carry paths and metadata, never file
contents."""

from __future__ import annotations

import dataclasses
import functools
import json
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import ops
from .config import Settings
from .gotenberg import Gotenberg, GotenbergError
from .paths import Input, PathError, Workspace, join, split, stem

INSTRUCTIONS = (
    "Tools for PDF files in your workspace: read, extract, render, create, convert to PDF, merge, split, rotate, "
    "stamp, annotate, redact, fill forms and encrypt. To create or edit Word, PowerPoint or Excel files use the "
    "`office` server, then pdf_convert to make a PDF of them. Paths are relative to your workspace (no leading "
    "'/', no '..'); symbolic links are not followed. Outputs are never written over an existing file unless you "
    "pass overwrite=true, and never over an input. Check results by rendering pages (pdf_render_pages) and "
    "looking at them, or by reading the text back."
)

OFFICE_EXT = {"docx", "doc", "odt", "rtf", "pptx", "ppt", "odp", "xlsx", "xls", "ods", "csv", "txt"}
PDFA = {"PDF/A-1b", "PDF/A-2b", "PDF/A-3b"}

_READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)


class Refused(Exception):
    """A request the tool turns down; the message is for the agent."""


def build_server(settings: Settings, ws: Workspace, gotenberg: Gotenberg) -> MCPServer:
    server = MCPServer(name="pdf", instructions=INSTRUCTIONS)
    limits = ops.Limits(settings.max_pages, settings.max_render_dpi, settings.max_text_chars, settings.max_output_files)

    def read(path: str) -> Input:
        return ws.read(path, settings.max_file_bytes)

    def read_pdf(path: str) -> Input:
        src = read(path)
        if not src.data.startswith(b"%PDF-") and b"%PDF-" not in src.data[:1024]:
            raise Refused(f"{path!r} is not a PDF")
        return src

    def write(path: str, data: bytes, overwrite: bool, sources: tuple[Input, ...]) -> str:
        if len(data) > settings.max_file_bytes:
            raise Refused("the result is larger than the size limit; nothing was written")
        return ws.write(path, data, overwrite=overwrite, sources=sources)

    def unprotected(result: dict[str, Any], src: Input, password: str | None) -> dict[str, Any]:
        """Says so when the output of a password-protected input is not
        protected: the tools write what they decrypted."""
        if password and ops.needs_password(src.data):
            result["note"] = (
                "the input is password-protected; this output is not password-protected "
                "(protect it with pdf_secure if it must be)"
            )
        return result

    def guard(fn: Any) -> Any:
        """Turns the expected failures into one-line errors for the agent."""

        @functools.wraps(fn)
        def wrapped(*a: Any, **kw: Any) -> Any:
            try:
                return fn(*a, **kw)
            # ToolError's message reaches the agent; anything else is reported by
            # the SDK as a bare "Error executing tool", which leaks nothing.
            except (PathError, ops.OpError, GotenbergError, Refused) as e:
                raise ToolError(str(e)) from None

        return wrapped

    @server.tool(
        description="Facts about a PDF: page count, page sizes, metadata, whether it is encrypted, has a form, "
        "and has text (no text but images means a scanned PDF that would need OCR, which these tools don't do).",
        annotations=_READ,
    )
    @guard
    def pdf_info(path: str, password: str | None = None) -> dict[str, Any]:
        return {"path": path, **ops.info(read_pdf(path).data, password, limits)}

    @server.tool(
        description="The text of a PDF, page by page. pages: '1-3,5', '2-end' or 'all' (default). Long text is "
        "cut at a limit; pass output (a .txt path) to write all of it to a file instead of returning it.",
        annotations=_WRITE,
    )
    @guard
    def pdf_extract_text(
        path: str, pages: str = "all", output: str | None = None, overwrite: bool = False, password: str | None = None
    ) -> dict[str, Any]:
        src = read_pdf(path)
        if output is None:
            text, truncated, nums = ops.extract_text(src.data, pages, password, limits)
            return {"pages": nums, "truncated": truncated, "text": text}
        big = dataclasses.replace(limits, max_text_chars=settings.max_file_bytes)
        text, truncated, nums = ops.extract_text(src.data, pages, password, big)
        write(output, text.encode(), overwrite, (src,))
        return {"pages": nums, "output": output, "characters": len(text), "truncated": truncated}

    @server.tool(
        description="Saves the images embedded in a PDF into output_dir (created if needed), one file each, "
        "named <pdf name>-p<page>-<n>.<ext>.",
        annotations=_WRITE,
    )
    @guard
    def pdf_extract_images(
        path: str, output_dir: str, pages: str = "all", overwrite: bool = False, password: str | None = None
    ) -> dict[str, Any]:
        src = read_pdf(path)
        split(output_dir)
        batch = ws.batch(settings.max_output_files)
        for name, data in ops.extract_images(src.data, pages, password, limits):
            batch.write(join(output_dir, f"{stem(path)}-{name}"), data, overwrite=overwrite, sources=(src,))
        return {"outputs": batch.written, "count": len(batch.written)}

    @server.tool(
        description="Renders PDF pages to PNG images in output_dir (named <pdf name>-p<page>.png) so you can look "
        "at the layout. dpi 36-200 (default 110). Use it to check a PDF you made or changed; to render the same "
        "pages again into the same folder, pass overwrite=true.",
        annotations=_WRITE,
    )
    @guard
    def pdf_render_pages(
        path: str,
        output_dir: str,
        pages: str = "1",
        dpi: int = 110,
        overwrite: bool = False,
        password: str | None = None,
    ) -> dict[str, Any]:
        src = read_pdf(path)
        split(output_dir)
        batch = ws.batch(settings.max_output_files)
        for name, data in ops.render_pages(src.data, pages, dpi, password, limits):
            batch.write(join(output_dir, f"{stem(path)}-{name}"), data, overwrite=overwrite, sources=(src,))
        return {"outputs": batch.written}

    @server.tool(
        description="Creates a simple PDF from plain text (paragraphs separated by blank lines) or from simple "
        "HTML with inline CSS (headings, paragraphs, lists, tables; no scripts, no remote images). For a "
        "designed document, write a Word file with the office server and pdf_convert it, or pdf_convert an "
        ".html file from your workspace. page_size: A4, LETTER, A3 or LEGAL.",
        annotations=_WRITE,
    )
    @guard
    def pdf_create(
        output: str,
        text: str | None = None,
        html: str | None = None,
        page_size: str = "A4",
        title: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        data = ops.create(text, html, page_size, title)
        pages = ops.result_pages(data)
        write(output, data, overwrite, ())
        return {"output": output, "pages": pages}

    @server.tool(
        description="Converts a Word, PowerPoint, Excel, OpenDocument, RTF, CSV or text file, or an .html file, "
        "from your workspace to PDF on the shared converter (LibreOffice, or Chromium for HTML). pdfa: "
        "PDF/A-1b, PDF/A-2b or PDF/A-3b for archiving; pdfua: true for accessible PDF/UA. HTML is rendered "
        "without JavaScript and without fetching anything; put images inline as data: URLs. Default output: "
        "the same name with .pdf.",
        annotations=_WRITE,
    )
    @guard
    def pdf_convert(
        path: str,
        output: str | None = None,
        pdfa: str | None = None,
        pdfua: bool = False,
        landscape: bool = False,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        name = split(path)[-1]
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if pdfa is not None and pdfa not in PDFA:
            raise Refused(f"pdfa must be one of {', '.join(sorted(PDFA))}")
        src = read(path)
        out = output or _sibling(path, ".pdf")
        fields: dict[str, str] = {}
        if pdfa:
            fields["pdfa"] = pdfa
        if pdfua:
            fields["pdfua"] = "true"
        if landscape:
            fields["landscape"] = "true"
        if ext in ("html", "htm"):
            body, _ = gotenberg.post("/forms/chromium/convert/html", [("index.html", src.data)], fields)
        elif ext in OFFICE_EXT:
            body, _ = gotenberg.post("/forms/libreoffice/convert", [(f"document.{ext}", src.data)], fields)
        elif ext == "pdf":
            if not (pdfa or pdfua):
                raise Refused("the file is already a PDF; pass pdfa or pdfua to convert it to an archival PDF")
            body, _ = gotenberg.post("/forms/pdfengines/convert", [("document.pdf", src.data)], fields)
        else:
            raise Refused(f"cannot convert .{ext or '?'} files; supported: html, pdf, {', '.join(sorted(OFFICE_EXT))}")
        pages = ops.result_pages(body)
        write(out, body, overwrite, (src,))
        return {"output": out, "pages": pages}

    @server.tool(
        description="Page operations. operation 'merge': paths (two or more PDFs, in "
        "order) -> output. 'extract': the pages of paths[0] -> one PDF at output. 'split': paths[0] -> one PDF "
        "per page range (pages, e.g. '1-3,4-end'; default every page) in output_dir, named <name>-<n>.pdf. "
        "'rotate': pages of paths[0] by angle 90, 180 or 270 -> output.",
        annotations=_WRITE,
    )
    @guard
    def pdf_pages(
        operation: Literal["merge", "extract", "split", "rotate"],
        paths: list[str],
        output: str | None = None,
        output_dir: str | None = None,
        pages: str | None = None,
        angle: int = 90,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        if not paths:
            raise Refused("paths is empty")
        if operation == "merge":
            if len(paths) < 2 or not output:
                raise Refused("merge needs two or more paths and an output")
            srcs = tuple(read_pdf(p) for p in paths)
            files = [(f"{i:04d}.pdf", s.data) for i, s in enumerate(srcs, 1)]
            body, _ = gotenberg.post("/forms/pdfengines/merge", files)
            total = ops.result_pages(body)
            write(output, body, overwrite, srcs)
            return {"output": output, "pages": total}
        if len(paths) != 1:
            raise Refused(f"{operation} takes exactly one path")
        src = read_pdf(paths[0])
        count = ops.page_count(src.data, None, limits)
        # Extract and split run here: on the converter, each part would upload the
        # whole PDF again.
        if operation == "extract":
            if not output or not pages:
                raise Refused("extract needs pages and an output")
            nums = ops.page_numbers(pages, count)
            write(output, next(ops.pick_pages(src.data, [nums], limits)), overwrite, (src,))
            return {"output": output, "pages": _span(pages, count)}
        if operation == "split":
            if not output_dir:
                raise Refused("split needs an output_dir")
            split(output_dir)
            parts = [ops.page_numbers(r, count) for r in pages.split(",")] if pages else [[i] for i in range(count)]
            if len(parts) > settings.max_output_files:
                raise Refused(f"more than {settings.max_output_files} parts; use larger ranges")
            batch = ws.batch(settings.max_output_files)
            for n, data in enumerate(ops.pick_pages(src.data, parts, limits), 1):
                batch.write(join(output_dir, f"{stem(paths[0])}-{n}.pdf"), data, overwrite=overwrite, sources=(src,))
            return {"outputs": batch.written, "ranges": [",".join(str(i + 1) for i in p) for p in parts]}
        if operation == "rotate":
            if angle not in (90, 180, 270) or not output:
                raise Refused("rotate needs angle 90, 180 or 270 and an output")
            fields = {"rotateAngle": str(angle)}
            if pages:
                fields["rotatePages"] = _span(pages, count)
            body, _ = gotenberg.post("/forms/pdfengines/rotate", [("document.pdf", src.data)], fields)
            write(output, body, overwrite, (src,))
            return {"output": output, "angle": angle, "pages": pages or "all"}
        raise Refused(f"unknown operation {operation!r}")

    @server.tool(
        description="Puts text on the pages of a PDF on the shared converter: operation 'watermark' behind the "
        "content, 'stamp' on top of it. text is the words to place; pages limits it (default every page); "
        "opacity 0.05-1 (default 0.3); font_size in points (default 48); rotation in degrees (default 45).",
        annotations=_WRITE,
    )
    @guard
    def pdf_stamp(
        operation: Literal["watermark", "stamp"],
        path: str,
        output: str,
        text: str,
        pages: str | None = None,
        opacity: float = 0.3,
        font_size: int = 48,
        rotation: int = 45,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        if not text.strip() or len(text) > 200:
            raise Refused("text must be 1-200 characters")
        if not 0.05 <= opacity <= 1 or not 4 <= font_size <= 300 or not -360 <= rotation <= 360:
            raise Refused("opacity, font_size or rotation out of range")
        src = read_pdf(path)
        count = ops.page_count(src.data, None, limits)
        prefix = operation
        # pdfcpu's option names; Gotenberg accepts only string values.
        options = {"opacity": f"{opacity:g}", "points": str(font_size), "rotation": str(rotation)}
        fields = {
            f"{prefix}Source": "text",
            f"{prefix}Expression": text,
            f"{prefix}Options": _json(options),
        }
        if pages:
            fields[f"{prefix}Pages"] = _span(pages, count)
        body, _ = gotenberg.post(f"/forms/pdfengines/{operation}", [("document.pdf", src.data)], fields)
        write(output, body, overwrite, (src,))
        return {"output": output, "operation": operation}

    @server.tool(
        description="Adds annotations to a PDF. Each item: type (highlight, underline, strikeout, note, freetext, "
        "rectangle), page (1-based), and either search (text on that page to mark) or rect [x0, y0, x1, y1] in "
        "points from the page's top left; text is the comment (note, freetext) or popup content.",
        annotations=_WRITE,
    )
    @guard
    def pdf_annotate(
        path: str, output: str, annotations: list[dict[str, Any]], overwrite: bool = False, password: str | None = None
    ) -> dict[str, Any]:
        src = read_pdf(path)
        data, added = ops.annotate(src.data, annotations, password, limits)
        write(output, data, overwrite, (src,))
        return unprotected({"output": output, "annotations_added": added}, src, password)

    @server.tool(
        description="Permanently removes content from a PDF: every occurrence of each of terms (case-insensitive), "
        "and whatever lies under each of areas ({page, rect: [x0, y0, x1, y1]} in points). The text, image "
        "pixels and drawings there are deleted from the file, not just covered; annotations (comments) whose text "
        "has a term are deleted and form values with one are cleared. The result is checked for the terms in its "
        "pages, annotations, form values, document information, XMP metadata and bookmarks before it is written. "
        "remove_metadata clears the document information, XMP metadata, bookmarks, attachments and scripts; "
        "without it, a term found there stops the redaction. Always write to a new output and keep the "
        "original until the person has checked it.",
        annotations=_WRITE,
    )
    @guard
    def pdf_redact(
        path: str,
        output: str,
        terms: list[str] | None = None,
        areas: list[dict[str, Any]] | None = None,
        pages: str | None = None,
        remove_metadata: bool = False,
        password: str | None = None,
    ) -> dict[str, Any]:
        src = read_pdf(path)
        data, summary = ops.redact(src.data, terms or [], areas or [], pages, remove_metadata, password, limits)
        write(output, data, False, (src,))
        return unprotected({"output": output, **summary}, src, password)

    @server.tool(
        description="PDF forms (AcroForm). operation 'list': the fields of path, with their type, value and "
        "options. 'fill': values {field name: value} -> output; check boxes and radio buttons take one of the "
        "field's options; flatten makes the filled fields part of the page.",
        annotations=_WRITE,
    )
    @guard
    def pdf_form(
        operation: Literal["list", "fill"],
        path: str,
        output: str | None = None,
        values: dict[str, Any] | None = None,
        flatten: bool = False,
        overwrite: bool = False,
        password: str | None = None,
    ) -> dict[str, Any]:
        src = read_pdf(path)
        if operation == "list":
            return {"fields": ops.form_fields(src.data, password)}
        if not output:
            raise Refused("fill needs an output")
        data, filled = ops.fill_form(src.data, values or {}, flatten, password)
        write(output, data, overwrite, (src,))
        return unprotected({"output": output, "filled": filled}, src, password)

    @server.tool(
        description="operation 'encrypt': protects a PDF with AES-256. user_password opens it; owner_password "
        "(optional, different from user_password) lifts the restrictions, and without it nobody can; allow lists "
        "what readers may do: print, modify, copy, annotate (default print; [] for nothing). "
        "'metadata': sets title, author, subject, keywords or creator (values). Writes output; the original "
        "stays as it is.",
        annotations=_WRITE,
    )
    @guard
    def pdf_secure(
        operation: Literal["encrypt", "metadata"],
        path: str,
        output: str,
        user_password: str | None = None,
        owner_password: str | None = None,
        allow: list[str] | None = None,
        values: dict[str, str] | None = None,
        overwrite: bool = False,
        password: str | None = None,
    ) -> dict[str, Any]:
        src = read_pdf(path)
        result: dict[str, Any] = {"output": output, "operation": operation}
        if operation == "encrypt":
            allowed = ["print"] if allow is None else allow
            data = ops.encrypt(src.data, user_password or "", owner_password, allowed, password)
            if not owner_password:
                result["note"] = "no owner password was given, so nobody can lift the restrictions"
        else:
            data = ops.set_metadata(src.data, values or {}, password)
            unprotected(result, src, password)
        write(output, data, overwrite, (src,))
        return result

    return server


def _sibling(path: str, ext: str) -> str:
    parts = split(path)
    return "/".join([*parts[:-1], stem(path) + ext])


def _span(spec: str, count: int) -> str:
    """'1-3,5' checked against the page count, in Gotenberg's syntax."""
    nums = ops.page_numbers(spec, count)
    return ",".join(str(n + 1) for n in nums)


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))
