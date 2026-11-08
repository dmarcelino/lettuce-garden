# release

What a lettuce-garden release is made of, and how `publish.yml` makes one. A release is the `lettucectl` image (the CLI and every file it reads at run time), the garden's published images, and a wrapper script pinned to the `lettucectl` image's digest, which admins put in their data directory. Installing and upgrading, for admins: [docs/operations.md](../docs/operations.md#installing-and-upgrading). Why it is built this way: [lib/DESIGN.md](../lib/DESIGN.md).

## Files
| File | What it does |
|---|---|
| `images` | The images `publish.yml` publishes, one per line: the name, the build context, the shared context (`garden-common`) and the Compose service. lettucectl, `content-tag` and `publish.yml` all read it. |
| `content-tag` | `release/content-tag <image>` prints the image's content tag: a hash of its build context and shared context (docs, tests, caches and the top-level names its `.dockerignore` lists left out), its name, and `../REBUILD`. |
| `lettucectl.Dockerfile` | The `lettucectl` image: Debian with `curl`, `jq`, `openssl`, `dig` and `flock`; the Docker CLI, Compose and Buildx from Docker's own `docker:<version>-cli` image, pinned by digest; and this repo's runtime files, with `RELEASE`. Built from the repo root. |
| `lettucectl.Dockerfile.dockerignore` | What the `lettucectl` image takes from the repo (an allowlist). |
| `lettucectl-wrapper.sh` | The wrapper, with `@VERSION@`, `@IMAGE@` and `@REPOSITORY@` for `publish.yml` to fill in. It runs the image with the data directory, the Docker socket, and the directories `backup` and `receipts` write to mounted at the same path; for `toolbox`, the operator's home read-only and what only the host's commands give; and it does `self-upgrade`. |

`VERSION` (the release's version) and `REBUILD` (a date; changing it rebuilds every image on fresh base images) are at the repo root. `RELEASE`, written by `publish.yml` and baked into the `lettucectl` image, holds the version, the Lettuce tag the release was tested with (`config.env.example`'s `LETTUCE_REF`), the repository, and each published image's full name, `ghcr.io/<owner>/lettuce-<image>:<content tag>@sha256:<digest>`; it is also attached to the GitHub release.

## How it plugs in
- **Release mode.** lettucectl runs in release mode when `RELEASE` exists ([lib/README.md](../lib/README.md#release-mode-is-a-file-not-a-setting)): it names the published images from it, builds only the two it cannot pull (the document tools and the code editor's shell), and checks that every host path is mounted at the same path in its container.
- **The overlays** name each published image `${GARDEN_IMAGE_<IMAGE>:-lettuce-<image>:local}`, so a clone builds and runs its own ([compose/README.md](../compose/README.md)).
- **`publish.yml`** runs on a `vX.Y.Z` or `vX.Y.Z-rc.N` tag, after every job of `ci.yml`: one job per image (built for `linux/amd64` and `linux/arm64` and pushed only when its content tag is not in the registry, with an SBOM, BuildKit provenance and a GitHub attestation), then `RELEASE`, the `lettucectl` image, the wrapper, `SHA256SUMS`, the notes (which images changed since the last release) and the GitHub release.

## Rules

### Images are named by their content, never by the release
Compose recreates a container when its image's name changes, even for the same image, and recreating the vault locks it. A release reuses every image whose content tag is already published, so it keeps its name and digest, and only the containers whose image changed restart. Never tag a published image by the release version, and never push to an existing content tag.

### Two images are never published
`doc-tools` (its PDF server uses PyMuPDF, AGPL-3.0: publishing the image would make it AGPL) and `code-server/exec` (built `FROM` the app-server's image, so one per Lettuce tag) are built on the host; their contexts ship in the `lettucectl` image. Keep them out of `images`.

### Nothing moves after a release
No `latest`, nothing published from `main`: the `lettucectl` image holds the Docker socket, so a moving tag would let one push reach every host. The wrapper names the image by digest, and `RELEASE` names every other image by digest.

### Base images get fixes through `REBUILD`
Reused images keep their base images. To pick up their security fixes, change `REBUILD` to the day's date in a release of its own: every content tag changes, every sidecar restarts and every vault locks, and the release notes say so.

### What the image carries is what lettucectl reads
A file lettucectl reads at run time (a compose file, a skill, the executor, a build context built on the host) goes into `lettucectl.Dockerfile` and its `.dockerignore`, or a release install cannot find it.

## Cutting a release
1. Set `VERSION` to the new version (and `REBUILD`, for a rebuild release) on `main`, and make sure `config.env.example`'s `LETTUCE_REF` is the Lettuce tag you tested with ([CONTRIBUTING.md](../CONTRIBUTING.md#upgrading-lettuce_ref)).
2. Tag it: `git tag vX.Y.Z && git push origin vX.Y.Z`. For a candidate to try on a test host first, `vX.Y.Z-rc.N`, published as a pre-release.
3. The first time only: make each `ghcr.io/<owner>/lettuce-*` package and `lettucectl` public, so hosts pull without credentials.

## Build and test
```bash
tests/run.sh test_content_tags        # content-tag
tests/run.sh test_every_published     # every image in images has its overlay variable
tests/run.sh test_wrapper_            # the wrapper, with docker stubbed
tests/run.sh test_release_            # lettucectl in release mode
shellcheck -x release/content-tag release/lettucectl-wrapper.sh
```
To try the image itself, write a `RELEASE` at the repo root (it is gitignored), `docker build -f release/lettucectl.Dockerfile -t <name> .`, delete `RELEASE`, and run a wrapper filled in with `<name>` from a data directory's `bin/`. `publish.yml` is checked by an `-rc` tag on a fork.
