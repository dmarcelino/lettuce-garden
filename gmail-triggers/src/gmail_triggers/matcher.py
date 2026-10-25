"""Which rules a batch of new messages sets off. Pure: no I/O, no clock.

Everything here reads untrusted email headers. Only the parsed sender address,
the subject line and Gmail's own verdict on the sender are used.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from email.utils import parseaddr

from .config import MAX_SUBJECT
from .text import CONTROL, encodable, one_line

# Never "arriving mail": the account's own sent mail and drafts. An agent
# replying must not set off its own trigger again.
IGNORED_LABELS = frozenset({"SENT", "DRAFT"})
# Gmail's own Authentication-Results carry this authserv-id.
GOOGLE_AUTHSERV_ID = "mx.google.com"
ADDRESS = re.compile(r"[^@\s\"'<>(),;:\\\[\]]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z0-9-]{2,63}")


@dataclass(frozen=True)
class Message:
    id: str
    label_ids: frozenset[str]
    sender: str  # the parsed From address, lowercased; "" when unparseable
    subject: str  # one line, at most MAX_SUBJECT characters
    verified: bool  # Gmail's DMARC check passed for the sender's domain


@dataclass(frozen=True)
class RuleView:
    """What matching needs from a rule."""

    id: str
    label_ids: frozenset[str]
    senders: frozenset[str]
    verified_only: bool


def sender_address(from_header: str) -> str:
    _, addr = parseaddr(CONTROL.sub(" ", encodable(from_header)))
    addr = addr.strip().lower()
    return addr if ADDRESS.fullmatch(addr) else ""


def _header(headers: Sequence[tuple[str, str]], name: str) -> list[str]:
    return [v for k, v in headers if k.lower() == name.lower()]


def dmarc_verified(auth_results: Sequence[str], sender: str) -> bool:
    """True when Gmail's Authentication-Results says dmarc=pass for the sender's domain.

    Only the topmost header counts, and only when Gmail wrote it: anything
    below it came with the message and may be forged."""
    if not auth_results or "@" not in sender:
        return False
    first = auth_results[0]
    authserv, _, rest = first.partition(";")
    if authserv.strip().lower() != GOOGLE_AUTHSERV_ID:
        return False
    domain = sender.rsplit("@", 1)[1]
    for clause in rest.split(";"):
        clause = re.sub(r"\([^)]*\)", " ", clause).strip().lower()
        if not clause.startswith("dmarc="):
            continue
        tokens = clause.split()
        if tokens[0] != "dmarc=pass":
            return False
        froms = [t.removeprefix("header.from=") for t in tokens if t.startswith("header.from=")]
        return froms == [domain]
    return False


def parse_message(raw: dict) -> Message:
    """From google.Gmail.message()'s trimmed dict."""
    headers = raw.get("headers") or []
    froms = _header(headers, "From")
    sender = sender_address(froms[0]) if len(froms) == 1 else ""
    subjects = _header(headers, "Subject")
    return Message(
        id=raw["id"],
        label_ids=frozenset(raw.get("labelIds") or ()),
        sender=sender,
        subject=one_line(subjects[0], MAX_SUBJECT) if subjects else "",
        verified=bool(sender) and dmarc_verified(_header(headers, "Authentication-Results"), sender),
    )


def is_own(message: Message, own_address: str) -> bool:
    return bool(message.label_ids & IGNORED_LABELS) or (bool(own_address) and message.sender == own_address.lower())


def rule_matches(rule: RuleView, message: Message) -> bool:
    if not rule.label_ids & message.label_ids:
        return False
    if rule.senders and message.sender not in rule.senders:
        return False
    return not rule.verified_only or message.verified


def match(messages: Iterable[Message], rules: Iterable[RuleView], own_address: str) -> dict[str, list[Message]]:
    """rule id -> its messages, in arrival order. Rules are expected to be the enabled ones."""
    rules = list(rules)
    out: dict[str, list[Message]] = {}
    for message in messages:
        if is_own(message, own_address):
            continue
        for rule in rules:
            if rule_matches(rule, message):
                out.setdefault(rule.id, []).append(message)
    return out
