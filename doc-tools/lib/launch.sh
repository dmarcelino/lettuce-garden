# shellcheck shell=sh
# Sourced by the launchers in bin/. A server runs for one `letta mcp call`, in
# the calling agent's shell directory; its workspace is the /work/<agent-id>
# that directory is in. Anywhere else, it refuses to start. letta-code passes
# the server only HOME, PATH, SHELL, TERM, USER and LOGNAME (no PWD), hence
# `pwd -P`, which also resolves symlinks.
# HOME and the caches move to /tmp: `letta mcp` sets HOME to Lettuce's
# mcp-home, which these tools must not write into.
# shellcheck disable=SC2034 # used by the launchers that source this
DOC_TOOLS=/opt/lettuce-doc-tools

doc_tools_env() {
  # No core files: a crash dump holds document content, and the host's
  # core_pattern may hand it to a crash reporter outside the container.
  # shellcheck disable=SC3045 # dash, bash and busybox sh all have it
  ulimit -c 0
  cwd=$(pwd -P)
  case $cwd in
    /work/*)
      id=${cwd#/work/}
      id=${id%%/*}
      ;;
    *) id= ;;
  esac
  case $id in
    '' | . | ..)
      echo "doc-tools: run from an agent workspace under /work/<agent-id> (cwd is $cwd)" >&2
      exit 2
      ;;
  esac
  WORKSPACE=/work/$id
  base=/tmp/lettuce-doc-tools
  (umask 077 && mkdir -p "$base/home" "$base/cache" "$base/tmp")
  export HOME="$base/home" XDG_CACHE_HOME="$base/cache" TMPDIR="$base/tmp" WORKSPACE
}
