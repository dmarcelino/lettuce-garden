#!/bin/bash
# Starts the code editor (docs/code-server.md), then becomes code-server with the
# arguments compose passes. Before that, on every start:
# - SSH keys for the shell container: the editor's client key (/data, only here)
#   and the shell's host key (/keys, which the shell mounts read-only), each made
#   once; authorized_keys and known_hosts are rewritten from them, so either
#   volume can be recreated on its own.
# - The fixed settings (settings.json beside this script) are merged over the
#   person's: Restricted Mode, the extension allowed in it, the terminal
#   profiles that lead to the shell. The person can change them in
#   the UI; a restart puts them back.
set -euo pipefail
umask 077

HERE=/opt/lettuce-editor
DATA=/data
KEYS=/keys
USER_SETTINGS=$DATA/user/User/settings.json

mkdir -p "$DATA/ssh" "$DATA/home" "$(dirname "$USER_SETTINGS")"
[[ -f $DATA/ssh/id_ed25519 ]] ||
  ssh-keygen -q -t ed25519 -N '' -C code-server-editor -f "$DATA/ssh/id_ed25519"
[[ -f $KEYS/ssh_host_ed25519_key ]] ||
  ssh-keygen -q -t ed25519 -N '' -C code-server-exec -f "$KEYS/ssh_host_ed25519_key"

# write FILE: stdin to FILE through a temp file, so a reader never sees half of it
write() {
  local tmp
  tmp=$(mktemp "$1.XXXXXX")
  cat >"$tmp"
  mv -f "$tmp" "$1"
}
write "$KEYS/authorized_keys" <"$DATA/ssh/id_ed25519.pub"
printf 'code-server-exec %s\n' "$(cut -d' ' -f1,2 "$KEYS/ssh_host_ed25519_key.pub")" | write "$DATA/ssh/known_hosts"

if [[ -s $USER_SETTINGS ]] && ! jq -e 'type == "object"' "$USER_SETTINGS" >/dev/null 2>&1; then
  # Comments or trailing commas (VS Code accepts them, jq doesn't): keep the file aside.
  mv -f "$USER_SETTINGS" "$USER_SETTINGS.unreadable"
  echo "lettuce-editor: settings.json was not plain JSON; kept as settings.json.unreadable" >&2
fi
if [[ -s $USER_SETTINGS ]]; then
  jq -s '.[0] * .[1]' "$USER_SETTINGS" "$HERE/settings.json" | write "$USER_SETTINGS"
else
  write "$USER_SETTINGS" <"$HERE/settings.json"
fi

export HOME=$DATA/home XDG_CONFIG_HOME=$DATA/home/.config XDG_DATA_HOME=$DATA/home/.local/share
exec /opt/code-server/bin/code-server "$@"
