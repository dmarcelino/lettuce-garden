"""The landing page's icon, drawn on first use so the repo carries no binary
files: the garden pages' look (an #7aa2f7 square) with an #0b1020 sprout, two
leaves on a stem (full-bleed, so it also works as maskable)."""

from __future__ import annotations

from functools import cache

from garden_common import icons

BG = (0x7A, 0xA2, 0xF7)
FG = (0x0B, 0x10, 0x20)


def _in_leaf(x: float, y: float, cx: float, cy: float) -> bool:
    # An ellipse tilted 45 degrees, centred on (cx, cy).
    u, v = (x - cx + y - cy) / 1.414, (x - cx - (y - cy)) / 1.414
    return (u / 0.16) ** 2 + (v / 0.075) ** 2 <= 1


def _pixel(x: float, y: float) -> tuple[int, int, int]:
    if abs(x - 0.5) <= 0.025 and 0.45 <= y <= 0.76:
        return FG
    if _in_leaf(x, y, 0.38, 0.40) or _in_leaf(1 - x, y, 0.38, 0.40):
        return FG
    return BG


@cache
def png(size: int) -> bytes:
    return icons.png(size, _pixel)
