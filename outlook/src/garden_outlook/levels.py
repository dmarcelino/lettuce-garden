"""The permission policy: which Outlook services agents may use, and how far.

Modelled on Lettuce's Google Workspace levels (bff/src/google/policy.ts): one level
per service, cumulative and lowest-first, `None` for off. The person sets them on
the Outlook page; the sidecar lists and executes only the tools a level permits, so
the policy holds where no agent shell can reach it.

Two differences from Google's model:
  * Calendar has a `self-events` level between `readonly` and `full`: create, update
    and delete events on the person's own calendar with **no attendees**. Attendees
    (which mail an invitation) need `full`. `calendar_allows_attendees` is the gate.
  * Shared mailboxes are not a level but a list of addresses, always read-only
    (`shared_mailboxes`); the mail read tools accept one only if it is listed.

Allowed recipients live alongside the levels (recipients.py).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from .recipients import InvalidRecipientEntry, normalize_entries, validate_entries

# Services and their ordered levels. A tool names the service and the lowest level
# that may run it (TOOL_REQUIREMENTS, mcp_tools.py).
LEVELS: dict[str, tuple[str, ...]] = {
    "mail": ("readonly", "organize", "drafts", "send", "full"),
    "calendar": ("readonly", "self-events", "full"),
    "tasks": ("readonly", "manage"),
}
SERVICES: tuple[str, ...] = tuple(LEVELS)


def is_level(service: str, value: object) -> bool:
    return isinstance(value, str) and value in LEVELS.get(service, ())


def level_rank(service: str, level: str | None) -> int:
    """Position of a level in its service's order; -1 for off/unknown."""
    if level is None:
        return -1
    try:
        return LEVELS[service].index(level)
    except (KeyError, ValueError):
        return -1


class InvalidPolicy(ValueError):
    """A save-time policy value that is not a known service, level or entry."""


@dataclass(frozen=True)
class Policy:
    """What agents may do with the connected mailbox. `levels[service]` is the level
    or None (off); `allowed_recipients` an address/`@domain` list (empty: no
    restriction); `shared_mailboxes` the read-only shared addresses."""

    levels: dict[str, str | None] = field(default_factory=dict)
    allowed_recipients: tuple[str, ...] = ()
    shared_mailboxes: tuple[str, ...] = ()

    def level(self, service: str) -> str | None:
        return self.levels.get(service)

    def permits(self, service: str, needed: str) -> bool:
        """True when the service's level is at least `needed`."""
        have = level_rank(service, self.level(service))
        want = level_rank(service, needed)
        return want >= 0 and have >= want

    @property
    def calendar_allows_attendees(self) -> bool:
        """Only `calendar: full` may create or notify attendees; `self-events` is
        own-calendar, no-invite."""
        return self.permits("calendar", "full")

    def allows_shared(self, mailbox: str) -> bool:
        value = mailbox.strip().lower()
        return value in self.shared_mailboxes


def _levels_of(value: object) -> dict[str, str | None]:
    raw = value if isinstance(value, dict) else {}
    return {service: (raw.get(service) if is_level(service, raw.get(service)) else None) for service in SERVICES}


def _mailboxes_of(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and "@" in item:
            address = item.strip().lower()
            if address and address not in out:
                out.append(address)
    return tuple(out)


def parse_policy(value: object) -> Policy:
    """Read a stored policy leniently: anything unrecognised loads as off/empty,
    never as more access (as Lettuce's settings parser does)."""
    raw = value if isinstance(value, dict) else {}
    return Policy(
        levels=_levels_of(raw.get("levels")),
        allowed_recipients=tuple(normalize_entries(raw.get("allowedRecipients") or [])),
        shared_mailboxes=_mailboxes_of(raw.get("sharedMailboxes")),
    )


def policy_to_json(policy: Policy) -> dict[str, object]:
    return {
        "levels": {service: policy.level(service) for service in SERVICES},
        "allowedRecipients": list(policy.allowed_recipients),
        "sharedMailboxes": list(policy.shared_mailboxes),
    }


def apply_update(current: Policy, body: object) -> Policy:
    """Apply a browser update, validating strictly. Absent keys keep their value;
    an unknown service, level or malformed entry is refused by name."""
    if not isinstance(body, dict):
        raise InvalidPolicy("Expected a JSON object")
    next_levels = dict(current.levels)
    next_recipients = current.allowed_recipients
    next_mailboxes = current.shared_mailboxes

    if "levels" in body:
        raw = body["levels"]
        if not isinstance(raw, dict):
            raise InvalidPolicy("levels must be an object")
        for key in raw:
            if key not in SERVICES:
                raise InvalidPolicy(f"Unknown service: {key}")
        for service in SERVICES:
            if service not in raw:
                continue
            level = raw[service]
            if level is None or level == "":
                next_levels[service] = None
            elif is_level(service, level):
                next_levels[service] = level
            else:
                raise InvalidPolicy(f"Unknown {service} level: {level}")

    if "allowedRecipients" in body:
        raw = body["allowedRecipients"]
        if not isinstance(raw, list) or not all(isinstance(entry, str) for entry in raw):
            raise InvalidPolicy("allowedRecipients must be a list of text")
        try:
            next_recipients = tuple(validate_entries(raw))
        except InvalidRecipientEntry as error:
            raise InvalidPolicy(str(error)) from error

    if "sharedMailboxes" in body:
        raw = body["sharedMailboxes"]
        if not isinstance(raw, list) or not all(isinstance(entry, str) for entry in raw):
            raise InvalidPolicy("sharedMailboxes must be a list of text")
        invalid = [entry for entry in raw if entry.strip() and "@" not in entry]
        if invalid:
            raise InvalidPolicy("Not a mailbox address: " + ", ".join(invalid))
        next_mailboxes = _mailboxes_of(raw)

    return replace(current, levels=next_levels, allowed_recipients=next_recipients, shared_mailboxes=next_mailboxes)


def serving(policy: Policy) -> bool:
    """Whether any service is on at all."""
    return any(policy.level(service) is not None for service in SERVICES)


def enabled_services(policy: Policy) -> Iterable[str]:
    return (service for service in SERVICES if policy.level(service) is not None)
