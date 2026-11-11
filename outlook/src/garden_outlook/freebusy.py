"""Free/busy maths for the find_free_time tool: parse Graph date-times and turn a
set of busy intervals into the free gaps within a window. Kept dependency-free so
it is unit-testable on its own."""

from __future__ import annotations

from datetime import datetime, timedelta

# The most events find_free_time pages through before giving up on completeness.
FREE_TIME_EVENT_CAP = 500


def parse_dt(value: object) -> datetime | None:
    """Parse a Graph dateTime (naive, e.g. '2026-01-20T09:00:00.0000000') or a
    user-supplied ISO string. Returns None if unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "")
    if "." in text:
        head, frac = text.split(".", 1)
        digits = "".join(ch for ch in frac if ch.isdigit())[:6]
        text = f"{head}.{digits}" if digits else head
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def free_gaps(
    window_start: datetime,
    window_end: datetime,
    busy: list[tuple[datetime, datetime]],
    minimum: timedelta,
) -> list[dict[str, str]]:
    """Gaps of at least `minimum` between the busy intervals, within the window.
    Busy intervals are clamped to the window and overlaps merged first."""
    clamped = sorted(
        (max(s, window_start), min(e, window_end)) for s, e in busy if e > window_start and s < window_end
    )
    merged: list[list[datetime]] = []
    for s, e in clamped:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    gaps: list[dict[str, str]] = []
    cursor = window_start
    for s, e in merged:
        if s - cursor >= minimum:
            gaps.append({"start": cursor.isoformat(), "end": s.isoformat()})
        cursor = max(cursor, e)
    if window_end - cursor >= minimum:
        gaps.append({"start": cursor.isoformat(), "end": window_end.isoformat()})
    return gaps
