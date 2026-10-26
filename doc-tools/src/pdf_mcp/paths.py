"""The workspace jail: every file the tools read or write is reached from one
directory fd, a component at a time, without following symlinks.

There are no string-prefix checks: each component is opened with O_NOFOLLOW
relative to its parent's fd, so a symlink anywhere on the path, or one swapped
in while the walk runs, makes the open fail instead of leaving the workspace.
Inputs are read through the fd that was checked. Outputs go to a dot temp file
in the destination directory and are renamed over the target only when
complete.

This guards against mistakes, not against the agent: agents are root in the
same container and can reach every file with their own shell. Isolation
between people comes from separate stacks.
"""

from __future__ import annotations

import os
import secrets
import stat
import time
from dataclasses import dataclass

TMP_SUFFIX = ".pdf-mcp-tmp"
# A temp file this old was left by a run that was killed; the next write to
# its directory removes it.
STALE_TMP_SECONDS = 600
MAX_PATH_CHARS = 1024
MAX_COMPONENT_CHARS = 255

# Temp files being written right now, as (directory fd, name), so a signal
# handler can remove them before the process exits.
_pending: set[tuple[int, str]] = set()

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
# O_NONBLOCK: opening a FIFO must not hang; it is refused right after.
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class PathError(Exception):
    """A path the tools refuse. The message names only the relative path."""


@dataclass(frozen=True)
class Input:
    path: str
    data: bytes
    dev: int
    ino: int


def split(path: object) -> list[str]:
    """The components of a workspace-relative path, or PathError."""
    if not isinstance(path, str) or not path:
        raise PathError("a path is required")
    if len(path) > MAX_PATH_CHARS:
        raise PathError("path is too long")
    if "\x00" in path:
        raise PathError("path contains a NUL character")
    if path.startswith("/"):
        raise PathError(f"{path!r} is absolute; give a path relative to your workspace")
    parts = path.split("/")
    for p in parts:
        if p in ("", ".", ".."):
            raise PathError(f"{path!r}: empty, '.' and '..' components are not allowed")
        if len(p) > MAX_COMPONENT_CHARS:
            raise PathError(f"{path!r}: a component is too long")
    return parts


class Workspace:
    def __init__(self, root: str) -> None:
        try:
            self._root = os.open(root, _DIR_FLAGS)
        except OSError as e:
            raise PathError("the workspace directory cannot be opened") from e

    def close(self) -> None:
        os.close(self._root)

    def _walk(self, parts: list[str], path: str, create: bool) -> int:
        """An fd of the directory PARTS name, below the root."""
        fd = os.dup(self._root)
        try:
            for p in parts:
                try:
                    nfd = os.open(p, _DIR_FLAGS, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise PathError(f"{path!r}: directory {p!r} does not exist") from None
                    try:
                        os.mkdir(p, 0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                    nfd = os.open(p, _DIR_FLAGS, dir_fd=fd)
                except OSError as e:
                    raise PathError(f"{path!r}: {p!r} is not a directory (symbolic links are not followed)") from e
                os.close(fd)
                fd = nfd
            return fd
        except BaseException:
            os.close(fd)
            raise

    def read(self, path: str, max_bytes: int) -> Input:
        """The bytes of a regular file in the workspace."""
        parts = split(path)
        dfd = self._walk(parts[:-1], path, create=False)
        try:
            try:
                fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=dfd)
            except FileNotFoundError:
                raise PathError(f"{path!r} does not exist") from None
            except OSError as e:
                raise PathError(f"{path!r} cannot be opened (symbolic links are not followed)") from e
        finally:
            os.close(dfd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise PathError(f"{path!r} is not a regular file")
            if st.st_size > max_bytes:
                raise PathError(f"{path!r} is larger than the {max_bytes // (1024 * 1024)} MB limit")
            os.set_blocking(fd, True)
            chunks, total = [], 0
            while True:
                b = os.read(fd, 1 << 20)
                if not b:
                    break
                total += len(b)
                if total > max_bytes:
                    raise PathError(f"{path!r} grew past the size limit while it was read")
                chunks.append(b)
            return Input(path, b"".join(chunks), st.st_dev, st.st_ino)
        finally:
            os.close(fd)

    def write(self, path: str, data: bytes, *, overwrite: bool = False, sources: tuple[Input, ...] = ()) -> str:
        """Writes DATA to PATH atomically. Refuses an existing target unless
        OVERWRITE, and any of SOURCES whatever OVERWRITE says. Returns PATH."""
        parts = split(path)
        name = parts[-1]
        if name.endswith(TMP_SUFFIX):
            raise PathError(f"{path!r}: that suffix is reserved")
        dfd = self._walk(parts[:-1], path, create=True)
        tmp = f".{name[:200]}.{secrets.token_hex(6)}{TMP_SUFFIX}"
        try:
            _sweep(dfd)
            self._check_target(dfd, name, path, overwrite, sources)
            _pending.add((dfd, tmp))
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=dfd)
            try:
                with os.fdopen(fd, "wb", closefd=False) as f:
                    f.write(data)
                    f.flush()
                    os.fsync(fd)
            finally:
                os.close(fd)
            # Checked again: the target may have appeared meanwhile.
            self._check_target(dfd, name, path, overwrite, sources)
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            _pending.discard((dfd, tmp))
            tmp = ""
            return path
        finally:
            if tmp:
                try:
                    os.unlink(tmp, dir_fd=dfd)
                except FileNotFoundError:
                    pass
            _pending.discard((dfd, tmp))
            os.close(dfd)

    @staticmethod
    def _check_target(dfd: int, name: str, path: str, overwrite: bool, sources: tuple[Input, ...]) -> None:
        try:
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if any((st.st_dev, st.st_ino) == (s.dev, s.ino) for s in sources):
            raise PathError(f"{path!r} is a source of this operation; choose another output name")
        if not stat.S_ISREG(st.st_mode):
            raise PathError(f"{path!r} exists and is not a regular file")
        if not overwrite:
            raise PathError(f"{path!r} already exists; choose another name or pass overwrite=true")

    def batch(self, max_files: int) -> OutputBatch:
        return OutputBatch(self, max_files)


class OutputBatch:
    """Several outputs of one operation (split, render, images), with a cap."""

    def __init__(self, ws: Workspace, max_files: int) -> None:
        self._ws = ws
        self._max = max_files
        self.written: list[str] = []

    def write(self, path: str, data: bytes, *, overwrite: bool, sources: tuple[Input, ...] = ()) -> None:
        if len(self.written) >= self._max:
            raise PathError(f"more than {self._max} output files; narrow the page range")
        self.written.append(self._ws.write(path, data, overwrite=overwrite, sources=sources))


def remove_pending() -> None:
    """Removes the temp files of writes still in progress (for a signal
    handler that is about to end the process)."""
    for dfd, name in list(_pending):
        try:
            os.unlink(name, dir_fd=dfd)
        except OSError:
            pass


def _sweep(dfd: int) -> None:
    """Removes temp files killed runs left in this directory."""
    now = time.time()
    try:
        names = os.listdir(dfd)
    except OSError:
        return
    for n in names:
        if n.startswith(".") and n.endswith(TMP_SUFFIX):
            try:
                st = os.stat(n, dir_fd=dfd, follow_symlinks=False)
                if stat.S_ISREG(st.st_mode) and now - st.st_mtime > STALE_TMP_SECONDS:
                    os.unlink(n, dir_fd=dfd)
            except OSError:
                pass


def join(directory: str, name: str) -> str:
    """DIRECTORY/NAME, both checked as workspace-relative."""
    split(directory)
    split(name)
    return f"{directory}/{name}"


def stem(path: str) -> str:
    base = split(path)[-1]
    return base.rsplit(".", 1)[0] if "." in base[1:] else base
