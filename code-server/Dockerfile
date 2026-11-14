# The code editor's gate (see docs/code-server.md): the lollo sidecar in front of
# lollo-code-server-editor. Built by lolloctl via compose/code-server.yml;
# tests/run-sidecar.sh builds the `test` stage and runs it. Both pass the shared
# package as the build context named `lollo-common`. The editor's and the shell's
# images live in editor/ and exec/, outside this build context.
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY --from=lollo-common . /tmp/lollo-common
RUN pip install --root-user-action=ignore /tmp/lollo-common && rm -rf /tmp/lollo-common
COPY pyproject.toml ./
COPY src ./src
RUN pip install --root-user-action=ignore .

FROM base AS test
RUN pip install --root-user-action=ignore ".[test]"
COPY tests ./tests
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

FROM base
# It holds no state and needs no capability; any uid would do.
USER 65534
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
CMD ["python", "-m", "code_server_gate"]
