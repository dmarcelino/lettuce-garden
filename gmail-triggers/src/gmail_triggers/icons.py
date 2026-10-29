"""App icons, drawn on first use so the repo carries no binary files: Lettuce's
square reversed, an #7aa2f7 square with an #0b1020 envelope (full-bleed, so it
also works as maskable)."""

from __future__ import annotations

from functools import cache

from garden_common import icons

BG = (0x7A, 0xA2, 0xF7)
FG = (0x0B, 0x10, 0x20)

# The envelope, in 0..1 coordinates.
LEFT, RIGHT, TOP, BOTTOM = 0.24, 0.76, 0.32, 0.68
STROKE = 0.045


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
    return icons.png(size, _pixel)
