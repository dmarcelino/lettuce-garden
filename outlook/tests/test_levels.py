import pytest

from garden_outlook.levels import (
    InvalidPolicy,
    Policy,
    apply_update,
    enabled_services,
    level_rank,
    parse_policy,
    policy_to_json,
    serving,
)


def test_level_rank_order_and_off():
    assert level_rank("mail", "readonly") == 0
    assert level_rank("mail", "send") == 3
    assert level_rank("mail", "full") == 4
    assert level_rank("mail", None) == -1
    assert level_rank("mail", "nonsense") == -1
    assert level_rank("calendar", "self-events") == 1


def test_permits_is_cumulative():
    policy = Policy(levels={"mail": "send"})
    for level in ("readonly", "organize", "drafts", "send"):
        assert policy.permits("mail", level)
    assert not policy.permits("mail", "full")


def test_permits_off_service():
    policy = Policy(levels={"mail": None})
    assert not policy.permits("mail", "readonly")


def test_calendar_self_events_blocks_attendees():
    assert not Policy(levels={"calendar": "self-events"}).calendar_allows_attendees
    assert not Policy(levels={"calendar": "readonly"}).calendar_allows_attendees
    assert Policy(levels={"calendar": "full"}).calendar_allows_attendees


def test_self_events_permits_write_but_not_full():
    policy = Policy(levels={"calendar": "self-events"})
    assert policy.permits("calendar", "readonly")
    assert policy.permits("calendar", "self-events")
    assert not policy.permits("calendar", "full")


def test_shared_mailbox_membership():
    policy = Policy(shared_mailboxes=("team@example.com",))
    assert policy.allows_shared("team@example.com")
    assert policy.allows_shared("TEAM@example.com")
    assert not policy.allows_shared("other@example.com")


def test_parse_policy_is_lenient():
    policy = parse_policy(
        {
            "levels": {"mail": "send", "calendar": "bogus", "tasks": "manage", "unknown": "full"},
            "allowedRecipients": ["ok@example.com", "bad entry", "@example.org"],
            "sharedMailboxes": ["team@example.com", "not-an-address"],
        }
    )
    assert policy.level("mail") == "send"
    assert policy.level("calendar") is None  # bogus -> off
    assert policy.level("tasks") == "manage"
    assert policy.allowed_recipients == ("ok@example.com", "@example.org")
    assert policy.shared_mailboxes == ("team@example.com",)


def test_parse_policy_garbage_is_empty():
    policy = parse_policy("not a dict")
    assert not serving(policy)
    assert list(enabled_services(policy)) == []


def test_round_trip_json():
    policy = parse_policy(
        {
            "levels": {"mail": "drafts", "calendar": "full", "tasks": None},
            "allowedRecipients": ["@example.com"],
            "sharedMailboxes": ["team@example.com"],
        }
    )
    assert parse_policy(policy_to_json(policy)) == policy


def test_apply_update_changes_only_given_keys():
    current = Policy(levels={"mail": "send", "calendar": "full", "tasks": "readonly"})
    nxt = apply_update(current, {"levels": {"mail": "readonly"}})
    assert nxt.level("mail") == "readonly"
    assert nxt.level("calendar") == "full"  # untouched
    assert nxt.level("tasks") == "readonly"


def test_apply_update_clears_with_null_or_blank():
    current = Policy(levels={"mail": "send"})
    assert apply_update(current, {"levels": {"mail": None}}).level("mail") is None
    assert apply_update(current, {"levels": {"mail": ""}}).level("mail") is None


def test_apply_update_rejects_unknown_service():
    with pytest.raises(InvalidPolicy):
        apply_update(Policy(), {"levels": {"webmail": "send"}})


def test_apply_update_rejects_unknown_level():
    with pytest.raises(InvalidPolicy):
        apply_update(Policy(), {"levels": {"mail": "superuser"}})


def test_apply_update_rejects_malformed_recipient():
    with pytest.raises(InvalidPolicy):
        apply_update(Policy(), {"allowedRecipients": ["ok@example.com", "nope"]})


def test_apply_update_rejects_non_list_recipients():
    with pytest.raises(InvalidPolicy):
        apply_update(Policy(), {"allowedRecipients": "ok@example.com"})


def test_apply_update_rejects_bad_shared_mailbox():
    with pytest.raises(InvalidPolicy):
        apply_update(Policy(), {"sharedMailboxes": ["team@example.com", "no-at-sign"]})


def test_apply_update_rejects_non_object():
    with pytest.raises(InvalidPolicy):
        apply_update(Policy(), ["not", "an", "object"])
