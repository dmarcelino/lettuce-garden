"""The app icon, drawn on first use so the repo carries no binary files: a blue
square with a white envelope."""

from __future__ import annotations

from functools import cache

from garden_common import icons

BG = (0, 90, 158)
FG = (255, 255, 255)


def _pixel(x: float, y: float) -> tuple[int, int, int]:
    # Coordinates in 0..1. An envelope body with a flap drawn as two diagonals.
    if not (0.18 <= x <= 0.82 and 0.28 <= y <= 0.72):
        return BG
    # The flap: the two lines from the top corners to the top-centre.
    if y <= 0.5:
        t = (y - 0.28) / 0.22
        left = 0.18 + t * (0.5 - 0.18)
        right = 0.82 - t * (0.82 - 0.5)
        if abs(x - left) <= 0.03 or abs(x - right) <= 0.03:
            return FG
    # The body outline.
    if abs(x - 0.18) <= 0.03 or abs(x - 0.82) <= 0.03 or abs(y - 0.28) <= 0.03 or abs(y - 0.72) <= 0.03:
        return FG
    return BG


@cache
def png(size: int) -> bytes:
    return icons.png(size, _pixel)
