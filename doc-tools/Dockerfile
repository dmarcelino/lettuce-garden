# The document tools (see docs/doc-tools.md): OfficeAgent.NET and the PDF MCP,
# with their own Python, under /opt/lettuce-doc-tools. lettucectl builds it
# with plain `docker build` as lettuce-doc-tools:<content hash> and every stack
# with the doc-tools profile mounts it read-only into its app-server
# (compose/doc-tools.yml): one copy per host, never run as a container.
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
 && mkdir -p /opt/lettuce-doc-tools/office \
 && tar -xzf /tmp/oa.tgz -C /opt/lettuce-doc-tools/office --no-same-owner --strip-components=1 \
 && rm /tmp/oa.tgz \
 && test -x /opt/lettuce-doc-tools/office/officeagent-mcp

# The interpreter lives inside /opt/lettuce-doc-tools: the venv records its
# absolute path, and the app-server sees only that directory.
FROM base AS python
ARG PYTHON_VERSION
RUN UV_PYTHON_INSTALL_DIR=/opt/lettuce-doc-tools/python uv python install "$PYTHON_VERSION" \
 && uv venv --python "$(UV_PYTHON_INSTALL_DIR=/opt/lettuce-doc-tools/python uv python find "$PYTHON_VERSION")" \
      /opt/lettuce-doc-tools/venv
WORKDIR /src
COPY requirements.lock ./
RUN uv pip install --python /opt/lettuce-doc-tools/venv --require-hashes -r requirements.lock
COPY pyproject.toml ./
COPY src ./src
RUN uv pip install --python /opt/lettuce-doc-tools/venv --no-deps . \
 && find /opt/lettuce-doc-tools -name __pycache__ -prune -exec rm -rf {} +

FROM python AS test
RUN uv pip install --python /opt/lettuce-doc-tools/venv pytest==9.1.1
COPY tests ./tests
ENV PATH=/opt/lettuce-doc-tools/venv/bin:$PATH
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

FROM base AS assemble
COPY --from=office /opt/lettuce-doc-tools/office /opt/lettuce-doc-tools/office
COPY --from=python /opt/lettuce-doc-tools/python /opt/lettuce-doc-tools/python
COPY --from=python /opt/lettuce-doc-tools/venv /opt/lettuce-doc-tools/venv
COPY office/config.json /opt/lettuce-doc-tools/office/config.json
COPY bin /opt/lettuce-doc-tools/bin
COPY lib /opt/lettuce-doc-tools/lib
RUN chmod -R a+rX,go-w /opt/lettuce-doc-tools && chmod 0755 /opt/lettuce-doc-tools/bin/*

# Only the tree, at the image root: mounted at /opt/lettuce-doc-tools.
FROM scratch
COPY --from=assemble /opt/lettuce-doc-tools/ /
