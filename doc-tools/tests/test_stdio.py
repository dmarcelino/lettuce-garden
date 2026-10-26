"""The server as letta-code runs it: a child process speaking MCP on stdio."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import CANARY, make_pdf

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
}


def start(root: Path, **env: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-I", "-B", "-m", "pdf_mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": os.environ["PATH"], "PDF_WORKSPACE_ROOT": str(root), "GOTENBERG_URL": "http://127.0.0.1:9", **env},
        cwd=root,
    )


def send(p: subprocess.Popen[bytes], msg: dict) -> None:
    assert p.stdin
    p.stdin.write(json.dumps(msg).encode() + b"\n")
    p.stdin.flush()


def recv(p: subprocess.Popen[bytes]) -> dict:
    assert p.stdout
    return json.loads(p.stdout.readline())


def session(p: subprocess.Popen[bytes]) -> None:
    send(p, INIT)
    assert "result" in recv(p)
    send(p, {"jsonrpc": "2.0", "method": "notifications/initialized"})


def test_stdio_session_lists_tools_and_reads_a_pdf(root: Path) -> None:
    (root / "a.pdf").write_bytes(make_pdf([CANARY]))
    p = start(root)
    try:
        session(p)
        send(p, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in recv(p)["result"]["tools"]}
        assert {"pdf_info", "pdf_convert", "pdf_redact"} <= names
        send(p, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "pdf_extract_text", "arguments": {"path": "a.pdf"}}})
        res = recv(p)["result"]
        assert not res.get("isError") and CANARY in res["content"][0]["text"]
        send(p, {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "pdf_info", "arguments": {"path": "/etc/passwd"}}})
        bad = recv(p)["result"]
        assert bad["isError"] and "absolute" in bad["content"][0]["text"]
    finally:
        assert p.stdin
        p.stdin.close()
        p.wait(10)
    assert p.returncode == 0
    assert p.stderr
    err = p.stderr.read()
    # Nothing about the document, or its paths, on stderr.
    assert CANARY.encode() not in err and b"a.pdf" not in err


def test_stdio_refuses_to_start_without_a_workspace(tmp_path: Path) -> None:
    p = subprocess.run(
        [sys.executable, "-I", "-B", "-m", "pdf_mcp"],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        timeout=30,
    )
    assert p.returncode == 2 and b"PDF_WORKSPACE_ROOT" in p.stderr


@pytest.mark.parametrize("sig", [signal.SIGTERM])
def test_sigterm_during_a_call_leaves_nothing_behind(root: Path, sig: signal.Signals) -> None:
    """letta-code stops a server with SIGTERM; a write in flight must clean up."""
    import socket
    import threading

    # A Gotenberg that accepts the upload and never answers.
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    held: list[socket.socket] = []
    threading.Thread(target=lambda: held.append(srv.accept()[0]), daemon=True).start()
    (root / "a.docx").write_bytes(b"PK")
    p = start(root, GOTENBERG_URL=f"http://127.0.0.1:{port}")
    try:
        session(p)
        send(p, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "pdf_convert", "arguments": {"path": "a.docx"}}})
        deadline = time.time() + 10
        while not held and time.time() < deadline:
            time.sleep(0.05)
        assert held, "the server never reached the converter"
        p.send_signal(sig)
        p.wait(10)
    finally:
        if p.poll() is None:
            p.kill()
        srv.close()
    assert sorted(x.name for x in root.iterdir()) == ["a.docx"]
