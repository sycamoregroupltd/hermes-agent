#!/usr/bin/env bash
# Focused RED regression for rc=0/no-terminal-signal worker exits.
# It feeds fixture payloads directly to catch-silent-exit.sh; no live kanban DB,
# provider routing, credentials, or network calls are required.
#
# Run standalone:         bash agent-hooks/catch-silent-exit.selftest.sh
# Run full suite:         bash agent-hooks/run-selftests.sh
set -euo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 "$script_dir/catch-silent-exit-harness.py" \
  --hook "$script_dir/catch-silent-exit.sh" \
  --fixtures "$script_dir/catch-silent-exit.fixtures.json" \
  "$@"
