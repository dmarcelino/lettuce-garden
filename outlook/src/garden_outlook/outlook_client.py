"""The Microsoft Graph client and FOCI token redemption.

The browser hands over a refresh token issued to one of Microsoft's own
first-party apps (the Outlook/Office web client). Those apps form a "family"
(FOCI), so that refresh token can be redeemed at the token endpoint for a
Microsoft Graph access token under the same first-party client id — no app
registration and no new admin consent, because the delegated permissions are
already granted to the family for this user. A tenant may still refuse the
redemption (Conditional Access, token protection, restricted first-party apps);
that surfaces here as a clear LostAccess message, which the connect flow shows.

Policy (levels, allowed recipients, attendees) is NOT enforced here — that is the
tool layer (mcp_tools.py); this layer only talks to Graph with the token.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import quote

import httpx

from .config import GRAPH_API_BASE, GRAPH_SCOPE, HTTP_TIMEOUT_SECONDS, REFRESH_WINDOW_SECONDS, Settings
from .tokens import LostAccess, Session, SessionStore

log = logging.getLogger("garden_outlook.client")


class OutlookError(Exception):
    """A non-success answer from Graph that is not an auth failure."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class OutlookClient:
    def __init__(self, settings: Settings, store: SessionStore, http: httpx.AsyncClient | None = None):
        self._settings = settings
        self._store = store
        # trust_env=False: the sidecar is never given the pipelock proxy variables,
        # and the token must never pass through pipelock (AGENTS.md). Outbound calls
        # go straight to Microsoft.
        self._http = http or httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, trust_env=False)

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- FOCI token redemption -------------------------------------------

    async def _redeem(self, refresh_token: str, client_id: str, token_endpoint: str) -> tuple[str, float, str | None]:
        """Exchange a refresh token for a Graph access token. Returns
        (access_token, expires_at, new_refresh_token). Raises LostAccess with a
        short reason on refusal."""
        data = {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
            "scope": GRAPH_SCOPE,
        }
        try:
            response = await self._http.post(token_endpoint, data=data)
        except httpx.HTTPError as error:
            raise LostAccess(f"could not reach the Microsoft token endpoint: {error}") from error
        if response.status_code != 200:
            raise LostAccess(_token_error(response))
        body = response.json()
        access = body.get("access_token")
        if not isinstance(access, str) or not access:
            raise LostAccess("the token endpoint returned no Graph access token; reconnect")
        expires_in = int(body.get("expires_in") or 3600)
        expires_at = time.time() + max(0, expires_in - 60)
        new_refresh = body.get("refresh_token") if isinstance(body.get("refresh_token"), str) else None
        return access, expires_at, new_refresh

    async def redeem_for_session(self, session: Session) -> Session:
        """Validate a just-connected session by redeeming its refresh token for a
        Graph token, returning the session to store (with the Graph access token).
        Used by the connect flow so a tenant that refuses fails loudly at once."""
        access, expires_at, new_refresh = await self._redeem(
            session.refresh_token, session.client_id, session.token_endpoint
        )
        return Session(
            access_token=access,
            refresh_token=new_refresh or session.refresh_token,
            expires_at=expires_at,
            account=session.account,
            client_id=session.client_id,
            token_endpoint=session.token_endpoint,
            scopes=(GRAPH_SCOPE,),
        )

    async def _refresh(self) -> str:
        session = self._store.session
        if session is None:
            raise LostAccess("not connected")
        if not session.refresh_token:
            raise LostAccess("the saved session cannot be refreshed; reconnect on the Outlook page")
        access, expires_at, new_refresh = await self._redeem(
            session.refresh_token, session.client_id, session.token_endpoint
        )
        self._store.apply_refresh(access, expires_at, new_refresh)
        log.info("refreshed the Graph access token")
        return access

    async def _access_token(self) -> str:
        session = self._store.session
        if session is None:
            raise LostAccess("not connected")
        if not session.access_token or self._store.needs_refresh(REFRESH_WINDOW_SECONDS):
            return await self._refresh()
        return session.access_token

    # -- transport --------------------------------------------------------

    async def _send(self, method: str, url: str, *, params: Any = None, json: Any = None) -> Any:
        token = await self._access_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        response = await self._http.request(method, url, params=params, json=json, headers=headers)
        if response.status_code == 401:
            token = await self._refresh()
            headers["Authorization"] = f"Bearer {token}"
            response = await self._http.request(method, url, params=params, json=json, headers=headers)
            if response.status_code == 401:
                raise LostAccess("Graph rejected the saved session; reconnect on the Outlook page")
        if response.status_code >= 400:
            raise OutlookError(response.status_code, _graph_error(response))
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def _request(self, method: str, path: str, *, params: Any = None, json: Any = None) -> Any:
        return await self._send(method, f"{GRAPH_API_BASE}{path}", params=params, json=json)

    async def _get_pages(self, path: str, params: dict[str, Any], cap: int) -> list[dict[str, Any]]:
        """Follow @odata.nextLink, gathering up to `cap` items across pages."""
        data = await self._request("GET", path, params=params)
        collected = list(data.get("value", [])) if isinstance(data, dict) else []
        next_url = data.get("@odata.nextLink") if isinstance(data, dict) else None
        while next_url and len(collected) < cap:
            page = await self._send("GET", next_url)
            if not isinstance(page, dict):
                break
            collected.extend(page.get("value", []))
            next_url = page.get("@odata.nextLink")
        return collected[:cap]

    @staticmethod
    def _root(mailbox: str | None) -> str:
        """`/me` for the connected account, `/users/<id>` for a shared mailbox."""
        return f"/users/{quote(mailbox)}" if mailbox else "/me"

    # -- mail -------------------------------------------------------------

    async def list_folders(self, mailbox: str | None = None) -> Any:
        return await self._request("GET", f"{self._root(mailbox)}/mailFolders", params={"$top": 100})

    async def list_messages(self, folder: str | None, top: int, mailbox: str | None = None) -> Any:
        base = self._root(mailbox)
        path = f"{base}/mailFolders/{quote(folder)}/messages" if folder else f"{base}/messages"
        return await self._request("GET", path, params={"$top": top, "$orderby": "receivedDateTime desc"})

    async def search_messages(self, query: str, top: int, mailbox: str | None = None) -> Any:
        # The query is wrapped in an OData $search phrase: "<query>". A double quote
        # or backslash in it would break out of (or malform) that phrase, so drop
        # them. Everything else is treated as search words, which is the intent.
        safe = query.replace("\\", " ").replace('"', " ").strip()
        params = {"$search": f'"{safe}"', "$top": top}
        return await self._request("GET", f"{self._root(mailbox)}/messages", params=params)

    async def get_message(self, message_id: str, mailbox: str | None = None) -> Any:
        return await self._request("GET", f"{self._root(mailbox)}/messages/{quote(message_id)}")

    async def list_attachments(self, message_id: str, mailbox: str | None = None) -> Any:
        return await self._request("GET", f"{self._root(mailbox)}/messages/{quote(message_id)}/attachments")

    async def get_attachment(self, message_id: str, attachment_id: str, mailbox: str | None = None) -> Any:
        path = f"{self._root(mailbox)}/messages/{quote(message_id)}/attachments/{quote(attachment_id)}"
        return await self._request("GET", path)

    async def patch_message(self, message_id: str, body: dict[str, Any]) -> Any:
        return await self._request("PATCH", f"/me/messages/{quote(message_id)}", json=body)

    async def move_message(self, message_id: str, destination_id: str) -> Any:
        return await self._request(
            "POST", f"/me/messages/{quote(message_id)}/move", json={"destinationId": destination_id}
        )

    async def create_draft(self, message: dict[str, Any]) -> Any:
        return await self._request("POST", "/me/messages", json=message)

    async def create_reply_draft(self, message_id: str, reply_all: bool, comment: str) -> Any:
        action = "createReplyAll" if reply_all else "createReply"
        return await self._request("POST", f"/me/messages/{quote(message_id)}/{action}", json={"comment": comment})

    async def create_forward_draft(self, message_id: str, to_recipients: list[dict[str, Any]], comment: str) -> Any:
        body = {"comment": comment, "toRecipients": to_recipients}
        return await self._request("POST", f"/me/messages/{quote(message_id)}/createForward", json=body)

    async def send_draft(self, message_id: str) -> Any:
        return await self._request("POST", f"/me/messages/{quote(message_id)}/send")

    # -- calendar ---------------------------------------------------------

    async def calendar_view(self, start: str, end: str, top: int) -> Any:
        params = {"startDateTime": start, "endDateTime": end, "$top": top, "$orderby": "start/dateTime"}
        return await self._request("GET", "/me/calendarView", params=params)

    async def calendar_view_all(self, start: str, end: str, cap: int) -> list[dict[str, Any]]:
        """Every event in the window, following paging up to `cap` (for free/busy)."""
        params = {"startDateTime": start, "endDateTime": end, "$top": 100, "$orderby": "start/dateTime"}
        return await self._get_pages("/me/calendarView", params, cap)

    async def get_event(self, event_id: str) -> Any:
        return await self._request("GET", f"/me/events/{quote(event_id)}")

    async def create_event(self, event: dict[str, Any]) -> Any:
        return await self._request("POST", "/me/events", json=event)

    async def update_event(self, event_id: str, patch: dict[str, Any]) -> Any:
        return await self._request("PATCH", f"/me/events/{quote(event_id)}", json=patch)

    async def delete_event(self, event_id: str) -> Any:
        return await self._request("DELETE", f"/me/events/{quote(event_id)}")

    async def respond_event(self, event_id: str, response: str, comment: str) -> Any:
        # response in {accept, decline, tentativelyAccept}
        body = {"comment": comment, "sendResponse": True}
        return await self._request("POST", f"/me/events/{quote(event_id)}/{response}", json=body)

    # -- tasks (Microsoft To Do) -----------------------------------------

    async def list_task_lists(self) -> Any:
        return await self._request("GET", "/me/todo/lists", params={"$top": 50})

    async def default_task_list(self) -> str:
        data = await self.list_task_lists()
        lists = data.get("value", []) if isinstance(data, dict) else []
        for item in lists:
            if isinstance(item, dict) and item.get("wellKnownListName") == "defaultList":
                return str(item.get("id"))
        if lists and isinstance(lists[0], dict):
            return str(lists[0].get("id"))
        raise OutlookError(404, "no To Do list found")

    async def list_tasks(self, list_id: str, top: int) -> Any:
        return await self._request("GET", f"/me/todo/lists/{quote(list_id)}/tasks", params={"$top": top})

    async def get_task(self, list_id: str, task_id: str) -> Any:
        return await self._request("GET", f"/me/todo/lists/{quote(list_id)}/tasks/{quote(task_id)}")

    async def create_task(self, list_id: str, task: dict[str, Any]) -> Any:
        return await self._request("POST", f"/me/todo/lists/{quote(list_id)}/tasks", json=task)

    async def update_task(self, list_id: str, task_id: str, patch: dict[str, Any]) -> Any:
        return await self._request("PATCH", f"/me/todo/lists/{quote(list_id)}/tasks/{quote(task_id)}", json=patch)

    async def delete_task(self, list_id: str, task_id: str) -> Any:
        return await self._request("DELETE", f"/me/todo/lists/{quote(list_id)}/tasks/{quote(task_id)}")


def _token_error(response: httpx.Response) -> str:
    """A short, de-identified reason the token endpoint refused, for the person."""
    try:
        body = response.json()
    except (ValueError, httpx.HTTPError):
        return "the Microsoft token endpoint refused the saved session; reconnect"
    code = body.get("error") if isinstance(body, dict) else None
    description = body.get("error_description") if isinstance(body, dict) else None
    first = str(description).splitlines()[0] if isinstance(description, str) and description else ""
    if code == "invalid_grant":
        return "the saved sign-in is no longer valid; reconnect on the Outlook page"
    if "AADSTS" in first:
        # e.g. Conditional Access / token protection / consent — tenant policy.
        return f"your organisation refused this ({first[:200]})"
    return f"the Microsoft token endpoint refused the saved session ({code or response.status_code}); reconnect"


def _graph_error(response: httpx.Response) -> str:
    """A short, safe message from a Graph error body — never the token."""
    try:
        data = response.json()
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message[:300]
    except (ValueError, httpx.HTTPError):
        pass
    return f"Graph returned HTTP {response.status_code}"
