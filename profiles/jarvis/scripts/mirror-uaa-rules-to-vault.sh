#!/usr/bin/env bash
set -euo pipefail
SHARED="/home/frank/.hermes/scripts/mirror_uaa_rules_to_vault.py"
exec /usr/bin/python3 "$SHARED" "$@"
