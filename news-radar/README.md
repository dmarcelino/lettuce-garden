# news-radar

The News Radar sidecar: sources agents configure (feeds, JSON APIs, pages, schema.org data, calendars), fetched on a timer without a model; monitors that say what an agent wants from them and when; and a report queue that the lollo executor in the app-server turns into one-off letta-code tasks, only when something is new. It is Python (Starlette, httpx, lxml), runs as the `lollo-news-radar` service behind the `lollo-news-radar` profile, serves its MCP server to agents at `http://lollo-news-radar:8000/mcp` and its page at `/news-radar/` on the stack's lollo host. For users and admins (using it, running it, the security model): [docs/news-radar.md](../docs/news-radar.md); the design record is [DESIGN.md](DESIGN.md).

![News Radar's containers, boundaries and reports](../docs/diagrams/news-radar-architecture.svg)

An interactive version, [news-radar-architecture.html](../docs/diagrams/news-radar-architecture.html), opens in a browser from a clone of this repo.

## Files
| Path | What it does |
|---|---|
| `Dockerfile` | `base` installs `lollo-common` from its build context, then this package; `test` adds pytest and `tests/`; the final stage runs as uid 10002, which owns `/data` so the named volume inherits it |
| `.dockerignore` | Caches and build output, kept out of the image and of its content tag |
| `pyproject.toml` | The package and its pinned dependencies |
| `src/lollo_news_radar/__main__.py` | Entry point: settings, store, fetcher, service, app; uvicorn on port 8000 without an access log |
| `src/lollo_news_radar/config.py` | `PREFIX` (`/news-radar`), `INTERNAL_NAME`, every limit, `Settings` from the environment (the `NEWS_RADAR_*` keys included), `default_schedule` |
| `src/lollo_news_radar/cron.py` | Schedules: numeric 5-field cron expressions joined by `;`, matched in the stack's timezone, in words for the page, fires per day for the cap |
| `src/lollo_news_radar/fetch.py` | The fetcher: `PublicOnlyBackend` (connects only to checked public addresses; a name that does not resolve is said as such), `PublicTransport` (httpx over it), robots.txt (and the sitemaps it lists), per-host spacing and back-off (a pause's length travels with the error as `retry_after`), redirects by hand, size caps, challenge pages, the header's charset |
| `src/lollo_news_radar/spec.py` | Source specs: validation of what an agent sends (kinds, JMESPath, CSS selectors, pages, follow, dates, watch, group_by), and the spec's fingerprint |
| `src/lollo_news_radar/extract.py` | One extractor per kind (`rss`, `json`, `html`, `schema_org`, `ical`, `sitemap`) and the same clean-up for all: one-line capped text (HTML codes and tags decoded for JSON and schema.org), canonical URLs, dates (relative ones from now; a yearless one in the year from 90 days back to 9 months ahead), stable keys; pages parsed in their encoding; `run_problem` (what makes a run unhealthy, for tests and scheduled runs alike) and `window_warning` |
| `src/lollo_news_radar/runner.py` | One run of a source: its pages, a sitemap index's two highest-numbered files of each family (`extract.sitemaps`), `follow` (a sitemap's newest `max` pages only), extraction; shared by the scheduler and `source_test`, which also asks for the first page's top repeated block |
| `src/lollo_news_radar/blocks.py` | Repeated blocks with links (the likely cards of a listing), with how many pages they link to and how many hold a date, month names in six languages included; for `source_inspect` and `source_test`'s warning |
| `src/lollo_news_radar/inspect.py` | `source_inspect`: feeds, WordPress routes, schema.org types, calendars, sitemaps, repeated blocks (`blocks.py`), and data URLs in a page and up to 6 of its same-site scripts (library and tracking scripts left out; account-like URLs and script code dropped, listing-like URLs first) |
| `src/lollo_news_radar/store.py` | SQLite on `/data`: sources and their last 5 specs, items, health, monitors (with the conversation each was set up in and their `where` places), reports (the executor's queue), passing tests, agents and heartbeat; `ExecutorQueue`. A column added later is added to an older store when it opens |
| `src/lollo_news_radar/digest.py` | What a report holds (filters matched as whole words without case or accents, `*` for a prefix, `where` on the venue; variants grouped by the source's `group_by`, one row per event across sources with `also_in`, news stories, sources needing repair), what each term does for a preview (`term_counts`), and its prompt within 8000 characters |
| `src/lollo_news_radar/service.py` | What the MCP tools, the page and the loops do: sources, monitors, due fetches, `fire` (a schedule into a report) |
| `src/lollo_news_radar/mcp_tools.py` | The MCP tools |
| `src/lollo_news_radar/app.py` | The page under `/news-radar/` (no JavaScript; it deletes monitors and sources and sets where a monitor's reports go), `/mcp`, the executor routes, `/healthz`, and the fetch and schedule loops in its lifespan |
| `src/lollo_news_radar/logs.py`, `icons.py` | Logging to stdout (never text from websites), the app icon drawn at first use |
| `src/lollo_news_radar/templates/`, `static/` | The page (Jinja) and its stylesheet |
| `tests/` | pytest: schedules, the fetcher (with a fake resolver and `httpx.MockTransport`), every extractor on synthetic pages, the service, the app |

## How it plugs in
- **Overlay:** [compose/news-radar.yml](../compose/news-radar.yml) defines `lollo-news-radar` (image `${LOLLO_IMAGE_NEWS_RADAR:-lollo-news-radar:local}`: from a clone, built from this directory with the `lollo-common` build context; in a release, the published image, a line in [release/images](../release/README.md#files) named by its content): uid 10002, read-only root, `/tmp` as tmpfs, `cap_drop: [ALL]`, `no-new-privileges`, 384 MB, no ports, on the stack's default network only.
- **Storage:** the named volume `news-radar-data` at `/data`, no host path. `backup` copies it (`has_news_radar`, `news_radar_volume` in `lib/common.sh`); `remove` deletes it with the stack.
- **Environment:** `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` (set from `CF_LOLLO_AUD`), `CF_ACCESS_ISSUER`, `ALLOWED_USERS`, `PUBLIC_ORIGIN` (set from `LOLLO_ORIGIN`), `LOLLO_EXECUTOR_TOKEN`, `TZ`, and the admin's `NEWS_RADAR_DEFAULT_TIMES`, `NEWS_RADAR_MAX_REPORTS_PER_DAY`, `NEWS_RADAR_MIN_INTERVAL`, `NEWS_RADAR_MAX_SOURCES`, `NEWS_RADAR_MAX_MONITORS`, which `up` copies from `config.env` into `stack.env` (`sync_news_radar_settings`).
- **Tunnel:** the `PROFILE_ROUTES` row in [lib/cloudflare.sh](../lib/cloudflare.sh) sends `^/news-radar(/|$)` on the lollo host to `http://lollo-news-radar:8000`. `/mcp`, `/executor/v1/` and `/healthz` are outside that prefix, so the tunnel answers them with 404.
- **Agents:** `lolloctl mcp` registers `{"name": "news-radar", "transport": "http", "url": "http://lollo-news-radar:8000/mcp"}` (the `news-radar` group of [lib/mcp-servers.mjs](../lib/mcp-servers.mjs)); `lolloctl skills` installs [news-radar-sources](../agent-skills/news-radar-sources/SKILL.md). `/mcp` accepts only the host `lollo-news-radar` and no `Origin`.
- **The executor:** `up` installs it when this profile is on (`executor_wanted`), with `--news-radar` in its cron line (`executor_cron`). Each minute it claims the queued reports from `/executor/v1/requests`, runs `letta cron add --once` for each, posts the results, the agent list when asked and a heartbeat. The routes are `lollo_common.executor.request_routes`, shared with Gmail triggers; the protocol is in [landing/executor/README.md](../landing/executor/README.md).
- **Pipelock:** `lollo-news-radar` is in the app-server's `NO_PROXY` (`compose/pipelock-proxy.yml`), and `check_pipelock` requires it there, since the executor's token passes between them. The service itself fetches directly.
- **`check_stack`:** `check_news_radar` (a hardened sidecar on the default network only, mounting only `news-radar-data` at `/data`), `news-radar-data` mounted by nothing else and no `volumes_from` reaching the service whatever the profiles, `check_lollo_audience`, and `check_landing`. Every `check_stack` rule: [CONTRIBUTING.md](../CONTRIBUTING.md#what-check_stack-enforces).

## Rules
The guarantees and limits people see are in [docs/news-radar.md](../docs/news-radar.md#security-model); these are the rules that keep them.

### The second written exception to "no second way into an agent"
The rule is in [CONTRIBUTING.md](../CONTRIBUTING.md#relationship-to-upstream). Monitors start turns the way Gmail triggers do: the service queues a report, the lollo executor runs `letta cron add --once`, and Lettuce's scheduler fires the task on the BFF's connection. The service opens no connection to the app-server. A report is queued only when one of a monitor's schedules fires and something is new or a source needs repair, never while the last report of that agent, schedule and conversation is on its way, and never past `NEWS_RADAR_MAX_REPORTS_PER_DAY` for the agent in a day (a lost report counts: its task may have been made). `monitor_put` refuses schedules that could pass it, counting one report per schedule and conversation. The last minute fired is kept in the store, so a restart catches up on schedules it missed, up to an hour. Unlike Gmail triggers, agents make the monitors; that gives them nothing `letta cron add` doesn't, and the person sees and deletes every monitor on the page. Never add a way for a monitor to start a turn other than its schedule (an alert mode is a design change: [DESIGN.md](DESIGN.md#out-of-scope-for-follow-ups)).

### The service never creates schedules in Lettuce
Schedules live in the service, which fires them; agents never create `letta cron` tasks for monitors (the tools' instructions and the skill say so). Deleting a monitor on the page then leaves nothing behind in Lettuce.

### Specs are data, never code
A source spec holds JMESPath expressions, CSS selectors, field names and numbers, validated by `spec.parse_spec`. Nothing an agent sends is executed or evaluated, and no page script runs. Never add a spec field that runs code, a template or a regular expression from the agent, or a fetcher that runs a page's JavaScript; a browser, if ever needed, is a separate throwaway container ([DESIGN.md](DESIGN.md#out-of-scope-for-follow-ups)).

### The fetcher reaches public addresses only, and stays polite
`PublicOnlyBackend` resolves each host itself, refuses the connection unless every address is public, and connects to the checked address; redirects are followed by hand and checked the same way. Robots.txt is honoured for `lollo-rozzo-news-radar` (unreadable means nothing is fetched, asked again after 10 minutes), requests to a host are at least `HOST_SPACING` apart (or its `Crawl-delay`), a 429 or 503 pauses the host, and challenge or waiting-room pages are failures. A call never waits out a pause longer than 60 s (`TOOL_WAIT`, 10 s, in an agent's tool call, which also ends after `TOOL_SECONDS`): it fails and the source is tried again later, so one slow site never holds up the others. No cookies, credentials or custom headers. Never weaken any of these per source.

### Text from websites is untrusted
It is stored and shown as text (Jinja autoescape, no script on the page), cut to fixed lengths, and reaches agents quoted between markers that say it is data; runs of three angle brackets in it are broken up (`digest.data_line`), so a site can't write the end marker. It is never logged: logs carry source names, counts and short reasons only.

### No model in the service
Relevance is the agent's, in its own turn. Never add a model call or API key here.

## Build and test
From a clone `lolloctl up` builds the image; a release pulls it. The tests need only Docker and never use the network:
```bash
tests/run-sidecar.sh news-radar                  # pytest in the image's test stage
node --test 'landing/executor/tests/*.test.mjs'  # the executor's side of the report queue
tests/run.sh news_radar                          # the CLI's checks, executor flag, backup and settings (stubbed docker)
node --test tests/mcp-servers.test.mjs          # the News Radar group in Settings -> MCP
```
Run them before changing this directory; a change to `lollo-common/` runs the suites listed in [its Build and test](../lollo-common/README.md#build-and-test).

## Upstream seams
These are the Lettuce behaviours News Radar depends on (all of them are listed in [CONTRIBUTING.md](../CONTRIBUTING.md#upstream-seams)):
- Through the executor: `letta cron add --once` with its flags and JSON output, `letta agents list`, as for Gmail triggers ([landing/executor/README.md](../landing/executor/README.md)); and `--conversation=<id>`, to report into one conversation.
- letta-code giving the agent's shell its conversation id as `$LETTA_CONVERSATION_ID`: the skill tells agents to pass it as `set_up_in`. Without it, a monitor reports in a new conversation each time and says so.
- The shared MCP list accepting an `http` entry of `name`, `transport` and `url`, as for the vault; the 120 s Lettuce's MCP bridge gives a tool call (`source_test` and `source_inspect` stay well inside it).
- The `mcp-servers` skill's wrapper, named in the `news-radar-sources` skill.
