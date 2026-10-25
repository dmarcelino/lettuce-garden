"""The loop: every POLL_SECONDS, ask Gmail what arrived since the stored
historyId and turn matches into queued requests.

No turn is ever started from here directly: a match becomes a request row,
which the executor in the app-server fetches over the API and turns into a
one-off letta-code task.
"""

from __future__ import annotations

import logging

import anyio

from .config import LOST_AFTER_SECONDS, MAX_FETCH_PER_POLL, RESUME_AFTER_SECONDS
from .google import Gmail, Google, GoogleUnavailable, HistoryExpired, Status
from .matcher import IGNORED_LABELS, Message, RuleView, match, parse_message
from .store import GAP_EXPIRED, GAP_PAUSED, Store
from .triggers import plan

log = logging.getLogger("gmail_triggers.poller")

# Google states that mean "switched off or broken until someone acts", as
# opposed to a passing network error: mail is not replayed after these.
PAUSED = frozenset({Status.DISABLED, Status.NOT_CONNECTED, Status.GMAIL_NOT_GRANTED, Status.RECONNECT})


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
            if e.status in PAUSED and self.store.get("paused_since") is None:
                self.store.set(paused_since=str(self.store.now()))
            log.info("not polling: %s", e.status.value)

    async def _poll(self, gmail: Gmail) -> None:
        store, now = self.store, self.store.now()
        start = store.get("history_id")
        if not start or store.get("account") != gmail.email:
            # First run (or another account): start from now, never replay old mail.
            store.set(account=gmail.email, history_id=await gmail.profile(), last_ok=str(now), paused_since=None)
            return
        last_ok = int(store.get("last_ok") or now)
        if store.get("paused_since") is not None and now - last_ok > RESUME_AFTER_SECONDS:
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
