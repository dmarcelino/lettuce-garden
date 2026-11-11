# syntax=docker/dockerfile:1
# The lettucectl image (release/README.md): the CLI and every file it reads at
# run time, plus the two build contexts built on the operator's host (doc-tools,
# code-server/exec). Built by publish.yml from the repo root, after it has
# written RELEASE; never run but through the wrapper (lettucectl-wrapper.sh).
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
COPY --chmod=0755 lettucectl /opt/lettuce-garden/lettucectl
COPY config.env.example RELEASE /opt/lettuce-garden/
COPY lib /opt/lettuce-garden/lib
COPY compose /opt/lettuce-garden/compose
COPY agent-skills /opt/lettuce-garden/agent-skills
COPY release/images /opt/lettuce-garden/release/images
COPY scripts/dns-preflight.sh /opt/lettuce-garden/scripts/dns-preflight.sh
COPY garden/executor/run.mjs /opt/lettuce-garden/garden/executor/run.mjs
COPY doc-tools /opt/lettuce-garden/doc-tools
COPY code-server/exec /opt/lettuce-garden/code-server/exec
RUN chmod -R a+rX,go-w /opt/lettuce-garden
ENTRYPOINT ["/opt/lettuce-garden/lettucectl"]
