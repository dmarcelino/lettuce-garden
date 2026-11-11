from __future__ import annotations

from datetime import date, datetime

import pytest

from garden_watchlist.config import default_schedule, parse_duration
from garden_watchlist.cron import Schedule, ScheduleError, max_fires_per_day


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def test_weekdays_at_nine():
    s = Schedule.parse("0 9 * * 1-5")
    assert s.matches(at("2027-01-15T09:00"))  # a Friday
    assert not s.matches(at("2027-01-16T09:00"))  # a Saturday
    assert not s.matches(at("2027-01-15T09:01"))
    assert s.describe() == "at 09:00, Monday to Friday"


def test_several_expressions_and_canonical_text():
    s = Schedule.parse("30  8 * * *;0 14,20 * * *")
    assert s.text == "30 8 * * *; 0 14,20 * * *"
    assert s.matches(at("2027-01-15T08:30")) and s.matches(at("2027-01-15T20:00"))
    assert s.describe() == "at 08:30, every day; at 14:00 and 20:00, every day"


def test_steps_ranges_and_sunday_as_seven():
    s = Schedule.parse("*/15 8-10 * * 7")
    assert s.matches(at("2027-01-17T10:45"))  # a Sunday
    assert not s.matches(at("2027-01-17T11:00"))


def test_day_of_month_or_day_of_week_when_both_are_set():
    s = Schedule.parse("0 9 1 * 5")
    assert s.matches(at("2027-01-15T09:00"))  # a Friday, not the 1st
    assert s.matches(at("2027-02-01T09:00"))  # the 1st, a Monday
    assert not s.matches(at("2027-01-14T09:00"))


@pytest.mark.parametrize(
    "text",
    ["", "0 9 * *", "0 9 * * MON", "60 9 * * *", "0 24 * * *", "0 9 * * L", "*/0 9 * * *", "0 9 ? * *", "5-1 * * * *"],
)
def test_bad_schedules_are_refused(text):
    with pytest.raises(ScheduleError):
        Schedule.parse(text)


def test_fires_within_and_per_day():
    s = Schedule.parse("0 8,14,20 * * *")
    assert s.fires_within(at("2027-01-15T13:45"), 20 * 60)
    assert not s.fires_within(at("2027-01-15T13:30"), 20 * 60)
    weekdays = Schedule.parse("0 9 * * 1-5")
    assert max_fires_per_day([s, weekdays], date(2027, 1, 15)) == 4
    assert max_fires_per_day([Schedule.parse("*/5 * * * *")], date(2027, 1, 15)) == 288


def test_default_times_become_cron():
    assert default_schedule("08:00,14:00,20:00") == "0 8,14,20 * * *"
    assert default_schedule("08:30, 14:00") == "0 14 * * *; 30 8 * * *"
    with pytest.raises(ValueError):
        default_schedule("8am")


def test_durations():
    assert parse_duration("6h") == 21600
    assert parse_duration("30m") == 1800
    with pytest.raises(ValueError):
        parse_duration("1w")
