"""Schedules: one or more numeric 5-field cron expressions ("0 9 * * 1-5"),
joined by ";", in the stack's timezone. The same subset letta-code's own
`--cron` takes: numbers, `*`, lists, ranges and steps; no names, seconds, `?`,
`L` or `#`. As in cron, when both day of month and day of week are restricted,
a day matching either matches."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

# (name, low, high)
FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31), ("month", 1, 12), ("day of week", 0, 7))
DAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
MAX_EXPRESSIONS = 8


class ScheduleError(ValueError):
    pass


def _field(text: str, name: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            if not step_text.isdigit() or int(step_text) == 0:
                raise ScheduleError(f"bad step in the {name} field")
            step = int(step_text)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            a, b = part.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise ScheduleError(f"bad range in the {name} field (numbers only)")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
            if step != 1:
                end = high
        else:
            raise ScheduleError(f"bad {name} field {text!r} (numbers, *, lists, ranges and steps only)")
        if not low <= start <= end <= high:
            raise ScheduleError(f"{name} out of range ({low}-{high})")
        values.update(range(start, end + 1, step))
    if name == "day of week" and 7 in values:
        values.discard(7)
        values.add(0)
    return frozenset(values)


@dataclass(frozen=True)
class Expression:
    text: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    any_day: bool
    any_weekday: bool

    @classmethod
    def parse(cls, text: str) -> Expression:
        parts = text.split()
        if len(parts) != 5:
            raise ScheduleError(f"{text!r} needs 5 fields: minute hour day-of-month month day-of-week")
        sets = [_field(p, *spec) for p, spec in zip(parts, FIELDS, strict=True)]
        return cls(" ".join(parts), *sets, any_day=parts[2] == "*", any_weekday=parts[4] == "*")

    def day_matches(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        dom = d.day in self.days
        dow = (d.isoweekday() % 7) in self.weekdays
        if self.any_day and self.any_weekday:
            return True
        if self.any_day:
            return dow
        if self.any_weekday:
            return dom
        return dom or dow

    def matches(self, t: datetime) -> bool:
        return t.minute in self.minutes and t.hour in self.hours and self.day_matches(t.date())

    def fires_on(self, d: date) -> int:
        return len(self.minutes) * len(self.hours) if self.day_matches(d) else 0


@dataclass(frozen=True)
class Schedule:
    text: str
    expressions: tuple[Expression, ...]

    @classmethod
    def parse(cls, text: str) -> Schedule:
        parts = [p.strip() for p in text.split(";") if p.strip()]
        if not parts:
            raise ScheduleError("the schedule is empty")
        if len(parts) > MAX_EXPRESSIONS:
            raise ScheduleError(f"at most {MAX_EXPRESSIONS} cron expressions")
        exprs = tuple(Expression.parse(p) for p in parts)
        # Canonical text: the group key of reports, so "0 9 * * *" and "0  9 * * *" are one schedule.
        return cls("; ".join(e.text for e in exprs), exprs)

    def matches(self, t: datetime) -> bool:
        return any(e.matches(t) for e in self.expressions)

    def fires_on(self, d: date) -> int:
        """How many minutes of day `d` fire (an upper bound when expressions overlap)."""
        return sum(e.fires_on(d) for e in self.expressions)

    def fires_within(self, start: datetime, seconds: int) -> bool:
        """Whether a minute in [start, start + seconds] fires."""
        t = start.replace(second=0, microsecond=0)
        end = start + timedelta(seconds=seconds)
        while t <= end:
            if self.matches(t):
                return True
            t += timedelta(minutes=1)
        return False

    def describe(self) -> str:
        """In words where simple ("at 08:00 and 20:00, Monday to Friday"), else the expressions."""
        parts = [_describe(e) for e in self.expressions]
        return "; ".join(parts)


def _describe(e: Expression) -> str:
    fields = e.text.split()
    if any(c in fields[0] + fields[1] for c in "*/") or len(e.minutes) * len(e.hours) > 12:
        return f"cron {e.text}"
    times = sorted(f"{h:02d}:{m:02d}" for h in e.hours for m in e.minutes)
    at = times[0] if len(times) == 1 else ", ".join(times[:-1]) + " and " + times[-1]
    if fields[2] != "*" or fields[3] != "*":
        return f"at {at}, cron {e.text}"
    if e.any_weekday:
        return f"at {at}, every day"
    days = sorted(e.weekdays)
    if days == [1, 2, 3, 4, 5]:
        return f"at {at}, Monday to Friday"
    if days == [0, 6]:
        return f"at {at}, at weekends"
    names = [DAY_NAMES[d] for d in [*[d for d in days if d], *[d for d in days if d == 0]]]
    return f"at {at}, on " + (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1])


def max_fires_per_day(schedules: list[Schedule], start: date, days: int = 31) -> int:
    """The most fires on any one day of the next `days`, all schedules together."""
    return max(sum(s.fires_on(start + timedelta(days=i)) for s in schedules) for i in range(days))
