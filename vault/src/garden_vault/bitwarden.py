"""Bitwarden's unencrypted JSON export (Settings > Export vault > .json), read
into the vault's kinds. Encrypted exports are refused: the vault never sees a
Bitwarden key. Folders, collections, passkeys, favourites, password history and
linked custom fields are not imported. Nothing here logs or keeps the file."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .kinds import KINDS
from .vault import Field, VaultError, build_fields

MAX_IMPORT_BYTES = 5 * 1024 * 1024
MAX_NAME = 80

# Bitwarden's item type -> (our kind, the item's sub-object, its keys -> ours).
TYPES: dict[int, tuple[str, str | None, dict[str, str]]] = {
    1: ("login", "login", {"username": "username", "password": "password", "totp": "totp"}),
    2: ("secure_note", None, {}),
    3: (
        "card",
        "card",
        {
            "cardholderName": "cardholder_name",
            "brand": "brand",
            "number": "number",
            "expMonth": "exp_month",
            "expYear": "exp_year",
            "code": "code",
        },
    ),
    4: (
        "identity",
        "identity",
        {
            "title": "title",
            "firstName": "first_name",
            "middleName": "middle_name",
            "lastName": "last_name",
            "username": "username",
            "company": "company",
            "ssn": "ssn",
            "passportNumber": "passport_number",
            "licenseNumber": "license_number",
            "email": "email",
            "phone": "phone",
            "address1": "address1",
            "address2": "address2",
            "address3": "address3",
            "city": "city",
            "state": "state",
            "postalCode": "postal_code",
            "country": "country",
        },
    ),
    5: (
        "ssh_key",
        "sshKey",
        {"privateKey": "private_key", "publicKey": "public_key", "keyFingerprint": "key_fingerprint"},
    ),
}
HIDDEN = 1  # a custom field Bitwarden masks; Text (0) and Boolean (2) are shown
LINKED = 3  # a custom field that points at another field: no value of its own


@dataclass(frozen=True)
class Item:
    kind: str
    name: str
    fields: tuple[Field, ...]


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value) if isinstance(value, str | int) else ""


def _name(raw: Any) -> str:
    return (_text(raw.get("name")).strip()[:MAX_NAME] if isinstance(raw, dict) else "") or "Untitled"


def _item(raw: Any) -> tuple[Item, list[str]]:
    """The item, and one line per field left out: a field the vault refuses
    (too long, a custom name it can't use or has already) is dropped, not the item."""
    kind_id = raw.get("type") if isinstance(raw, dict) else None
    if not isinstance(kind_id, int) or isinstance(kind_id, bool) or kind_id not in TYPES:
        raise VaultError("not a type the vault keeps")
    kind, section, keys = TYPES[kind_id]
    sub = raw.get(section) if section else {}
    sub = sub if isinstance(sub, dict) else {}
    values = {ours: _text(sub.get(theirs)) for theirs, ours in keys.items()}
    if kind == "login":
        uris = sub.get("uris") if isinstance(sub.get("uris"), list) else []
        values["uris"] = "\n".join(_text(u.get("uri")) for u in uris if isinstance(u, dict) and _text(u.get("uri")))
    values["notes"] = _text(raw.get("notes"))
    fields = raw.get("fields") if isinstance(raw.get("fields"), list) else []
    custom = [
        (_text(f.get("name")), _text(f.get("value")), f.get("type") == HIDDEN)
        for f in fields
        if isinstance(f, dict) and f.get("type") != LINKED
    ]

    name = _name(raw)
    kept: dict[str, str] = {}
    kept_custom: list[tuple[str, str, bool]] = []
    left_out: list[str] = []
    for key, value in values.items():
        if value.strip():
            try:
                build_fields(kind, {**kept, key: value}, kept_custom)
                kept[key] = value
            except VaultError as e:
                left_out.append(f"{name}: {KINDS[kind].spec(key).label} left out ({e})")  # type: ignore[union-attr]
    for field_name, value, hidden in custom:
        if value.strip():
            try:
                build_fields(kind, kept, [*kept_custom, (field_name, value, hidden)])
                kept_custom.append((field_name, value, hidden))
            except VaultError as e:
                left_out.append(f"{name}: custom field {field_name or '(no name)'} left out ({e})")
    return Item(kind, name, build_fields(kind, kept, kept_custom)), left_out


def read_export(raw: bytes) -> tuple[list[Item], list[str]]:
    """The items to add, and one line per item or field left out (its name and why)."""
    if len(raw) > MAX_IMPORT_BYTES:
        raise VaultError(f"the file is larger than {MAX_IMPORT_BYTES // (1024 * 1024)} MB")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        raise VaultError("this is not a Bitwarden JSON export") from e
    # Before the shape check: a password-protected export has no items at all.
    if isinstance(data, dict) and data.get("encrypted"):
        raise VaultError("this export is encrypted: export again choosing .json, not encrypted")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise VaultError("this is not a Bitwarden JSON export")
    items: list[Item] = []
    left_out: list[str] = []
    for raw_item in data["items"]:
        try:
            item, dropped = _item(raw_item)
        except VaultError as e:
            left_out.append(f"{_name(raw_item)}: {e}")
            continue
        items.append(item)
        left_out.extend(dropped)
    return items, left_out
