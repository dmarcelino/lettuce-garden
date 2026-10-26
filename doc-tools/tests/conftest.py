from __future__ import annotations

import io
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import anyio
import httpx
import pymupdf
import pytest
from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NumberObject, TextStringObject

from pdf_mcp.config import Settings
from pdf_mcp.gotenberg import Gotenberg
from pdf_mcp.paths import Workspace
from pdf_mcp.server import build_server

# A fake value tests assert never reaches logs, stderr or error messages.
CANARY = "CANARY_DOC_7731_DO_NOT_LEAK"


def make_pdf(pages: list[str] | None = None, *, image: bool = False) -> bytes:
    doc = pymupdf.open()
    for text in pages or ["Hello page one"]:
        page = doc.new_page()
        if text:
            page.insert_text((72, 100), text, fontsize=12)
        if image:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False)
            pix.set_rect(pix.irect, (200, 30, 30))
            page.insert_image(pymupdf.Rect(72, 200, 172, 300), pixmap=pix)
    return doc.tobytes()


def make_form_pdf() -> bytes:
    """One page with a text field `name` and a check box `agree`."""
    w = PdfWriter()
    page = w.add_blank_page(612, 792)
    text = DictionaryObject({
        NameObject("/FT"): NameObject("/Tx"), NameObject("/T"): TextStringObject("name"),
        NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Widget"),
        NameObject("/Rect"): ArrayObject([NumberObject(72), NumberObject(700), NumberObject(300), NumberObject(720)]),
    })
    box = DictionaryObject({
        NameObject("/FT"): NameObject("/Btn"), NameObject("/T"): TextStringObject("agree"),
        NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Widget"),
        NameObject("/Rect"): ArrayObject([NumberObject(72), NumberObject(650), NumberObject(90), NumberObject(668)]),
        NameObject("/AP"): DictionaryObject({NameObject("/N"): DictionaryObject({
            NameObject("/Yes"): DictionaryObject(), NameObject("/Off"): DictionaryObject()})}),
    })
    refs = [w._add_object(text), w._add_object(box)]
    page[NameObject("/Annots")] = ArrayObject(refs)
    w._root_object[NameObject("/AcroForm")] = DictionaryObject({NameObject("/Fields"): ArrayObject(refs)})
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    ws = tmp_path / "work" / "agent-1"
    ws.mkdir(parents=True)
    (tmp_path / "work" / "agent-2").mkdir()
    (tmp_path / "work" / "agent-2" / "theirs.pdf").write_bytes(make_pdf(["other agent"]))
    (tmp_path / "outside.pdf").write_bytes(make_pdf(["outside"]))
    return ws


@pytest.fixture
def ws(root: Path) -> Iterator[Workspace]:
    w = Workspace(str(root))
    yield w
    w.close()


class FakeGotenberg:
    """Plays Gotenberg: records each request; answers from a handler."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.handler: Callable[[httpx.Request], httpx.Response] = self.echo

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        return self.handler(request)

    @staticmethod
    def echo(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=make_pdf(["converted"]))


@pytest.fixture
def fake() -> FakeGotenberg:
    return FakeGotenberg()


@pytest.fixture
def settings(root: Path) -> Settings:
    return Settings(workspace=str(root), max_file_bytes=2 * 1024 * 1024, max_output_files=20, timeout_seconds=5)


@pytest.fixture
def call(settings: Settings, ws: Workspace, fake: FakeGotenberg) -> Iterator[Callable[..., Any]]:
    """call(tool, **args) -> the tool's result, through the MCP server."""
    g = Gotenberg("http://gotenberg:3000", 5, settings.max_file_bytes, transport=httpx.MockTransport(fake))
    server = build_server(settings, ws, g)

    def run(name: str, **args: Any) -> Any:
        result = anyio.run(server.call_tool, name, args)
        assert not result.is_error, result
        return json.loads(result.content[0].text)

    yield run
    g.close()
