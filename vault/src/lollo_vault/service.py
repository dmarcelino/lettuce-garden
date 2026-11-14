"""What agents can do with the vault, shared by the MCP tools and the web UI.

An agent lists secrets (names, types and public fields; never private values),
asks for the private fields of one secret with a purpose, and gets them once
the user has approved, or straight away for the fields the user set to "share
without asking". By default ("placeholders" delivery) the agent gets a
single-use handle instead of the values, and `vault-run`
(agent-skills/using-vault-secrets) redeems it to pass them to one command, which
may be `vault-fill` putting them into a file in place of {{VAULT_<FIELD>}}. The
values themselves ("text" delivery) need a reason the user reads. Requests, results and handles live only in memory: a
restart locks the vault anyway.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from secrets import token_urlsafe
from typing import Any

import anyio

from .config import (
    ERASE_DELAY_SECONDS,
    ERASE_GRACE_SECONDS,
    HANDLE_TTL_SECONDS,
    MAX_HANDLES,
    MAX_PENDING,
    MAX_PURPOSE_LENGTH,
    MAX_WAIT_SECONDS,
    PREFIX,
    REQUEST_TTL_SECONDS,
    RESULT_TTL_SECONDS,
)
from .kinds import may_share_without_asking, preview
from .logs import SECRET_FILTER
from .push import Pusher
from .vault import AUTO, Locked, Secret, Vault, VaultError

log = logging.getLogger("lollo_vault")

PENDING, DONE, DENIED, EXPIRED = "pending", "done", "denied", "expired"
PLACEHOLDERS, TEXT = "placeholders", "text"
SKILL_DIR = "/root/.letta/skills/using-vault-secrets"
VAULT_RUN = f"python3 {SKILL_DIR}/vault-run"
VAULT_FILL = f"python3 {SKILL_DIR}/vault-fill"
# Prefixes the variable names, so two secrets with the same fields (two
# addresses) can go into one command: VAULT_HOME_CITY, VAULT_WORK_CITY.
ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9]{0,19}")

# Pushes asking to unlock are sent at most this often.
UNLOCK_NOTICE_SECONDS = 300

# Nothing starts an agent's turn when the user answers: an agent that ends its
# turn with nothing waiting never collects what the user approved.
KEEP_WAITING = (
    f"Wait for the answer before you end your turn: call get_result with this id and wait_seconds "
    f"{MAX_WAIT_SECONDS}, and call it again until the status is no longer pending (the request expires after "
    f"{REQUEST_TTL_SECONDS // 60} minutes). If you can run a command in the background, run each call as its own "
    "background command, so you are told when it finishes. Don't end your turn with nothing waiting: nothing "
    "tells you when the user answers."
)


def rules(purpose: str) -> str:
    """Sent with every value shared as text, so the agent has them in front of it
    when it uses the values. Asked to check an address, an agent once pasted it
    back into the chat."""
    return (
        f"Use these values only for the purpose the user approved ({purpose}), and send them only where that "
        "purpose needs them: not to a search, map, lookup or validation service unless the purpose says so. "
        "Never repeat them, in full or in part, in your reply, memory, files or logs. Refer to the secret by its "
        "name; when asked to check or confirm it, say what you checked and the result, not the value. The user "
        "can see it in the vault."
    )


@dataclass
class Request:
    id: str
    secret_id: str
    secret_name: str
    secret_kind: str
    details: dict[str, str]  # the secret's public fields, as context for the user
    fields: tuple[str, ...]  # private field keys asked for, in the secret's order
    labels: dict[str, str]
    purpose: str
    preview: str
    created_at: int
    expires_at: int
    delivery: str = PLACEHOLDERS
    reason: str = ""  # why the agent must read the values (text delivery)
    alias: str = ""
    status: str = PENDING
    decided_by: str | None = None
    decided_at: int | None = None
    shared: tuple[str, ...] = ()
    values: dict[str, str] | None = field(default=None, repr=False)
    lost_to_lock: bool = False  # approved, but the vault locked before the agent collected


@dataclass
class Handle:
    id: str = field(repr=False)
    secret_id: str
    secret_name: str
    labels: str
    purpose: str
    expires_at: int
    env: dict[str, str] = field(repr=False)  # VAULT_[<ALIAS>_]<FIELD> -> value


def hours(seconds: int) -> str:
    n = seconds // 3600
    return f"{n} hour" if n == 1 else f"{n} hours"


ERASE_DELAY = hours(ERASE_DELAY_SECONDS)
ERASE_GRACE = hours(ERASE_GRACE_SECONDS)


def env_name(key: str, alias: str = "") -> str:
    return f"VAULT_{alias.upper()}_{key.upper()}" if alias else f"VAULT_{key.upper()}"


class Service:
    def __init__(self, vault: Vault, pusher: Pusher, public_origin: str):
        self.vault = vault
        self.pusher = pusher
        self._origin = public_origin
        self._requests: dict[str, Request] = {}
        self._handles: dict[str, Handle] = {}
        self._tasks: anyio.abc.TaskGroup | None = None
        self._last_unlock_notice = -UNLOCK_NOTICE_SECONDS
        # An erase that came due while the server was down still waits this long.
        self._erase_not_before = vault.now() + ERASE_GRACE_SECONDS

    def bind_task_group(self, tg: anyio.abc.TaskGroup | None) -> None:
        self._tasks = tg

    def _spawn_push(self, payload: dict[str, str]) -> None:
        if self._tasks is None:
            raise RuntimeError("service task group is not running")
        self._tasks.start_soon(self._push, payload)

    async def _push(self, payload: dict[str, str]) -> None:
        try:
            await self.pusher.send(payload)
        except Exception as e:
            log.warning("push failed error=%s", type(e).__name__)

    def url(self, path: str) -> str:
        return f"{self._origin}{PREFIX}/{path}"

    def _locked_answer(self) -> dict[str, Any]:
        now = self.vault.now()
        if now - self._last_unlock_notice >= UNLOCK_NOTICE_SECONDS:
            self._last_unlock_notice = now
            self._spawn_push(
                {
                    "title": "Vault locked",
                    "body": "An agent wants to use your vault. Unlock it to continue.",
                    "url": f"{PREFIX}/",
                    "tag": "vault-locked",
                }
            )
        return {
            "locked": True,
            "message": "The user's vault is locked. Send them this link in your reply now, since they may not "
            f"get a notification, and ask them to unlock it: {self.url('')} Then try again.",
        }

    # ── erase without the passphrase ─────────────────────────────────────────
    # Every step is announced on every device, so whoever has the passphrase
    # hears of an erase they did not ask for while there is time to stop it.
    def _erase_notice(self, tag: str, title: str, body: str) -> None:
        # A tag per kind of notice: the same tag would silently replace one still on screen.
        self._spawn_push({"title": title, "body": body, "url": f"{PREFIX}/", "tag": f"vault-erase-{tag}"})

    def pending_erase(self) -> tuple[int, str] | None:
        """(when it will really run, who asked), counting the wait after a restart."""
        pending = self.vault.pending_erase()
        return None if pending is None else (max(pending[0], self._erase_not_before), pending[1])

    def announce_overdue_erase(self) -> None:
        """At startup: an erase due within the grace period gets a last notice."""
        pending = self.vault.pending_erase()
        if pending is not None and pending[0] < self._erase_not_before:
            self._erase_notice(
                "restart",
                f"Vault erase in {ERASE_GRACE}",
                f"The server restarted around the time your vault's erase was due. It is erased in {ERASE_GRACE} "
                "unless someone unlocks the vault or cancels the erase.",
            )

    def schedule_erase(self, email: str) -> int:
        at, new = self.vault.schedule_erase(email)
        if new:
            self.vault.audit(email, "scheduled an erase")
            self._erase_notice(
                "scheduled",
                "Vault erase scheduled",
                f"{email} asked to erase your vault. Everything in it is deleted in {ERASE_DELAY} "
                "unless someone unlocks the vault or cancels the erase.",
            )
        return at

    def cancel_erase(self, email: str) -> bool:
        if not self.vault.cancel_erase():
            return False
        self.erase_cancelled(email, by_unlock=False)
        return True

    def erase_cancelled(self, email: str, by_unlock: bool) -> None:
        """After `cancel_erase`, or an unlock that cancelled one."""
        self.vault.audit(email, "cancelled the scheduled erase" + (" by unlocking" if by_unlock else ""))
        how = "unlocked the vault, which cancels" if by_unlock else "cancelled"
        self._erase_notice("cancelled", "Vault erase cancelled", f"{email} {how} the erase. Nothing was deleted.")

    def erase_if_due(self) -> bool:
        """Run from housekeeping, which must go on (expiry, auto-lock) if this fails;
        a failed erase stays scheduled and is tried again on the next pass."""
        try:
            by = self.vault.erase_if_due(not_before=self._erase_not_before)
        except Exception as e:
            log.warning("scheduled erase failed error=%s", type(e).__name__)
            return False
        if by is None:
            return False
        self.vault.audit(by, f"erased the vault (scheduled {ERASE_DELAY} earlier)")
        self._erase_notice("done", "Vault erased", "Your vault was erased as scheduled. Open it to create a new one.")
        return True

    # ── housekeeping ─────────────────────────────────────────────────────────
    def expire(self) -> None:
        now = self.vault.now()
        if not self.vault.is_unlocked():
            # Values the agent has not collected are not kept past a lock.
            self._handles.clear()
            for req in self._requests.values():
                if req.values is not None:
                    req.values, req.lost_to_lock = None, True
        for rid, req in list(self._requests.items()):
            if req.status == PENDING and req.expires_at <= now:
                req.status = EXPIRED
            if req.status != PENDING and (req.decided_at or req.created_at) + RESULT_TTL_SECONDS <= now:
                del self._requests[rid]
        for hid, handle in list(self._handles.items()):
            if handle.expires_at <= now:
                del self._handles[hid]

    def pending(self) -> list[Request]:
        self.expire()
        return sorted((r for r in self._requests.values() if r.status == PENDING), key=lambda r: r.created_at)

    def recent(self) -> list[Request]:
        self.expire()
        done = (r for r in self._requests.values() if r.status != PENDING)
        return sorted(done, key=lambda r: r.decided_at or r.created_at, reverse=True)

    def request(self, rid: str) -> Request | None:
        self.expire()
        return self._requests.get(rid)

    # ── agent side ───────────────────────────────────────────────────────────
    def list_secrets(self) -> dict[str, Any]:
        try:
            found = self.vault.secrets()
        except Locked:
            return self._locked_answer()
        return {
            "secrets": [
                {
                    "id": s.id,
                    "name": s.name,
                    "type": s.kind_label,
                    "use_for": s.usage,
                    "details": {f.label: f.value for f in s.public_fields},
                    "private_fields": [
                        {"key": f.key, "label": f.label, "needs_approval": not self._auto(s, f.key)}
                        for f in s.private_fields
                    ],
                }
                for s in found
            ]
        }

    @staticmethod
    def _auto(secret: Secret, key: str) -> bool:
        return secret.share_mode == AUTO and may_share_without_asking(secret.kind, key)

    @staticmethod
    def _values(secret: Secret, keys: tuple[str, ...]) -> dict[str, str]:
        return {k: f.value for k in keys if (f := secret.field(k)) is not None}

    def _deliver(
        self,
        secret_id: str,
        secret_name: str,
        labels: dict[str, str],
        values: dict[str, str],
        delivery: str,
        purpose: str,
        alias: str,
    ) -> dict[str, Any]:
        """A handle that vault-run redeems, or for "text" the values themselves."""
        if delivery == TEXT:
            return {"secret": secret_name, "values": values, "rules": rules(purpose)}
        names = {k: env_name(k, alias) for k in values}
        handle = Handle(
            id=token_urlsafe(32),
            secret_id=secret_id,
            secret_name=secret_name,
            labels=", ".join(labels[k] for k in values),
            purpose=purpose,
            expires_at=self.vault.now() + HANDLE_TTL_SECONDS,
            env={names[k]: v for k, v in values.items()},
        )
        self._handles[handle.id] = handle
        return {
            "secret": secret_name,
            "handle": handle.id,
            "placeholders": {f"{{{{{names[k]}}}}}": labels[k] for k in values},
            "expires_in": HANDLE_TTL_SECONDS,
            "fill": f"{VAULT_RUN} {handle.id} -- {VAULT_FILL} <template> <output>",
            "run": f"{VAULT_RUN} {handle.id} -- <command reading ${next(iter(names.values()))}>",
            "message": "Put the placeholders where the values go (a template file for vault-fill, or $VAULT_... "
            "in a command) and run it with vault-run once, within "
            f"{HANDLE_TTL_SECONDS // 60} minutes; load the using-vault-secrets skill for how. Never open the "
            "filled file or print the values.",
        }

    def redeem(self, handle_ids: Any, taken: Any = ()) -> tuple[int, dict[str, Any]]:
        """Called by vault-run through /redeem: the values of its handles, once, while
        unlocked. All or nothing: an unknown handle, or a variable name that two
        handles share or that `taken` (the VAULT_ names already in vault-run's
        environment) holds, uses up none of them, so the user's approvals survive."""
        self.expire()
        if (
            not isinstance(handle_ids, list)
            or not 0 < len(handle_ids) <= MAX_HANDLES
            or not all(isinstance(h, str) for h in handle_ids)
            or len(set(handle_ids)) != len(handle_ids)
        ):
            return 400, {"error": "expected 1 to 10 different handles"}
        handles = [self._handles.get(h) for h in handle_ids]
        if any(h is None for h in handles):
            return 404, {"error": "unknown or expired handle; request that secret again (the others are still valid)"}
        names = [n for h in handles for n in h.env]  # type: ignore[union-attr]
        twice = sorted({n for n in names if names.count(n) > 1})
        if twice:
            error = f"{', '.join(twice)} would come from two secrets: request one of them again with an alias"
            return 409, {"error": error}
        preset = sorted(set(names) & {t for t in taken if isinstance(t, str)}) if isinstance(taken, list) else []
        if preset:
            error = (
                f"{', '.join(preset)} is already set in vault-run's environment (a nested vault-run, or a variable "
                "of your own): unset it, or request the secret again with an alias"
            )
            return 409, {"error": error}
        env: dict[str, str] = {}
        for handle in handles:
            assert handle is not None
            del self._handles[handle.id]
            detail = f"{handle.secret_name}: {handle.labels}. Purpose: {handle.purpose}"
            with SECRET_FILTER.guarding(handle.env.values()):
                self.vault.audit("agent", "given to a command", handle.secret_id, detail)
                log.info("redeemed secret=%s fields=%s", handle.secret_id, ",".join(handle.env))
            env.update(handle.env)
        return 200, {"secrets": [h.secret_name for h in handles if h is not None], "env": env}

    async def request_secret(
        self,
        secret_id: str,
        purpose: str,
        fields: list[str] | None = None,
        delivery: str = PLACEHOLDERS,
        reason: str | None = None,
        alias: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(secret_id, str) or not isinstance(purpose, str):
            return {"status": "refused", "error": "secret_id and purpose must be strings"}
        if fields is not None and not isinstance(fields, list):
            return {"status": "refused", "error": "fields must be a list of private field keys"}
        if delivery not in (PLACEHOLDERS, TEXT):
            return {"status": "refused", "error": f"delivery must be '{PLACEHOLDERS}' or '{TEXT}'"}
        reason = reason.strip() if isinstance(reason, str) else ""
        if delivery == TEXT and not 0 < len(reason) <= MAX_PURPOSE_LENGTH:
            error = (
                f"delivery '{TEXT}' puts the values in the conversation, which the AI provider sees: use "
                f"'{PLACEHOLDERS}' unless you must read them, and then give the reason you must "
                f"(1-{MAX_PURPOSE_LENGTH} characters), which the user reads before approving"
            )
            return {"status": "refused", "error": error}
        alias = alias if isinstance(alias, str) else ""
        if alias and not ALIAS.fullmatch(alias):
            return {"status": "refused", "error": "alias must be 1-20 letters or digits, starting with a letter"}
        self.expire()
        if delivery == PLACEHOLDERS and len(self._handles) >= MAX_HANDLES:
            error = "too many unused handles; run vault-run with the ones you have, or wait for them to expire"
            return {"status": "refused", "error": error}
        purpose = purpose.strip()
        if not purpose or len(purpose) > MAX_PURPOSE_LENGTH:
            return {"status": "refused", "error": f"say what the values are for (1-{MAX_PURPOSE_LENGTH} characters)"}
        try:
            secret = self.vault.secret(secret_id)
        except Locked:
            return self._locked_answer()
        if secret is None:
            return {"status": "refused", "error": f"no secret '{secret_id}' (see list_secrets)"}
        private = {f.key for f in secret.private_fields}
        wanted = private if not fields else {f for f in fields if isinstance(f, str)}
        public = wanted & {f.key for f in secret.public_fields}
        if public:
            return {"status": "refused", "error": f"{', '.join(sorted(public))}: already in list_secrets"}
        if not wanted or wanted - private:
            known = ", ".join(f.key for f in secret.private_fields)
            return {"status": "refused", "error": f"ask for one or more of this secret's private fields: {known}"}
        keys = tuple(f.key for f in secret.private_fields if f.key in wanted)
        field_labels = {k: secret.field(k).label for k in keys}  # type: ignore[union-attr]
        labels = ", ".join(field_labels.values())

        why = f"Purpose: {purpose}" + (f". Reason to read them: {reason}" if delivery == TEXT else "")
        if all(self._auto(secret, k) for k in keys):
            values = self._values(secret, keys)
            with SECRET_FILTER.guarding(values.values()):
                self.vault.audit("agent", "shared without asking", secret.id, f"{secret.name}: {labels}. {why}")
                log.info("shared without asking secret=%s fields=%s", secret.id, ",".join(keys))
            self._spawn_push(
                {
                    "title": "Shared with an agent",
                    "body": f"{secret.name}: {labels}",
                    "url": f"{PREFIX}/audit",
                    "tag": f"vault-shared-{secret.id}",
                }
            )
            delivered = self._deliver(secret.id, secret.name, field_labels, values, delivery, purpose, alias)
            return {"status": DONE, **delivered}

        if sum(1 for r in self._requests.values() if r.status == PENDING) >= MAX_PENDING:
            return {"status": "refused", "error": "too many requests are waiting; ask the user to answer them first"}
        now = self.vault.now()
        req = Request(
            id=token_urlsafe(16),
            secret_id=secret.id,
            secret_name=secret.name,
            secret_kind=secret.kind_label,
            details={f.label: f.value for f in secret.public_fields},
            fields=keys,
            labels=field_labels,
            purpose=purpose,
            preview=preview(secret.kind, list(secret.fields)),
            created_at=now,
            expires_at=now + REQUEST_TTL_SECONDS,
            delivery=delivery,
            reason=reason if delivery == TEXT else "",
            alias=alias,
        )
        self._requests[req.id] = req
        log.info("pending id=%s secret=%s fields=%s", req.id, secret.id, ",".join(keys))
        self._spawn_push(
            {
                "title": "Approve sharing",
                "body": f"{secret.name}: {labels}",
                "url": f"{PREFIX}/r/{req.id}",
                "tag": f"vault-request-{req.id}",
            }
        )
        approve_url = self.url(f"r/{req.id}")
        return {
            "id": req.id,
            "status": PENDING,
            "approve_url": approve_url,
            "message": "Waiting for the user's approval. Send them this link in your reply now, since they may "
            f"not get a notification: {approve_url} {KEEP_WAITING}",
        }

    async def get_result(self, rid: str, wait_seconds: float = 0) -> dict[str, Any]:
        wait = max(0.0, min(float(wait_seconds or 0), MAX_WAIT_SECONDS))
        with anyio.move_on_after(wait):
            while (req := self.request(rid) if isinstance(rid, str) else None) and req.status == PENDING:
                await anyio.sleep(0.5)
        req = self.request(rid) if isinstance(rid, str) else None
        if req is None:
            return {"status": "unknown", "error": f"no request '{rid}' (requests are forgotten after a restart)"}
        if req.status == PENDING:
            message = f"Still waiting for the user's approval. {KEEP_WAITING}"
            return {"id": rid, "status": PENDING, "approve_url": self.url(f"r/{rid}"), "message": message}
        if req.status in (DENIED, EXPIRED):
            return {"id": rid, "status": req.status}
        if req.lost_to_lock:
            note = "the vault was locked before the values were collected; request them again"
            return {"id": rid, "status": EXPIRED, "note": note}
        if req.values is None:
            return {"id": rid, "status": DONE, "note": "the values were already returned"}
        values, req.values = req.values, None
        delivered = self._deliver(
            req.secret_id, req.secret_name, req.labels, values, req.delivery, req.purpose, req.alias
        )
        result: dict[str, Any] = {"id": rid, "status": DONE, **delivered}
        withheld = [req.labels[k] for k in req.fields if k not in req.shared]
        if withheld:
            result["withheld"] = withheld
        return result

    # ── user side ────────────────────────────────────────────────────────────
    def decide(self, rid: str, approve: bool, fields: list[str], email: str) -> Request:
        req = self.request(rid)
        if req is None or req.status != PENDING:
            raise VaultError("this request is no longer waiting (answered, expired, or the vault restarted)")
        now = self.vault.now()
        if not approve:
            req.status, req.decided_by, req.decided_at = DENIED, email, now
            self.vault.audit(email, "denied", req.secret_id, f"{req.secret_name}. Purpose: {req.purpose}")
            return req
        shared = tuple(k for k in req.fields if k in set(fields))
        if not shared:
            raise VaultError("tick at least one field to share, or deny")
        secret = self.vault.secret(req.secret_id)
        if secret is None:
            raise VaultError("that secret was deleted")
        values = self._values(secret, shared)
        if len(values) != len(shared):
            raise VaultError("that secret changed; ask the agent to request it again")
        labels = ", ".join(req.labels[k] for k in shared)
        with SECRET_FILTER.guarding(values.values()):
            why = f"Purpose: {req.purpose}" + (f". Reason to read them: {req.reason}" if req.reason else "")
            self.vault.audit(email, "shared on approval", req.secret_id, f"{req.secret_name}: {labels}. {why}")
            log.info("approved id=%s secret=%s fields=%s by=%s", rid, req.secret_id, ",".join(shared), email)
        req.status, req.decided_by, req.decided_at, req.shared, req.values = DONE, email, now, shared, values
        return req
