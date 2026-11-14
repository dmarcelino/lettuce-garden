# The landing service (the landing page at the root of the lollo's host and the
# agent list; landing/DESIGN.md). Built by lolloctl via
# compose/landing.yml; tests/run-sidecar.sh builds the `test` stage and runs it.
# Both pass the shared package as the build context named `lollo-common`. The
# executor (executor/) runs in the app-server, not in this image.
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
# It holds no state (the agent list lives in memory) and needs no capability; any uid would do.
USER 65534
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
CMD ["python", "-m", "lollo_landing"]
