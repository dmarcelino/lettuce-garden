import dataclasses
import json
import logging

from secret_broker.logs import SecretFilter
from secret_broker.push import Pusher
from secret_broker.vault import Vault

from .conftest import EMAIL, FAST_KDF, ORIGIN, SECRET, FakeSender


def test_push_uses_broker_vapid_key_and_skips_removed_users(settings, vault):
    sender = FakeSender()
    vault.add_subscription("https://push.example/1", "k1", "a1", EMAIL)
    vault.add_subscription("https://push.example/2", "k2", "a2", "removed@example.com")
    payload = {"title": "t", "body": "b", "url": "/secrets/", "tag": "x"}
    assert Pusher(settings, vault, sender).send_sync(payload) == 1
    call = sender.calls[0]
    assert call["subscription_info"]["endpoint"] == "https://push.example/1"
    assert call["vapid_private_key"] == settings.vapid_private_key
    assert call["vapid_claims"] == {"sub": "mailto:ops@example.com"}
    assert json.loads(call["data"])["title"] == "t"


def test_gone_subscription_is_removed(settings, vault):
    vault.add_subscription("https://push.example/1", "k1", "a1", EMAIL)
    assert Pusher(settings, vault, FakeSender(fail_status=410)).send_sync({"title": "t"}) == 0
    assert vault.subscriptions() == []


def test_other_push_failure_keeps_subscription(settings, vault):
    vault.add_subscription("https://push.example/1", "k1", "a1", EMAIL)
    assert Pusher(settings, vault, FakeSender(fail_status=500)).send_sync({"title": "t"}) == 0
    assert len(vault.subscriptions()) == 1


def test_subscriptions_made_on_another_origin_are_dropped_on_start(settings, vault_path, clock, caplog):
    """A subscription belongs to the service worker of the origin it was made on,
    whose notifications open only that origin's pages. After a move (the garden's
    own host), they would lead to pages that are gone: the person turns
    notifications on again from the new origin."""
    caplog.set_level(logging.INFO)
    vault = Vault(vault_path, clock=clock, kdf=FAST_KDF)
    # Made before the vault recorded an origin: they predate the garden's host.
    vault.add_subscription("https://push.example/old-1", "k1", "a1", EMAIL)
    vault.add_subscription("https://push.example/old-2", "k2", "a2", EMAIL)
    Pusher(settings, vault, FakeSender()).adopt_origin()
    assert vault.subscriptions() == []
    assert "push subscriptions dropped: 2 made on another origin" in caplog.text
    assert "push.example" not in caplog.text and "old-1" not in caplog.text

    # Same origin on the next start: kept.
    caplog.clear()
    vault.add_subscription("https://push.example/new", "k3", "a3", EMAIL)
    vault = Vault(vault_path, clock=clock, kdf=FAST_KDF)
    Pusher(settings, vault, FakeSender()).adopt_origin()
    assert [s["endpoint"] for s in vault.subscriptions()] == ["https://push.example/new"]
    assert "dropped" not in caplog.text

    # Another origin: dropped again.
    moved = dataclasses.replace(settings, public_origin="https://hal-garden.example.org")
    Pusher(moved, vault, FakeSender()).adopt_origin()
    assert vault.subscriptions() == []
    assert "dropped: 1 made on another origin" in caplog.text
    Pusher(moved, vault, FakeSender()).adopt_origin()
    vault.add_subscription("https://push.example/newer", "k4", "a4", EMAIL)
    Pusher(moved, vault, FakeSender()).adopt_origin()
    assert len(vault.subscriptions()) == 1


def test_a_new_vault_records_its_origin_without_dropping_anything(settings, vault, caplog):
    caplog.set_level(logging.INFO)
    Pusher(settings, vault, FakeSender()).adopt_origin()
    assert "dropped" not in caplog.text
    vault.add_subscription("https://push.example/1", "k1", "a1", EMAIL)
    Pusher(settings, vault, FakeSender()).adopt_origin()
    assert len(vault.subscriptions()) == 1
    assert vault.push_origin() == ORIGIN


def test_a_vault_reset_keeps_the_push_origin_and_subscriptions(settings, vault):
    Pusher(settings, vault, FakeSender()).adopt_origin()
    vault.add_subscription("https://push.example/1", "k1", "a1", EMAIL)
    vault.reset()
    Pusher(settings, vault, FakeSender()).adopt_origin()
    assert len(vault.subscriptions()) == 1


def test_log_filter_redacts_values_while_in_use(caplog):
    f = SecretFilter()
    logger = logging.getLogger("test.redaction")
    logger.addFilter(f)
    try:
        with caplog.at_level(logging.INFO, logger="test.redaction"):
            with f.guarding([SECRET, "PT"]):
                logger.info("oops %s in PT", SECRET)
                try:
                    raise ValueError(SECRET)
                except ValueError:
                    logger.exception("failed")
            logger.info("after %s", "done")
    finally:
        logger.removeFilter(f)
    assert SECRET not in caplog.text
    assert "[REDACTED] in PT" in caplog.text and "after done" in caplog.text
