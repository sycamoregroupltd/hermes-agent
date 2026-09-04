#!/usr/bin/env bash
# pre_tool_call wrapper for the append-only arena write gate.
# Fail-open on JSON/parse errors. Fast-path skip when the payload does not
# mention journal.md or IMPROVEMENTS.md.
set -uo pipefail
if [ "${ALLOW_APPEND_ONLY_REWRITE:-}" = "1" ]; then
  printf '{}\n'
  exit 0
fi
# Stream payload via pipe instead of reading full JSON body into Bash variable
if ! grep -qiE 'journal\.md|IMPROVEMENTS\.md'; then
  printf '{}\n'
  exit 0
fi
python3 "$(dirname "$0")/gate-append-only-writes.py" 2>/dev/null || printf '{}\n'
exit 0
