"""The allowed-recipients list: who the connected account may send to or invite.

An extra constraint on top of the service level (levels.py). It mirrors upstream
Lettuce PR #83 (Settings -> Google Workspace "Allowed recipients"): each entry is a
bare address (``someone@example.com``) or a whole domain (``@example.com``); a
recipient is allowed if it matches an entry exactly or its domain is listed, and
the connected account may always mail itself.

Unlike upstream's guard, which hooks the HTTP client and parses raw MIME, this
sidecar owns its tools and composes every request from typed address arguments, so
the recipients are checked here, at the tool boundary, from explicit lists. The one
exception is sending an already-stored draft, whose recipients the tool re-reads
from Outlook before the check (the draft may have been composed at a looser moment).

Empty list means no recipient restriction (the level still gates whether sending
exists at all). The guard fails closed: a caller that cannot produce a clean list
of addresses must refuse rather than send.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# A bare address or `@domain`, nothing an address header could read as more than
# one: no spaces, commas, brackets, quotes, semicolons or a second `@`. Same shape
# as upstream's RECIPIENT_ENTRY so the two lists stay interchangeable.
_ENTRY = re.compile(r"^([^\s@,;<>()\[\]\"'\\]+)?@([a-z0-9-]+\.)+[a-z0-9-]+$")

# How Settings points the person at the list, quoted in a refusal.
SETTINGS_HINT = "set the allowed recipients on the Outlook page"


def recipient_entry(value: str) -> str | None:
    """A list entry, lowercased, or None if it is not a bare address or `@domain`."""
    entry = value.strip().lower()
    return entry if _ENTRY.match(entry) else None


def normalize_entries(values: Iterable[str]) -> list[str]:
    """Clean a list of entries: lowercase, drop blanks and malformed ones, de-dupe,
    keeping order. For reading a stored list leniently."""
    out: list[str] = []
    for value in values:
        entry = recipient_entry(value) if isinstance(value, str) else None
        if entry is not None and entry not in out:
            out.append(entry)
    return out


class InvalidRecipientEntry(ValueError):
    """A save-time entry that is neither a bare address nor an `@domain`."""


def validate_entries(values: Iterable[str]) -> list[str]:
    """Clean a list the person just saved, refusing any malformed entry by name.
    Blank entries are dropped; everything else must be an address or `@domain`."""
    given = [value.strip() for value in values if value.strip()]
    invalid = [value for value in given if recipient_entry(value) is None]
    if invalid:
        raise InvalidRecipientEntry("Not an address or @domain: " + ", ".join(invalid))
    return normalize_entries(given)


class AllowedRecipients:
    """The allowed list plus the connected account, which may always mail itself.

    Build it from a stored list and the account address; `outside()` names the
    recipients a send or invite would reach that the list does not cover.
    """

    def __init__(self, entries: Iterable[str], own: str | None = None):
        normalized = normalize_entries(entries)
        # Empty: the person set no restriction, so everything is in-list.
        self.unrestricted = not normalized
        self.addresses: set[str] = set()
        self.domains: set[str] = set()
        for entry in normalized:
            if entry.startswith("@"):
                self.domains.add(entry[1:])
            else:
                self.addresses.add(entry)
        self.own = own.strip().lower() if own and own.strip() else None

    def __contains__(self, address: str) -> bool:
        if self.unrestricted:
            return True
        value = address.strip().lower()
        if self.own is not None and value == self.own:
            return True
        if value in self.addresses:
            return True
        local, at, domain = value.rpartition("@")
        return bool(local and at and domain in self.domains)

    def outside(self, addresses: Iterable[str]) -> list[str]:
        """The addresses not allowed, in order and without repeats."""
        seen: list[str] = []
        for address in addresses:
            value = address.strip().lower()
            if value and value not in self and value not in seen:
                seen.append(value)
        return seen
