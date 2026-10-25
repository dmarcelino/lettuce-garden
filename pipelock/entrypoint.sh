#!/bin/sh
# Creates the receipt-signing key on the first start, then runs the proxy.
# The key is never replaced: receipts already written are verified against it.
set -eu
umask 077

CONFIG=/etc/pipelock/pipelock.yaml
KEY=/data/keys/receipt-signing.json

mkdir -p /data/keys /data/evidence
if [ ! -e "$KEY" ]; then
  pipelock signing key generate --purpose receipt-signing --out "$KEY"
fi
# The public half, for `pipelock verify-receipt --key`. Derived, so rewriting
# it is harmless and repairs a missing or stale copy.
pipelock signing pubkey --key-file "$KEY" --out "$KEY.pub" >/dev/null

exec pipelock run --config "$CONFIG" --listen 0.0.0.0:8888 "$@"
