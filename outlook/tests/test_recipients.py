import pytest

from garden_outlook.recipients import (
    AllowedRecipients,
    InvalidRecipientEntry,
    normalize_entries,
    recipient_entry,
    validate_entries,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("someone@example.com", "someone@example.com"),
        ("  Someone@Example.COM ", "someone@example.com"),
        ("@example.com", "@example.com"),
        ("@sub.example.co.uk", "@sub.example.co.uk"),
    ],
)
def test_valid_entries(value, expected):
    assert recipient_entry(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-an-address",
        "two@@example.com",
        "a@example.com, b@example.com",
        "name <a@example.com>",
        'a"b@example.com',
        "@example",
        "@.com",
        "a@example.com b@example.com",
    ],
)
def test_invalid_entries(value):
    assert recipient_entry(value) is None


def test_normalize_drops_blanks_and_dedupes():
    assert normalize_entries(["a@example.com", "", "A@EXAMPLE.COM", "bad", "@x.org"]) == [
        "a@example.com",
        "@x.org",
    ]


def test_validate_refuses_malformed_by_name():
    with pytest.raises(InvalidRecipientEntry) as exc:
        validate_entries(["ok@example.com", "nope", "also bad"])
    assert "nope" in str(exc.value)
    assert "also bad" in str(exc.value)


def test_validate_keeps_blanks_out():
    assert validate_entries(["  ", "ok@example.com"]) == ["ok@example.com"]


def test_empty_list_is_unrestricted():
    guard = AllowedRecipients([], own="me@example.com")
    assert guard.unrestricted
    assert "anyone@anywhere.test" in guard
    assert guard.outside(["anyone@anywhere.test"]) == []


def test_exact_address_match():
    guard = AllowedRecipients(["ok@example.com"], own="me@example.com")
    assert "ok@example.com" in guard
    assert "OK@Example.com" in guard
    assert "other@example.com" not in guard


def test_domain_match():
    guard = AllowedRecipients(["@example.com"], own="me@elsewhere.test")
    assert "anyone@example.com" in guard
    assert "anyone@sub.example.com" not in guard  # only the exact domain
    assert "anyone@example.org" not in guard


def test_own_account_always_allowed():
    guard = AllowedRecipients(["@example.com"], own="me@elsewhere.test")
    assert "me@elsewhere.test" in guard
    assert "ME@Elsewhere.test" in guard


def test_outside_returns_disallowed_in_order_without_repeats():
    guard = AllowedRecipients(["@example.com", "ok@other.test"], own="me@example.com")
    out = guard.outside(
        [
            "a@example.com",  # allowed (domain)
            "bad@nope.test",  # disallowed
            "ok@other.test",  # allowed (exact)
            "BAD@nope.test",  # same as before, normalized
            "second@nope.test",  # disallowed
        ]
    )
    assert out == ["bad@nope.test", "second@nope.test"]


def test_no_own_account():
    guard = AllowedRecipients(["ok@example.com"])
    assert guard.own is None
    assert "ok@example.com" in guard
    assert "whoever@example.com" not in guard
