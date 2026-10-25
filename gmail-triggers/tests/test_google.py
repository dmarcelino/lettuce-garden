import json
import os

import httpx
import pytest

from gmail_triggers.google import Google, GoogleUnavailable, Status, credential_file_name

from .conftest import BODY, READONLY, FakeGoogle, snapshot, write_creds, write_policy

pytestmark = pytest.mark.anyio


def google_for(settings, clock, fake):
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return Google(settings.policy_dir, settings.creds_dir, client, clock=clock)


def test_credential_file_name_matches_upstream():
    assert credential_file_name("alfred@gmail.com") == "alfred@gmail.com.json"
    assert credential_file_name("al+fred@example.com") == "al%2Bfred@example.com.json"
    assert credential_file_name("a/b@example.com") == "a%2Fb@example.com.json"


@pytest.mark.parametrize(
    "setup, status",
    [
        ("none", Status.NOT_CONNECTED),
        ("disabled", Status.DISABLED),
        ("no_gmail_permission", Status.GMAIL_NOT_GRANTED),
        ("no_creds", Status.NOT_CONNECTED),
        ("no_gmail_scope", Status.GMAIL_NOT_GRANTED),
    ],
)
async def test_unusable_google_makes_no_calls(settings, clock, setup, status):
    if setup == "disabled":
        write_policy(settings.policy_dir, enabled=False)
    elif setup == "no_gmail_permission":
        write_policy(settings.policy_dir, permissions=("calendar:readonly",))
    elif setup in ("no_creds", "no_gmail_scope"):
        write_policy(settings.policy_dir)
    if setup == "no_gmail_scope":
        write_creds(settings.creds_dir, scopes=("https://www.googleapis.com/auth/calendar.readonly",))
    fake = FakeGoogle()
    google = google_for(settings, clock, fake)
    with pytest.raises(GoogleUnavailable) as e:
        await google.connect()
    assert e.value.status == status
    assert google.status()[0] == status
    assert fake.requests == []


async def test_refresh_happens_in_memory_and_never_writes_creds(settings, clock):
    write_policy(settings.policy_dir)
    write_creds(settings.creds_dir, expiry=clock() - 10)
    os.chmod(settings.creds_dir, 0o555)
    before = snapshot(settings.creds_dir)
    fake = FakeGoogle()
    google = google_for(settings, clock, fake)
    gmail = await google.connect()
    assert await gmail.profile() == str(fake.history_id)
    await (await google.connect()).labels()
    assert len(fake.token_calls) == 1
    assert snapshot(settings.creds_dir) == before
    assert sorted(os.listdir(settings.creds_dir)) == sorted(before)
    # Close to expiry: refreshed again, still in memory.
    clock.t += 3600
    await google.connect()
    assert len(fake.token_calls) == 2
    assert snapshot(settings.creds_dir) == before


async def test_a_fresh_file_token_is_used_without_refreshing(settings, clock):
    write_policy(settings.policy_dir)
    write_creds(settings.creds_dir, expiry=clock() + 1800)
    fake = FakeGoogle()
    gmail = await google_for(settings, clock, fake).connect()
    await gmail.profile()
    assert fake.token_calls == []
    assert fake.gmail_calls[0].headers["authorization"] == "Bearer file-access-token"


async def test_invalid_grant_means_reconnect_until_the_token_changes(settings, clock):
    write_policy(settings.policy_dir)
    write_creds(settings.creds_dir)
    fake = FakeGoogle()
    fake.token_error = "invalid_grant"
    google = google_for(settings, clock, fake)
    with pytest.raises(GoogleUnavailable) as e:
        await google.connect()
    assert e.value.status == Status.RECONNECT
    assert google.status()[0] == Status.RECONNECT
    write_creds(settings.creds_dir, refresh_token="refresh-token-2")
    assert google.status()[0] == Status.OK


async def test_other_refresh_errors_are_reported(settings, clock):
    write_policy(settings.policy_dir)
    write_creds(settings.creds_dir)
    fake = FakeGoogle()
    fake.token_error = "invalid_client"
    google = google_for(settings, clock, fake)
    with pytest.raises(GoogleUnavailable) as e:
        await google.connect()
    assert e.value.status == Status.ERROR and "invalid_client" in e.value.detail


async def test_the_refresh_token_only_goes_to_google(settings, clock):
    write_policy(settings.policy_dir)
    write_creds(settings.creds_dir, token_uri="https://token.example.org/token")
    fake = FakeGoogle()
    with pytest.raises(GoogleUnavailable) as e:
        await google_for(settings, clock, fake).connect()
    assert e.value.status == Status.ERROR
    assert fake.requests == []


async def test_metadata_only_and_no_snippet(settings, clock):
    write_policy(settings.policy_dir)
    write_creds(settings.creds_dir)
    fake = FakeGoogle()
    mid = fake.add()
    gmail = await google_for(settings, clock, fake).connect()
    raw = await gmail.message(mid)
    call = fake.gmail_calls[-1]
    assert call.url.params["format"] == "metadata"
    assert call.url.params["fields"] == "id,labelIds,payload/headers"
    assert sorted(call.url.params.get_list("metadataHeaders")) == ["Authentication-Results", "From", "Subject"]
    assert BODY not in repr(raw)
    assert {name for name, _ in raw["headers"]} == {"Authentication-Results", "From", "Subject"}
    assert await gmail.message("does-not-exist") is None


async def test_history_paginates_and_403_means_gmail_not_granted(settings, clock):
    write_policy(settings.policy_dir)
    write_creds(settings.creds_dir)
    fake = FakeGoogle()
    start = str(fake.history_id)
    ids = [fake.add() for _ in range(5)]
    fake.page_size = 2
    google = google_for(settings, clock, fake)
    page = await (await google.connect()).history(start)
    assert [m for m, _ in page.added] == ids and page.complete
    assert page.history_id == str(fake.history_id)
    fake.gmail_status = 403
    with pytest.raises(GoogleUnavailable) as e:
        await (await google.connect()).labels()
    assert e.value.status == Status.GMAIL_NOT_GRANTED


async def test_expired_access_token_is_refreshed_once(settings, clock):
    write_policy(settings.policy_dir)
    write_creds(settings.creds_dir, expiry=clock() + 1800)
    fake = FakeGoogle()
    google = google_for(settings, clock, fake)
    gmail = await google.connect()
    fake.file_token_valid = False  # revoked early: Gmail answers 401
    assert (await gmail.labels())[0].id == "INBOX"
    assert len(fake.token_calls) == 1


def test_readonly_scope_constant():
    assert READONLY.endswith("gmail.readonly")


def gmail_error(code: int, reason: str | None = None, status: str | None = None) -> bytes:
    error: dict = {"code": code, "message": "x"}
    if reason:
        error["errors"] = [{"domain": "usageLimits", "reason": reason, "message": "x"}]
    if status:
        error["status"] = status
    return json.dumps({"error": error}).encode()


@pytest.mark.parametrize(
    "code, body",
    [
        (403, gmail_error(403, reason="rateLimitExceeded")),
        (403, gmail_error(403, reason="userRateLimitExceeded")),
        (403, gmail_error(403, reason="dailyLimitExceeded")),
        (403, gmail_error(403, reason="quotaExceeded")),
        (403, gmail_error(403, status="RESOURCE_EXHAUSTED")),
        (429, gmail_error(429, status="RESOURCE_EXHAUSTED")),
        (429, gmail_error(429)),
        (429, b""),
        (429, b"<html>Too Many Requests</html>"),
    ],
)
async def test_rate_and_quota_limits_are_a_passing_error(settings, clock, code, body):
    write_policy(settings.policy_dir)
    write_creds(settings.creds_dir)
    fake = FakeGoogle()
    fake.gmail_status, fake.gmail_error_body = code, body
    google = google_for(settings, clock, fake)
    with pytest.raises(GoogleUnavailable) as e:
        await (await google.connect()).labels()
    assert e.value.status == Status.ERROR
    assert google.status()[0] == Status.ERROR


@pytest.mark.parametrize(
    "body",
    [
        gmail_error(403, reason="insufficientPermissions", status="PERMISSION_DENIED"),
        gmail_error(403, reason="forbidden"),
        gmail_error(403, status="PERMISSION_DENIED"),
        gmail_error(403),
        b"",
        b"<html>Forbidden</html>",
        b'{"error": "not an object"}',
    ],
)
async def test_a_permission_403_means_gmail_not_granted(settings, clock, body):
    write_policy(settings.policy_dir)
    write_creds(settings.creds_dir)
    fake = FakeGoogle()
    fake.gmail_status, fake.gmail_error_body = 403, body
    google = google_for(settings, clock, fake)
    with pytest.raises(GoogleUnavailable) as e:
        await (await google.connect()).labels()
    assert e.value.status == Status.GMAIL_NOT_GRANTED
