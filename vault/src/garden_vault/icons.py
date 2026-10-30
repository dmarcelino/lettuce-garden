"""App icons, drawn on first use so the repo carries no binary files: a green
square with a white key-hole shape (full-bleed, so it also works as maskable)."""

from __future__ import annotations

from functools import cache

from garden_common import icons

BG = (46, 125, 50)
FG = (255, 255, 255)


def _pixel(x: float, y: float) -> tuple[int, int, int]:
    # Coordinates in 0..1. Key hole: a circle above a tapering stem.
    cx, cy = 0.5, 0.42
    if (x - cx) ** 2 + (y - cy) ** 2 <= 0.12**2:
        return FG
    if 0.42 <= y <= 0.72 and abs(x - cx) <= 0.04 + (y - 0.42) * 0.12:
        return FG
    return BG


@cache
def png(size: int) -> bytes:
    return icons.png(size, _pixel)
