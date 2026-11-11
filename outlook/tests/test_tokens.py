import logging

import pytest

from garden_outlook import logs
from garden_outlook.tokens import ConnectError, SessionStore, session_from_payload

from .conftest import EMAIL

GOOD = {
    "accessToken": "access-token-abc-123456",
    "refreshToken": "refresh-token-def-123456",
    "expiresOn": 2000000000,
    "account": EMAIL,
    "clientId": "office-foci-client-id",
    "tokenEndpoint": "https://login.microsoftonline.com/organizations/oauth2/v2.0/token",
    "scopes": ["https://graph.microsoft.com/.default"],
}


def test_session_from_payload_valid():
    session = session_from_payload(GOOD)
    assert session.account == EMAIL
    assert session.refresh_token == "refresh-token-def-123456"
    assert session.token_endpoint.startswith("https://")


@pytest.mark.parametrize(
    "mutation",
    [
        {"refreshToken": ""},
        {"refreshToken": None},
        {"refreshToken": "short"},
        {"account": "not-an-address"},
        {"clientId": ""},
        {"tokenEndpoint": "http://insecure.example/token"},
        {"tokenEndpoint": "ftp://x"},
    ],
)
def test_session_from_payload_rejects(mutation):
    payload = {**GOOD, **mutation}
    with pytest.raises(ConnectError):
        session_from_payload(payload)


def test_session_from_payload_allows_missing_access_token():
    # The browser's own access token is not used; the refresh token is what matters.
    payload = {**GOOD}
    del payload["accessToken"]
    assert session_from_payload(payload).access_token == ""


def test_store_round_trip(settings):
    store = SessionStore(settings)
    assert store.session is None
    store.set(session_from_payload(GOOD))
    assert store.account == EMAIL
    # A fresh store from the same settings reads the sealed session back.
    reloaded = SessionStore(settings)
    assert reloaded.session is not None
    assert reloaded.session.refresh_token == "refresh-token-def-123456"
    reloaded.disconnect()
    assert SessionStore(settings).session is None


def test_wrong_secret_cannot_read(settings):
    SessionStore(settings).set(session_from_payload(GOOD))
    other = settings.__class__(
        team_domain=settings.team_domain,
        audience=settings.audience,
        issuer=settings.issuer,
        allowed_users=settings.allowed_users,
        public_origin=settings.public_origin,
        secret="a-totally-different-secret-value-32xx",
        data_dir=settings.data_dir,
    )
    # A different secret cannot read it: it loads as disconnected, never as someone
    # else's token.
    assert SessionStore(other).session is None


def test_needs_refresh(settings):
    store = SessionStore(settings)
    store.set(session_from_payload({**GOOD, "expiresOn": 10_000_000_000}))
    assert not store.needs_refresh(300, now=0)
    assert store.needs_refresh(300, now=10_000_000_000)


def test_logs_filter_scrubs_the_token(settings):
    logs.setup()
    store = SessionStore(settings)
    store.set(session_from_payload(GOOD))
    logger = logging.getLogger("garden_outlook.test")
    record = logger.makeRecord("x", logging.INFO, __file__, 1, "token is refresh-token-def-123456", None, None)
    assert logs.SECRET_FILTER.filter(record) is True
    assert "refresh-token-def-123456" not in record.getMessage()
    assert "[REDACTED]" in record.getMessage()
