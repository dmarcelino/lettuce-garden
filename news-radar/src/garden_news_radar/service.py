"""What the MCP tools, the page and the scheduler do, in one place: sources
(inspect, test, put, delete), monitors, the scheduled fetches, and turning a
schedule that fires into a report for the executor."""

from __future__ import annotations

import dataclasses
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

import anyio
from garden_common.executor import AGENT_ID
from garden_common.text import encodable, one_line

from .config import (
    CARRY_SECONDS,
    FETCH_AHEAD_SECONDS,
    FOLLOW_REFRESH_SECONDS,
    MAX_DAYS_AHEAD,
    MAX_ITEMS_PER_MONITOR,
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
from .digest import MonitorDigest, considered_since, group_value, monitor_digest, more, render, term_counts
from .extract import fill_rates, run_problem, window_warning
from .fetch import Fetcher
from .inspect import inspect
from .runner import RunResult, run_source
from .spec import Spec, SpecError, parse_spec
from .store import QUEUED, Monitor, Report, Store, new_report_id

log = logging.getLogger("garden_news_radar.service")

# Where a monitor's reports go: new (a new conversation each report), default (the
# agent's main one), or the id of one of its conversations, as letta-code gives it
# in $LETTA_CONVERSATION_ID. The executor checks the same shape.
CONVERSATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
# Said for the conversation an agent saves a monitor from (set_up_in).
HERE = "here"
TEST_ITEMS = 10
# A test fetches fewer pages and follows fewer links than a scheduled run, to
# answer within the MCP call's time.
TEST_PAGES = 3
TEST_FOLLOW = 5


def _block_warning(spec: Spec, result: RunResult) -> str:
    """A page whose blocks link to far more pages than the items a spec reads
    probably lists more than the spec sees (schema.org data on a few cards only).
    Pages linked, not blocks: a calendar repeats one event in every day's cell."""
    block, found = result.largest_block, len(result.items)
    if not block or block["links"] < 2 * found or block["links"] - found < 10:
        return ""
    if spec.kind == "html" and block["items"] == spec.extract["items"]:
        return ""
    return (
        f"{found} items, but the page repeats {block['items']} {block['count']} times, linking to {block['links']} "
        "different pages: this spec may read only part of the listing. Try kind html with extract.items set to "
        "those blocks, or the site's sitemap with follow"
    )


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
        if not isinstance(term, str) or not term.strip("* \t") or len(term) > MAX_TERM:
            raise ToolError(f"each of {what} must be text of 1 to {MAX_TERM} characters")
        out.append(one_line(term, MAX_TERM))
    return list(dict.fromkeys(out))


def _conversation_id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not CONVERSATION_ID.fullmatch(value):
        raise ToolError(
            f"{what} must be new, default or a conversation id such as your $LETTA_CONVERSATION_ID"
            if what == "conversation"
            else f"{what} must be your conversation id, $LETTA_CONVERSATION_ID"
        )
    return value


# Where a monitor's reports go, in the agent's words (the page has its own).
REPORTS_TO_WORDS = {
    "here": "this conversation (where it was set up)",
    "new": "a new conversation each report",
    "default": "your main conversation",
}


def conversation_in_words(m: Monitor) -> str:
    return REPORTS_TO_WORDS.get(m.reports_to) or f"another conversation ({m.conversation})"


def reports_per_day(store: Store, settings: Settings, m: Monitor) -> int:
    """The most reports a day the agent's monitors would make with `m` as given:
    one report per schedule and conversation, as fire groups them. The admin's
    max_reports_per_day bounds it."""
    groups = {(o.schedule, o.conversation) for o in store.monitors(m.agent_id) if o.name != m.name}
    groups.add((m.schedule, m.conversation))
    today = datetime.now(settings.tz).date()
    return max_fires_per_day([Schedule.parse(s) for s, _ in groups], today)


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
    def _today(self) -> date:
        return datetime.fromtimestamp(self.store.now(), self.settings.tz).date()

    def _retry_note(self, out: dict[str, Any], retry_after: int) -> dict[str, Any]:
        """A site that asked for a pause: when to try it again, said so an agent
        doesn't poll a host that answers nothing but this until then."""
        if retry_after > 0:
            at = self._local(self.store.now() + retry_after)
            out["retry_after"] = retry_after
            out["next"] = (
                f"do not call source_test or source_inspect for this site before {at:%H:%M} ({retry_after} s): "
                "until then it only answers this. Go on with other sources; if it keeps asking for pauses, it "
                "is rate-limiting this stack: tell the person it can't be followed reliably"
            )
        return out

    async def source_inspect(self, url: Any) -> dict[str, Any]:
        if not isinstance(url, str) or not url.strip():
            raise ToolError("url is required")
        with anyio.move_on_after(TOOL_SECONDS):
            out = await inspect(self.fetcher, url.strip(), max_wait=TOOL_WAIT)
            return self._retry_note(out, int(out.pop("retry_after", 0) or 0))
        return {"url": url.strip(), "error": f"took too long (over {int(TOOL_SECONDS)} s); try a narrower page"}

    async def source_test(self, spec_obj: Any) -> dict[str, Any]:
        spec = self._spec(spec_obj)
        out: dict[str, Any] = {"name": spec.name, "kind": spec.kind}
        with anyio.move_on_after(TOOL_SECONDS):
            result = await run_source(
                spec,
                self.fetcher,
                max_pages=TEST_PAGES,
                max_follow=TEST_FOLLOW,
                max_wait=TOOL_WAIT,
                look_for_blocks=True,
            )
            return self._test_result(spec, result, out)
        out.update(passed=False, error=f"took too long (over {int(TOOL_SECONDS)} s); try fewer pages or follows")
        return out

    def _test_result(self, spec: Spec, result: RunResult, out: dict[str, Any]) -> dict[str, Any]:
        if result.error:
            out.update(passed=False, error=result.error, sample=result.sample)
            return self._retry_note(out, result.retry_after)
        items = result.items
        rates = fill_rates(items, spec)
        today = self._today()
        problems = []
        if not items:
            problems.append("no items found")
        else:
            # What would make a scheduled run unhealthy fails the test too (items without a
            # title are never kept, so a title is not checked here).
            problem = run_problem(items, spec, today)
            if problem:
                problems.append(problem)
        if spec.group_by:
            values = [group_value({**i.fields, "extra": i.extra}, spec.group_by) for i in items]
            out["groups_in_sample"] = len({v for v in values if v}) + values.count("")
        out.update(
            passed=not problems,
            items_found=len(items),
            pages_fetched=result.pages,
            followed=result.followed,
            fill_rates=rates,
            items=[i.as_dict() for i in items[:TEST_ITEMS]],
        )
        warnings = [w for w in (window_warning(items, spec, today), _block_warning(spec, result)) if w]
        if warnings:
            out["warnings"] = warnings
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
                    # Fetched once after source_put, then only while a monitor uses it.
                    "unused": not users.get(name),
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
    def _draft(
        self,
        agent_id: Any,
        name: Any,
        sources: Any,
        schedule: Any = None,
        conversation: Any = HERE,
        include: Any = None,
        exclude: Any = None,
        days_ahead: Any = None,
        news: Any = False,
        set_up_in: Any = None,
        where: Any = None,
    ) -> tuple[Monitor, Schedule]:
        """A checked monitor, not saved: what monitor_put saves and a draft preview shows."""
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
        here = "" if set_up_in is None else _conversation_id(set_up_in, "set_up_in")
        if conversation == HERE:
            # Without set_up_in the service can't know where "here" is.
            conversation = here or "new"
        conversation = _conversation_id(conversation, "conversation")
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
            where=_terms(where, "where"),
            days_ahead=days_ahead,
            news=news,
            set_up_in=here,
        )
        return monitor, parsed

    def monitor_put(
        self,
        agent_id: Any,
        name: Any,
        sources: Any,
        schedule: Any = None,
        conversation: Any = None,
        include: Any = None,
        exclude: Any = None,
        days_ahead: Any = None,
        news: Any = False,
        set_up_in: Any = None,
        where: Any = None,
        from_now: Any = False,
    ) -> dict[str, Any]:
        """conversation left out: a monitor saved again keeps where it reports (the
        person may have chosen on the page); a new one reports here. "here" is
        set_up_in, or the one already known for this monitor."""
        agent = _agent(agent_id)
        current = self.store.monitor_named(agent, name) if isinstance(name, str) else None
        if set_up_in is None and current and current.set_up_in:
            set_up_in = current.set_up_in
        if conversation is None:
            conversation = current.conversation if current else HERE
        monitor, parsed = self._draft(
            agent, name, sources, schedule, conversation, include, exclude, days_ahead, news, set_up_in, where
        )
        if current is None and self.store.count_monitors() >= self.settings.max_monitors:
            raise ToolError(f"this stack has {self.settings.max_monitors} monitors, the most allowed; delete one")
        most = reports_per_day(self.store, self.settings, monitor)
        if most > self.settings.max_reports_per_day:
            raise ToolError(
                f"your monitors' schedules would report up to {most} times a day; the admin allows "
                f"{self.settings.max_reports_per_day} per agent. Share a schedule and conversation with another "
                "monitor, or fire less often"
            )
        if not isinstance(from_now, bool):
            raise ToolError("from_now must be true or false")
        unfetched = self._not_fetched(monitor.sources)
        if from_now and unfetched:
            raise ToolError(
                f"these sources have not been fetched yet: {', '.join(unfetched)}. They are fetched within a minute "
                "of source_put; save again then, or from_now would let what they already hold through as new"
            )
        now = self.store.now()
        stored = self.store.put_monitor(monitor, delivered_upto=now if from_now else None)
        out = {
            "saved": stored.name,
            "schedule": stored.schedule,
            "schedule_in_words": parsed.describe() + f" ({self.settings.timezone})",
            "conversation_in_words": conversation_in_words(stored),
            "monitor": stored.as_dict(),
            "next": "show the person monitor_preview, and tell them the schedule and where reports go, in words; "
            "they can change where reports go on the News Radar page",
        }
        if from_now:
            at = self._local(now)
            out["from_now"] = (
                f"reports start from now ({at:%H:%M}): what the sources held before is not reported, only what "
                "they list or change after"
            )
        if unfetched:
            out["not_fetched_yet"] = unfetched
        if conversation == HERE and not stored.set_up_in:
            out["note"] = (
                "reports go to a new conversation each time: to have them in this one, save it again with "
                'set_up_in ($LETTA_CONVERSATION_ID) and conversation "here"'
            )
        return out

    def monitor_list(self, agent_id: Any) -> dict[str, Any]:
        agent = _agent(agent_id)
        return {
            "monitors": [
                {
                    **m.as_dict(),
                    "schedule_in_words": Schedule.parse(m.schedule).describe(),
                    "conversation_in_words": conversation_in_words(m),
                }
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

    def monitor_preview(
        self,
        agent_id: Any,
        name: Any,
        sources: Any = None,
        include: Any = None,
        exclude: Any = None,
        days_ahead: Any = None,
        news: Any = False,
        where: Any = None,
        from_now: Any = False,
    ) -> dict[str, Any]:
        """The saved monitor's next report; with sources, a draft's (checked as
        monitor_put would, never saved): what its first report would hold, and what
        each filter term did. from_now: as if saved with it (existing_skipped says
        how many rows that leaves out)."""
        draft = sources is not None
        if draft:
            m, _ = self._draft(agent_id, name, sources, None, "new", include, exclude, days_ahead, news, where=where)
        else:
            m = self._monitor(agent_id, name)
        if not isinstance(from_now, bool):
            raise ToolError("from_now must be true or false")
        now = self.store.now()
        # Where a saved monitor's report starts, and what became of its last one:
        # without them an empty preview reads as something lost.
        since = considered_since(m, now) if not draft and not from_now else None
        last = self.store.last_report(m.id) if not draft else None
        states = self.store.source_states()
        skipped = 0
        if from_now:
            before = monitor_digest(self.store, m, now, self.settings.tz, states)
            skipped = len(before.rows) + before.left_out
            m = dataclasses.replace(m, delivered_upto=now)
        d = monitor_digest(self.store, m, now, self.settings.tz, states)
        notes = ["a preview: nothing is marked as reported"]
        out: dict[str, Any] = {
            "monitor": m.name,
            "draft": draft,
            "would_report": d.worth_reporting,
            "rows": d.rows[:20],
            "rows_total": len(d.rows) + d.left_out,
            "needs_repair": [{"source": a.source, "reason": a.reason} for a in d.alerts],
            "terms": term_counts(self.store, m, now, self.settings.tz),
        }
        if from_now:
            out["existing_skipped"] = skipped
        if since is not None:
            out["shows_since"] = f"{self._local(since):%Y-%m-%d %H:%M}"
            why = (
                "earlier rows were already reported, or skipped with from_now"
                if m.delivered_upto >= now - CARRY_SECONDS
                else f"a report reaches back at most {CARRY_SECONDS // 86400} days"
            )
            notes.append(
                f"only what is new or changed since {out['shows_since']} shows: {why}. Preview with sources to see "
                "what a new monitor with these filters would report"
            )
        if last:
            out["last_report"] = {
                "at": f"{self._local(last.at):%Y-%m-%d %H:%M}",
                "status": last.status,
                "to": conversation_in_words(dataclasses.replace(m, conversation=last.conversation)),
            }
        if d.left_out:
            out["over_cap"] = {"cap": MAX_ITEMS_PER_MONITOR, "left_out": d.left_out}
            notes.append(
                f"a report holds at most {MAX_ITEMS_PER_MONITOR} rows, the newest: narrow the filters, or save with "
                "from_now if only what is announced from now on matters"
            )
        unfetched = self._not_fetched(m.sources)
        if unfetched:
            out["not_fetched_yet"] = unfetched
            notes.append("sources are fetched within a minute of source_put: preview again then")
        out["note"] = "; ".join(notes)
        return out

    def _local(self, ts: int) -> datetime:
        """A timestamp in the stack's timezone, the one people and schedules use."""
        return datetime.fromtimestamp(ts, self.settings.tz)

    def _not_fetched(self, sources: list[str]) -> list[str]:
        """Sources saved but not fetched yet: a preview can't show what they hold."""
        states = self.store.source_states()
        return [s for s in sources if s not in states or states[s].last_run is None]

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
        used = {s for m in self.store.monitors() for s in m.sources}
        due = []
        for name in self.store.source_specs():
            spec = self._stored_spec(name)
            if spec is None:
                continue
            st = states.get(name)
            last = st.last_run if st else None
            if last is not None and name not in used:
                continue  # unused: fetched once after source_put, for previews, then left alone
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
            problem=run_problem(result.items, spec, self._today()),
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
            "name": one_line(f"News Radar: {words}", MAX_TASK_NAME),
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
                items=sum(d.items for d in digests),
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
