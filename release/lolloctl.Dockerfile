# syntax=docker/dockerfile:1
# The lolloctl image (release/README.md): the CLI and every file it reads at
# run time, plus the two build contexts built on the operator's host (doc-tools,
# code-server/exec). Built by publish.yml from the repo root, after it has
# written RELEASE; never run but through the wrapper (lolloctl-wrapper.sh).
#
# The Docker CLI and its Compose and Buildx plugins come from Docker's own
# image, pinned by digest, so the Compose that runs the stacks is this one, not
# the host's. To upgrade: the digest of `docker buildx imagetools inspect
# docker:<version>-cli`.
FROM docker:29.9.3-cli@sha256:2357639ad1ea7d0194134bf3292622fd4e562483966ed674e5d7c945b5f3b28d AS docker

FROM debian:12-slim
RUN apt-get update \
  && apt-get install -y --no-install-recommends bash ca-certificates curl dnsutils jq openssl util-linux \
  && rm -rf /var/lib/apt/lists/*
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY --from=docker /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
COPY --chmod=0755 lolloctl /opt/lollo-rozzo/lolloctl
COPY config.env.example RELEASE /opt/lollo-rozzo/
COPY lib /opt/lollo-rozzo/lib
COPY compose /opt/lollo-rozzo/compose
COPY agent-skills /opt/lollo-rozzo/agent-skills
COPY release/images /opt/lollo-rozzo/release/images
COPY scripts/dns-preflight.sh /opt/lollo-rozzo/scripts/dns-preflight.sh
COPY landing/executor/run.mjs /opt/lollo-rozzo/landing/executor/run.mjs
COPY doc-tools /opt/lollo-rozzo/doc-tools
COPY code-server/exec /opt/lollo-rozzo/code-server/exec
RUN chmod -R a+rX,go-w /opt/lollo-rozzo
ENTRYPOINT ["/opt/lollo-rozzo/lolloctl"]
