"""App icons, drawn on first use so the repo carries no binary files: an #7aa2f7
square with an #0b1020 ring around a dot (a lens), full-bleed so it also
works as maskable."""

from __future__ import annotations

from functools import cache

from garden_common import icons

BG = (0x7A, 0xA2, 0xF7)
FG = (0x0B, 0x10, 0x20)


def _pixel(x: float, y: float) -> tuple[int, int, int]:
    d2 = (x - 0.5) ** 2 + (y - 0.5) ** 2
    if 0.20**2 <= d2 <= 0.25**2 or d2 <= 0.08**2:
        return FG
    return BG


@cache
def png(size: int) -> bytes:
    return icons.png(size, _pixel)
