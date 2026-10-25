"""App icons, drawn on first use so the repo carries no binary files: Lettuce's
square reversed, an #7aa2f7 square with an #0b1020 envelope (full-bleed, so it
also works as maskable)."""

from __future__ import annotations

import struct
import zlib
from functools import cache

BG = (0x7A, 0xA2, 0xF7)
FG = (0x0B, 0x10, 0x20)

# The envelope, in 0..1 coordinates.
LEFT, RIGHT, TOP, BOTTOM = 0.24, 0.76, 0.32, 0.68
STROKE = 0.045


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _near_segment(x: float, y: float, ax: float, ay: float, bx: float, by: float, width: float) -> bool:
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
    px, py = ax + t * dx, ay + t * dy
    return (x - px) ** 2 + (y - py) ** 2 <= width**2


def _pixel(x: float, y: float) -> tuple[int, int, int]:
    h = STROKE / 2
    inside_outer = LEFT - h <= x <= RIGHT + h and TOP - h <= y <= BOTTOM + h
    inside_inner = LEFT + h < x < RIGHT - h and TOP + h < y < BOTTOM - h
    if inside_outer and not inside_inner:
        return FG
    mid = (0.5, 0.53)
    if inside_outer and (_near_segment(x, y, LEFT, TOP, *mid, h) or _near_segment(x, y, RIGHT, TOP, *mid, h)):
        return FG
    return BG


@cache
def png(size: int) -> bytes:
    rows = bytearray()
    for j in range(size):
        rows.append(0)
        for i in range(size):
            rows.extend(_pixel((i + 0.5) / size, (j + 0.5) / size))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + _chunk(b"IEND", b"")
    )
