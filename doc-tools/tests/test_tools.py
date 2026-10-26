from __future__ import annotations

import dataclasses
import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import httpx
import pymupdf
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pypdf import PdfReader, PdfWriter
from pypdf.constants import UserAccessPermissions

from conftest import CANARY, FakeGotenberg, make_form_pdf, make_pdf
from pdf_mcp import ops
from pdf_mcp.config import Settings
from pdf_mcp.gotenberg import Gotenberg
from pdf_mcp.paths import Workspace
from pdf_mcp.server import build_server

Call = Callable[..., Any]


def text_of(path: Path) -> str:
    with pymupdf.open(path) as doc:
        return "".join(p.get_text() for p in doc)


def protect(data: bytes, user: str = "pw-1", owner: str = "owner-pw") -> bytes:
    """data encrypted with pypdf; user "" makes a PDF that opens without a
    password but keeps the owner's restrictions."""
    w = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    w.encrypt(user_password=user, owner_password=owner, permissions_flag=UserAccessPermissions(0), algorithm="AES-256")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


@pytest.fixture
def call_with(ws: Workspace, fake: FakeGotenberg, settings: Settings) -> Callable[..., Call]:
    """call_with(**settings) -> a `call` for a server with those settings changed."""
    clients: list[Gotenberg] = []

    def make(**changes: Any) -> Call:
        s = dataclasses.replace(settings, **changes)
        g = Gotenberg("http://gotenberg:3000", 5, s.max_file_bytes, transport=httpx.MockTransport(fake))
        clients.append(g)
        server = build_server(s, ws, g)

        def run(name: str, **args: Any) -> Any:
            result = anyio.run(server.call_tool, name, args)
            assert not result.is_error, result
            return json.loads(result.content[0].text)

        return run

    yield make
    for g in clients:
        g.close()


@pytest.fixture
def refused(settings: Settings, ws: Workspace, fake: FakeGotenberg) -> Callable[..., str]:
    """refused(tool, **args) -> the error message the agent sees."""
    g = Gotenberg("http://gotenberg:3000", 5, settings.max_file_bytes, transport=httpx.MockTransport(fake))
    server = build_server(settings, ws, g)

    def run(name: str, **args: Any) -> str:
        with pytest.raises(ToolError) as e:
            anyio.run(server.call_tool, name, args)
        return str(e.value)

    return run


def test_every_tool_says_whether_it_writes() -> None:
    server = build_server(Settings(workspace="/"), None, None)  # type: ignore[arg-type]
    tools = anyio.run(server.list_tools)
    assert len(tools) == 12
    for t in tools:
        assert t.annotations is not None and t.annotations.open_world_hint is False, t.name
    assert {t.name for t in tools if t.annotations.read_only_hint} == {"pdf_info"}


def test_info_and_ocr_hint(call: Call, root: Path) -> None:
    (root / "a.pdf").write_bytes(make_pdf(["one", "two"]))
    (root / "scan.pdf").write_bytes(make_pdf([""], image=True))
    got = call("pdf_info", path="a.pdf")
    assert got["page_count"] == 2 and got["has_text"] is True and "note" not in got
    scan = call("pdf_info", path="scan.pdf")
    assert scan["has_text"] is False and "OCR" in scan["note"]


def test_extract_text_by_range_and_to_a_file(call: Call, root: Path) -> None:
    (root / "a.pdf").write_bytes(make_pdf(["alpha", "beta", "gamma"]))
    got = call("pdf_extract_text", path="a.pdf", pages="2-end")
    assert got["pages"] == [2, 3] and "beta" in got["text"] and "alpha" not in got["text"]
    out = call("pdf_extract_text", path="a.pdf", output="a.txt")
    assert out["output"] == "a.txt" and "gamma" in (root / "a.txt").read_text()


def test_bad_page_range(refused: Callable[..., str], root: Path) -> None:
    (root / "a.pdf").write_bytes(make_pdf(["alpha"]))
    assert "outside 1-1" in refused("pdf_extract_text", path="a.pdf", pages="2")


def test_extract_images_and_render(call: Call, root: Path) -> None:
    (root / "a.pdf").write_bytes(make_pdf(["x"], image=True))
    imgs = call("pdf_extract_images", path="a.pdf", output_dir="imgs")
    assert imgs["count"] == 1 and (root / imgs["outputs"][0]).stat().st_size > 0
    shots = call("pdf_render_pages", path="a.pdf", output_dir="shots", pages="1", dpi=72)
    assert shots["outputs"] == ["shots/a-p1.png"]
    assert (root / "shots" / "a-p1.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_dpi_cap(refused: Callable[..., str], root: Path) -> None:
    (root / "a.pdf").write_bytes(make_pdf())
    assert "dpi" in refused("pdf_render_pages", path="a.pdf", output_dir="s", dpi=1200)


def test_create_from_text(call: Call, root: Path) -> None:
    got = call("pdf_create", output="new.pdf", text="Title\n\nSecond paragraph", title="T")
    assert got["pages"] == 1 and "Second paragraph" in text_of(root / "new.pdf")


def test_annotate_highlight_and_note(call: Call, root: Path) -> None:
    (root / "a.pdf").write_bytes(make_pdf(["mark this phrase"]))
    got = call(
        "pdf_annotate",
        path="a.pdf",
        output="b.pdf",
        annotations=[{"type": "highlight", "page": 1, "search": "this phrase"}, {"type": "note", "page": 1, "text": "ok"}],
    )
    assert got["annotations_added"] == 2
    with pymupdf.open(root / "b.pdf") as doc:
        assert sorted(a.type[1] for a in doc[0].annots()) == ["Highlight", "Text"]


def test_redaction_removes_the_text_from_the_file(call: Call, root: Path) -> None:
    (root / "a.pdf").write_bytes(make_pdf([f"public part {CANARY} more public"]))
    got = call("pdf_redact", path="a.pdf", output="r.pdf", terms=[CANARY], remove_metadata=True)
    assert got["matches"] == {CANARY: 1} and got["pages_changed"] == [1]
    raw = (root / "r.pdf").read_bytes()
    with pymupdf.open(root / "r.pdf") as doc:
        text = doc[0].get_text()
        streams = b"".join(doc.xref_stream(x) or b"" for x in range(1, doc.xref_length()) if doc.xref_is_stream(x))
    assert CANARY not in text and "public part" in text
    assert CANARY.encode() not in raw and CANARY.encode() not in streams
    # The original is untouched.
    assert CANARY in text_of(root / "a.pdf")


def test_redaction_never_overwrites(refused: Callable[..., str], root: Path) -> None:
    (root / "a.pdf").write_bytes(make_pdf(["x"]))
    (root / "r.pdf").write_bytes(b"keep")
    assert "already exists" in refused("pdf_redact", path="a.pdf", output="r.pdf", terms=["x"])
    assert (root / "r.pdf").read_bytes() == b"keep"


def test_form_list_and_fill(call: Call, root: Path) -> None:
    (root / "f.pdf").write_bytes(make_form_pdf())
    fields = {f["name"]: f for f in call("pdf_form", operation="list", path="f.pdf")["fields"]}
    assert fields["name"]["type"] == "text" and fields["agree"]["type"] == "button"
    got = call("pdf_form", operation="fill", path="f.pdf", output="g.pdf", values={"name": "Alfred"})
    assert got["filled"] == ["name"]
    assert PdfReader(root / "g.pdf").get_fields()["name"]["/V"] == "Alfred"


def test_form_fill_unknown_field(refused: Callable[..., str], root: Path) -> None:
    (root / "f.pdf").write_bytes(make_form_pdf())
    assert "no field named nope" in refused("pdf_form", operation="fill", path="f.pdf", output="g.pdf", values={"nope": 1})


def test_encrypt_aes256(call: Call, root: Path) -> None:
    (root / "a.pdf").write_bytes(make_pdf(["secret text"]))
    call("pdf_secure", operation="encrypt", path="a.pdf", output="e.pdf", user_password="pw-1", allow=["print"])
    r = PdfReader(root / "e.pdf")
    assert r.is_encrypted and r.decrypt("wrong") == 0
    assert r.decrypt("pw-1") != 0 and "secret text" in r.pages[0].extract_text()
    assert r.trailer["/Encrypt"]["/V"] == 5  # AES-256
    info = call("pdf_info", path="e.pdf")
    assert info["needs_password"] is True and info["page_count"] is None
    assert call("pdf_info", path="e.pdf", password="pw-1")["page_count"] == 1


def test_metadata(call: Call, root: Path) -> None:
    (root / "a.pdf").write_bytes(make_pdf())
    call("pdf_secure", operation="metadata", path="a.pdf", output="m.pdf", values={"title": "Report"})
    assert call("pdf_info", path="m.pdf")["metadata"]["title"] == "Report"


def test_convert_office_goes_to_gotenberg(call: Call, root: Path, fake: FakeGotenberg) -> None:
    (root / "deck.pptx").write_bytes(b"PK fake pptx")
    got = call("pdf_convert", path="deck.pptx", pdfa="PDF/A-2b")
    assert got["output"] == "deck.pdf" and (root / "deck.pdf").read_bytes().startswith(b"%PDF-")
    req = fake.requests[-1]
    assert req.url.path == "/forms/libreoffice/convert"
    body = req.content
    assert b'name="pdfa"' in body and b"PDF/A-2b" in body and b'filename="document.pptx"' in body
    # Only the extension leaves the stack, not the workspace path.
    assert b"deck" not in body.split(b"PK fake pptx")[0]


def test_convert_html_uses_chromium(call: Call, root: Path, fake: FakeGotenberg) -> None:
    (root / "p.html").write_bytes(b"<h1>hi</h1>")
    call("pdf_convert", path="p.html", output="out/p.pdf")
    assert fake.requests[-1].url.path == "/forms/chromium/convert/html"
    assert b'filename="index.html"' in fake.requests[-1].content
    assert (root / "out" / "p.pdf").exists()


def test_convert_refuses_unknown_types(refused: Callable[..., str], root: Path) -> None:
    (root / "a.exe").write_bytes(b"MZ")
    assert "cannot convert .exe" in refused("pdf_convert", path="a.exe")


def test_merge_keeps_order_and_split_writes_parts(call: Call, root: Path, fake: FakeGotenberg) -> None:
    (root / "a.pdf").write_bytes(make_pdf(["a"]))
    (root / "b.pdf").write_bytes(make_pdf(["b1", "b2"]))
    call("pdf_pages", operation="merge", paths=["b.pdf", "a.pdf"], output="ab.pdf")
    body = fake.requests[-1].content
    assert body.index(b'filename="0001.pdf"') < body.index(b'filename="0002.pdf"')
    got = call("pdf_pages", operation="split", paths=["b.pdf"], output_dir="parts")
    assert got["outputs"] == ["parts/b-1.pdf", "parts/b-2.pdf"] and got["ranges"] == ["1", "2"]


def test_split_and_extract_run_here_not_on_the_converter(call: Call, root: Path, fake: FakeGotenberg) -> None:
    # One upload of the whole PDF per part would pass letta-code's 60 s on a
    # long document.
    (root / "b.pdf").write_bytes(make_pdf(["b1", "b2", "b3"]))
    got = call("pdf_pages", operation="split", paths=["b.pdf"], output_dir="parts")
    assert got["outputs"] == ["parts/b-1.pdf", "parts/b-2.pdf", "parts/b-3.pdf"]
    assert [text_of(root / p).strip() for p in got["outputs"]] == ["b1", "b2", "b3"]
    got = call("pdf_pages", operation="split", paths=["b.pdf"], output_dir="ranges", pages="1-2,3")
    assert got["ranges"] == ["1,2", "3"] and text_of(root / "ranges" / "b-1.pdf").split() == ["b1", "b2"]
    call("pdf_pages", operation="extract", paths=["b.pdf"], pages="3,1", output="y.pdf")
    assert text_of(root / "y.pdf").split() == ["b3", "b1"]
    assert fake.requests == []


def test_results_longer_than_the_page_limit_are_written_and_reported(
    call_with: Callable[..., Call], root: Path, fake: FakeGotenberg
) -> None:
    # The limit is for inputs; a result past it was reported as a failure after
    # it had been written, and a retry then found the output in the way.
    call = call_with(max_pages=3)
    four = make_pdf(["1", "2", "3", "4"])
    fake.handler = lambda r: httpx.Response(200, headers={"content-type": "application/pdf"}, content=four)
    (root / "a.pdf").write_bytes(make_pdf(["a1", "a2"]))
    (root / "b.pdf").write_bytes(make_pdf(["b1", "b2"]))
    assert call("pdf_pages", operation="merge", paths=["a.pdf", "b.pdf"], output="ab.pdf")["pages"] == 4
    (root / "d.docx").write_bytes(b"PK")
    assert call("pdf_convert", path="d.docx")["pages"] == 4


def test_stamp_sends_text_and_options(call: Call, root: Path, fake: FakeGotenberg) -> None:
    (root / "a.pdf").write_bytes(make_pdf())
    call("pdf_stamp", operation="watermark", path="a.pdf", output="w.pdf", text="DRAFT")
    body = fake.requests[-1].content
    assert fake.requests[-1].url.path == "/forms/pdfengines/watermark"
    assert b"DRAFT" in body and b'name="watermarkSource"' in body
    # Gotenberg refuses numeric option values.
    assert b'{"opacity":"0.3","points":"48","rotation":"45"}' in body


@pytest.mark.parametrize(
    "response, message",
    [
        (httpx.Response(503), "busy"),
        (httpx.Response(400, content=b"bad field"), "refused the request"),
        (httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>"), "unexpected answer"),
        (httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"not a pdf"), "not a PDF"),
    ],
)
def test_gotenberg_failures_write_nothing(
    refused: Callable[..., str], root: Path, fake: FakeGotenberg, response: httpx.Response, message: str
) -> None:
    fake.handler = lambda r: response
    (root / "a.docx").write_bytes(b"PK")
    assert message in refused("pdf_convert", path="a.docx")
    assert sorted(p.name for p in root.iterdir()) == ["a.docx"]


def test_gotenberg_unreachable_and_timeout(refused: Callable[..., str], root: Path, fake: FakeGotenberg) -> None:
    (root / "a.docx").write_bytes(b"PK")

    def down(r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    fake.handler = down
    assert "unreachable" in refused("pdf_convert", path="a.docx")

    def slow(r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    fake.handler = slow
    assert "took too long" in refused("pdf_convert", path="a.docx")


def test_oversize_result_writes_nothing(refused: Callable[..., str], root: Path, fake: FakeGotenberg) -> None:
    fake.handler = lambda r: httpx.Response(
        200, headers={"content-type": "application/pdf"}, content=b"%PDF-" + b"0" * (3 * 1024 * 1024)
    )
    (root / "a.docx").write_bytes(b"PK")
    assert "size limit" in refused("pdf_convert", path="a.docx")
    assert not (root / "a.pdf").exists()


def test_errors_never_name_paths_outside_the_workspace(refused: Callable[..., str], root: Path) -> None:
    msg = refused("pdf_info", path="../agent-2/theirs.pdf")
    assert str(root.parent) not in msg and "agent-2/theirs" in msg
    msg = refused("pdf_info", path="missing.pdf")
    assert str(root) not in msg


def test_malformed_pdf(refused: Callable[..., str], root: Path) -> None:
    (root / "bad.pdf").write_bytes(b"%PDF-1.7\n garbage")
    assert "not a readable PDF" in refused("pdf_info", path="bad.pdf") or "PDF" in refused("pdf_info", path="bad.pdf")
    (root / "txt.pdf").write_bytes(b"hello")
    assert "is not a PDF" in refused("pdf_info", path="txt.pdf")


def test_results_carry_no_file_contents(call: Call, root: Path) -> None:
    (root / "a.pdf").write_bytes(make_pdf(["x"], image=True))
    got = call("pdf_render_pages", path="a.pdf", output_dir="s")
    assert len(json.dumps(got)) < 500


def test_render_never_replaces_an_existing_file_by_default(call: Call, refused: Callable[..., str], root: Path) -> None:
    (root / "a.pdf").write_bytes(make_pdf())
    (root / "s").mkdir()
    (root / "s" / "a-p1.png").write_bytes(b"mine")
    assert "already exists" in refused("pdf_render_pages", path="a.pdf", output_dir="s")
    assert (root / "s" / "a-p1.png").read_bytes() == b"mine"
    call("pdf_render_pages", path="a.pdf", output_dir="s", overwrite=True)
    assert (root / "s" / "a-p1.png").read_bytes()[:4] == b"\x89PNG"


def test_render_refuses_more_pages_than_the_output_cap_before_rendering(
    refused: Callable[..., str], root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rendered: list[int] = []
    real = pymupdf.Page.get_pixmap
    monkeypatch.setattr(pymupdf.Page, "get_pixmap", lambda self, *a, **k: rendered.append(1) or real(self, *a, **k))
    (root / "a.pdf").write_bytes(make_pdf([str(i) for i in range(21)]))  # the cap is 20
    assert "more than the 20" in refused("pdf_render_pages", path="a.pdf", output_dir="s", pages="all")
    assert rendered == [] and not (root / "s").exists()


def test_render_holds_one_page_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    rendered: list[int] = []
    real = pymupdf.Page.get_pixmap
    monkeypatch.setattr(pymupdf.Page, "get_pixmap", lambda self, *a, **k: rendered.append(1) or real(self, *a, **k))
    pages = ops.render_pages(make_pdf(["1", "2", "3"]), "all", 72, None, ops.Limits(10, 200, 1000, 10))
    next(iter(pages))
    assert rendered == [1]


def test_extract_images_refuses_more_than_the_output_cap_before_writing(
    refused: Callable[..., str], root: Path
) -> None:
    doc = pymupdf.open()
    for i in range(21):  # the cap is 20; distinct colours, so no image is shared
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 8, 8), False)
        pix.set_rect(pix.irect, (i * 10, 30, 30))
        doc.new_page().insert_image(pymupdf.Rect(72, 72, 172, 172), pixmap=pix)
    (root / "a.pdf").write_bytes(doc.tobytes())
    assert "more than the 20" in refused("pdf_extract_images", path="a.pdf", output_dir="imgs")
    assert not (root / "imgs").exists()


def test_restricted_pdf_that_opens_without_a_password_needs_none(
    call: Call, refused: Callable[..., str], root: Path
) -> None:
    (root / "f.pdf").write_bytes(protect(make_form_pdf(), user=""))
    assert {f["name"] for f in call("pdf_form", operation="list", path="f.pdf")["fields"]} == {"name", "agree"}
    call("pdf_form", operation="fill", path="f.pdf", output="g.pdf", values={"name": "Alfred"})
    call("pdf_secure", operation="metadata", path="f.pdf", output="m.pdf", values={"title": "T"})
    # One that needs a password still asks for it.
    (root / "u.pdf").write_bytes(protect(make_form_pdf()))
    assert "pass its password" in refused("pdf_form", operation="list", path="u.pdf")
    assert "wrong password" in refused("pdf_form", operation="list", path="u.pdf", password="nope")
    assert call("pdf_form", operation="list", path="u.pdf", password="pw-1")["fields"]


def test_outputs_of_a_password_protected_pdf_say_they_are_not_protected(call: Call, root: Path) -> None:
    (root / "a.pdf").write_bytes(protect(make_pdf(["keep SECRET word"])))
    got = call("pdf_redact", path="a.pdf", output="r.pdf", terms=["SECRET"], password="pw-1")
    assert "not password-protected" in got["note"]
    with pymupdf.open(root / "r.pdf") as doc:
        assert not doc.needs_pass and "SECRET" not in doc[0].get_text() and "keep" in doc[0].get_text()
    got = call("pdf_annotate", path="a.pdf", output="n.pdf", annotations=[{"type": "note", "page": 1}], password="pw-1")
    assert "not password-protected" in got["note"]
    got = call("pdf_secure", operation="metadata", path="a.pdf", output="m.pdf", values={"title": "T"}, password="pw-1")
    assert "not password-protected" in got["note"]
    (root / "p.pdf").write_bytes(make_pdf(["plain"]))
    assert "note" not in call("pdf_annotate", path="p.pdf", output="q.pdf", annotations=[{"type": "note", "page": 1}])


def test_redaction_reaches_beyond_the_page_text(call: Call, refused: Callable[..., str], root: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), f"public part {CANARY}")
    page.add_text_annot((72, 200), f"ask {CANARY}")
    page.add_text_annot((72, 300), "unrelated comment")
    doc.set_metadata({"title": f"About {CANARY}"})
    doc.set_toc([[1, f"Part {CANARY}", 1]])
    (root / "a.pdf").write_bytes(doc.tobytes())
    msg = refused("pdf_redact", path="a.pdf", output="r.pdf", terms=[CANARY])
    assert "document information" in msg and "bookmarks" in msg and "remove_metadata" in msg
    assert CANARY not in msg and not (root / "r.pdf").exists()
    got = call("pdf_redact", path="a.pdf", output="r.pdf", terms=[CANARY], remove_metadata=True)
    assert got["annotations_removed"] == 1
    raw = (root / "r.pdf").read_bytes()
    with pymupdf.open(root / "r.pdf") as out:
        objects = "".join(out.xref_object(x) for x in range(1, out.xref_length()))
        streams = b"".join(out.xref_stream(x) or b"" for x in range(1, out.xref_length()) if out.xref_is_stream(x))
        assert [a.info["content"] for a in out[0].annots()] == ["unrelated comment"]
        assert out.get_toc() == [] and "public part" in out[0].get_text()
    assert CANARY.encode() not in raw and CANARY not in objects and CANARY.encode() not in streams


def test_redaction_clears_form_values_with_a_term(call: Call, root: Path) -> None:
    (root / "f.pdf").write_bytes(make_form_pdf())
    call("pdf_form", operation="fill", path="f.pdf", output="g.pdf", values={"name": CANARY})
    got = call("pdf_redact", path="g.pdf", output="r.pdf", terms=[CANARY])
    assert got["fields_cleared"] == 1
    assert CANARY not in str(PdfReader(root / "r.pdf").get_fields()["name"].get("/V"))


def test_encrypt_restrictions_hold_without_an_owner_password(
    call: Call, refused: Callable[..., str], root: Path
) -> None:
    # pypdf makes the user password the owner password when none is given,
    # which would let anyone who can open the file lift the restrictions.
    (root / "a.pdf").write_bytes(make_pdf(["secret text"]))
    got = call("pdf_secure", operation="encrypt", path="a.pdf", output="none.pdf", user_password="pw-1", allow=[])
    assert "owner password" in got["note"]
    r = PdfReader(root / "none.pdf")
    assert r.decrypt("pw-1") == 1  # PasswordType.USER_PASSWORD, not OWNER_PASSWORD
    assert not r.user_access_permissions & UserAccessPermissions.PRINT
    call("pdf_secure", operation="encrypt", path="a.pdf", output="print.pdf", user_password="pw-1")
    r = PdfReader(root / "print.pdf")
    assert r.decrypt("pw-1") == 1 and r.user_access_permissions & UserAccessPermissions.PRINT
    assert "must differ" in refused(
        "pdf_secure", operation="encrypt", path="a.pdf", output="same.pdf", user_password="pw-1", owner_password="pw-1"
    )
