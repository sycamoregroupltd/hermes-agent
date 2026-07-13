#!/usr/bin/env bash
# sycode-token-safe-curl.sh
# Safe curl wrapper: passes X-Sycode-Token via a mode-600 tempfile curl config.
# Usage: sycode-token-safe-curl.sh [curl args...]
set -euo pipefail

TOKEN="${SYCODE_READ_TOKEN:?SYCODE_READ_TOKEN not set}"
TMPCFG=$(mktemp /tmp/sycode-curl-XXXXXX.conf)
trap 'rm -f "$TMPCFG"' EXIT
chmod 600 "$TMPCFG"
printf 'header = "X-Sycode-Token: %s"\n' "$TOKEN" > "$TMPCFG"
exec curl -K "$TMPCFG" "$@"