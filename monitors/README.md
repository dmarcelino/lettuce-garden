# monitors

The monitors sidecar: sources agents configure (feeds, JSON APIs, pages, schema.org data, calendars), fetched on a timer without a model; monitors that say what an agent wants from them and when; and a report queue that the garden executor in the app-server turns into one-off letta-code tasks, only when something is new. It is Python (Starlette, httpx, lxml), runs as the `garden-monitors` service behind the `garden-monitors` profile, serves its MCP server to agents at `http://garden-monitors:8000/mcp` and its page at `/monitors/` on the stack's garden host. For users and admins (using it, running it, the security model): [docs/monitors.md](../docs/monitors.md); the design record is [DESIGN.md](DESIGN.md).

![The monitors' containers, boundaries and reports](../docs/diagrams/monitors-architecture.svg)

An interactive version, [monitors-architecture.html](../docs/diagrams/monitors-architecture.html), opens in a browser from a clone of this repo.

## Files
| Path | What it does |
|---|---|
| `Dockerfile` | `base` installs `garden-common` from its build context, then this package; `test` adds pytest and `tests/`; the final stage runs as uid 10002, which owns `/data` so the named volume inherits it |
| `pyproject.toml` | The package and its pinned dependencies |
| `src/garden_monitors/__main__.py` | Entry point: settings, store, fetcher, service, app; uvicorn on port 8000 without an access log |
| `src/garden_monitors/config.py` | `PREFIX` (`/monitors`), `INTERNAL_NAME`, every limit, `Settings` from the environment (the `MONITORS_*` keys included), `default_schedule` |
| `src/garden_monitors/cron.py` | Schedules: numeric 5-field cron expressions joined by `;`, matched in the stack's timezone, in words for the page, fires per day for the cap |
| `src/garden_monitors/fetch.py` | The fetcher: `PublicOnlyBackend` (connects only to checked public addresses), `PublicTransport` (httpx over it), robots.txt, per-host spacing and back-off, redirects by hand, size caps, challenge pages |
| `src/garden_monitors/spec.py` | Source specs: validation of what an agent sends (kinds, JMESPath, CSS selectors, pages, follow, dates, watch), and the spec's fingerprint |
| `src/garden_monitors/extract.py` | One extractor per kind (`rss`, `json`, `html`, `schema_org`, `ical`) and the same clean-up for all: one-line capped text, canonical URLs, dates, stable keys |
| `src/garden_monitors/runner.py` | One run of a source: its pages, `follow`, extraction; shared by the scheduler and `source_test` |
| `src/garden_monitors/inspect.py` | `source_inspect`: feeds, WordPress routes, schema.org types, calendars and data URLs in a page's scripts |
| `src/garden_monitors/store.py` | SQLite on `/data`: sources and their last 5 specs, items, health, monitors, reports (the executor's queue), passing tests, agents and heartbeat; `ExecutorQueue` |
| `src/garden_monitors/digest.py` | What a report holds (filters, news stories, sources needing repair) and its prompt within 8000 characters |
| `src/garden_monitors/service.py` | What the MCP tools, the page and the loops do: sources, monitors, due fetches, `fire` (a schedule into a report) |
| `src/garden_monitors/mcp_tools.py` | The MCP tools |
| `src/garden_monitors/app.py` | The page under `/monitors/` (no JavaScript), `/mcp`, the executor routes, `/healthz`, and the fetch and schedule loops in its lifespan |
| `src/garden_monitors/logs.py`, `icons.py` | Logging to stdout (never text from websites), the app icon drawn at first use |
| `src/garden_monitors/templates/`, `static/` | The page (Jinja) and its stylesheet |
| `tests/` | pytest: schedules, the fetcher (with a fake resolver and `httpx.MockTransport`), every extractor on synthetic pages, the service, the app |

## How it plugs in
- **Overlay:** [compose/monitors.yml](../compose/monitors.yml) defines `garden-monitors` (image `lettuce-monitors:local`, built from this directory with the `garden-common` build context): uid 10002, read-only root, `/tmp` as tmpfs, `cap_drop: [ALL]`, `no-new-privileges`, 384 MB, no ports, on the stack's default network only.
- **Storage:** the named volume `monitors-data` at `/data`, no host path. `backup` copies it (`has_monitors`, `monitors_volume` in `lib/common.sh`); `remove` deletes it with the stack.
- **Environment:** `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` (set from `CF_GARDEN_AUD`), `CF_ACCESS_ISSUER`, `ALLOWED_USERS`, `PUBLIC_ORIGIN` (set from `GARDEN_ORIGIN`), `GARDEN_EXECUTOR_TOKEN`, `TZ`, and the admin's `MONITORS_DEFAULT_TIMES`, `MONITORS_MAX_REPORTS_PER_DAY`, `MONITORS_MIN_INTERVAL`, `MONITORS_MAX_SOURCES`, `MONITORS_MAX_MONITORS`, which `up` copies from `config.env` into `stack.env` (`sync_monitors_settings`).
- **Tunnel:** the `PROFILE_ROUTES` row in [lib/cloudflare.sh](../lib/cloudflare.sh) sends `^/monitors(/|$)` on the garden host to `http://garden-monitors:8000`. `/mcp`, `/executor/v1/` and `/healthz` are outside that prefix, so the tunnel answers them with 404.
- **Agents:** `lettucectl mcp` registers `{"name": "monitors", "transport": "http", "url": "http://garden-monitors:8000/mcp"}` (the `monitors` group of [lib/mcp-servers.mjs](../lib/mcp-servers.mjs)); `lettucectl skills` installs [monitoring-sources](../agent-skills/monitoring-sources/SKILL.md). `/mcp` accepts only the host `garden-monitors` and no `Origin`.
- **The executor:** `up` installs it when this profile is on (`executor_wanted`), with `--monitors` in its cron line (`executor_cron`). Each minute it claims the queued reports from `/executor/v1/requests`, runs `letta cron add --once` for each, posts the results, the agent list when asked and a heartbeat. The routes are `garden_common.executor.request_routes`, shared with Gmail triggers; the protocol is in [garden/executor/README.md](../garden/executor/README.md).
- **Pipelock:** `garden-monitors` is in the app-server's `NO_PROXY` (`compose/pipelock-proxy.yml`), and `check_pipelock` requires it there, since the executor's token passes between them. The service itself fetches directly.
- **`check_stack`:** `check_monitors` (a hardened sidecar on the default network only, mounting only `monitors-data` at `/data`), `monitors-data` mounted by nothing else and no `volumes_from` reaching the service whatever the profiles, `check_garden_audience`, and `check_garden`. Every `check_stack` rule: [CONTRIBUTING.md](../CONTRIBUTING.md#what-check_stack-enforces).

## Rules
The guarantees and limits people see are in [docs/monitors.md](../docs/monitors.md#security-model); these are the rules that keep them.

### The second written exception to "no second way into an agent"
The rule is in [CONTRIBUTING.md](../CONTRIBUTING.md#relationship-to-upstream). Monitors start turns the way Gmail triggers do: the service queues a report, the garden executor runs `letta cron add --once`, and Lettuce's scheduler fires the task on the BFF's connection. The service opens no connection to the app-server. A report is queued only when one of a monitor's schedules fires and something is new or a source needs repair, never while the last report of that agent, schedule and conversation is on its way, and never past `MONITORS_MAX_REPORTS_PER_DAY` for the agent in a day (a lost report counts: its task may have been made). `monitor_put` refuses schedules that could pass it, counting one report per schedule and conversation. The last minute fired is kept in the store, so a restart catches up on schedules it missed, up to an hour. Unlike Gmail triggers, agents make the monitors; that gives them nothing `letta cron add` doesn't, and the person sees and deletes every monitor on the page. Never add a way for a monitor to start a turn other than its schedule (an alert mode is a design change: [DESIGN.md](DESIGN.md#out-of-scope-for-follow-ups)).

### The service never creates schedules in Lettuce
Schedules live in the service, which fires them; agents never create `letta cron` tasks for monitors (the tools' instructions and the skill say so). Deleting a monitor on the page then leaves nothing behind in Lettuce.

### Specs are data, never code
A source spec holds JMESPath expressions, CSS selectors, field names and numbers, validated by `spec.parse_spec`. Nothing an agent sends is executed or evaluated, and no page script runs. Never add a spec field that runs code, a template or a regular expression from the agent, or a fetcher that runs a page's JavaScript; a browser, if ever needed, is a separate throwaway container ([DESIGN.md](DESIGN.md#out-of-scope-for-follow-ups)).

### The fetcher reaches public addresses only, and stays polite
`PublicOnlyBackend` resolves each host itself, refuses the connection unless every address is public, and connects to the checked address; redirects are followed by hand and checked the same way. Robots.txt is honoured for `lettuce-garden-monitors` (unreadable means nothing is fetched, asked again after 10 minutes), requests to a host are at least `HOST_SPACING` apart (or its `Crawl-delay`), a 429 or 503 pauses the host, and challenge or waiting-room pages are failures. A call never waits out a pause longer than 60 s (`TOOL_WAIT`, 10 s, in an agent's tool call, which also ends after `TOOL_SECONDS`): it fails and the source is tried again later, so one slow site never holds up the others. No cookies, credentials or custom headers. Never weaken any of these per source.

### Text from websites is untrusted
It is stored and shown as text (Jinja autoescape, no script on the page), cut to fixed lengths, and reaches agents quoted between markers that say it is data; runs of three angle brackets in it are broken up (`digest.data_line`), so a site can't write the end marker. It is never logged: logs carry source names, counts and short reasons only.

### No model in the service
Relevance is the agent's, in its own turn. Never add a model call or API key here.

## Build and test
`lettucectl up` builds the image. The tests need only Docker and never use the network:
```bash
tests/run-sidecar.sh monitors                   # pytest in the image's test stage
node --test 'garden/executor/tests/*.test.mjs'  # the executor's side of the report queue
tests/run.sh monitors                           # the CLI's checks, executor flag, backup and settings (stubbed docker)
node --test tests/mcp-servers.test.mjs          # the monitors group in Settings -> MCP
```
Run them before changing this directory; a change to `garden-common/` runs the suites listed in [its Build and test](../garden-common/README.md#build-and-test).

## Upstream seams
These are the Lettuce behaviours monitors depend on (all of them are listed in [CONTRIBUTING.md](../CONTRIBUTING.md#upstream-seams)):
- Through the executor: `letta cron add --once` with its flags and JSON output, `letta agents list`, as for Gmail triggers ([garden/executor/README.md](../garden/executor/README.md)).
- The shared MCP list accepting an `http` entry of `name`, `transport` and `url`, as for the vault; the 120 s Lettuce's MCP bridge gives a tool call (`source_test` and `source_inspect` stay well inside it).
- The `mcp-servers` skill's wrapper, named in the `monitoring-sources` skill.
