#!/bin/bash
# Tests of the editor image, run in its `test` stage (tests/run-sidecar.sh
# code-server), as root and without the network. Each test_* function runs in a
# subshell; ssh is a fake that records what the terminal sends, and the real
# code-server binary is swapped for a fake before the entrypoint tests.
set -uo pipefail

EXT=/opt/code-server/lib/vscode/extensions
HERE=/opt/lettuce-editor
FAKES=$(mktemp -d)
passed=0 failed=0

fail() {
  echo "    $*" >&2
  exit 1
}
eq() { [[ $1 == "$2" ]] || fail "expected [$2], got [$1]"; }

# A fake ssh: records its arguments, one per line, and the remote command alone.
cat >"$FAKES/ssh" <<'EOF'
#!/bin/bash
printf '%s\n' "$@" >"$FAKE_LOG"
printf '%s' "${!#}" >"$FAKE_LOG.command"
EOF
chmod +x "$FAKES/ssh"
# What the remote shell sees: agent-env that runs its command, and an env that
# prints where it runs and its arguments, one per line.
mkdir -p "$FAKES/remote"
cat >"$FAKES/remote/agent-env" <<'EOF'
#!/bin/bash
exec "$@"
EOF
cat >"$FAKES/remote/env" <<'EOF'
#!/bin/bash
printf 'pwd=%s\n' "$PWD"
printf '[%s]\n' "$@"
EOF
chmod +x "$FAKES/remote/agent-env" "$FAKES/remote/env"

# remote: runs the recorded command the way sshd does (bash -c), with the fakes on PATH
remote() { PATH=$FAKES/remote:$PATH TERM=xterm-256color bash -c "$(cat "$FAKE_LOG.command")"; }

test_only_the_listed_extensions_are_kept() {
  local d pattern keep
  for d in "$EXT"/*/; do
    d=$(basename "$d")
    keep=
    while read -r pattern; do
      case $pattern in '' | '#'*) continue ;; esac
      # shellcheck disable=SC2254 # the patterns are globs (theme-*)
      case $d in $pattern) keep=1 ;; esac
    done <"$HERE/extensions-kept"
    [[ -n $keep ]] || fail "$d is not in extensions-kept"
  done
  for d in ms-vscode.js-debug npm gulp markdown-math ipynb simple-browser copilot terminal-suggest; do
    [[ ! -e $EXT/$d ]] || fail "$d is still there"
  done
  # A listed name that a release renamed would silently drop a feature.
  while read -r pattern; do
    case $pattern in '' | '#'* | *'*'*) continue ;; esac
    [[ -d $EXT/$pattern ]] || fail "$pattern is listed but not in the release"
  done <"$HERE/extensions-kept"
  [[ -f $EXT/node_modules/typescript/lib/tsserver.js ]] || fail "the bundled TypeScript is missing"
}

test_the_markdown_preview_stays_strict_and_has_no_markdown_editor() {
  local md=$EXT/markdown-language-features
  grep -qE 'getSecurityLevelForResource\([A-Za-z_$][A-Za-z0-9_$]*\)\{return 0;' "$md/dist/extension.js" ||
    fail "the preview's security level is not pinned to Strict"
  eq "$(jq -r '[.contributes.customEditors[].viewType] | join(" ")' "$md/package.json")" vscode.markdown.preview.editor
  [[ ! -e $md/markdown-editor-out ]] || fail "the Markdown Editor's bundle is still there"
  jq -e '.contributes.commands | map(.command) | index("markdown.showPreviewToSide")' "$md/package.json" >/dev/null ||
    fail "no preview command"
}

test_nothing_to_run_workspace_code_with() {
  local c
  for c in node nodejs npm npx python3 python gcc cc make; do
    ! command -v "$c" >/dev/null || fail "$c is on PATH"
  done
  [[ ! -s /etc/shells ]] || fail "/etc/shells lists shells VS Code would offer as profiles"
}

test_code_server_runs_from_the_image() {
  /opt/code-server/bin/code-server --version | grep -q '^4\.143\.1 ' || fail "unexpected version"
}

test_the_terminal_prints_the_notice_and_opens_bash_through_agent_env() {
  local out
  mkdir -p /work/agent-2
  cd /work/agent-2 || exit 1
  out=$(FAKE_LOG=$FAKES/term.log PATH=$FAKES:$PATH "$HERE/bin/exec-shell")
  grep -q "never store tokens or passwords here" <<<"$out" || fail "no notice"
  eq "$(sed -n '1,4p' "$FAKES/term.log" | tr '\n' ' ')" "-F /opt/lettuce-editor/ssh_config -tt code-server-exec "
  out=$(cd / && FAKE_LOG=$FAKES/term.log remote)
  eq "$out" "pwd=/work/agent-2
[TERM=xterm-256color]
[bash]
[-i]"
}

test_the_terminal_opens_in_any_directory_name() {
  local dir="/work/agent 3/it's \$HOME \"x\"" out
  mkdir -p "$dir"
  cd "$dir" || exit 1
  FAKE_LOG=$FAKES/term3.log PATH=$FAKES:$PATH "$HERE/bin/exec-shell" >/dev/null
  out=$(cd / && FAKE_LOG=$FAKES/term3.log remote)
  eq "$(head -1 <<<"$out")" "pwd=$dir"
}

test_the_terminal_opens_in_work_from_a_folder_the_shell_lacks() {
  local d out
  mkdir -p /work /data/home /tmp/workspace
  for d in /data/home /tmp/workspace /; do
    cd "$d" || exit 1
    out=$(FAKE_LOG=$FAKES/term4.log PATH=$FAKES:$PATH "$HERE/bin/exec-shell")
    grep -q "starting in /work" <<<"$out" || fail "$d: no word that the terminal starts in /work"
    out=$(cd / && FAKE_LOG=$FAKES/term4.log remote)
    eq "$(head -1 <<<"$out")" "pwd=/work"
  done
  cd /work || exit 1
  out=$(FAKE_LOG=$FAKES/term4.log PATH=$FAKES:$PATH "$HERE/bin/exec-shell")
  ! grep -q "starting in" <<<"$out" || fail "/work itself is not a folder the shell lacks"
}

test_the_terminal_passes_the_exit_status() {
  mkdir -p "$FAKES/seven"
  printf '#!/bin/bash\nexit 7\n' >"$FAKES/seven/ssh"
  chmod +x "$FAKES/seven/ssh"
  local rc=0
  PATH=$FAKES/seven:$PATH "$HERE/bin/exec-shell" >/dev/null || rc=$?
  eq "$rc" 7
}

# The entrypoint, with the real code-server swapped for a fake that records its arguments.
start_editor() {
  /opt/lettuce-editor/entrypoint.sh --auth none /work >/dev/null
}

test_code_server_starts_with_the_usual_umask_and_no_stale_socket() {
  mkdir -p /run/code-server /data /keys
  : >/run/code-server/editor.sock
  start_editor
  # The keys are private; what the person creates in the workspaces is not.
  eq "$(cat "$FAKES/umask")" 0022
  [[ ! -e /run/code-server/editor.sock ]] || fail "a stale socket was left for code-server to trip on"
}

test_the_entrypoint_makes_keys_once_and_trusts_only_its_own() {
  mkdir -p /data /keys
  start_editor || fail "first start failed"
  local client host
  client=$(cat /data/ssh/id_ed25519.pub)
  host=$(cat /keys/ssh_host_ed25519_key.pub)
  eq "$(cat /keys/authorized_keys)" "$client"
  eq "$(cat /data/ssh/known_hosts)" "code-server-exec $(cut -d' ' -f1,2 <<<"$host")"
  eq "$(stat -c %a /data/ssh/id_ed25519 /keys/ssh_host_ed25519_key | tr '\n' ' ')" "600 600 "
  [[ ! -e /keys/id_ed25519 ]] || fail "the client key reached the shell's volume"
  start_editor
  eq "$(cat /data/ssh/id_ed25519.pub)" "$client"
  eq "$(cat /keys/ssh_host_ed25519_key.pub)" "$host"
  # Either volume recreated on its own: the other side follows.
  rm /keys/*
  start_editor
  eq "$(cat /keys/authorized_keys)" "$client"
  eq "$(cut -d' ' -f2,3 /data/ssh/known_hosts)" "$(cut -d' ' -f1,2 /keys/ssh_host_ed25519_key.pub)"
  eq "$(cat "$FAKES/started")" "--auth none /work"
}

test_the_fixed_settings_come_back_and_the_persons_stay() {
  local s=/data/user/User/settings.json
  start_editor
  jq '. + {"terminal.integrated.defaultProfile.linux": "bash", "editor.fontSize": 15}
    | .["terminal.integrated.profiles.linux"].bash = {"path": "bash"}
    | .["terminal.integrated.profiles.linux"].local = {"path": "/bin/bash"}
    | .["extensions.supportUntrustedWorkspaces"]["vscode.npm"] = {"supported": true}' "$s" >"$s.new"
  mv "$s.new" "$s"
  start_editor
  eq "$(jq -r '."terminal.integrated.defaultProfile.linux"' "$s")" shell
  # Each fixed setting comes back whole: nothing added inside one survives a restart.
  eq "$(jq -c '."terminal.integrated.profiles.linux"' "$s")" "$(jq -c '."terminal.integrated.profiles.linux"' "$HERE/settings.json")"
  eq "$(jq -c '."extensions.supportUntrustedWorkspaces"' "$s")" "$(jq -c '."extensions.supportUntrustedWorkspaces"' "$HERE/settings.json")"
  eq "$(jq -r '."editor.fontSize"' "$s")" 15
  eq "$(jq -r '."security.workspace.trust.enabled"' "$s")" true
  eq "$(jq -r '."chat.disableAIFeatures"' "$s")" true
  printf '{\n  // a comment\n  "editor.fontSize": 12,\n}\n' >"$s"
  start_editor 2>/dev/null
  [[ -f $s.unreadable ]] || fail "the unreadable file was not kept"
  eq "$(jq -S . "$s")" "$(jq -S . "$HERE/settings.json")"
}

# Swapped in after test_code_server_runs_from_the_image.
fake_code_server() {
  mv /opt/code-server/bin/code-server /opt/code-server/bin/code-server.real
  printf '#!/bin/bash\nprintf "%%s" "$*" >%q\numask >%q\n' "$FAKES/started" "$FAKES/umask" >/opt/code-server/bin/code-server
  chmod +x /opt/code-server/bin/code-server
}

run() {
  local rc
  # A bare statement on purpose: inside `if`, `||` or `&&` bash ignores errexit
  # in the subshell too, and every assert relies on it to stop the test.
  (set -e; "$1")
  rc=$?
  if ((rc == 0)); then
    echo "ok    $1"
    passed=$((passed + 1))
  else
    echo "FAIL  $1"
    failed=$((failed + 1))
  fi
}

run test_code_server_runs_from_the_image
fake_code_server
for t in $(declare -F | awk '{print $3}' | grep '^test_' | grep -v '^test_code_server_runs'); do
  run "$t"
done
echo
echo "$passed passed, $failed failed"
((failed == 0))
