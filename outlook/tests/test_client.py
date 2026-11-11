import httpx
import pytest

from garden_outlook.outlook_client import OutlookError
from garden_outlook.tokens import ConnectError, LostAccess

from .conftest import EMAIL, connect

pytestmark = pytest.mark.anyio

PAYLOAD = {
    "refreshToken": "refresh-token-def-123456",
    "account": EMAIL,
    "clientId": "office-foci-client-id",
    "tokenEndpoint": "https://login.microsoftonline.com/organizations/oauth2/v2.0/token",
}


async def test_call_without_session_raises(service):
    with pytest.raises(LostAccess):
        await service.client.list_folders()


async def test_connect_redeems_refresh_token_for_graph(service, fake_outlook):
    await service.connect(PAYLOAD)
    # The refresh token was redeemed (token endpoint hit) and a Graph token stored.
    assert fake_outlook.refreshes == 1
    assert service.store.session is not None
    assert service.store.session.access_token == "graph-access-token-xyz"
    assert service.store.account == EMAIL


async def test_connect_rejects_a_payload_with_no_refresh_token(service):
    with pytest.raises(ConnectError):
        await service.connect({"account": EMAIL, "clientId": "x", "tokenEndpoint": "https://login.microsoftonline.com/o/token"})


async def test_connect_surfaces_a_tenant_refusal(settings):
    from garden_outlook.service import Service

    def refuse(request):
        if "login.microsoftonline.com" in request.url.host:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "AADSTS53003 blocked by Conditional Access"})
        return httpx.Response(200, json={})

    svc = Service(settings, http=httpx.AsyncClient(transport=httpx.MockTransport(refuse)))
    with pytest.raises(LostAccess):
        await svc.connect(PAYLOAD)
    assert svc.store.session is None  # nothing stored on refusal


async def test_refresh_when_near_expiry(service, fake_outlook):
    connect(service, expires_in=10)  # inside the refresh window
    await service.client.list_folders()
    assert fake_outlook.refreshes == 1


async def test_no_refresh_when_fresh(service, fake_outlook):
    connect(service, expires_in=100000)
    await service.client.list_folders()
    assert fake_outlook.refreshes == 0


async def test_graph_error_text_is_short_and_safe(service):
    connect(service)

    def handler(request):
        return httpx.Response(403, json={"error": {"code": "ErrorAccessDenied", "message": "No can do"}})

    service.client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(OutlookError) as exc:
        await service.client.list_folders()
    assert exc.value.status == 403
    assert "No can do" in str(exc.value)
