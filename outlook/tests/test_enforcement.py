"""Policy enforcement, driven through the real ASGI app over /mcp (as the bridge
would), so the test exercises the actual tool handlers, not a shortcut."""

import pytest
from starlette.testclient import TestClient

from garden_outlook.app import create_app

from .conftest import ORIGIN, connect

MCP = "http://garden-outlook:8000/mcp"


@pytest.fixture
def client(service, authenticator, signing_key):
    app = create_app(service, authenticator, ORIGIN)
    with TestClient(app, base_url=ORIGIN) as c:
        yield c, service


def call(client, name, args):
    c, _ = client
    r = c.post(
        MCP,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}},
        headers={
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2025-06-18",
            "cf-access-jwt-assertion": "",
        },
    )
    assert r.status_code == 200, r.text
    return r.json()["result"]["structuredContent"]


def test_not_connected_is_reported(client):
    _, service = client
    service.update_policy({"levels": {"mail": "readonly"}})
    out = call(client, "search_messages", {"query": "hi"})
    assert "Not connected" in out.get("error", "")


def test_readonly_blocks_send(client):
    c, service = client
    connect(service)
    service.update_policy({"levels": {"mail": "readonly"}})
    assert "messages" in call(client, "search_messages", {"query": "hi"})
    out = call(client, "send_draft", {"message_id": "m1"})
    assert "mail:send" in out.get("error", "")


def test_send_checks_allowed_recipients(client, fake_outlook):
    c, service = client
    connect(service)
    service.update_policy({"levels": {"mail": "send"}, "allowedRecipients": ["@ok.test"]})
    # The stored draft is addressed off-list -> refused.
    fake_outlook.draft_recipients = ["stranger@evil.test"]
    out = call(client, "send_draft", {"message_id": "m1"})
    assert "allowed recipients" in out.get("error", "").lower()
    # On-list -> sent.
    fake_outlook.draft_recipients = ["friend@ok.test"]
    assert call(client, "send_draft", {"message_id": "m1"}).get("sent") is True


def test_self_events_forbids_attendees(client):
    c, service = client
    connect(service)
    service.update_policy({"levels": {"calendar": "self-events"}})
    blocked = call(client, "create_event", {"subject": "x", "start": "2026-01-20T09:00:00", "end": "2026-01-20T09:30:00", "attendees": ["a@ok.test"]})
    assert "calendar:full" in blocked.get("error", "")
    # No attendees -> allowed at self-events.
    ok = call(client, "create_event", {"subject": "x", "start": "2026-01-20T09:00:00", "end": "2026-01-20T09:30:00"})
    assert ok.get("created") is True


def test_self_events_cannot_update_a_meeting_that_has_attendees(client, fake_outlook):
    c, service = client
    connect(service)
    service.update_policy({"levels": {"calendar": "self-events"}})
    # An event with no attendees: a self-events reschedule is allowed.
    fake_outlook.event_attendees = []
    assert call(client, "update_event", {"event_id": "e1", "start": "2026-01-20T10:00:00"}).get("ok") is True
    # The same event once it has attendees: updating it would notify them -> needs full.
    fake_outlook.event_attendees = ["guest@ok.test"]
    out = call(client, "update_event", {"event_id": "e1", "start": "2026-01-20T10:00:00"})
    assert "calendar:full" in out.get("error", "")


def test_find_free_time_returns_gaps(client):
    c, service = client
    connect(service)
    service.update_policy({"levels": {"calendar": "readonly"}})
    out = call(client, "find_free_time", {"start": "2026-01-20T00:00:00", "end": "2026-01-20T23:59:00", "minutes": 30})
    assert "free" in out and isinstance(out["free"], list) and out["free"]
    assert "busy" in out


def test_search_sanitises_quotes(client, fake_outlook):
    c, service = client
    connect(service)
    service.update_policy({"levels": {"mail": "readonly"}})
    call(client, "search_messages", {"query": 'a" OR subject:x'})
    search = [r for r in fake_outlook.requests if r.url.path.endswith("/messages") and "$search" in r.url.params]
    assert search, "no search request recorded"
    value = search[-1].url.params["$search"]
    # Wrapped in one phrase, with the injected quote removed (not broken out of).
    assert value.startswith('"') and value.endswith('"')
    assert value.count('"') == 2


def test_full_calendar_checks_invitees(client):
    c, service = client
    connect(service)
    service.update_policy({"levels": {"calendar": "full"}, "allowedRecipients": ["@ok.test"]})
    blocked = call(client, "create_event", {"subject": "x", "start": "2026-01-20T09:00:00", "end": "2026-01-20T09:30:00", "attendees": ["a@evil.test"]})
    assert "allowed recipients" in blocked.get("error", "").lower()
    ok = call(client, "create_event", {"subject": "x", "start": "2026-01-20T09:00:00", "end": "2026-01-20T09:30:00", "attendees": ["a@ok.test"]})
    assert ok.get("created") is True


def test_shared_mailbox_must_be_listed(client):
    c, service = client
    connect(service)
    service.update_policy({"levels": {"mail": "readonly"}, "sharedMailboxes": ["team@ok.test"]})
    assert "messages" in call(client, "search_messages", {"query": "hi", "mailbox": "team@ok.test"})
    out = call(client, "search_messages", {"query": "hi", "mailbox": "other@x.test"})
    assert "shared mailbox" in out.get("error", "").lower()


def test_tasks_manage_required_to_create(client):
    c, service = client
    connect(service)
    service.update_policy({"levels": {"tasks": "readonly"}})
    assert "tasks" in call(client, "list_tasks", {})
    out = call(client, "create_task", {"title": "do it"})
    assert "tasks:manage" in out.get("error", "")


def test_mail_body_not_logged(client, caplog):
    import logging

    c, service = client
    connect(service)
    service.update_policy({"levels": {"mail": "readonly"}})
    with caplog.at_level(logging.INFO):
        out = call(client, "read_message", {"message_id": "m1"})
    # The agent receives the body, but the server never logs it.
    assert "CANARY_OUTLOOK_5521_DO_NOT_LEAK" in out.get("body", "")
    assert "CANARY_OUTLOOK_5521_DO_NOT_LEAK" not in caplog.text
