import sqlite3

import pytest

from garden_vault import crypto
from garden_vault.config import ERASE_DELAY_SECONDS
from garden_vault.kinds import KINDS
from garden_vault.vault import AUTO, Locked, Vault, VaultError, build_fields

from .conftest import CARD, FAST_KDF, PASSPHRASE, SECRET, STREET, add_address, add_card


def test_seal_round_trip_and_unique_nonces():
    key = crypto.new_key()
    a = crypto.seal(key, b"hello", b"row:1")
    b = crypto.seal(key, b"hello", b"row:1")
    assert a != b
    assert crypto.open_sealed(key, a, b"row:1") == b"hello"


def test_tampering_or_moving_a_blob_fails():
    key = crypto.new_key()
    blob = crypto.seal(key, b"hello", b"secret:a")
    with pytest.raises(crypto.CryptoError):
        crypto.open_sealed(key, blob, b"secret:b")
    with pytest.raises(crypto.CryptoError):
        crypto.open_sealed(crypto.new_key(), blob, b"secret:a")
    flipped = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(crypto.CryptoError):
        crypto.open_sealed(key, flipped, b"secret:a")


def test_nothing_readable_on_disk(vault, vault_path):
    add_address(vault)
    add_card(vault)
    vault.audit("alfred@gmail.com", "shared on approval", None, f"Home address: {STREET}")
    raw = open(vault_path, "rb").read()
    for needle in (STREET, CARD, SECRET, "Home address", "Lisboa", "Visa", "Alfred Test", PASSPHRASE, "deliveries"):
        assert needle.encode() not in raw, needle


def test_restart_comes_back_locked(vault, vault_path, clock):
    secret_id = add_address(vault)
    again = Vault(vault_path, clock=clock, kdf=FAST_KDF)
    assert again.is_setup() and not again.is_unlocked()
    with pytest.raises(Locked):
        again.secrets()
    with pytest.raises(VaultError, match="wrong passphrase"):
        again.unlock("not the passphrase at all")
    again.unlock(PASSPHRASE)
    assert STREET in again.secret(secret_id).field("text").value


def test_setup_rules(vault_path, clock):
    v = Vault(vault_path, clock=clock, kdf=FAST_KDF)
    assert not v.is_setup()
    with pytest.raises(VaultError, match="12"):
        v.setup("short")
    v.setup(PASSPHRASE)
    with pytest.raises(VaultError, match="already"):
        v.setup(PASSPHRASE)


def test_auto_lock_counts_only_user_activity(vault, clock):
    add_address(vault)
    clock.t += 11 * 3600
    vault.secrets()  # agent-side use does not keep it open
    clock.t += 2 * 3600
    assert not vault.is_unlocked()
    vault.unlock(PASSPHRASE)
    clock.t += 11 * 3600
    vault.touch()  # the person using the UI does
    clock.t += 2 * 3600
    assert vault.is_unlocked()


def test_lock(vault):
    vault.lock()
    with pytest.raises(Locked):
        vault.secrets()


def test_change_passphrase_keeps_secrets(vault, vault_path, clock):
    secret_id = add_address(vault)
    with pytest.raises(VaultError):
        vault.change_passphrase("wrong wrong wrong", "another long passphrase")
    vault.change_passphrase(PASSPHRASE, "another long passphrase")
    again = Vault(vault_path, clock=clock, kdf=FAST_KDF)
    with pytest.raises(VaultError):
        again.unlock(PASSPHRASE)
    again.unlock("another long passphrase")
    assert again.secret(secret_id).name == "Home address"


def test_reset_erases_everything(vault, vault_path):
    add_card(vault)
    vault.add_subscription("https://push.example/1", "k", "a", "alfred@gmail.com")
    vault.reset()
    assert not vault.is_setup() and not vault.is_unlocked()
    assert len(vault.subscriptions()) == 1
    assert CARD.encode() not in open(vault_path, "rb").read()


def test_erase_can_be_scheduled_only_for_a_locked_vault(vault, vault_path, clock):
    with pytest.raises(VaultError, match="unlocked"):
        vault.schedule_erase("alfred@gmail.com")
    assert vault.pending_erase() is None
    empty = Vault(str(vault_path) + ".new", clock=clock, kdf=FAST_KDF)
    with pytest.raises(VaultError, match="no vault"):
        empty.schedule_erase("alfred@gmail.com")


def test_scheduled_erase_runs_after_24_hours(vault, vault_path, clock):
    add_card(vault)
    vault.add_subscription("https://push.example/1", "k", "a", "alfred@gmail.com")
    vault.lock()
    at, new = vault.schedule_erase("alfred@gmail.com")
    assert new and at == int(clock.t) + ERASE_DELAY_SECONDS
    assert vault.pending_erase() == (at, "alfred@gmail.com")
    clock.t += 3600
    # Asking again neither moves the time nor counts as a new request.
    assert vault.schedule_erase("hal@example.com") == (at, False)
    assert vault.pending_erase() == (at, "alfred@gmail.com")
    clock.t = at - 1
    assert vault.erase_if_due() is None and vault.is_setup()
    clock.t = at
    assert vault.erase_if_due() == "alfred@gmail.com"
    assert not vault.is_setup() and vault.pending_erase() is None
    assert len(vault.subscriptions()) == 1
    assert CARD.encode() not in open(vault_path, "rb").read()
    assert vault.erase_if_due() is None


def test_scheduled_erase_survives_a_restart(vault, vault_path, clock):
    vault.lock()
    at, _ = vault.schedule_erase("alfred@gmail.com")
    again = Vault(vault_path, clock=clock, kdf=FAST_KDF)
    assert again.pending_erase() == (at, "alfred@gmail.com")
    clock.t = at + 1
    assert again.erase_if_due() == "alfred@gmail.com" and not again.is_setup()


def test_unlocking_or_cancelling_stops_the_erase(vault, clock):
    vault.lock()
    vault.schedule_erase("alfred@gmail.com")
    assert vault.unlock(PASSPHRASE) is True
    assert vault.pending_erase() is None
    vault.lock()
    assert vault.unlock(PASSPHRASE) is False
    vault.lock()
    vault.schedule_erase("alfred@gmail.com")
    assert vault.cancel_erase() is True and vault.pending_erase() is None
    assert vault.cancel_erase() is False
    clock.t += 2 * ERASE_DELAY_SECONDS
    assert vault.erase_if_due() is None and vault.is_setup()


def test_unlock_fails_when_the_vault_was_erased_meanwhile(vault, monkeypatch):
    vault.lock()
    unwrap = vault._unwrap

    def erase_during_kdf(passphrase):
        key = unwrap(passphrase)
        vault.reset()
        return key

    monkeypatch.setattr(vault, "_unwrap", erase_during_kdf)
    with pytest.raises(VaultError, match="erased"):
        vault.unlock(PASSPHRASE)
    assert not vault.is_unlocked()


def test_types_are_short_and_private_where_it_matters():
    shapes = {key: [(f.key, f.sensitive, f.required) for f in kind.fields] for key, kind in KINDS.items()}
    text = [("text", True, True)]
    assert shapes["note"] == shapes["address"] == shapes["company"] == text
    assert shapes["membership"] == [("programme", False, False), ("member_number", True, True), ("notes", True, False)]
    assert shapes["id_document"] == [("document_type", False, False), ("number", True, True), ("notes", True, False)]
    assert shapes["card"] == [
        ("cardholder", False, False),
        ("number", True, True),
        ("expiry", False, False),
        ("security_code", True, False),
        ("notes", True, False),
    ]
    assert shapes["login"] == [("website", False, False), ("username", False, False), ("password", True, True)]


@pytest.mark.parametrize("kind", ["note", "address", "company", "membership"])
def test_these_types_may_share_without_asking_even_with_private_fields(vault, kind):
    values = {"text": "x", "member_number": "123"}
    fields = build_fields(kind, values)
    assert any(f.sensitive for f in fields)
    assert vault.secret(vault.add_secret(kind, "s", "", fields, AUTO)).share_mode == AUTO


@pytest.mark.parametrize(
    "kind,values",
    [("card", {"number": CARD}), ("login", {"password": SECRET}), ("id_document", {"number": "AB123456"})],
)
def test_cards_logins_and_documents_always_ask(vault, kind, values):
    with pytest.raises(VaultError, match="always ask"):
        vault.add_secret(kind, "s", "", build_fields(kind, values), AUTO)


def test_build_fields_rules():
    fields = build_fields("login", {"username": "alfred", "password": SECRET, "unknown": "dropped"})
    assert [(f.key, f.sensitive) for f in fields] == [("username", False), ("password", True)]
    with pytest.raises(VaultError, match="Password is required"):
        build_fields("login", {"website": "shop.example"})
    with pytest.raises(VaultError, match="Text is required"):
        build_fields("address", {"text": "   "})
    with pytest.raises(VaultError, match="longer"):
        build_fields("note", {"text": "x" * 5000})
    with pytest.raises(VaultError):
        build_fields("rocket", {"text": "y"})


def test_audit_is_encrypted_and_readable_when_unlocked(vault):
    vault.audit("alfred@gmail.com", "viewed private fields", None, "Visa")
    assert vault.audit_log()[0].detail == "Visa"
    vault.lock()
    vault.audit("alfred@gmail.com", "failed unlock")
    with pytest.raises(Locked):
        vault.audit_log()


def test_a_failed_cancel_leaves_the_vault_locked(vault, monkeypatch):
    vault.lock()
    vault.schedule_erase("alfred@gmail.com")

    def broken():
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(vault, "cancel_erase", broken)
    with pytest.raises(sqlite3.OperationalError):
        vault.unlock(PASSPHRASE)
    # Unlocked with the erase still scheduled, it would run after the next lock, unannounced.
    assert not vault.is_unlocked() and vault.pending_erase() is not None


def test_an_erase_waits_for_its_not_before(vault, clock):
    vault.lock()
    at, _ = vault.schedule_erase("alfred@gmail.com")
    clock.t = at + 10
    assert vault.erase_if_due(not_before=at + 3600) is None and vault.is_setup()
    clock.t = at + 3600
    assert vault.erase_if_due(not_before=at + 3600) == "alfred@gmail.com"
