import json
import logging
import sqlite3
from collections import Counter

import anyio
import pytest

from garden_vault.config import (
    ERASE_GRACE_SECONDS,
    HANDLE_TTL_SECONDS,
    MAX_HANDLES,
    MAX_PENDING,
    MAX_WAIT_SECONDS,
    REQUEST_TTL_SECONDS,
)
from garden_vault.vault import Vault, VaultError

from .conftest import (
    ADDRESS_TEXT,
    CARD,
    EMAIL,
    FAST_KDF,
    ORIGIN,
    PASSPHRASE,
    SECRET,
    STREET,
    FakeSender,
    add_address,
    add_card,
    make_service,
    with_service,
)

pytestmark = pytest.mark.anyio


async def test_list_secrets_shows_public_details_never_private_values(settings, vault):
    add_address(vault)
    add_card(vault)
    listed = make_service(settings, vault).list_secrets()
    text = json.dumps(listed)
    for needle in (STREET, CARD, SECRET, "Lisboa", '123"'):
        assert needle not in text
    card = next(s for s in listed["secrets"] if s["name"] == "Visa")
    assert card["type"] == "Payment card"
    assert card["details"] == {"Name on card": "Alfred Test", "Expiry (MM/YY)": "12/30"}
    assert [f["key"] for f in card["private_fields"]] == ["number", "security_code", "notes"]
    assert card["needs_approval"] is True
    address = next(s for s in listed["secrets"] if s["name"] == "Home address")
    assert address["details"] == {} and address["use_for"] == "deliveries"


async def test_locked_vault_refuses_and_asks_to_unlock(settings, vault):
    add_address(vault)
    vault.lock()
    sender = FakeSender()
    vault.add_subscription("https://push.example/1", "k", "a", EMAIL)
    service = make_service(settings, vault, sender)

    async def flow():
        return service.list_secrets(), await service.request_secret("x", "delivery")

    a, b = await with_service(service, flow)
    assert a["locked"] and b["locked"]
    # Push may be off, so the agent is told to post the link rather than that the user knows.
    assert service.url("") in b["message"] and "notified" not in b["message"]
    # One unlock notice, not one per call.
    assert len(sender.calls) == 1 and "locked" in json.loads(sender.calls[0]["data"])["title"].lower()


async def test_approval_flow_shares_only_ticked_fields_once(settings, vault, caplog):
    caplog.set_level(logging.DEBUG)
    card = add_card(vault)
    sender = FakeSender()
    vault.add_subscription("https://push.example/1", "k", "a", EMAIL)
    service = make_service(settings, vault, sender)

    async def flow():
        sub = await service.request_secret(card, "Pay for the train")  # no fields: all private ones
        assert sub["status"] == "pending" and sub["approve_url"] == f"{ORIGIN}/vault/r/{sub['id']}"
        assert sub["approve_url"] in sub["message"] and "notified" not in sub["message"]
        req = service.request(sub["id"])
        assert req.fields == ("number", "security_code", "notes")
        assert req.details == {"Name on card": "Alfred Test", "Expiry (MM/YY)": "12/30"}
        assert (await service.get_result(sub["id"]))["status"] == "pending"
        service.decide(sub["id"], True, ["number", "security_code"], EMAIL)
        return await service.get_result(sub["id"]), await service.get_result(sub["id"])

    first, second = await with_service(service, flow)
    assert first["status"] == "done" and first["values"] == {"number": CARD, "security_code": "123"}
    assert first["withheld"] == ["Notes"]
    assert "values" not in second
    pushed = json.dumps([c["data"] for c in sender.calls])
    assert "Visa" in pushed and CARD not in pushed and "4242" not in pushed
    assert CARD not in caplog.text and SECRET not in caplog.text
    assert any(e.action == "shared on approval" for e in vault.audit_log())


async def test_a_pending_answer_tells_the_agent_to_keep_waiting(settings, vault):
    # Nothing wakes an agent whose turn ended with nothing running: the user's
    # approval lands, and the agent never collects it.
    service = make_service(settings, vault, FakeSender())

    async def flow():
        sub = await service.request_secret(add_card(vault), "Pay for the train")
        return sub, await service.get_result(sub["id"])

    sub, still = await with_service(service, flow)
    for answer in (sub, still):
        message = answer["message"]
        assert f"wait_seconds {MAX_WAIT_SECONDS}" in message and "background" in message
        assert "until" in message and "end your turn" in message
    assert sub["approve_url"] in sub["message"]


async def test_public_fields_are_not_requested(settings, vault):
    card = add_card(vault)
    service = make_service(settings, vault)
    res = await with_service(service, lambda: service.request_secret(card, "x", ["expiry", "number"]))
    assert res["status"] == "refused" and "already in list_secrets" in res["error"]


async def test_deny_and_expiry(settings, vault, clock):
    secret = add_address(vault)
    service = make_service(settings, vault)

    async def flow():
        a = await service.request_secret(secret, "x")
        service.decide(a["id"], False, [], EMAIL)
        b = await service.request_secret(secret, "y")
        clock.t += REQUEST_TTL_SECONDS + 1
        with pytest.raises(VaultError):
            service.decide(b["id"], True, ["text"], EMAIL)
        return await service.get_result(a["id"]), await service.get_result(b["id"])

    a, b = await with_service(service, flow)
    assert a["status"] == "denied" and b["status"] == "expired"


async def test_cannot_approve_twice_or_add_fields(settings, vault):
    card = add_card(vault)
    service = make_service(settings, vault)

    async def flow():
        sub = await service.request_secret(card, "x", ["number"])
        with pytest.raises(VaultError):
            service.decide(sub["id"], True, ["notes"], EMAIL)  # not requested: nothing left to share
        service.decide(sub["id"], True, ["number", "notes"], EMAIL)
        with pytest.raises(VaultError):
            service.decide(sub["id"], True, ["number"], EMAIL)
        return await service.get_result(sub["id"])

    assert (await with_service(service, flow))["values"] == {"number": CARD}


async def test_shared_values_come_with_the_rules_for_using_them(settings, vault):
    # Asked to "check" an address, an agent pasted it back into the chat. The
    # rules arrive with the values, so they are in front of the agent when it
    # uses them, whether the user approved or the secret is shared without asking.
    auto = add_address(vault, share_mode="auto")
    card = add_card(vault)
    service = make_service(settings, vault)

    async def flow():
        direct = await service.request_secret(auto, "Check my delivery address")
        sub = await service.request_secret(card, "Pay for the train", ["number"])
        service.decide(sub["id"], True, ["number"], EMAIL)
        return direct, await service.get_result(sub["id"])

    direct, approved = await with_service(service, flow)
    for res, purpose, value in ((direct, "Check my delivery address", STREET), (approved, "Pay for the train", CARD)):
        rules = res["rules"]
        assert purpose in rules and value not in rules
        assert "never repeat" in rules.lower() and "your reply" in rules
        assert "lookup" in rules and "service" in rules
        assert "say what you checked" in rules


async def test_free_text_address_shared_without_asking(settings, vault):
    secret = add_address(vault, share_mode="auto")
    sender = FakeSender()
    vault.add_subscription("https://push.example/1", "k", "a", EMAIL)
    service = make_service(settings, vault, sender)
    res = await with_service(service, lambda: service.request_secret(secret, "delivery form"))
    assert res["status"] == "done" and res["secret"] == "Home address" and res["values"] == {"text": ADDRESS_TEXT}
    assert vault.audit_log()[0].action == "shared without asking"
    assert STREET not in json.dumps([c["data"] for c in sender.calls])


@pytest.mark.parametrize(
    "args,error",
    [
        (("nope", "x"), "no secret"),
        (("SECRET", "x", ["iban"]), "private fields"),
        (("SECRET", ""), "say what"),
        (("SECRET", "x" * 600), "say what"),
    ],
)
async def test_bad_requests_refused(settings, vault, args, error):
    secret = add_address(vault)
    service = make_service(settings, vault)
    args = tuple(secret if a == "SECRET" else a for a in args)
    res = await with_service(service, lambda: service.request_secret(*args))
    assert res["status"] == "refused" and error in res["error"]


async def test_pending_cap(settings, vault):
    secret = add_address(vault)
    service = make_service(settings, vault)

    async def flow():
        for _ in range(MAX_PENDING):
            assert (await service.request_secret(secret, "x"))["status"] == "pending"
        return await service.request_secret(secret, "x")

    assert (await with_service(service, flow))["status"] == "refused"


async def test_long_poll_returns_on_decision(settings, vault):
    secret = add_address(vault)
    service = make_service(settings, vault)

    async def flow():
        sub = await service.request_secret(secret, "x")

        async def answer():
            await anyio.sleep(0.2)
            service.decide(sub["id"], True, ["text"], EMAIL)

        async with anyio.create_task_group() as tg:
            tg.start_soon(answer)
            with anyio.fail_after(5):
                return await service.get_result(sub["id"], wait_seconds=10)

    assert (await with_service(service, flow))["status"] == "done"


async def test_deleted_secret_cannot_be_shared(settings, vault):
    secret = add_address(vault)
    service = make_service(settings, vault)

    async def flow():
        sub = await service.request_secret(secret, "x")
        vault.delete_secret(secret)
        with pytest.raises(VaultError):
            service.decide(sub["id"], True, ["text"], EMAIL)

    await with_service(service, flow)


async def test_restart_forgets_requests(settings, vault, vault_path, clock):
    from garden_vault.vault import Vault

    from .conftest import FAST_KDF

    secret = add_address(vault)
    service = make_service(settings, vault)
    sub = await with_service(service, lambda: service.request_secret(secret, "x"))
    fresh = Vault(vault_path, clock=clock, kdf=FAST_KDF)
    fresh.unlock(PASSPHRASE)
    assert (await make_service(settings, fresh).get_result(sub["id"]))["status"] == "unknown"


# ── env delivery: a one-time handle instead of the values ─────────────────────


async def test_env_delivery_returns_a_handle_never_values(settings, vault, caplog):
    caplog.set_level(logging.DEBUG)
    card = add_card(vault)
    service = make_service(settings, vault)

    async def flow():
        sub = await service.request_secret(card, "Pay for the train", ["number", "notes"], delivery="env")
        assert service.request(sub["id"]).delivery == "env"
        service.decide(sub["id"], True, ["number"], EMAIL)
        return await service.get_result(sub["id"]), await service.get_result(sub["id"])

    first, second = await with_service(service, flow)
    assert first["status"] == "done" and first["secret"] == "Visa" and "values" not in first
    assert first["env"] == {"VAULT_NUMBER": "Card number"} and first["withheld"] == ["Notes"]
    assert first["expires_in"] == HANDLE_TTL_SECONDS and first["handle"] in first["run"]
    assert "vault-run" in first["run"]
    assert CARD not in json.dumps(first) and SECRET not in json.dumps(first)
    assert "handle" not in second and "values" not in second

    redeemed = service.redeem(first["handle"])
    assert redeemed == {"secret": "Visa", "env": {"VAULT_NUMBER": CARD}}
    assert service.redeem(first["handle"]) is None  # single use
    assert any(e.action == "given to a command" for e in vault.audit_log())
    assert CARD not in caplog.text and first["handle"] not in caplog.text


async def test_handle_expires(settings, vault, clock):
    secret = add_address(vault, share_mode="auto")
    service = make_service(settings, vault)
    res = await with_service(service, lambda: service.request_secret(secret, "delivery form", delivery="env"))
    clock.t += HANDLE_TTL_SECONDS + 1
    assert service.redeem(res["handle"]) is None


async def test_env_delivery_without_asking_returns_a_handle_at_once(settings, vault):
    secret = add_address(vault, share_mode="auto")
    service = make_service(settings, vault)
    res = await with_service(service, lambda: service.request_secret(secret, "delivery form", delivery="env"))
    assert res["status"] == "done" and res["env"] == {"VAULT_TEXT": "Text"}
    assert STREET not in json.dumps(res)
    assert service.redeem(res["handle"]) == {"secret": "Home address", "env": {"VAULT_TEXT": ADDRESS_TEXT}}


@pytest.mark.parametrize("bad", ["", "file", None, 1])
async def test_unknown_delivery_refused(settings, vault, bad):
    secret = add_address(vault)
    service = make_service(settings, vault)
    res = await with_service(service, lambda: service.request_secret(secret, "x", delivery=bad))
    assert res["status"] == "refused" and "delivery" in res["error"]


@pytest.mark.parametrize("bad", ["nope", "", None, 42])
async def test_redeem_unknown_handle(settings, vault, bad):
    assert make_service(settings, vault).redeem(bad) is None


async def test_redeem_is_audited_with_the_purpose(settings, vault):
    secret = add_address(vault, share_mode="auto")
    service = make_service(settings, vault)
    res = await with_service(service, lambda: service.request_secret(secret, "delivery form", delivery="env"))
    service.redeem(res["handle"])
    entry = next(e for e in vault.audit_log() if e.action == "given to a command")
    assert entry.detail == "Home address: Text. Purpose: delivery form"


async def test_lock_forgets_handles_and_uncollected_values(settings, vault):
    auto = add_address(vault, share_mode="auto")
    card = add_card(vault)
    service = make_service(settings, vault)

    async def flow():
        handle = (await service.request_secret(auto, "delivery form", delivery="env"))["handle"]
        sub = await service.request_secret(card, "Pay for the train", ["number"])
        service.decide(sub["id"], True, ["number"], EMAIL)
        vault.lock()
        locked = service.redeem(handle), await service.get_result(sub["id"])
        vault.unlock(PASSPHRASE)
        return locked, service.redeem(handle), await service.get_result(sub["id"])

    (redeemed, result), after_unlock, result_after_unlock = await with_service(service, flow)
    assert redeemed is None and after_unlock is None
    for res in (result, result_after_unlock):
        assert res["status"] == "expired" and "locked" in res["note"] and "values" not in res
    assert CARD not in json.dumps([result, result_after_unlock])


async def test_unused_handles_are_capped(settings, vault):
    secret = add_address(vault, share_mode="auto")
    service = make_service(settings, vault)

    async def flow():
        for _ in range(MAX_HANDLES):
            assert (await service.request_secret(secret, "x", delivery="env"))["status"] == "done"
        refused = await service.request_secret(secret, "x", delivery="env")
        text = await service.request_secret(secret, "x")  # text delivery is not capped
        return refused, text

    refused, text = await with_service(service, flow)
    assert refused["status"] == "refused" and "handles" in refused["error"]
    assert text["status"] == "done"


def pushed_titles(sender: FakeSender) -> list[str]:
    # Sorted: each push is sent from its own worker thread, so they finish in any order.
    return sorted(json.loads(c["data"])["title"] for c in sender.calls)


def audit_actions(vault) -> list[tuple[str, str]]:
    # Actor and action are plaintext, so this reads them while locked or erased.
    return [(r["actor"], r["action"]) for r in vault._q("SELECT actor, action FROM audit ORDER BY rowid")]


async def test_scheduled_erase_tells_every_device_and_runs_when_due(settings, vault, clock):
    add_card(vault)
    vault.add_subscription("https://push.example/1", "k", "a", EMAIL)
    vault.add_subscription("https://push.example/2", "k", "a", EMAIL)
    vault.lock()
    sender = FakeSender()
    service = make_service(settings, vault, sender)

    async def flow():
        at = service.schedule_erase(EMAIL)
        service.schedule_erase(EMAIL)  # already scheduled: no second notice
        assert service.erase_if_due() is False
        for _ in range(100):  # the two pushes run in worker threads
            if len(sender.calls) >= 2:
                break
            await anyio.sleep(0.01)
        scheduled = (pushed_titles(sender), audit_actions(vault))
        clock.t = at
        assert service.erase_if_due() is True
        return scheduled

    (titles, actions) = await with_service(service, flow)
    assert titles == ["Vault erase scheduled"] * 2  # one per device
    assert actions[-1] == (EMAIL, "scheduled an erase")
    assert pushed_titles(sender) == sorted(["Vault erase scheduled"] * 2 + ["Vault erased"] * 2)
    assert audit_actions(vault) == [(EMAIL, "erased the vault (scheduled 24 hours earlier)")]
    assert not vault.is_setup()
    pushed = json.dumps([c["data"] for c in sender.calls])
    for needle in (CARD, SECRET, "Visa"):
        assert needle not in pushed


async def test_cancelled_erase_is_announced(settings, vault):
    vault.add_subscription("https://push.example/1", "k", "a", EMAIL)
    vault.lock()
    sender = FakeSender()
    service = make_service(settings, vault, sender)

    async def flow():
        service.schedule_erase(EMAIL)
        assert service.cancel_erase(EMAIL) is True
        assert service.cancel_erase(EMAIL) is False  # nothing left to cancel: no notice
        service.schedule_erase(EMAIL)
        assert vault.unlock(PASSPHRASE) is True
        service.erase_cancelled(EMAIL, by_unlock=True)

    await with_service(service, flow)
    assert pushed_titles(sender) == sorted(["Vault erase scheduled", "Vault erase cancelled"] * 2)
    cancelled = [json.loads(c["data"])["body"] for c in sender.calls if "cancelled" in c["data"]]
    assert sorted("unlocked" in b for b in cancelled) == [False, True]
    assert [a for _, a in audit_actions(vault)][-4:] == [
        "scheduled an erase",
        "cancelled the scheduled erase",
        "scheduled an erase",
        "cancelled the scheduled erase by unlocking",
    ]


async def test_each_erase_notice_has_its_own_tag(settings, vault, clock):
    # One tag would let a later notice silently replace one still on screen.
    vault.add_subscription("https://push.example/1", "k", "a", EMAIL)
    vault.lock()
    sender = FakeSender()
    service = make_service(settings, vault, sender)

    async def flow():
        service.schedule_erase(EMAIL)
        service.cancel_erase(EMAIL)
        clock.t = service.schedule_erase(EMAIL)
        service.erase_if_due()

    await with_service(service, flow)
    tags = Counter(json.loads(c["data"])["tag"] for c in sender.calls)
    assert tags == {"vault-erase-scheduled": 2, "vault-erase-cancelled": 1, "vault-erase-done": 1}


async def test_a_failed_erase_is_logged_and_retried(settings, vault, clock, monkeypatch, caplog):
    vault.lock()
    sender = FakeSender()
    vault.add_subscription("https://push.example/1", "k", "a", EMAIL)
    service = make_service(settings, vault, sender)

    def broken():
        raise sqlite3.OperationalError("database or disk is full")

    async def flow():
        clock.t = service.schedule_erase(EMAIL)
        monkeypatch.setattr(vault, "reset", broken)
        assert service.erase_if_due() is False  # housekeeping goes on
        monkeypatch.undo()
        return service.erase_if_due()

    assert await with_service(service, flow) is True
    assert "erase failed" in caplog.text and "OperationalError" in caplog.text
    assert pushed_titles(sender) == sorted(["Vault erase scheduled", "Vault erased"])


async def test_an_erase_due_during_downtime_waits_an_hour_and_says_so(settings, vault, vault_path, clock):
    vault.lock()
    vault.add_subscription("https://push.example/1", "k", "a", EMAIL)
    at, _ = vault.schedule_erase(EMAIL)
    clock.t = at + 4 * 3600  # the server was down when it came due
    sender = FakeSender()
    service = make_service(settings, Vault(vault_path, clock=clock, kdf=FAST_KDF), sender)
    restarted = int(clock.t)

    async def flow():
        service.announce_overdue_erase()
        assert service.pending_erase() == (restarted + ERASE_GRACE_SECONDS, EMAIL)
        assert service.erase_if_due() is False
        clock.t = restarted + ERASE_GRACE_SECONDS - 1
        assert service.erase_if_due() is False
        clock.t += 1
        return service.erase_if_due()

    assert await with_service(service, flow) is True
    assert pushed_titles(sender) == sorted(["Vault erase in 1 hour", "Vault erased"])


async def test_no_restart_notice_for_an_erase_far_away(settings, vault):
    vault.lock()
    vault.add_subscription("https://push.example/1", "k", "a", EMAIL)
    vault.schedule_erase(EMAIL)
    sender = FakeSender()
    service = make_service(settings, vault, sender)

    async def flow():
        service.announce_overdue_erase()

    await with_service(service, flow)
    assert sender.calls == []
