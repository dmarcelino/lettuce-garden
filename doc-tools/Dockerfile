# The document tools (see docs/doc-tools.md): OfficeAgent.NET and the PDF MCP,
# with their own Python, under /opt/lollo-doc-tools. lolloctl builds it
# with plain `docker build` as lollo-doc-tools:<content hash> and copies the
# tree into the volume lollo-doc-tools-<content hash> (ensure_doc_tools_volume),
# which every stack with the lollo-doc-tools profile mounts read-only into its
# app-server (compose/doc-tools.yml): one copy per host, never run as a container.
# `tests/run-sidecar.sh doc-tools` builds the `test` stage and runs it.
#
# Upgrading OfficeAgent: change OFFICEAGENT_VERSION and OFFICEAGENT_SHA256 (from
# the release's SHA256SUMS, checked against `gh attestation verify`).
ARG OFFICEAGENT_VERSION=1.2.0
ARG OFFICEAGENT_SHA256=c1d61b37d7bada1e93a004643a36ea8fa59eed8d1329913b6ce0fd8649dd5cb0
ARG PYTHON_VERSION=3.12.15

FROM ghcr.io/astral-sh/uv:0.13.5 AS uv

# Debian 12, like the app-server: what runs here runs there.
FROM debian:12-slim AS base
ENV PIP_NO_CACHE_DIR=1 UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=manual UV_LINK_MODE=copy PYTHONDONTWRITEBYTECODE=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv

FROM base AS office
ARG OFFICEAGENT_VERSION
ARG OFFICEAGENT_SHA256
RUN [ "$(dpkg --print-architecture)" = amd64 ] || { echo "doc-tools: OfficeAgent is pinned for x86_64 only" >&2; exit 1; }
RUN curl -fsSL -o /tmp/oa.tgz \
      "https://github.com/ilia-sokolov/OfficeAgent.NET/releases/download/v${OFFICEAGENT_VERSION}/officeagent-mcp-linux-x64.tar.gz" \
 && echo "${OFFICEAGENT_SHA256}  /tmp/oa.tgz" | sha256sum -c - \
 && mkdir -p /opt/lollo-doc-tools/office \
 && tar -xzf /tmp/oa.tgz -C /opt/lollo-doc-tools/office --no-same-owner --strip-components=1 \
 && rm /tmp/oa.tgz \
 && test -x /opt/lollo-doc-tools/office/officeagent-mcp

# The interpreter lives inside /opt/lollo-doc-tools: the venv records its
# absolute path, and the app-server sees only that directory.
FROM base AS python
ARG PYTHON_VERSION
RUN UV_PYTHON_INSTALL_DIR=/opt/lollo-doc-tools/python uv python install "$PYTHON_VERSION" \
 && uv venv --python "$(UV_PYTHON_INSTALL_DIR=/opt/lollo-doc-tools/python uv python find "$PYTHON_VERSION")" \
      /opt/lollo-doc-tools/venv
WORKDIR /src
COPY requirements.lock ./
RUN uv pip install --python /opt/lollo-doc-tools/venv --require-hashes -r requirements.lock
COPY pyproject.toml ./
COPY src ./src
RUN uv pip install --python /opt/lollo-doc-tools/venv --no-deps . \
 && find /opt/lollo-doc-tools -name __pycache__ -prune -exec rm -rf {} +

FROM python AS test
RUN uv pip install --python /opt/lollo-doc-tools/venv pytest==9.1.1
COPY tests ./tests
ENV PATH=/opt/lollo-doc-tools/venv/bin:$PATH
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

FROM base AS assemble
COPY --from=office /opt/lollo-doc-tools/office /opt/lollo-doc-tools/office
COPY --from=python /opt/lollo-doc-tools/python /opt/lollo-doc-tools/python
COPY --from=python /opt/lollo-doc-tools/venv /opt/lollo-doc-tools/venv
COPY office/config.json /opt/lollo-doc-tools/office/config.json
COPY bin /opt/lollo-doc-tools/bin
COPY lib /opt/lollo-doc-tools/lib
RUN chmod -R a+rX,go-w /opt/lollo-doc-tools && chmod 0755 /opt/lollo-doc-tools/bin/*

# Only the tree, at the path the app-servers mount it: never run, only copied
# into the volume they share (ensure_doc_tools_volume in lolloctl).
FROM scratch
COPY --from=assemble /opt/lollo-doc-tools/ /opt/lollo-doc-tools/
