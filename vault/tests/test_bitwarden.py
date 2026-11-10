"""Reading Bitwarden's unencrypted JSON export; test_app covers the upload."""

import json

import pytest

from garden_vault.bitwarden import MAX_IMPORT_BYTES, read_export
from garden_vault.vault import VaultError

from .conftest import CARD, SECRET, STREET

# The shape Bitwarden writes (Settings > Export vault > .json), with fake values.
EXPORT = {
    "encrypted": False,
    "folders": [{"id": "f1", "name": "Personal"}],
    "items": [
        {
            "type": 1,
            "name": "Shop",
            "folderId": "f1",
            "favorite": True,
            "notes": None,
            "fields": [
                {"name": "PIN", "value": "4321", "type": 1},
                {"name": "Remember me", "value": "true", "type": 2},
                {"name": "Linked user", "value": None, "type": 3, "linkedId": 100},
            ],
            "login": {
                "username": "alfred",
                "password": SECRET,
                "totp": None,
                "uris": [{"match": None, "uri": "https://shop.example.com"}, {"uri": "https://example.org"}],
                "fido2Credentials": [{"credentialId": "passkey"}],
            },
            "passwordHistory": [{"password": "old"}],
        },
        {"type": 2, "name": "Gate", "notes": "Gate code 1234", "secureNote": {"type": 0}},
        {
            "type": 3,
            "name": "Visa",
            "card": {
                "cardholderName": "Alfred Test",
                "brand": "Visa",
                "number": CARD,
                "expMonth": "12",
                "expYear": "2030",
                "code": "123",
            },
        },
        {
            "type": 4,
            "name": "Home",
            "identity": {
                "title": "Mr",
                "firstName": "Alfred",
                "middleName": None,
                "lastName": "Test",
                "address1": STREET,
                "city": "Lisboa",
                "postalCode": "1000-001",
                "country": "PT",
                "phone": "+351 910 000 042",
                "passportNumber": "TEST_PASSPORT_1",
            },
        },
        {
            "type": 5,
            "name": "Server",
            "sshKey": {"privateKey": SECRET, "publicKey": "ssh-ed25519 AAAA", "keyFingerprint": "SHA256:abc"},
        },
        {"type": 2, "name": "Empty note", "notes": None, "secureNote": {"type": 0}},
        {"type": 9, "name": "From the future"},
    ],
}


def fields(item):
    return {f.key: (f.value, f.sensitive) for f in item.fields}


def test_reads_every_type_with_its_fields():
    items, skipped = read_export(json.dumps(EXPORT).encode())
    assert [(i.kind, i.name) for i in items] == [
        ("login", "Shop"),
        ("secure_note", "Gate"),
        ("card", "Visa"),
        ("identity", "Home"),
        ("ssh_key", "Server"),
    ]
    login, note, card, identity, ssh = items
    assert fields(login) == {
        "username": ("alfred", False),
        "password": (SECRET, True),
        "uris": ("https://shop.example.com\nhttps://example.org", False),
        "pin": ("4321", True),
        "remember_me": ("true", True),
    }
    # Masked as in Bitwarden: its Hidden custom fields (type 1), not Text or Boolean ones.
    assert {f.key: f.masked for f in login.fields} == {
        "username": False,
        "password": True,
        "uris": False,
        "pin": True,
        "remember_me": False,
    }
    assert fields(note) == {"notes": ("Gate code 1234", True)}
    assert fields(card)["number"] == (CARD, True) and fields(card)["exp_year"] == ("2030", True)
    assert fields(card)["cardholder_name"] == ("Alfred Test", False)
    assert fields(identity)["postal_code"] == ("1000-001", True)
    assert fields(identity)["passport_number"] == ("TEST_PASSPORT_1", True)
    assert fields(identity)["country"] == ("PT", False) and "middle_name" not in fields(identity)
    assert fields(ssh) == {
        "private_key": (SECRET, True),
        "public_key": ("ssh-ed25519 AAAA", False),
        "key_fingerprint": ("SHA256:abc", False),
    }
    assert skipped == ["Empty note: fill in at least one field", "From the future: not a type the vault keeps"]
    assert "passkey" not in repr(items) and "old" not in [f.value for i in items for f in i.fields]


@pytest.mark.parametrize(
    "raw,error",
    [
        (b"not json", "not a Bitwarden"),
        (b"[]", "not a Bitwarden"),
        (b'{"encrypted": true, "passwordProtected": true, "salt": "x", "data": "x"}', "encrypted"),
        (b'{"encrypted": true, "encKeyValidation_DO_NOT_EDIT": "x", "items": []}', "encrypted"),
        (b"\xff\xfe", "not a Bitwarden"),
        (b" " * (MAX_IMPORT_BYTES + 1), "larger"),
    ],
)
def test_refuses_what_is_not_a_plain_export(raw, error):
    with pytest.raises(VaultError, match=error):
        read_export(raw)


def test_long_names_are_cut_and_missing_ones_named():
    export = {"items": [{"type": 2, "name": "x" * 200, "notes": "n"}, {"type": 2, "notes": "n"}]}
    items, _ = read_export(json.dumps(export).encode())
    assert [i.name for i in items] == ["x" * 80, "Untitled"]


def test_a_field_the_vault_refuses_is_left_out_not_its_item():
    item = {
        "type": 4,
        "name": "Home",
        "notes": "x" * 5000,
        "identity": {"city": "Lisboa", "phone": "y" * 5000},
        "fields": [
            {"name": "Phone", "value": "clashes with the built-in phone", "type": 0},
            {"name": "PIN", "value": "1", "type": 0},
            {"name": "pin", "value": "2", "type": 1},
            {"name": "日本", "value": "no ASCII in the name", "type": 0},
            {"name": "Kept", "value": "k", "type": 0},
        ]
        + [{"name": f"extra {i}", "value": "v", "type": 0} for i in range(20)],
    }
    items, left_out = read_export(json.dumps({"items": [item]}).encode())
    assert [i.name for i in items] == ["Home"]
    kept = fields(items[0])
    assert kept["city"] == ("Lisboa", True) and kept["pin"] == ("1", True) and kept["kept"] == ("k", True)
    assert "phone" not in kept and "notes" not in kept
    assert sum(1 for f in items[0].fields if f.key not in ("city",)) == 20  # custom fields stop at 20
    assert left_out[:2] == [
        "Home: Phone left out (Phone is longer than 4000 characters)",
        "Home: Notes left out (Notes is longer than 4000 characters)",
    ]
    assert left_out[2] == (
        "Home: custom field Phone left out (two fields would be called phone: rename the custom field Phone)"
    )
    assert left_out[3].startswith("Home: custom field pin left out (two fields would be called pin")
    assert left_out[4].startswith("Home: custom field 日本 left out (a custom field needs a name")
    assert left_out[-2].startswith("Home: custom field extra 18 left out (at most 20")
    assert left_out[-1].startswith("Home: custom field extra 19 left out (at most 20")
    assert "Lisboa" not in " ".join(left_out) and "clashes with" not in " ".join(left_out)


@pytest.mark.parametrize(
    "item",
    [
        {"type": [1], "name": "List type"},
        {"type": True, "name": "Bool type"},
        {"type": 2, "name": "Odd fields", "notes": "n", "fields": 5},
        {"type": 1, "name": "Odd login", "login": ["x"], "notes": "n"},
        "not an object",
    ],
)
def test_a_malformed_item_is_left_out_not_the_import(item):
    items, left_out = read_export(json.dumps({"items": [item, {"type": 2, "name": "Fine", "notes": "n"}]}).encode())
    assert "Fine" in [i.name for i in items]
    assert len(items) + sum(1 for line in left_out if "not a type" in line) == 2
