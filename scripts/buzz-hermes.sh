#!/usr/bin/env bash
# buzz CLI wrapper for the hermes-acp Buzz peer.
#
# Why this exists: the harness exports BUZZ_PRIVATE_KEY for buzz-acp itself, but
# Hermes's terminal tool builds a fresh sandboxed environment, so a bare `buzz`
# call inside a tool sees no credential and dies with
#   {"error":"auth_error","message":"auth error: BUZZ_PRIVATE_KEY is required"}
#
# It also fixes a second mismatch: buzz-acp needs BUZZ_RELAY_URL=ws://…, while
# the buzz CLI speaks http://. Exporting one breaks the other, so the CLI URL is
# set here rather than in the shared environment.
#
# The secret is read from the identity file at call time and never stored in any
# environment, unit file, or .env.
set -euo pipefail

KEY_FILE=/home/frank/buzz-bridge-pilot/state/identities/hermes.key

if [[ ! -r "$KEY_FILE" ]]; then
  echo "buzz-hermes: identity unreadable: $KEY_FILE" >&2
  exit 3
fi

export BUZZ_PRIVATE_KEY
BUZZ_PRIVATE_KEY="$(tr -d '[:space:]' < "$KEY_FILE")"
export BUZZ_RELAY_URL="http://localhost:3030"

exec /home/frank/.local/bin/buzz "$@"
