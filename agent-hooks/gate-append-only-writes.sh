#!/usr/bin/env bash
# pre_tool_call wrapper for the append-only arena write gate.
# Fail-open on JSON/parse errors. Fast-path skip when the payload does not
# mention journal.md or IMPROVEMENTS.md.
set -uo pipefail
if [ "${ALLOW_APPEND_ONLY_REWRITE:-}" = "1" ]; then
  printf '{}\n'
  exit 0
fi
# Capture stdin to tempfile so both grep and Python can read it
TMPFILE="$(mktemp -t gate-append-only-XXXXXX)"
trap 'rm -f "$TMPFILE"' EXIT
cat > "$TMPFILE"
# Fast-path: skip if payload doesn't mention journal.md or IMPROVEMENTS.md
if ! grep -qiE 'journal\.md|IMPROVEMENTS\.md' "$TMPFILE"; then
  printf '{}\n'
  exit 0
fi
python3 "$(dirname "$0")/gate-append-only-writes.py" < "$TMPFILE" 2>/dev/null || printf '{}\n'
exit 0
