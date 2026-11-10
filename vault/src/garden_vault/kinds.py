"""The kinds of secret the vault holds: Bitwarden's five item types, with its
fields, so an agent asks for the one field a task needs (a postal code, not a
whole address) and a Bitwarden export imports as is (bitwarden.py). One table
drives the add/edit form and its groups (Bitwarden's sections), which fields agents can read without asking (public)
and which need approval (private), which private fields the person may let
agents use without asking, and the preview on the approval page.

Every secret may also carry custom fields (Bitwarden's text, hidden and
checkbox fields): always private, never shared without asking; only hidden ones
are masked on screen."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    # Private: shared with agents only on approval (or without asking, where
    # `auto` allows it). Public: agents read it in list_secrets, like the name.
    sensitive: bool = False
    multiline: bool = False
    # A private field the person may let agents use without asking.
    auto: bool = False
    hint: str = ""
    # The group it shows under on the form and the secret's page.
    section: str = ""
    # Hidden from the person too: typed into a password box, never sent back to the
    # edit page, and dots on the secret's page until "Show hidden fields". Bitwarden's
    # rule (its form's password-type fields); about the screen only, not agents.
    masked: bool = False


@dataclass(frozen=True)
class Kind:
    key: str
    label: str
    fields: tuple[FieldSpec, ...]

    @property
    def auto_allowed(self) -> bool:
        """Whether the person may choose "Agents may use it without asking"."""
        return any(f.auto for f in self.fields)

    def spec(self, key: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.key == key), None)

    @property
    def groups(self) -> tuple[tuple[str, tuple[FieldSpec, ...]], ...]:
        """The fields by section, in order."""
        out: list[tuple[str, list[FieldSpec]]] = []
        for f in self.fields:
            if not out or out[-1][0] != f.section:
                out.append((f.section, []))
            out[-1][1].append(f)
        return tuple((title, tuple(specs)) for title, specs in out)


def _private(
    key: str,
    label: str,
    auto: bool = False,
    multiline: bool = False,
    hint: str = "",
    section: str = "",
    masked: bool = False,
) -> FieldSpec:
    return FieldSpec(
        key, label, sensitive=True, auto=auto, multiline=multiline, hint=hint, section=section, masked=masked
    )


def _notes(auto: bool = False) -> FieldSpec:
    return _private("notes", "Notes", auto=auto, multiline=True, section="Additional options")


def _group(title: str, *specs: FieldSpec) -> tuple[FieldSpec, ...]:
    return tuple(replace(f, section=title) for f in specs)


KINDS: dict[str, Kind] = {
    k.key: k
    for k in (
        Kind(
            "login",
            "Login",
            (
                *_group(
                    "Login credentials",
                    FieldSpec("username", "Username"),
                    _private("password", "Password", masked=True),
                    _private("totp", "Authenticator key (TOTP)", masked=True),
                    # Bitwarden's "Autofill options"; the vault fills nothing in.
                    FieldSpec("uris", "Websites", multiline=True, hint="one per line"),
                ),
                _notes(),
            ),
        ),
        Kind(
            "card",
            "Card",
            (
                *_group(
                    "Card details",
                    FieldSpec("cardholder_name", "Cardholder name"),
                    FieldSpec("brand", "Brand", hint="e.g. Visa"),
                    _private("number", "Number", masked=True),
                    _private("exp_month", "Expiration month", hint="e.g. 12"),
                    _private("exp_year", "Expiration year", hint="e.g. 2030"),
                    _private("code", "Security code", masked=True),
                ),
                _notes(),
            ),
        ),
        Kind(
            "identity",
            "Identity",
            (
                *_group(
                    "Personal details",
                    FieldSpec("title", "Title", hint="e.g. Mr, Ms, Dr"),
                    FieldSpec("first_name", "First name"),
                    FieldSpec("middle_name", "Middle name"),
                    FieldSpec("last_name", "Last name"),
                    _private("username", "Username"),
                    _private("company", "Company"),
                ),
                *_group(
                    "Identification",
                    _private("ssn", "Social Security number", masked=True),
                    _private("passport_number", "Passport number", masked=True),
                    _private("license_number", "License number"),
                ),
                *_group("Contact info", _private("email", "Email"), _private("phone", "Phone")),
                *_group(
                    "Address",
                    _private("address1", "Address 1", auto=True),
                    _private("address2", "Address 2", auto=True),
                    _private("address3", "Address 3", auto=True),
                    _private("city", "City / Town", auto=True),
                    _private("state", "State / Province", auto=True),
                    _private("postal_code", "Zip / Postal code", auto=True),
                    FieldSpec("country", "Country"),
                ),
                _notes(),
            ),
        ),
        Kind("secure_note", "Secure note", (_notes(auto=True),)),
        Kind(
            "ssh_key",
            "SSH key",
            (
                *_group(
                    "SSH key",
                    _private("private_key", "Private key", multiline=True, masked=True),
                    FieldSpec("public_key", "Public key", multiline=True),
                    FieldSpec("key_fingerprint", "Fingerprint"),
                ),
                _notes(),
            ),
        ),
    )
}

MAX_CUSTOM_FIELDS = 20
MAX_CUSTOM_NAME = 40
CUSTOM_FIELDS = "Custom fields"


def custom_key(name: str) -> str:
    """A custom field's key, from its name: what agents ask for, and VAULT_<KEY> for commands."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def group_fields(kind: str, fields: Iterable[Any]) -> list[tuple[str, tuple[Any, ...]]]:
    """A secret's stored fields under the same groups as its form, custom
    fields last; groups with nothing in them are left out."""
    fields = tuple(fields)
    groups = KINDS[kind].groups if kind in KINDS else ()
    builtin = {s.key for _, specs in groups for s in specs}
    out = [(title, tuple(f for f in fields if f.key in {s.key for s in specs})) for title, specs in groups]
    out.append((CUSTOM_FIELDS, tuple(f for f in fields if f.key not in builtin)))
    return [(title, fs) for title, fs in out if fs]


def may_share_without_asking(kind: str, key: str) -> bool:
    spec = KINDS[kind].spec(key) if kind in KINDS else None
    return spec is not None and spec.auto


def _tail(value: str, n: int = 4) -> str:
    compact = re.sub(r"\s+", "", value)
    return compact[-n:] if len(compact) > n else ""


def preview(kind: str, fields: list[Any]) -> str:
    """Enough to tell which secret it is, without revealing anything private."""
    v = {f.key: f.value for f in fields}
    if kind == "card":
        tail = _tail(v.get("number", ""))
        return " ".join(x for x in (v.get("brand", ""), f"ending {tail}" if tail else "") if x)
    if kind == "login":
        site = (v.get("uris", "").splitlines() or [""])[0]
        return " at ".join(x for x in (v.get("username", ""), site) if x)
    if kind == "identity":
        name = " ".join(v[k] for k in ("first_name", "last_name") if v.get(k))
        return ", ".join(x for x in (name, v.get("country", "")) if x)
    if kind == "ssh_key":
        return v.get("key_fingerprint", "")
    return ""
