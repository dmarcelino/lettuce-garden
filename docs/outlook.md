# Outlook

Each stack can connect **one Outlook mailbox** for its agents: mail, calendar and
tasks, plus read-only access to shared mailboxes. The person connects their own
signed-in Outlook web session with a bookmarklet, and decides on a web page exactly
how far agents may go — read only, organise, draft, send — and who they may send to.
It is opt-in, behind the `garden-outlook` profile.

Unlike the Google Workspace integration, this needs **no app registration and no
admin consent**. It calls **Microsoft Graph** (the current, supported API), but it
gets its access token by reusing the sign-in your browser already has: the
bookmarklet hands over the refresh token the Outlook web app holds, and the sidecar
exchanges it for a Graph token under one of Microsoft's own apps (this is called
FOCI). Because that reuses an existing first-party sign-in, no admin has to approve
anything — but a strict tenant can still refuse the exchange (see the security
model), and where a normal Graph app registration is available, prefer it.

```
you  ──bookmarklet on outlook.office.com──> opens /outlook/#<session>   (the refresh token, in the URL fragment)
you  ──https://<garden>/outlook/ ─────────> sets levels + allowed recipients; the session is sealed on disk
agent──(MCP)──> garden-outlook ──> graph.microsoft.com   only what the policy allows; the token stays here
```

## For the person using it

1. **Open `https://<your garden>/outlook/`** and sign in as usual; `<your garden>`
   is your stack's garden address, `<name>-garden.<domain>`.
2. **Add the bookmarklet.** Drag the **Connect Outlook** link on that page to your
   browser's bookmarks bar. Expand "What the bookmarklet does" to read it first — it
   reads the sign-in your browser already holds for Outlook and sends it to your
   stack; it does not type your password and sends nothing anywhere else.
3. **Connect.** Open `https://outlook.office.com/mail/`, wait for your mail to load,
   then click the **Connect Outlook** bookmark. A new tab opens on your Outlook page
   and connects the session. You only reconnect when your Outlook session really
   ends (days, usually), not every hour.
4. **Choose what agents may do.** On the Outlook page, set a level per service and
   save:

   | Service | Levels (least to most) |
   |---|---|
   | Mail | off · readonly · organize (move, flag, categories) · drafts · send · full (mailbox settings) |
   | Calendar | off · readonly · **self-events** (block your own time, no invitations) · full (invitations, RSVPs) |
   | Tasks | off · readonly · manage |

   - **self-events** lets an agent create or change events on *your* calendar with
     no attendees, so it can hold time but cannot invite anyone. Inviting needs
     **full**.
   - **Allowed recipients:** one address or `@domain` per line. Agents may only send
     to, or invite, these (your own address is always allowed). Leave empty for no
     restriction.
   - **Shared mailboxes:** one address per line. Agents may *read* these mailboxes
     and never write to them.

To stop, **Disconnect** on the page, or turn the `garden-outlook` profile off.

## For the operator (running it)

Turn the profile on for every stack:

```
lettucectl profiles <your current list>,garden-outlook
```

`up`/`profiles` generate `OUTLOOK_SECRET` once per stack (the key that encrypts the
stored session), create the garden host if it is not there yet, add the tunnel route
`/outlook/`, and register the `outlook` MCP server in each stack's Settings → MCP so
agents see it. There is no Microsoft-side setup and no redirect URI to register.

`lettucectl backup` deliberately does **not** copy the Outlook session: reconnecting
with the bookmarklet is cheap, and a backup should not carry a live mail credential.

## Security model (what is and isn't guaranteed)

- **The token stays in the sidecar.** Agents get MCP tools only; the bearer token
  and refresh material never reach them. They are sealed with `OUTLOOK_SECRET`
  (held only in the sidecar's environment, never on the data volume) on a volume no
  other service mounts — `lettucectl check` refuses a stack where one does.
- **The level and recipient list are the boundary, and they are real.** Every tool
  checks the policy before acting, in the sidecar, so an agent calling the endpoint
  directly cannot exceed its level or send to an off-list address. There is no
  in-app approval step; the level and the allowed-recipients list are the whole
  control.
- **This guards the account's identity, not your data.** The allowed-recipients list
  limits who the account mails or invites. It does not stop an agent reading what the
  level allows, and agent shells have their own network access. Email, event and task
  text is untrusted input: tools label it so, but a model can still be misled by it.
- **Supported API, unsupported way in.** The data calls use Microsoft Graph, which is
  current and documented. What is unsupported is *how the token is obtained* —
  reusing the browser's first-party refresh token (FOCI) instead of a registered app.
  That is deliberate (it needs no admin), but it is a grey-area use of Microsoft's own
  apps: a tenant can refuse it with Conditional Access, token-protection or
  first-party-app restrictions. If it is refused, the `/outlook/` page says so when
  you connect. Device-based sign-in policies can also mean a refresh from the server
  is refused and you reconnect more often, or that it does not work at all.
- **This guards the account's identity, not your data.** (Repeated because it matters.)
  The level and allowed-recipients list limit what the account sends and who it
  invites — not what an agent can read within its level, nor where it sends data by
  other means.
- **Not for accounts with a supported path.** Where a normal Graph app registration is
  available, use it instead.
