"""App icons, drawn on first use so the repo carries no binary files: each
sidecar gives its own pixel function (0..1 coordinates to an RGB colour)."""

from __future__ import annotations

import struct
import zlib
from collections.abc import Callable

Pixel = Callable[[float, float], tuple[int, int, int]]


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def png(size: int, pixel: Pixel) -> bytes:
    """A square RGB PNG of `size` pixels, each coloured by `pixel` at its centre."""
    rows = bytearray()
    for j in range(size):
        rows.append(0)
        for i in range(size):
            rows.extend(pixel((i + 0.5) / size, (j + 0.5) / size))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + _chunk(b"IEND", b"")
    )
