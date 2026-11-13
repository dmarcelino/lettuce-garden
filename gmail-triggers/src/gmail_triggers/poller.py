"""The loop: every POLL_SECONDS, ask Gmail what arrived since the stored
historyId and turn matches into queued requests.

No turn is ever started from here directly: a match becomes a request row,
which the executor in the app-server fetches over the API and turns into a
one-off letta-code task.
"""

from __future__ import annotations

import logging

import anyio

from .config import CATCH_UP_SECONDS, LOST_AFTER_SECONDS, MAX_FETCH_PER_POLL, RESUME_AFTER_SECONDS
from .google import Gmail, Google, GoogleUnavailable, HistoryExpired, Status
from .matcher import IGNORED_LABELS, Message, RuleView, match, parse_message
from .store import GAP_EXPIRED, GAP_PAUSED, PAUSE_CLEARED, Store
from .triggers import plan

log = logging.getLogger("gmail_triggers.poller")

# Google states that mean "switched off or broken until someone acts", as
# opposed to a passing network error, which the next poll catches up on.
PAUSED = frozenset({Status.DISABLED, Status.NOT_CONNECTED, Status.GMAIL_NOT_GRANTED, Status.RECONNECT})
# The ones the person chose in Settings -> Google (switched off, disconnected,
# Gmail not allowed). Once one has lasted RESUME_AFTER_SECONDS in a row, the
# pause counts as chosen and mail that arrived meanwhile is not replayed. A
# shorter spell (a token file caught mid-write, Disconnect then Connect to fix
# the sign-in) does not count. Otherwise (the sign-in expired) the pause is
# caught up if Google is back within CATCH_UP_SECONDS of the last good poll.
CHOSEN = frozenset({Status.DISABLED, Status.NOT_CONNECTED, Status.GMAIL_NOT_GRANTED})


class Poller:
    def __init__(self, store: Store, google: Google, poll_seconds: int):
        self.store = store
        self.google = google
        self.poll_seconds = poll_seconds

    async def run(self) -> None:
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("poll failed")
            await anyio.sleep(self.poll_seconds)

    async def run_once(self) -> None:
        self.store.expire_claims(LOST_AFTER_SECONDS)
        try:
            gmail = await self.google.connect()
            await self._poll(gmail)
        except GoogleUnavailable as e:
            # Disabled, not connected or failing: poll nothing; the page says why.
            if e.status in PAUSED:
                self._note_pause(e.status)
            log.info("not polling: %s", e.status.value)

    def _note_pause(self, status: Status) -> None:
        store, now = self.store, self.store.now()
        if store.get("paused_since") is None:
            store.set(paused_since=str(now))
        since = store.get("chosen_since")
        if status not in CHOSEN:
            if since is not None:
                store.set(chosen_since=None)
        elif since is None:
            store.set(chosen_since=str(now))
        elif now - int(since) > RESUME_AFTER_SECONDS:
            store.set(pause_chosen="1")

    def _pause_was_chosen(self, now: int) -> bool:
        since = self.store.get("chosen_since")
        return self.store.get("pause_chosen") is not None or (
            since is not None and now - int(since) > RESUME_AFTER_SECONDS
        )

    async def _poll(self, gmail: Gmail) -> None:
        store, now = self.store, self.store.now()
        start = store.get("history_id")
        if not start or store.get("account") != gmail.email:
            # First run (or another account): start from now, never replay old mail.
            store.set(account=gmail.email, history_id=await gmail.profile(), last_ok=str(now), **PAUSE_CLEARED)
            return
        last_ok = int(store.get("last_ok") or now)
        limit = RESUME_AFTER_SECONDS if self._pause_was_chosen(now) else CATCH_UP_SECONDS
        if store.get("paused_since") is not None and now - last_ok > limit:
            # Back after Google was off or broken for a while: don't replay it all.
            store.restart_at(await gmail.profile(), GAP_PAUSED)
            log.warning("Google was unusable since %s; starting again from now", last_ok)
            return
        try:
            page = await gmail.history(start)
        except HistoryExpired:
            store.restart_at(await gmail.profile(), GAP_EXPIRED)
            log.warning("history expired; mail since %s was not checked", last_ok)
            return

        added = dict(page.added)
        new_ids = store.unhandled(added)
        wanted = {lid for r in store.rules(enabled_only=True) for lid in r.label_ids}
        candidates = [m for m in new_ids if added[m] & wanted and not added[m] & IGNORED_LABELS]
        fetch, skipped = candidates[:MAX_FETCH_PER_POLL], candidates[MAX_FETCH_PER_POLL:]
        if skipped:
            log.warning("%d messages matched labels; checking the first %d", len(candidates), MAX_FETCH_PER_POLL)
        messages: list[Message] = []
        for mid in fetch:
            raw = await gmail.message(mid)
            if raw is None:
                continue
            try:
                # Labels it arrived with, plus any it has now.
                raw["labelIds"] = sorted(set(raw["labelIds"]) | added[mid])
                messages.append(parse_message(raw))
            except Exception:
                # One odd message must not stop every poll from here on.
                log.exception("skipped message %s", mid)

        # Read again: a rule may have been turned off or deleted during the fetches.
        rules = store.rules(enabled_only=True)
        views = [RuleView(r.id, frozenset(r.label_ids), frozenset(r.senders), r.verified_only) for r in rules]
        held, sent = plan(store, rules, match(messages, views, gmail.email), now)
        unchecked = set(skipped)
        store.record_poll(
            handled=[m for m in new_ids if m not in unchecked],
            history_id=page.history_id,
            held=held,
            sent=sent,
            skipped=len(skipped),
        )
