from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from pdf_mcp.paths import TMP_SUFFIX, PathError, Workspace

MB = 1024 * 1024


@pytest.mark.parametrize(
    "path",
    ["", "/etc/passwd", "../agent-2/theirs.pdf", "a/../../outside.pdf", "a//b.pdf", "./a.pdf", "a\x00b.pdf", "x" * 2000],
)
def test_bad_paths_are_refused(ws: Workspace, path: str) -> None:
    with pytest.raises(PathError):
        ws.read(path, MB)
    with pytest.raises(PathError):
        ws.write(path, b"x")


def test_reads_a_file_in_a_subdirectory(ws: Workspace, root: Path) -> None:
    (root / "in").mkdir()
    (root / "in" / "a.txt").write_bytes(b"hello")
    got = ws.read("in/a.txt", MB)
    assert got.data == b"hello" and got.path == "in/a.txt"


def test_symlink_as_the_file_is_refused(ws: Workspace, root: Path) -> None:
    (root / "link.pdf").symlink_to(root.parent.parent / "outside.pdf")
    with pytest.raises(PathError, match="symbolic links"):
        ws.read("link.pdf", MB)


def test_symlink_as_a_directory_is_refused(ws: Workspace, root: Path) -> None:
    (root / "other").symlink_to(root.parent / "agent-2")
    with pytest.raises(PathError, match="symbolic links"):
        ws.read("other/theirs.pdf", MB)
    with pytest.raises(PathError, match="symbolic links"):
        ws.write("other/new.pdf", b"x")
    assert not (root.parent / "agent-2" / "new.pdf").exists()


def test_writing_through_a_symlink_target_replaces_the_link_not_its_target(ws: Workspace, root: Path) -> None:
    target = root.parent.parent / "outside.pdf"
    before = target.read_bytes()
    (root / "out.pdf").symlink_to(target)
    with pytest.raises(PathError, match="not a regular file"):
        ws.write("out.pdf", b"new", overwrite=True)
    assert target.read_bytes() == before


def test_directory_swapped_for_a_symlink_mid_walk_is_refused(ws: Workspace, root: Path) -> None:
    """A race: the directory is replaced by a symlink while reads run."""
    (root / "d").mkdir()
    (root / "d" / "f.txt").write_bytes(b"mine")
    stop = threading.Event()
    escaped: list[bytes] = []

    def swap() -> None:
        while not stop.is_set():
            os.rename(root / "d", root / "d.real")
            os.symlink(root.parent / "agent-2", root / "d")
            os.unlink(root / "d")
            os.rename(root / "d.real", root / "d")

    t = threading.Thread(target=swap)
    t.start()
    try:
        deadline = time.time() + 1.5
        while time.time() < deadline:
            try:
                escaped.append(ws.read("d/theirs.pdf", MB).data)
            except PathError:
                pass
    finally:
        stop.set()
        t.join()
    assert escaped == []


@pytest.mark.parametrize("kind", ["fifo", "dir"])
def test_special_files_are_refused(ws: Workspace, root: Path, kind: str) -> None:
    p = root / "special"
    if kind == "fifo":
        os.mkfifo(p)
    else:
        p.mkdir()
    with pytest.raises(PathError):
        ws.read("special", MB)


def test_device_is_refused(tmp_path: Path) -> None:
    ws = Workspace("/dev")
    try:
        with pytest.raises(PathError, match="not a regular file"):
            ws.read("null", MB)
    finally:
        ws.close()


def test_size_limit(ws: Workspace, root: Path) -> None:
    (root / "big.bin").write_bytes(b"x" * (MB + 1))
    with pytest.raises(PathError, match="larger than"):
        ws.read("big.bin", MB)


def test_write_refuses_an_existing_file_unless_overwrite(ws: Workspace, root: Path) -> None:
    (root / "a.pdf").write_bytes(b"old")
    with pytest.raises(PathError, match="already exists"):
        ws.write("a.pdf", b"new")
    assert (root / "a.pdf").read_bytes() == b"old"
    ws.write("a.pdf", b"new", overwrite=True)
    assert (root / "a.pdf").read_bytes() == b"new"


def test_write_never_replaces_a_source(ws: Workspace, root: Path) -> None:
    (root / "a.pdf").write_bytes(b"old")
    src = ws.read("a.pdf", MB)
    with pytest.raises(PathError, match="source"):
        ws.write("a.pdf", b"new", overwrite=True, sources=(src,))
    assert (root / "a.pdf").read_bytes() == b"old"


def test_write_creates_directories_and_leaves_no_temp_files(ws: Workspace, root: Path) -> None:
    ws.write("out/sub/a.pdf", b"data")
    assert (root / "out" / "sub" / "a.pdf").read_bytes() == b"data"
    assert [p.name for p in (root / "out" / "sub").iterdir()] == ["a.pdf"]


def test_failed_write_leaves_no_temp_file(ws: Workspace, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        ws.write("a.pdf", b"data")
    assert list(root.iterdir()) == []


def test_stale_temp_files_are_swept(ws: Workspace, root: Path) -> None:
    stale = root / f".a.pdf.abc{TMP_SUFFIX}"
    fresh = root / f".b.pdf.def{TMP_SUFFIX}"
    stale.write_bytes(b"x")
    fresh.write_bytes(b"x")
    old = time.time() - 3600
    os.utime(stale, (old, old))
    ws.write("c.pdf", b"x")
    assert not stale.exists() and fresh.exists()


def test_reserved_suffix_is_refused(ws: Workspace) -> None:
    with pytest.raises(PathError, match="reserved"):
        ws.write(f"x{TMP_SUFFIX}", b"x")


def test_workspace_root_may_not_be_a_symlink(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    with pytest.raises(PathError):
        Workspace(str(tmp_path / "link"))


def test_remove_pending_deletes_a_write_in_progress(ws: Workspace, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """What the SIGTERM handler relies on: a half-written temp file is known."""
    from pdf_mcp import paths

    seen: list[list[str]] = []

    def interrupted(*a: object, **k: object) -> None:
        seen.append(sorted(p.name for p in root.iterdir()))
        paths.remove_pending()
        seen.append(sorted(p.name for p in root.iterdir()))
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "fsync", interrupted)
    with pytest.raises(KeyboardInterrupt):
        ws.write("a.pdf", b"data")
    assert len(seen[0]) == 1 and seen[0][0].endswith(TMP_SUFFIX)
    assert seen[1] == [] and list(root.iterdir()) == []
