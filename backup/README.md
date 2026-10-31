# backup

The image `lettucectl backup` copies a stack with: `lettuce-backup:local`, Debian slim plus rsync (tar and gzip come with the base). The state under `stacks/<name>/state/` is root-owned, so `backup` reads it as root inside a throwaway container of this image instead of with sudo on the host. No profile: every stack can be backed up. How to back up and restore is in [docs/operations.md](../docs/operations.md#backups).

![lettucectl backup, step by step](../docs/diagrams/backup-workflow.svg)

An interactive version, [backup-workflow.html](../docs/diagrams/backup-workflow.html), opens in a browser from a clone of this repo.

## Files
| File | What it does |
|---|---|
| `Dockerfile` | `FROM debian:bookworm-slim`, installs `rsync`. Nothing else: the copy script comes from `lettucectl` (`BACKUP_COPY`) at run time |

## How it plugs in
The image is built with plain `docker build` (`build_backup_image`), never by Compose, so its containers carry no Compose labels and `stale_services` never counts them. `cmd_backup` rebuilds it on every run (cached layers make that cheap); `ensure_backup_image` builds it only when it is missing.

Every container of it runs with `BACKUP_RUN`: `--rm --init` (as PID 1, sh would ignore the signal Docker forwards on Ctrl-C), `--network none`, `--read-only` with a tmpfs `/tmp`, `no-new-privileges`, and `--cap-drop ALL` plus only `CHOWN`, `DAC_OVERRIDE` and `FOWNER`: read everything, write into the operator's directory, and hand the copies to the operator.

`lettucectl backup <name|all> <dir>`:
1. Refuses a destination that is not a directory, contains `:` or `,`, or lies inside this repo (it is public, and a backup holds `stack.env` and the agents' logins), and needs `flock`.
2. Runs `backup_stack` for each stack in its own subshell, so one failing stack does not stop the others; the failed ones are listed at the end and the command exits 1.
3. Takes `flock -n` on `<dir>/<name>/.lock`; a second backup of the same stack stops there, so it cannot delete the first one's `.partial-*`.
4. Mounts, read-only: the state at `/src/state`, `stack.env` at `/src/stack.env`, and `lettuce-<name>_vault-data` at `/src/vault` and `lettuce-<name>_pipelock-data` at `/src/pipelock` when the volume exists (checked with `volume inspect` first, since `run -v` would create a missing one), whatever the profiles. It leaves out caches, upstream's skills (the BFF reinstalls them), the skills `lettucectl skills` installed, and with `--no-tools` `state/tools`.
5. Removes any `.partial-*` an interrupted run left, from inside a container (a snapshot can hold read-only directories the operator cannot empty).
6. For a running stack, asks: pause, copy live, or abort; `--pause` or `--live` answers ahead of time. With pause, the `EXIT` trap that unpauses the stack is set before `compose pause`, so a pause that fails partway is undone too.
7. Copies with `rsync -aH --numeric-ids --chown=<operator uid:gid>` into `<dir>/<name>/.partial-<TS>` (`<TS>` is the UTC start time, `YYYYMMDDTHHMMSSZ`), with `--link-dest` to the newest earlier snapshot, so unchanged files are hard links. rsync's code 24 (files vanished, as in a live copy) is a warning.
8. Renames `.partial-<TS>` to `<TS>` only when the copy succeeded, so `--link-dest` and `--keep` never pick a broken one. With `--keep N`, `backup_prune` deletes all but the newest N snapshots, from inside a container.

With `--compress`, step 7 writes a tar.gz of the same tree to stdout instead, with numeric owners and no `--link-dest`, into `.partial-<TS>.tar.gz` (mode 600), renamed when complete.

The same image fills the document tools' volume: `fill_doc_tools_volume` (under `ensure_doc_tools_volume`) streams the tools out of the `doc-tools` image with `docker cp` into a container of this image with `BACKUP_RUN`'s limits, which unpacks them into the volume and writes the completion marker last ([docs/doc-tools.md](../docs/doc-tools.md)).

`backup` touches no Compose file and is not part of `check_stack`; its rules are under "Key invariants" in [AGENTS.md](../AGENTS.md) ("`backup` reads the state only through its own container").

## Build and test
```bash
tests/run.sh test_backup_    # backup's offline tests (docker is stubbed)
tests/run.sh                 # all offline tests
```
The image itself has no tests; `docker build -t lettuce-backup:local backup` builds it by hand, as the restore steps in docs/operations.md do on another host.

## Upstream seams
- `upstream/docker/agent-skills/`: `backup` leaves out a skill of each name there, since the BFF rewrites upstream's skills on every connect.
