"""What the MCP tools, the page and the scheduler do, in one place: sources
(inspect, test, put, delete), monitors, the scheduled fetches, and turning a
schedule that fires into a report for the executor."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import anyio
from garden_common.executor import AGENT_ID
from garden_common.text import encodable, one_line

from .config import (
    FETCH_AHEAD_SECONDS,
    FOLLOW_REFRESH_SECONDS,
    MAX_DAYS_AHEAD,
    MAX_NAME,
    MAX_SCHEDULE,
    MAX_SOURCES_PER_MONITOR,
    MAX_TASK_DESCRIPTION,
    MAX_TASK_NAME,
    MAX_TERM,
    MAX_TERMS,
    NAME,
    TEST_VALID_SECONDS,
    TOOL_SECONDS,
    TOOL_WAIT,
    Settings,
)
from .cron import Schedule, ScheduleError, max_fires_per_day
from .digest import MonitorDigest, monitor_digest, more, render
from .extract import fill_rates
from .fetch import Fetcher
from .inspect import inspect
from .runner import RunResult, run_source
from .spec import Spec, SpecError, parse_spec
from .store import QUEUED, Monitor, Report, Store, new_report_id

log = logging.getLogger("garden_monitors.service")

CONVERSATIONS = ("new", "default")
TEST_ITEMS = 10
# A test fetches fewer pages and follows fewer links than a scheduled run, to
# answer within the MCP call's time.
TEST_PAGES = 3
TEST_FOLLOW = 5


class ToolError(Exception):
    """What an agent asked for that can't be done; the message says why."""


def _agent(agent_id: Any) -> str:
    if not isinstance(agent_id, str) or not AGENT_ID.fullmatch(agent_id):
        raise ToolError("agent_id must be your agent id (agent-...), as in $LETTA_AGENT_ID")
    return agent_id


def _terms(value: Any, what: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_TERMS:
        raise ToolError(f"{what} must be a list of at most {MAX_TERMS} words or phrases")
    out = []
    for term in value:
        if not isinstance(term, str) or not term.strip() or len(term) > MAX_TERM:
            raise ToolError(f"each of {what} must be text of 1 to {MAX_TERM} characters")
        out.append(one_line(term, MAX_TERM))
    return list(dict.fromkeys(out))


def _required(spec: Spec) -> tuple[str, ...]:
    return ("title", "start") if spec.item_kind == "event" else ("title",)


class Service:
    def __init__(self, store: Store, fetcher: Fetcher, settings: Settings):
        self.store = store
        self.fetcher = fetcher
        self.settings = settings

    # -- settings -------------------------------------------------------------------
    def settings_info(self) -> dict[str, Any]:
        s = self.settings
        return {
            "timezone": s.timezone,
            "default_schedule": s.default_schedule,
            "default_schedule_in_words": Schedule.parse(s.default_schedule).describe(),
            "max_reports_per_day_per_agent": s.max_reports_per_day,
            "min_fetch_interval_minutes": s.min_interval // 60,
            "max_sources": s.max_sources,
            "max_monitors": s.max_monitors,
            "note": "Schedules are numeric 5-field cron in this timezone, not UTC. Several expressions join with ';'.",
        }

    def _spec(self, obj: Any) -> Spec:
        try:
            return parse_spec(obj, self.settings.min_interval)
        except SpecError as e:
            raise ToolError(f"spec: {e}") from e

    def _stored_spec(self, name: str) -> Spec | None:
        """The stored spec, or None (logged) when there is none or it no longer
        passes the checks: one bad row must not stop the other sources. Its interval
        is checked against the admin's minimum when it runs (due_sources), not here."""
        raw = self.store.source_spec(name)
        if raw is None:
            return None
        try:
            return parse_spec(raw, 0)
        except SpecError as e:
            log.warning("source %s: its saved spec no longer passes the checks (%s); skipped", name, e)
            return None

    def _schedule(self, monitor: Monitor) -> Schedule | None:
        try:
            return Schedule.parse(monitor.schedule)
        except ScheduleError:
            log.warning("a monitor's saved schedule no longer parses; skipped")
            return None

    # -- sources ----------------------------------------------------------------------
    async def source_inspect(self, url: Any) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise ToolError("url is required")
        with anyio.move_on_after(TOOL_SECONDS):
            return await inspect(self.fetcher, url.strip(), max_wait=TOOL_WAIT)
        return {"url": url.strip(), "error": f"took too long (over {int(TOOL_SECONDS)} s); try a narrower page"}

    async def source_test(self, spec_obj: Any) -> dict[str, Any]:
        spec = self._spec(spec_obj)
        out: dict[str, Any] = {"name": spec.name, "kind": spec.kind}
        with anyio.move_on_after(TOOL_SECONDS):
            result = await run_source(
                spec, self.fetcher, max_pages=TEST_PAGES, max_follow=TEST_FOLLOW, max_wait=TOOL_WAIT
            )
            return self._test_result(spec, result, out)
        out.update(passed=False, error=f"took too long (over {int(TOOL_SECONDS)} s); try fewer pages or follows")
        return out

    def _test_result(self, spec: Spec, result: RunResult, out: dict[str, Any]) -> dict[str, Any]:
        if result.error:
            out.update(passed=False, error=result.error, sample=result.sample)
            return out
        items = result.items
        rates = fill_rates(items, spec)
        problems = []
        if not items:
            problems.append("no items found")
        else:
            titled = sum(1 for i in items if i.fields.get("title")) / len(items)
            if titled < 0.8:
                problems.append("fewer than 8 in 10 items have a title")
        out.update(
            passed=not problems,
            items_found=len(items),
            pages_fetched=result.pages,
            followed=result.followed,
            fill_rates=rates,
            items=[i.as_dict() for i in items[:TEST_ITEMS]],
        )
        if problems:
            out["problems"] = problems
            out["sample"] = result.sample
        else:
            self.store.record_test(spec.fingerprint)
            out["next"] = "source_put this exact spec within 15 minutes to start it"
        return out

    def source_put(self, spec_obj: Any = None, revert: Any = None, name: Any = None) -> dict[str, Any]:
        if revert is not None:
            if not isinstance(name, str) or self.store.source_spec(name) is None:
                raise ToolError("revert needs the name of an existing source")
            versions = self.store.source_versions(name)
            if not isinstance(revert, int) or isinstance(revert, bool) or not 1 <= revert < len(versions):
                raise ToolError(f"revert is how many versions back, 1 to {len(versions) - 1}")
            spec = self._spec(versions[revert][1])
        else:
            spec = self._spec(spec_obj)
            since = self.store.now() - TEST_VALID_SECONDS
            if not self.store.tested_since(spec.fingerprint, since):
                raise ToolError("run source_test on this exact spec first; a passing test is valid for 15 minutes")
        current = self._stored_spec(spec.name)
        if current is None and self.store.count_sources() >= self.settings.max_sources:
            raise ToolError(f"this stack has {self.settings.max_sources} sources, the most allowed; delete one")
        reset = current is None or (current.url, current.kind, current.extract, current.pages, current.follow) != (
            spec.url,
            spec.kind,
            spec.extract,
            spec.pages,
            spec.follow,
        )
        self.store.put_source(spec.name, spec.raw, reset)
        return {"saved": spec.name, "spec": spec.raw, "first_fetch": "within a minute"}

    def source_list(self) -> dict[str, Any]:
        states = self.store.source_states()
        users: dict[str, list[str]] = {}
        for m in self.store.monitors():
            for s in m.sources:
                users.setdefault(s, []).append(m.name)
        out = []
        for name, raw in self.store.source_specs().items():
            st = states.get(name)
            out.append(
                {
                    "name": name,
                    "url": raw.get("url", ""),
                    "kind": raw.get("kind", ""),
                    "every": raw.get("every", ""),
                    "healthy": st.healthy if st else True,
                    "used_by": users.get(name, []),
                }
            )
        return {"sources": out}

    def source_status(self, name: Any) -> dict[str, Any]:
        if not isinstance(name, str) or self.store.source_spec(name) is None:
            raise ToolError("no source of that name")
        st = self.store.source_state(name)
        return {
            "name": name,
            "spec": self.store.source_spec(name),
            "healthy": st.healthy,
            "unhealthy_reason": st.unhealthy_reason,
            "last_run": self._iso(st.last_run),
            "last_ok": self._iso(st.last_ok),
            "failures_in_a_row": st.failures,
            "last_error": st.last_error,
            "items_per_run": st.counts,
            "fill_rates": st.fill,
            "items_kept": self.store.count_items(name),
            "sample": st.sample,
            "versions": [self._iso(at) for at, _ in self.store.source_versions(name)],
        }

    def source_delete(self, name: Any) -> dict[str, Any]:
        if not isinstance(name, str) or self.store.source_spec(name) is None:
            raise ToolError("no source of that name")
        users = self.store.source_users(name)
        if users:
            raise ToolError("monitors use it: " + ", ".join(m.name for m in users) + "; change or delete them first")
        self.store.delete_source(name)
        return {"deleted": name}

    # -- monitors ---------------------------------------------------------------------
    def monitor_put(
        self,
        agent_id: Any,
        name: Any,
        sources: Any,
        schedule: Any = None,
        conversation: Any = "new",
        include: Any = None,
        exclude: Any = None,
        days_ahead: Any = None,
        news: Any = False,
    ) -> dict[str, Any]:
        agent = _agent(agent_id)
        if not isinstance(name, str) or not NAME.fullmatch(name) or len(name) > MAX_NAME:
            raise ToolError("name must be lowercase letters, digits and -")
        if not isinstance(sources, list) or not 1 <= len(sources) <= MAX_SOURCES_PER_MONITOR:
            raise ToolError(f"sources must list 1 to {MAX_SOURCES_PER_MONITOR} source names")
        known = self.store.source_specs()
        missing = [s for s in sources if not isinstance(s, str) or s not in known]
        if missing:
            raise ToolError(f"no such source: {missing[0]!s:.80}; source_put it first")
        if schedule is None or (isinstance(schedule, str) and not schedule.strip()):
            schedule = self.settings.default_schedule
        if not isinstance(schedule, str) or len(schedule) > MAX_SCHEDULE:
            raise ToolError("schedule is numeric 5-field cron in the stack's timezone, such as 0 9 * * 1-5")
        try:
            parsed = Schedule.parse(schedule)
        except ScheduleError as e:
            raise ToolError(f"schedule: {e}") from e
        if conversation not in CONVERSATIONS:
            raise ToolError("conversation must be new (a new conversation each report) or default")
        if days_ahead is not None and (
            not isinstance(days_ahead, int) or isinstance(days_ahead, bool) or not 0 <= days_ahead <= MAX_DAYS_AHEAD
        ):
            raise ToolError(f"days_ahead must be a whole number from 0 to {MAX_DAYS_AHEAD}")
        if not isinstance(news, bool):
            raise ToolError("news must be true or false")
        monitor = Monitor(
            agent_id=agent,
            name=name,
            sources=list(dict.fromkeys(sources)),
            schedule=parsed.text,
            conversation=conversation,
            include=_terms(include, "include"),
            exclude=_terms(exclude, "exclude"),
            days_ahead=days_ahead,
            news=news,
        )
        current = self.store.monitor_named(agent, name)
        if current is None and self.store.count_monitors() >= self.settings.max_monitors:
            raise ToolError(f"this stack has {self.settings.max_monitors} monitors, the most allowed; delete one")
        # One report per schedule and conversation (as fire groups them), so a
        # schedule used with both conversations counts twice.
        groups = {(m.schedule, m.conversation) for m in self.store.monitors(agent) if m.name != name}
        groups.add((parsed.text, conversation))
        today = datetime.now(self.settings.tz).date()
        most = max_fires_per_day([Schedule.parse(s) for s, _ in groups], today)
        if most > self.settings.max_reports_per_day:
            raise ToolError(
                f"your monitors' schedules would report up to {most} times a day; the admin allows "
                f"{self.settings.max_reports_per_day} per agent. Share a schedule with another monitor, "
                "or fire less often"
            )
        stored = self.store.put_monitor(monitor)
        return {
            "saved": stored.name,
            "schedule": stored.schedule,
            "schedule_in_words": parsed.describe() + f" ({self.settings.timezone})",
            "monitor": stored.as_dict(),
            "next": "show the person monitor_preview, and tell them the schedule in words",
        }

    def monitor_list(self, agent_id: Any) -> dict[str, Any]:
        agent = _agent(agent_id)
        return {
            "monitors": [
                {**m.as_dict(), "schedule_in_words": Schedule.parse(m.schedule).describe()}
                for m in self.store.monitors(agent)
            ]
        }

    def _monitor(self, agent_id: Any, name: Any) -> Monitor:
        agent = _agent(agent_id)
        m = self.store.monitor_named(agent, name) if isinstance(name, str) else None
        if m is None:
            raise ToolError("no monitor of that name for this agent")
        return m

    def monitor_delete(self, agent_id: Any, name: Any) -> dict[str, Any]:
        m = self._monitor(agent_id, name)
        self.store.delete_monitor(m.id)
        return {"deleted": m.name}

    def monitor_preview(self, agent_id: Any, name: Any) -> dict[str, Any]:
        m = self._monitor(agent_id, name)
        now = self.store.now()
        d = monitor_digest(self.store, m, now, self.settings.tz, self.store.source_states())
        return {
            "monitor": m.name,
            "would_report": d.worth_reporting,
            "rows": d.rows[:20],
            "rows_total": len(d.rows) + d.left_out,
            "needs_repair": [{"source": a.source, "reason": a.reason} for a in d.alerts],
            "note": "a preview: nothing is marked as reported",
        }

    def digest_more(self, digest_id: Any, offset: Any = 0) -> dict[str, Any]:
        report = self.store.report(digest_id) if isinstance(digest_id, str) else None
        if report is None:
            raise ToolError("no report with that digest_id")
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise ToolError("offset must be a whole number")
        return {"digest_id": report.id, **more(report.digest, offset)}

    # -- the scheduler's work -------------------------------------------------------------
    def due_sources(self, now: int) -> list[str]:
        """Sources whose interval has passed, or whose monitors' schedules fire soon
        with an older run; oldest run first."""
        states = self.store.source_states()
        local = datetime.fromtimestamp(now, self.settings.tz)
        soon: set[str] = set()
        for m in self.store.monitors():
            schedule = self._schedule(m)
            if not m.paused and schedule and schedule.fires_within(local, FETCH_AHEAD_SECONDS):
                soon.update(m.sources)
        due = []
        for name in self.store.source_specs():
            spec = self._stored_spec(name)
            if spec is None:
                continue
            st = states.get(name)
            last = st.last_run if st else None
            # The admin's minimum applies to sources saved before it was raised, too.
            every = max(spec.every, self.settings.min_interval)
            if last is None or now - last >= every or (name in soon and now - last >= FETCH_AHEAD_SECONDS):
                due.append((last or 0, name))
        return [name for _, name in sorted(due)]

    async def fetch_source(self, name: str) -> None:
        spec = self._stored_spec(name)
        if spec is None:
            return
        st = self.store.source_state(name)
        result = await run_source(
            spec,
            self.fetcher,
            etag=st.etag,
            last_modified=st.last_modified,
            known_url=lambda url: self.store.known_url(name, url, self.store.now() - FOLLOW_REFRESH_SECONDS),
        )
        if self.store.source_spec(name) is None:
            return  # deleted while it ran
        self.store.record_run(
            name,
            items=None if result.error or result.not_modified else result.items,
            watch=spec.watch,
            required=_required(spec),
            error=result.error,
            not_modified=result.not_modified,
            etag=result.etag,
            last_modified=result.last_modified,
            sample=result.sample,
            fill=fill_rates(result.items, spec) if result.items else {},
        )
        log.info(
            "source %s: %s",
            name,
            f"failed ({result.error})"
            if result.error
            else "not modified"
            if result.not_modified
            else f"{len(result.items)} items",
        )

    def fire(self, minute: datetime) -> list[str]:
        """For every group (agent, schedule, conversation) whose schedule matches
        `minute`, a report when something is worth reporting; returns the ids queued."""
        groups: dict[tuple[str, str, str], list[Monitor]] = {}
        for m in self.store.monitors():
            schedule = self._schedule(m)
            if not m.paused and schedule and schedule.matches(minute):
                groups.setdefault((m.agent_id, m.schedule, m.conversation), []).append(m)
        queued = []
        now = self.store.now()
        states = self.store.source_states()
        for (agent, schedule, conversation), monitors in groups.items():
            if self.store.pending_report(agent, schedule, conversation):
                log.info("report for %s skipped: the last one is still on its way", agent)
                continue
            digests = [monitor_digest(self.store, m, now, self.settings.tz, states) for m in monitors]
            if not any(d.worth_reporting for d in digests):
                self.store.mark_empty([m.id for m in monitors], now)
                continue
            # Monitors with nothing to say stay out of the report.
            quiet = [d.monitor.id for d in digests if not d.rows and not d.alerts]
            self.store.mark_empty(quiet, now)
            digests = [d for d in digests if d.rows or d.alerts]
            if self.store.reports_since(agent, now - 86400) >= self.settings.max_reports_per_day:
                log.warning(
                    "report for %s skipped: %d reports in the last day", agent, self.settings.max_reports_per_day
                )
                continue
            queued.append(self._queue(agent, schedule, conversation, digests, now))
        return queued

    def _queue(self, agent: str, schedule: str, conversation: str, digests: list[MonitorDigest], now: int) -> str:
        report_id = new_report_id()
        words = Schedule.parse(schedule).describe()
        prompt, rows = render(digests, words, report_id)
        names = ", ".join(d.monitor.name for d in digests)
        request = {
            "id": report_id,
            "agent_id": agent,
            "conversation": conversation,
            "name": one_line(f"Monitors: {words}", MAX_TASK_NAME),
            "description": one_line(f"Monitor report: {names}", MAX_TASK_DESCRIPTION),
            "prompt": encodable(prompt),
        }
        self.store.add_report(
            Report(
                id=report_id,
                agent_id=agent,
                schedule=schedule,
                conversation=conversation,
                monitors=[d.monitor.id for d in digests],
                alerted=sorted({a.source for d in digests for a in d.alerts}),
                at=now,
                # A second early: an item a fetch records later in this same second
                # comes again rather than never (a repeat beats a gap).
                upto=now - 1,
                items=sum(len(d.rows) + d.left_out for d in digests),
                status=QUEUED,
                request=request,
                digest=rows,
            )
        )
        log.info("report %s queued for %s (%d monitors)", report_id, agent, len(digests))
        return report_id

    def tick(self, now: datetime) -> list[str]:
        """Fires every minute since the last tick (kept in the store, so a restart
        catches up on what it missed, up to an hour); returns the reports queued."""
        stored = self.store.get("schedule_last")
        last = datetime.fromtimestamp(int(stored), self.settings.tz) if stored else None
        queued: list[str] = []
        for minute in minutes_between(last, now):
            queued += self.fire(minute)
        self.store.set(schedule_last=str(int(now.replace(second=0, microsecond=0).timestamp())))
        return queued

    def _iso(self, ts: int | None) -> str:
        return datetime.fromtimestamp(ts, self.settings.tz).isoformat(timespec="minutes") if ts else ""


def minutes_between(last: datetime | None, now: datetime, cap: int = 60) -> list[datetime]:
    """The whole minutes after `last` up to `now`, at most `cap` (the latest): a
    service that was down for hours fires only what it missed in the last hour."""
    end = now.replace(second=0, microsecond=0)
    if last is None:
        return [end]
    start = last.replace(second=0, microsecond=0) + timedelta(minutes=1)
    out = []
    t = start
    while t <= end:
        out.append(t)
        t += timedelta(minutes=1)
    return out[-cap:]
