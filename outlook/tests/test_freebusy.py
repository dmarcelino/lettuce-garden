from datetime import timedelta

from garden_outlook.freebusy import free_gaps, parse_dt


def test_parse_graph_fractional_seconds():
    assert parse_dt("2026-01-20T09:00:00.0000000").hour == 9


def test_parse_trailing_z():
    assert parse_dt("2026-01-20T09:00:00Z").minute == 0


def test_parse_rejects_garbage():
    assert parse_dt("not a date") is None
    assert parse_dt(None) is None
    assert parse_dt("") is None


def _w():
    return parse_dt("2026-01-20T09:00:00"), parse_dt("2026-01-20T17:00:00")


def test_gaps_around_merged_overlapping_busy():
    ws, we = _w()
    busy = [
        (parse_dt("2026-01-20T10:00:00"), parse_dt("2026-01-20T11:00:00")),
        (parse_dt("2026-01-20T10:30:00"), parse_dt("2026-01-20T12:00:00")),  # overlaps -> 10:00-12:00
    ]
    assert free_gaps(ws, we, busy, timedelta(minutes=30)) == [
        {"start": "2026-01-20T09:00:00", "end": "2026-01-20T10:00:00"},
        {"start": "2026-01-20T12:00:00", "end": "2026-01-20T17:00:00"},
    ]


def test_gap_below_minimum_is_dropped():
    ws, we = _w()
    busy = [(parse_dt("2026-01-20T09:10:00"), parse_dt("2026-01-20T17:00:00"))]  # only a 10-min gap
    assert free_gaps(ws, we, busy, timedelta(minutes=30)) == []


def test_busy_outside_window_ignored_and_clamped():
    ws, we = _w()
    busy = [(parse_dt("2026-01-19T00:00:00"), parse_dt("2026-01-19T23:00:00"))]  # previous day
    assert free_gaps(ws, we, busy, timedelta(minutes=30)) == [
        {"start": "2026-01-20T09:00:00", "end": "2026-01-20T17:00:00"}
    ]


def test_free_whole_window_when_no_busy():
    ws, we = _w()
    assert free_gaps(ws, we, [], timedelta(minutes=30)) == [
        {"start": "2026-01-20T09:00:00", "end": "2026-01-20T17:00:00"}
    ]
