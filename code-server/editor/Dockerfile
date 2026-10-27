# The code editor's image (docs/code-server.md): code-server, pinned by version and
# SHA-256 per architecture, with its built-in extensions pruned to
# extensions-kept, and nothing to run workspace code with: no node on PATH (code-server's
# own sits under /opt/code-server/lib), no npm, no compilers, no shell profiles to
# detect (/etc/shells is empty). openssh-client carries the terminal to the
# shell container. Built by lettucectl via compose/code-server.yml;
# tests/run-sidecar.sh code-server builds the `test` stage and runs it.
FROM debian:12-slim AS fetch
ARG CODE_SERVER_VERSION=4.143.1
ARG SHA256_AMD64=614877cf71005f14ea252bd2d9b1c12237cf7d73681ef150f0dff7f13849fdb5
ARG SHA256_ARM64=51481a891754dc2585b86d007aec24ac675453fc5ef6cfa346c4462449277384
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl jq \
  && rm -rf /var/lib/apt/lists/*
COPY extensions-kept /tmp/extensions-kept
RUN set -eu; \
  case "$TARGETARCH" in amd64) sum=$SHA256_AMD64 ;; arm64) sum=$SHA256_ARM64 ;; *) echo "unsupported: $TARGETARCH" >&2; exit 1 ;; esac; \
  curl -fsSLo /tmp/cs.tgz "https://github.com/coder/code-server/releases/download/v$CODE_SERVER_VERSION/code-server-$CODE_SERVER_VERSION-linux-$TARGETARCH.tar.gz"; \
  echo "$sum  /tmp/cs.tgz" | sha256sum -c -; \
  mkdir /opt/code-server; \
  tar -xzf /tmp/cs.tgz -C /opt/code-server --strip-components=1 --no-same-owner; \
  rm /tmp/cs.tgz; \
  cd /opt/code-server/lib/vscode/extensions; \
  for d in */; do \
    d=${d%/}; keep=; \
    while read -r pattern; do \
      case $pattern in ''|'#'*) continue ;; esac; \
      case $d in $pattern) keep=1 ;; esac; \
    done </tmp/extensions-kept; \
    [ -n "$keep" ] || rm -rf "$d"; \
  done
# The Markdown preview renders agents' files in a webview on the stack's origin; its
# own policy (Strict: no script but its own) is what keeps their HTML from running.
# Strict is pinned, so the security picker's "Disable" (no policy at all) does
# nothing, and the Markdown Editor (a second, richer webview that VS Code itself
# only opens in an untrusted workspace after an "Open Anyway") is removed. Both
# edits fail the build when this release's code does not match them.
RUN set -eu; \
  md=/opt/code-server/lib/vscode/extensions/markdown-language-features; \
  js=$md/dist/extension.js; \
  anchor='getSecurityLevelForResource\([A-Za-z_$][A-Za-z0-9_$]*\)\{'; \
  n=$(grep -oE "$anchor" "$js" | wc -l); \
  [ "$n" = 1 ] || { echo "markdown: expected one getSecurityLevelForResource, found $n" >&2; exit 1; }; \
  sed -Ei "s/($anchor)/\1return 0;/" "$js"; \
  grep -qE 'getSecurityLevelForResource\([A-Za-z_$][A-Za-z0-9_$]*\)\{return 0;' "$js"; \
  jq -e '[.contributes.customEditors[] | select(.viewType == "vscode.markdown.editor")] | length == 1' "$md/package.json" >/dev/null \
    || { echo "markdown: the Markdown Editor contribution moved" >&2; exit 1; }; \
  jq '.contributes.customEditors |= map(select(.viewType != "vscode.markdown.editor"))' "$md/package.json" >/tmp/package.json; \
  mv /tmp/package.json "$md/package.json"; \
  rm -rf "$md/markdown-editor-out" "$md/markdown-editor-src"

FROM debian:12-slim AS base
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client jq \
  && rm -rf /var/lib/apt/lists/* \
  && : >/etc/shells
COPY --from=fetch /opt/code-server /opt/code-server
COPY settings.json ssh_config extensions-kept entrypoint.sh /opt/lettuce-editor/
COPY bin /opt/lettuce-editor/bin
# Points --extensions-dir somewhere nothing can be installed (the root is read-only);
# the empty list keeps code-server from logging its absence on every scan.
RUN mkdir /opt/lettuce-editor/no-extensions && echo '[]' >/opt/lettuce-editor/no-extensions/extensions.json

FROM base AS test
COPY tests /tests
CMD ["bash", "/tests/test.sh"]

FROM base
# Root on purpose: agents write the workspaces as root. Compose drops every
# capability, so without CAP_DAC_OVERRIDE this process edits only what root owns.
USER 0
# No port: compose/code-server.yml runs it on a Unix socket only the gate mounts.
ENTRYPOINT ["/opt/lettuce-editor/entrypoint.sh"]
