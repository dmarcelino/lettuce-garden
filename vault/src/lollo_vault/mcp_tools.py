"""The three MCP tools agents get. None of them adds, changes or deletes
secrets; that is the web UI's job, behind Cloudflare Access."""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .config import MAX_WAIT_SECONDS
from .service import Service

INSTRUCTIONS = (
    "The user's personal vault, with Bitwarden's item types: logins, cards, identities (name, address, email, "
    "phone, passport...), secure notes and SSH keys. list_secrets shows each secret's name, type, public details "
    "and the keys of its private fields. Ask request_secret for only the private fields the task needs (the "
    "postal code, not the whole identity). "
    "Keep private values out of the conversation: whatever you read is sent to the AI provider. The default "
    "delivery, 'placeholders', gives you a handle instead of the values: write {{VAULT_<FIELD>}} where they go "
    "(a document, an email's JSON arguments, a form-filling script reading $VAULT_<FIELD>) and put them in at the "
    "last step with vault-run and vault-fill, so the values go from the vault to the file, command or MCP call "
    "without passing through you. Load the using-vault-secrets skill first. Ask for 'text' only when you must "
    "reason about the value itself (and a public detail such as the country is not enough); it needs a reason the "
    "user reads before approving. "
    "Unless the user set a field to share without asking, they approve first: post the approve_url you get back "
    "in your reply, since they may not get a notification, and wait with get_result. Use values only for the "
    "approved purpose. Never repeat a value in your reply, memory or files, and never open a filled file to check "
    "it: check the template instead."
)


def build_server(service: Service) -> MCPServer:
    server = MCPServer(name="vault", instructions=INSTRUCTIONS)

    @server.tool(
        description="List the secrets in the user's vault: id, name, type, what the user said it may be used "
        "for, its public details (e.g. username, websites, cardholder, name, country) and the keys of its private "
        "fields, each saying whether using it needs the user's approval. Never includes private values.",
        annotations=ToolAnnotations(title="List secrets", read_only_hint=True, open_world_hint=False),
    )
    async def list_secrets() -> dict[str, Any]:
        return service.list_secrets()

    # Not read-only on purpose: Lettuce's bridge then shows its own prompt too.
    # That prompt is a convenience; the vault's approval page is the gate.
    @server.tool(
        description="Ask for private fields of one secret. Give a short purpose the user will read before "
        "approving. fields lists private field keys from list_secrets; ask only for what the task needs (leaving "
        "it out asks for all of them). delivery 'placeholders' (the default) returns a handle and the "
        "{{VAULT_<FIELD>}} placeholders it fills, valid once for a few minutes: vault-run and vault-fill put the "
        "values into a file, command or MCP call without you seeing them. alias prefixes the names "
        "({{VAULT_<ALIAS>_<FIELD>}}), to use two secrets with the same fields in one command. delivery 'text' "
        "returns the values into the conversation, which the AI provider sees: use it only when you must reason "
        "about a value, and give the reason, which the user reads. Secrets that need approval return an id for "
        "get_result and an approve_url to post to the user; fields the user shares without asking answer at once. "
        "Never repeat a value in your reply, memory or files.",
        annotations=ToolAnnotations(title="Request a secret", read_only_hint=False, open_world_hint=False),
    )
    async def request_secret(
        secret_id: str,
        purpose: str,
        fields: list[str] | None = None,
        delivery: Literal["placeholders", "text"] = "placeholders",
        reason: str | None = None,
        alias: str | None = None,
    ) -> dict[str, Any]:
        return await service.request_secret(secret_id, purpose, fields, delivery, reason, alias)

    @server.tool(
        description="Get the answer to a request_secret: pending, denied, expired, or done with the handle and "
        "placeholders (or, for delivery 'text', the values) of the fields the user agreed to share (they may "
        f"withhold some). wait_seconds (0-{MAX_WAIT_SECONDS}) waits for the answer. Handles and values are "
        "returned once. Never repeat a value in your reply, memory or files.",
        annotations=ToolAnnotations(title="Get request result", read_only_hint=True, open_world_hint=False),
    )
    async def get_result(request_id: str, wait_seconds: float = 0) -> dict[str, Any]:
        return await service.get_result(request_id, wait_seconds)

    return server
