#!/bin/bash
# Starts the code editor (docs/code-server.md), then becomes code-server with the
# arguments compose passes. Before that, on every start:
# - SSH keys for the shell container: the editor's client key (/data, only here)
#   and the shell's host key (/keys, which the shell mounts read-only), each made
#   once; authorized_keys and known_hosts are rewritten from them, so either
#   volume can be recreated on its own.
# - The fixed settings (settings.json beside this script) replace the person's
#   of the same name, whole: Restricted Mode, the extension allowed in it, the
#   terminal profiles that lead to the shell. The person can change them in
#   the UI; a restart puts them back, with nothing added inside them.
# - A socket left by the last run is removed: code-server listens only on
#   /run/code-server/editor.sock, which only the gate mounts besides it.
# - SHELL is the terminal profile, like root's login shell (Dockerfile): VS Code
#   opens it when no setting names a profile (a new Settings Profile has none).
#   VSCODE_CLI=1 keeps code-server from running it to read its environment, which
#   would open a session in the shell container on every extension host start.
# The keys and settings are written with umask 077; code-server runs with 022,
# so what the person creates in the workspaces is readable like agents' files.
set -euo pipefail
umask 077

HERE=/opt/lettuce-editor
DATA=/data
KEYS=/keys
USER_SETTINGS=$DATA/user/User/settings.json

mkdir -p "$DATA/ssh" "$DATA/home" "$(dirname "$USER_SETTINGS")"
[[ -f $DATA/ssh/id_ed25519 ]] ||
  ssh-keygen -q -t ed25519 -N '' -C garden-code-server-editor -f "$DATA/ssh/id_ed25519"
[[ -f $KEYS/ssh_host_ed25519_key ]] ||
  ssh-keygen -q -t ed25519 -N '' -C garden-code-server-exec -f "$KEYS/ssh_host_ed25519_key"

# write FILE: stdin to FILE through a temp file, so a reader never sees half of it
write() {
  local tmp
  tmp=$(mktemp "$1.XXXXXX")
  cat >"$tmp"
  mv -f "$tmp" "$1"
}
write "$KEYS/authorized_keys" <"$DATA/ssh/id_ed25519.pub"
printf 'garden-code-server-exec %s\n' "$(cut -d' ' -f1,2 "$KEYS/ssh_host_ed25519_key.pub")" | write "$DATA/ssh/known_hosts"

if [[ -s $USER_SETTINGS ]] && ! jq -e 'type == "object"' "$USER_SETTINGS" >/dev/null 2>&1; then
  # Comments or trailing commas (VS Code accepts them, jq doesn't): keep the file aside.
  mv -f "$USER_SETTINGS" "$USER_SETTINGS.unreadable"
  echo "lettuce-editor: settings.json was not plain JSON; kept as settings.json.unreadable" >&2
fi
if [[ -s $USER_SETTINGS ]]; then
  jq -s '.[0] + .[1]' "$USER_SETTINGS" "$HERE/settings.json" | write "$USER_SETTINGS"
else
  write "$USER_SETTINGS" <"$HERE/settings.json"
fi

rm -f /run/code-server/editor.sock

export HOME=$DATA/home XDG_CONFIG_HOME=$DATA/home/.config XDG_DATA_HOME=$DATA/home/.local/share
export SHELL=$HERE/bin/exec-shell VSCODE_CLI=1
umask 022
exec /opt/code-server/bin/code-server "$@"
