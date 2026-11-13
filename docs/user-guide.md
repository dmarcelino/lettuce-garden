# Your Lettuce stack

This page is for you if someone runs lettuce-garden and has given you a stack. It covers what you get, how to sign in and where to start. Whoever runs the server is called the *admin* below.

## What you get
- **Your own agent server.** Letta agents with their own memory, conversations, workspaces and secrets. Nobody else's agents can reach yours, and yours can't reach theirs.
- **Lettuce**, the web app for talking to your agents, at `https://<name>.<domain>`. It is a PWA: on a phone, add it to the Home Screen.
- **Your garden** at `https://<name>-garden.<domain>/`, when the admin has turned on any of its pages. Its home page links to each one that is on:

  | Page | What it does | Guide |
  |---|---|---|
  | Vault, `/vault/` | Keeps your logins, cards, identities, notes and SSH keys encrypted with your own passphrase (you can import them from Bitwarden). Agents ask to use one, and you approve each request, unless you let an everyday detail such as an address be used without asking. By default agents fill the values in without reading them. | [Using the vault](vault.md#using-it) |
  | Gmail triggers, `/gmail-triggers/` | Rules such as "when an email arrives in label X, ask agent Y to do Z". | [Using Gmail triggers](gmail-triggers.md#using-it) |
  | News Radar, `/news-radar/` | What your agents keep an eye on for you (films, concerts, news on a topic) and when they report; ask an agent to set one up. | [Using News Radar](news-radar.md#using-it) |
  | Code editor, `/code-server/` | VS Code in the browser on your agents' workspaces, with a terminal that has the agents' tools. | [Using the code editor](code-server.md#using-it) |

  Two more features have no page: your agents may have Office and PDF tools ([document tools](doc-tools.md#using-it)), and their web traffic may go through a proxy that checks where it goes and records it, which catches mistakes but is not a wall: an agent can go around it ([egress proxy](egress-firewall.md)). Ask your admin which are on.

The admin tells you your exact addresses (`<name>` and `<domain>` above).

## Signing in
Open your Lettuce address and sign in with the Google account the admin allowed. The garden uses the same sign-in. You are signed in to each address separately, so the garden may ask once more.

If Cloudflare says you may not sign in, or Lettuce says forbidden or unknown user, your email is not on the stack's list yet: ask the admin.

## First steps
1. **Connect a model provider:** in Lettuce, **Settings -> Providers & models**. Your own API key keeps your spending separate from everyone else's. Provider keys stay in your stack, on the server.
2. **Create an agent** from the sidebar.
3. **Optionally connect Google** (Gmail, Calendar, Tasks, Contacts) in **Settings -> Google**, if the admin turned it on. The admin gives you the client ID and secret; start with low access levels. Details: [google-setup.md](google-setup.md#5-each-person-connect-in-lettuce).
4. **Turn on notifications** on each device you use, in Lettuce and, if you use it, on the vault's **Notifications** page.

## Good to know
- **Your agents are powerful inside your stack.** They run commands as root on the server, and can read everything in your stack: every agent's memory and workspace, and your provider keys. Only the vault keeps secrets from them: an agent gets a private value only when you approve, or when you let that detail be used without asking.
- **Whatever you share with an agent goes to your AI provider** and is stored in the conversation on the server.
- **In the code editor, never choose "Trust"** for a folder. Agents write those folders, and trusting one would let what they wrote run in your browser, on the same address as your vault. The [code editor guide](code-server.md#using-it) explains why.
- **Your data is only as private as the server.** The admin (anyone with root on it) can read your stack. The vault is encrypted on disk with your passphrase, which nobody can recover for you; while it is unlocked, someone with root on the server could still read it ([what the vault protects](vault.md#security-model)).
