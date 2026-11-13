# tests

The CLI's offline tests and the scripts that run every other test suite. Each folder's own tests (the sidecars' pytest, the executor's `node --test`, the editor image's `test.sh`) are described in that folder's README. What to run before changing what: [CONTRIBUTING.md](../CONTRIBUTING.md#building-and-testing).

## Build and test
No build step. Before changing a file here, run what it runs: `tests/run.sh` for `run.sh` or a stub, the folder's suite for `run-sidecar.sh` (every folder it takes, for a change to the script itself), `tests/run-pipelock.sh` for itself, and `node --test tests/<name>.test.mjs` for a `*.test.mjs`. Shellcheck covers `run*.sh` and `stubs/*` ([CONTRIBUTING.md](../CONTRIBUTING.md#building-and-testing) has the line).

## Files
| File | What it does |
|---|---|
| `run.sh` | The CLI's tests: every `test_*` function, or those whose name contains the argument (`tests/run.sh test_add_`) |
| `stubs/` | `docker`, `curl`, `dig`, `gh` and `secret-tool` stand-ins that `run.sh` puts first on `PATH` |
| `run-sidecar.sh <dir>` | Builds a sidecar's image to its `test` stage and runs its tests in it; extra arguments go to pytest |
| `run-pipelock.sh` | The pipelock image and config against a real pipelock ([pipelock/README.md](../pipelock/README.md#build-and-test)) |
| `mcp-servers.test.mjs` | `lib/mcp-servers.mjs` against temp settings files, with hooks that land a concurrent write before and after its rename |
| `check-doc-links.test.mjs` | `scripts/check-doc-links.mjs` against temp Markdown trees |

## How `run.sh` works
- Each test gets a copy of the repo in a temp dir: the code in `$H` (`$T/home`, `LETTUCE_HOME`), its data in `$D` (`$H/data`, a clone's default `LETTUCE_DATA`) and Lettuce's source at `config.env.example`'s tag in `$U` (`$D/upstream/$REF`). It runs under `set -e`, so a failing assert ends it.
- Tests run in parallel, `TEST_JOBS` at a time (default: one per CPU; `TEST_JOBS=1` runs them one by one). Each prints its block, failure details then `ok` or `FAIL`, when it ends, so the order varies; the failures are listed again at the end. A test therefore keeps everything it writes under its own `$T` and never relies on another test's files or on timing. `TMPDIR` points into the run's own folder, removed when it ends, so every temp dir (the tests' and lettucectl's) goes with it.
- `ctl` runs `lettucectl` with `LETTUCE_DATA` set to `$CTL_DATA`, empty by default, so the tester's own environment never reaches it. `release_on` turns the copy into a released lettucectl: a `$H/RELEASE`, `CTL_DATA=$D`, and its container's mounts (`mounts PATH...`, in `$T/mounts.json`). `wrapper_in DIR` and `wctl DIR ARGS...` run the release wrapper from `DIR/bin`.
- `tests/stubs/{docker,curl,dig,gh,secret-tool}` come first on `PATH`. Nothing reaches the network, Docker or the real home.
- `lettucectl` runs docker under `env -i`, so the stubs read their settings from `$T/stub.env`, not the environment. Write one with `stub KEY=value`. Stub values can't hold spaces, since `stub.env` is sourced.

## The stubs

### curl (Cloudflare)
- Logs `curl METHOD PATH BODY` to `calls.log`, and the arguments to `argv.log`.
- Answers with canned ids: `acc1`, `zone1`, `pol1`, `app1`/`aud1`, `tun1`/`tuntok1`, `rec1`. The garden's Access application is `app2`/`aud2` and its DNS record `rec2`. It also takes `PUT /accounts/acc1/access/apps/app1`, the application update.
- `STUB_CF_FAIL="METHOD /path"` forces a failure. `STUB_CF_TAKEN="host ..."` names that already exist in the zone (a DNS lookup finds them).
- `STUB_CF_ZONE_STATUS` and `STUB_CF_DNSSEC` set the zone's status (default `active`) and its DNSSEC (default `disabled`).
- Downloads from `github.com` (Lettuce's tag archive, a release's files) log `curl-get URL` and answer from `$T/github/<file name>`, failing when it is not there. `make_upstream_archive [TAG]` puts Lettuce's source there.
- Unknown routes fail, so a new Cloudflare call needs a route here.

### docker
- **Rendered config.** `compose config --format json` returns `$T/compose-config.json`, which tests overwrite to simulate rendered configs. It fills `${KEY}` from the `--env-file`, and logs that file's `COMPOSE_PROFILES` as a `docker-env-file` line in `calls.log`. When set, it logs `DOC_TOOLS_NETWORK` and `DOC_TOOLS_VOLUME` as a `docker-env` line. Every `compose` call logs a `docker-env-images` line: `LETTUCE_HOME`, `APP_SERVER_IMAGE`, `LETTA_IMAGE_NS` and, in release mode, `GARDEN_IMAGE_VAULT`.
- **Services.** `compose config --services` lists `app-server`, `bff` and the services of each profile in the `--env-file`'s `COMPOSE_PROFILES`, like upstream: `garden-gmail-triggers`, `garden-watchlist` and `garden-pipelock` for their profiles, `garden-code-server`, `garden-code-server-editor` and `garden-code-server-exec` for `garden-code-server`, and `garden` with any profile `compose/garden.yml` gives it.
- **Containers.** `docker ps -a --filter label=com.docker.compose.project=...` answers from `$T/containers.txt` lines of `project service` (helper `containers NAME SERVICE...`). `compose ps --status running --services` counts every container of the project there as running.
- **`exec`.** `letta agents list` inside `compose exec` answers from `$T/agents.json` (helper `agents NAME...`, ids `agent-1`, `agent-2`, ...) and swallows stdin like the real thing. Any other `exec`:
  - appends its stdin to `$T/exec-stdin.log`;
  - saves its `sh -c` script to `$T/exec-script` and the arguments after it, NUL-separated, to `$T/exec-args`. `run_exec_script ROOT [STDIN]` replays the script with `/root/.letta` and `/etc/cron.d` under ROOT; `exec_args` prints the arguments;
  - prints `$T/exec-stdout`, or `$T/exec-stdout-<group>` for a `lib/mcp-servers.mjs` run of that group when it exists;
  - exits with `STUB_EXEC_RC`. `STUB_EXEC_FAIL_GROUP=<group>` makes that group's runs exit 3.
- **`run`.** Writes the value of each `-e NAME` to `$T/run-env.log`, so tests can tell values passed through the environment from argv. Saves its `sh -c` script to `$T/run-script`. Creates the `.partial-*` it is given in the directory mounted at `/dst`, like the backup copy. Exits with `STUB_RUN_RC`, or 1 when its arguments contain `STUB_RUN_FAIL` (only the first `STUB_RUN_FAIL_TIMES` times when that is set).
- **The host and the registry.** `version --format` answers `STUB_PLATFORM` (default `linux/amd64`); `manifest inspect` publishes the image for the platforms in `STUB_MANIFEST_PLATFORMS`, or fails when it is unset; `pull` exits with `STUB_PULL_RC`; `inspect --format {{json .Mounts}}` (`check_mounts`) prints `$T/mounts.json`, or fails without it.
- **Images, volumes, pause.** `image inspect` answers a Compose-built image's config (fails with `STUB_IMAGE_RC`, or for an image in `STUB_IMAGES_MISSING`). `volume inspect` exits with `STUB_VOLUME_RC`, or 1 for a volume named in `STUB_VOLUMES_MISSING`. `compose pause` exits with `STUB_PAUSE_RC`. `restart --no-deps bff` exits with `STUB_RESTART_RC`.
- **Doc-tools resources.**
  - Networks live in `$T/networks` (`name label internal attached-ids`): `network ls/inspect/create/connect/disconnect/rm`, with `STUB_NETWORK_LS_RC`, `STUB_NETWORK_CREATE_RC` and `STUB_NETWORK_RM_RC`.
  - Tools volumes live in `$T/volumes` (`name tag complete in-use`, optionally a driver and an option count, which `volume inspect --format` reports): `volume ls/create/rm/inspect`, with `STUB_VOLUME_CREATE_RC`.
  - The shared project's Gotenberg exists once its `up` ran (`lettuce_shared gotenberg` in `containers.txt`, id `gotenberg-id`; `STUB_SHARED_UP_RC`).
  - `build` exits with `STUB_BUILD_RC`; `create` answers `doc-tools-cid` (`STUB_CREATE_RC`); `cp` streams `TOOLS-TAR` (`STUB_CP_RC`).
  - A `run` mounting a tools volume answers the marker check from `$T/volumes`, or, for the fill, saves its stdin to `$T/fill-stdin` and marks the volume complete.
  - `ps -aq --filter label=lettucectl.doc-tools=fill` prints `$T/fill-containers`.

### dig, gh and secret-tool
- `dig` answers from `$T/dns.txt` lines of `name TYPE answer`.
- `gh` and `secret-tool` print `STUB_GH_TOKEN` and `STUB_SECRET_TOOL`, or fail like a logged-out tool.

## Helpers

### General
- `ctl` runs lettucectl, sets `$RC` and puts its output in `$OUT`.
- Asserts: `ok_rc`/`bad_rc`, `out_has`/`out_lacks`, `log_has`/`log_lacks`, `eq`.
- `calls` prints the Cloudflare calls (`METHOD PATH`), `body_of METHOD PATH` the last such call's body, `senv NAME KEY` a `stack.env` value.
- `added` makes a fresh `alfred` stack. `add` runs once per run for each starting state (everything under `$T`, and `CTL_DATA`); later tests that start from the same state get a copy, with the first test's `$T` replaced by theirs. The copy has the same secrets, so a test of what `add` itself does or makes, or of secrets differing, runs `ctl add`.
- `config_edit` applies a jq filter to the rendered config; `line_of TEXT` orders calls.
- Toolbox tests run with `HOME=$T/u` (helper `tb`), so the real home is never read.

### Per feature
`check` needs each profile's service in `$T/compose-config.json`. Each feature has a helper to call before `added` (`*_config`) and one to call after it (`with_*`), and a `*_refused FILTER MESSAGE` that asserts `check` refuses the config edited by FILTER with MESSAGE.

| Feature | Helpers |
|---|---|
| vault | `vault_config`, `with_vault` |
| pipelock | `pipelock_config`, `with_pipelock` (also set the app-server's proxy variables) |
| Gmail triggers | `triggers_config`, `with_triggers` (also place a placeholder `garden/executor/run.mjs` in the test's copy), `triggers_refused`; `state_elsewhere` (a `LETTA_STATE_DIR` outside `stacks/`), `installed_up` (a running stack with the executor installed), `partial_stream` (an installer tar missing a file) |
| watchlist | `watchlist_config`, `with_watchlist`, `watchlist_refused`, `MS` (the jq path of the service) |
| garden host | `garden_config` (the `garden` service as Compose renders it; also places the placeholder executor), `GARDEN_ENV` (what every garden service's environment renders to), `taken_gardens LABEL` (every garden name `pick_garden_hostname` tries), `with_all_gardens`, `garden_refused`, `ingress` (the tunnel rules a call sent), `LANDING`/`GARDEN_404`/`LETTUCE_TAIL` (expected rules), `executor_cron_text`, `EXECUTOR_ARGS` |
| `mcp` | `mcp_answer JSON [GROUP]` (what the script would print), `MCP_ADD`/`MCP_REMOVE` (document tools) and `VAULT_ADD`/`VAULT_REMOVE` (its `exec` calls as `calls.log` shows them; the watchlist group's are the same with `watchlist`), `with_docs_and_vault` (both profiles on for alfred), `bff_restarts` (the BFF's lone restarts) |
| doc-tools | `docs_config [NAME]`, `with_docs` (also write `$T/shared-config.json` from the flags of the real `compose/shared/gotenberg.yml`), `docs_tree` (a `doc-tools/` to hash), `docs_tag` (the tag lettucectl computes), `docs_refused`, `tools_volume NAME COMPLETE [IN_USE]`, `CHECK_RUN`/`FILL_RUN` (their flags as `calls.log` shows them) |
| code editor | `code_config` (the three services, networks and volumes as Compose renders them for alfred, taken from a real render), `with_code` (also sets the profile), `code_refused`; `CG`/`CE`/`CX` (the jq paths of the gate, the editor and the shell) |
