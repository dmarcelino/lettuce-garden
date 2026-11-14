"""The `vault-fill` helper agents run under vault-run (agent-skills/using-vault-secrets):
templates with {{VAULT_<NAME>}} filled from the environment, each value escaped
for where it lands, and nothing written or printed when something is wrong."""

import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from .conftest import STREET
from .test_vault_run import HELPER as VAULT_RUN
from .test_vault_run import vault_url  # noqa: F401 (fixture)

HELPER = Path(__file__).resolve().parents[2] / "agent-skills" / "using-vault-secrets" / "vault-fill"
TRICKY = 'Rua "Falsa" <123> & co\\ TEST_938742'
VALUES = {"VAULT_ADDRESS1": STREET, "VAULT_CITY": "Lisboa", "VAULT_NOTES": TRICKY}


def fill(tmp_path, name, body, values=VALUES, output="out"):
    template = tmp_path / name
    template.write_bytes(body if isinstance(body, bytes) else body.encode())
    out = tmp_path / (output + template.suffix) if output != "-" else "-"
    env = {k: v for k, v in os.environ.items() if not k.startswith("VAULT_")}
    env.update(values)
    r = subprocess.run(
        [sys.executable, str(HELPER), str(template), str(out)], env=env, capture_output=True, timeout=30
    )
    return r, out


def test_text_is_filled_as_is_and_nothing_printed(tmp_path):
    r, out = fill(tmp_path, "letter.md", "To: {{VAULT_ADDRESS1}}, {{VAULT_CITY}}\n{{VAULT_NOTES}}\n")
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""
    assert out.read_text() == f"To: {STREET}, Lisboa\n{TRICKY}\n"
    assert out.stat().st_mode & 0o777 == 0o600
    assert "{{VAULT_CITY}}" in (tmp_path / "letter.md").read_text()  # the template is untouched


def test_json_values_are_escaped_inside_strings(tmp_path):
    template = json.dumps({"to": "hal@example.com", "body": "Ship to {{VAULT_NOTES}}\n{{VAULT_CITY}}"})
    r, out = fill(tmp_path, "send.json", template)
    assert r.returncode == 0, r.stderr
    assert json.loads(out.read_text())["body"] == f"Ship to {TRICKY}\nLisboa"


@pytest.mark.parametrize(
    "template,expected",
    [
        ("name,street\nAlfred,{{VAULT_NOTES}}\n", 'name,street\nAlfred,"Rua ""Falsa"" <123> & co\\ TEST_938742"\n'),
        ('a,"{{VAULT_CITY}}, PT"\r\nb,{{VAULT_ADDRESS1}}', f'a,"Lisboa, PT"\r\nb,{STREET}'),
    ],
)
def test_csv_values_are_quoted_and_stay_one_cell(tmp_path, template, expected):
    r, out = fill(tmp_path, "orders.csv", template, {**VALUES, "VAULT_NOTES": 'Rua "Falsa" <123> & co\\ TEST_938742'})
    assert r.returncode == 0, r.stderr
    assert out.read_bytes().decode() == expected


def test_csv_with_a_comma_in_the_value(tmp_path):
    import csv

    r, out = fill(tmp_path, "orders.csv", "id,street\n1,{{VAULT_ADDRESS1}}\n", {"VAULT_ADDRESS1": "Flat 2, 10 Main St"})
    assert r.returncode == 0, r.stderr
    assert list(csv.reader(out.open(newline=""))) == [["id", "street"], ["1", "Flat 2, 10 Main St"]]


@pytest.mark.parametrize("ext", [".html", ".xml", ".svg"])
def test_markup_values_are_escaped(tmp_path, ext):
    r, out = fill(tmp_path, f"page{ext}", "<p>{{VAULT_NOTES}}</p>")
    assert r.returncode == 0, r.stderr
    assert out.read_text() == "<p>Rua &quot;Falsa&quot; &lt;123&gt; &amp; co\\ TEST_938742</p>"


def make_docx(document_xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/test")  # stored, first
        z.writestr("word/document.xml", document_xml, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("word/media/image1.png", b"\x89PNG not xml {{VAULT_CITY}}")
    return buf.getvalue()


def test_office_files_are_filled_in_their_xml(tmp_path):
    doc = '<w:document><w:t>{{VAULT_NOTES}}</w:t><w:t xml:space="preserve">{{VAULT_CITY}}</w:t></w:document>'
    r, out = fill(tmp_path, "letter.docx", make_docx(doc))
    assert r.returncode == 0, r.stderr
    with zipfile.ZipFile(out) as z:
        infos = z.infolist()
        assert [i.filename for i in infos] == ["mimetype", "word/document.xml", "word/media/image1.png"]
        assert infos[0].compress_type == zipfile.ZIP_STORED
        xml = z.read("word/document.xml").decode()
        assert "&lt;123&gt; &amp; co" in xml and ">Lisboa<" in xml and "{{" not in xml
        assert z.read("word/media/image1.png").endswith(b"{{VAULT_CITY}}")  # only XML parts are filled


def test_a_placeholder_split_by_formatting_writes_nothing(tmp_path):
    doc = "<w:document><w:r><w:t>{{VAULT_</w:t></w:r><w:r><w:t>CITY}}</w:t></w:r></w:document>"
    r, out = fill(tmp_path, "letter.docx", make_docx(doc))
    assert r.returncode == 1 and b"split by formatting" in r.stderr and not out.exists()


@pytest.mark.parametrize("body", ["{{VAULT_COUNTRY}}", "{{ VAULT_CITY }}"])
def test_a_placeholder_without_a_value_writes_nothing(tmp_path, body):
    r, out = fill(tmp_path, "letter.txt", f"{{{{VAULT_CITY}}}} {body}")
    assert r.returncode == 1 and not out.exists()
    assert b"Lisboa" not in r.stderr and STREET.encode() not in r.stderr


def test_binary_files_are_refused(tmp_path):
    r, out = fill(tmp_path, "scan.pdf", b"%PDF-1.7\n\xff\xfe\x00{{VAULT_CITY}}")
    assert r.returncode == 1 and b"fill the source" in r.stderr and not out.exists()


def test_the_template_is_never_overwritten(tmp_path):
    template = tmp_path / "t.txt"
    template.write_text("{{VAULT_CITY}}")
    env = {**os.environ, **VALUES}
    r = subprocess.run([sys.executable, str(HELPER), str(template), str(template)], env=env, capture_output=True)
    assert r.returncode == 1 and template.read_text() == "{{VAULT_CITY}}"


def test_usage():
    r = subprocess.run([sys.executable, str(HELPER), "only-one"], capture_output=True)
    assert r.returncode == 1 and b"usage: vault-fill" in r.stderr


def test_piped_into_a_command_under_vault_run(tmp_path, vault_url):  # noqa: F811
    """The generic route for an MCP call (an email): the filled JSON goes from
    vault-fill to the next command inside vault-run, and only scrubbed output comes out."""
    template = tmp_path / "send.json"
    template.write_text(json.dumps({"body": "Meet in {{VAULT_HOME_CITY}}"}))
    sink = tmp_path / "received.json"
    pipeline = f'python3 {HELPER} {template} - | tee {sink}'
    env = {k: v for k, v in os.environ.items() if not k.startswith("VAULT_")}
    env["LOLLO_VAULT_URL"] = vault_url[0]
    r = subprocess.run(
        [sys.executable, str(VAULT_RUN), "home", "--", "sh", "-c", pipeline], env=env, capture_output=True, timeout=30
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(sink.read_text())["body"] == "Meet in Lisboa TEST_938742"
    assert b"TEST_938742" not in r.stdout and b"[VAULT_HOME_CITY]" in r.stdout
