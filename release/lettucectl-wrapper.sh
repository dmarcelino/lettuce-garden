#!/usr/bin/env bash
# lettucectl for lettuce-garden @VERSION@: runs lettucectl from its published
# image, pinned by digest (release/README.md). publish.yml fills in the three
# values below and attaches the result to the release as `lettucectl`.
#
# It lives in <data>/bin/, and the directory above is the data directory
# (config.env, stacks.conf, stacks/...). Every host path is mounted into the
# container at the same path: Docker reads the paths lettucectl gives it on the
# host, and lettucectl checks this (check_mounts).
set -euo pipefail
VERSION=@VERSION@
IMAGE=@IMAGE@
REPOSITORY=@REPOSITORY@

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

self=$(readlink -f -- "${BASH_SOURCE[0]}")
bin=$(dirname -- "$self")
data=$(dirname -- "$bin")
install_hint="create the data directory once, owned by you, and put this file in its bin/:
  sudo install -d -o \"\$USER\" -g \"\$(id -gn)\" -m 700 /srv/lettuce-garden /srv/lettuce-garden/bin
  mv lettucectl /srv/lettuce-garden/bin/ && chmod 755 /srv/lettuce-garden/bin/lettucectl
  ln -s /srv/lettuce-garden/bin/lettucectl ~/.local/bin/lettucectl"
[[ ${bin##*/} == bin && $data != / && $data != "$HOME" ]] || die "this wrapper must be in <data directory>/bin/; $install_hint"
[[ -O $data && -w $data ]] || die "$data must be owned by you and writable; $install_hint"

# Run on the host by `self-upgrade`, with the new wrapper: its image must be
# pullable before it replaces the current one.
if [[ ${1:-} == _pull ]]; then
  exec docker pull -q "$IMAGE"
fi

sock=/var/run/docker.sock
if [[ -n ${DOCKER_HOST:-} ]]; then
  [[ $DOCKER_HOST == unix://* ]] || die "DOCKER_HOST must be a unix:// socket; lettucectl mounts it into its container"
  sock=${DOCKER_HOST#unix://}
fi
[[ -S $sock ]] || die "no Docker socket at $sock"

# --init: as PID 1, lettucectl would ignore the signal Docker forwards on
# Ctrl-C. --user: what it writes is yours. HOME: the Docker CLI and Buildx
# write their settings there, and nothing there outlives the command.
args=(run --rm -i --init --user "$(id -u):$(id -g)" --group-add "$(stat -c %g "$sock")"
  --tmpfs /tmp -e HOME=/tmp -e TERM -e TZ -e LETTUCE_DATA="$data"
  -v "$sock:$sock" -e DOCKER_HOST="unix://$sock"
  -v "$data:$data")
# A terminal only when stdout is one too, so a pipe gets clean output; with only
# stdin a terminal (`lettucectl apply | tee log`), lettucectl is told, so it
# still asks before it changes anything (stdin_is_terminal).
if [[ -t 0 && -t 1 ]]; then
  args+=(-t)
elif [[ -t 0 ]]; then
  args+=(-e LETTUCECTL_STDIN_TTY=1)
fi
# Relative paths in the arguments mean the same inside.
args+=(-w "$PWD")

# mount_same PATH -> mounts PATH (an existing directory) at the same path
mount_same() {
  local p
  p=$(realpath -e -- "$1" 2>/dev/null) || return 0
  [[ -d $p ]] || return 0
  [[ $p != *[:,]* ]] || die "$p cannot contain ':' or ','"
  args+=(-v "$p:$p")
}

# positional ARGS... -> the arguments that are not options, one per line
# (backup's --keep takes a value)
positional() {
  while (($#)); do
    case $1 in
      --keep) shift ;;
      -*) ;;
      *) printf '%s\n' "$1" ;;
    esac
    shift || true
  done
}

# mount_ro_outside_home PATH -> mounts PATH (a file or directory) read-only at
# the same path, unless it is under $HOME, which toolbox_host mounts whole
mount_ro_outside_home() {
  local p
  p=$(realpath -e -- "$1" 2>/dev/null) || return 0
  [[ $p != "$HOME" && $p != "$HOME"/* ]] || return 0
  [[ $p != *[:,]* ]] || die "$p cannot contain ':' or ','"
  args+=(-v "$p:$p:ro")
}

# toolbox_host -> what `toolbox` takes from the host: the home (read-only, for
# the files it mounts), the ssh agent, and the tokens and `secret` values that
# only the host's commands can give. Values go by name (-e NAME), never in argv.
toolbox_host() {
  local file line w name a val
  args+=(-v "$HOME:$HOME:ro" -e LETTUCECTL_HOST_HOME="$HOME" -e XDG_CONFIG_HOME -e CLAUDE_CONFIG_DIR -e CLAUDE_CODE_OAUTH_TOKEN)
  # The Claude config and the toolbox file may live outside the home; just them,
  # read-only, or lettucectl would not find them in its container.
  [[ -z ${CLAUDE_CONFIG_DIR:-} ]] || mount_ro_outside_home "$CLAUDE_CONFIG_DIR"
  mount_ro_outside_home "${XDG_CONFIG_HOME:-$HOME/.config}/lettucectl/toolbox"
  if [[ -n ${SSH_AUTH_SOCK:-} && -S $SSH_AUTH_SOCK ]]; then
    args+=(-v "$SSH_AUTH_SOCK:$SSH_AUTH_SOCK" -e SSH_AUTH_SOCK)
  fi
  if command -v gh >/dev/null; then
    LETTUCECTL_GH_TOKEN=$(gh auth token 2>/dev/null </dev/null) || LETTUCECTL_GH_TOKEN=""
    export LETTUCECTL_GH_TOKEN
    args+=(-e LETTUCECTL_GH_TOKEN)
  fi
  if [[ -z ${CLAUDE_CODE_OAUTH_TOKEN:-} ]] && command -v secret-tool >/dev/null; then
    LETTUCECTL_CLAUDE_TOKEN=$(secret-tool lookup service claude-toolbox 2>/dev/null </dev/null) || LETTUCECTL_CLAUDE_TOKEN=""
    export LETTUCECTL_CLAUDE_TOKEN
    args+=(-e LETTUCECTL_CLAUDE_TOKEN)
  fi
  # The toolbox file's `secret NAME COMMAND [ARG...]` lines; lettucectl checks
  # the rest of the file, and refuses a secret it gets no value for.
  file=${XDG_CONFIG_HOME:-$HOME/.config}/lettucectl/toolbox
  [[ -f $file ]] || return 0
  # The same checks as lettucectl's toolbox_personal, but before anything runs:
  # these commands run here, as you.
  [[ -O $file ]] || die "$file is not owned by you"
  (((8#$(stat -c %a "$file") & 8#022) == 0)) || die "$file is writable by others (chmod 600 $file)"
  while IFS= read -r line || [[ -n $line ]]; do
    read -ra w <<<"$line"
    [[ ${#w[@]} -ge 3 && ${w[0]} == secret && ${w[1]} =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    name=${w[1]}
    a=()
    for val in "${w[@]:2}"; do
      # shellcheck disable=SC2088 # matching a literal ~ from the toolbox file
      case $val in
        "~") a+=("$HOME") ;;
        "~/"*) a+=("$HOME/${val#"~/"}") ;;
        *) a+=("$val") ;;
      esac
    done
    val=$("${a[@]}" </dev/null) || die "$file: secret $name: '${a[*]}' failed"
    export "LETTUCECTL_SECRET_$name=$val"
    args+=(-e "LETTUCECTL_SECRET_$name")
  done <"$file"
}

case ${1:-} in
  backup)
    mapfile -t pos < <(positional "${@:2}")
    [[ -z ${pos[1]:-} ]] || mount_same "${pos[1]}"
    ;;
  receipts)
    mapfile -t pos < <(positional "${@:2}")
    [[ -z ${pos[1]:-} ]] || mount_same "$(dirname -- "${pos[1]}")"
    ;;
  toolbox) toolbox_host ;;
  self-upgrade)
    # The current image fetches the new wrapper and checks its sum; the new
    # wrapper pulls its image; only then does it replace this one, which is
    # kept as lettucectl.previous to go back to.
    (($# == 2)) || die "usage: lettucectl self-upgrade vX.Y.Z"
    docker "${args[@]}" "$IMAGE" _fetch-wrapper "$2"
    bash "$bin/.lettucectl.new" _pull >/dev/null || {
      rm -f "$bin/.lettucectl.new"
      die "could not pull the image of release $2; this install is unchanged"
    }
    cp -p -- "$self" "$bin/lettucectl.previous"
    chmod 755 "$bin/.lettucectl.new"
    mv -f -- "$bin/.lettucectl.new" "$bin/lettucectl"
    printf 'lettuce-garden %s -> %s (the previous wrapper is %s)\n' "$VERSION" "$2" "$bin/lettucectl.previous" >&2
    exec "$bin/lettucectl" _after-upgrade
    ;;
  wrapper-version)
    printf 'lettuce-garden %s, %s, from github.com/%s\n' "$VERSION" "$IMAGE" "$REPOSITORY"
    exit 0
    ;;
esac

exec docker "${args[@]}" "$IMAGE" "$@"
