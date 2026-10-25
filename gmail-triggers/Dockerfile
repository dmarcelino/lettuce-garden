# The gmail-triggers garden sidecar (see docs/gmail-triggers.md). Built by
# lettucectl via compose/gmail-triggers.yml; tests/run-sidecar.sh builds the
# `test` stage and runs it. Both pass the shared package as the build context
# named `garden-common`.
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY --from=garden-common . /tmp/garden-common
RUN pip install --root-user-action=ignore /tmp/garden-common && rm -rf /tmp/garden-common
COPY pyproject.toml ./
COPY src ./src
RUN pip install --root-user-action=ignore .

FROM base AS test
RUN pip install --root-user-action=ignore ".[test]"
COPY tests ./tests
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

FROM base
# Root on purpose: /data is a root-owned host path under the stack's state
# directory (the app-server runs as root). Compose drops every capability,
# so without CAP_DAC_OVERRIDE this process can write only what root owns and
# what is mounted, on a read-only root filesystem.
USER 0
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
CMD ["python", "-m", "gmail_triggers"]
