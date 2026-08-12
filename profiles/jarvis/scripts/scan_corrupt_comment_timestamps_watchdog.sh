#!/usr/bin/env bash
# Watchdog wrapper around the canonical scan_corrupt_comment_timestamps.py.
# Silent when clean (empty stdout -> no delivery); emits + exits 1 on detection
# so the scheduler fires an error alert. Read-only, no board mutation.
set -u
SCANNER="/home/frank/.hermes/scripts/scan_corrupt_comment_timestamps.py"
OUT="$(python3 "$SCANNER" 2>&1)"
RC=$?
if [ "$RC" -ne 0 ]; then
  echo "[corrupt-comment-timestamp-watchdog] DETECTED corruption in task_comments.created_at:"
  echo "$OUT"
  exit 1
fi
# Clean: emit nothing so the no_agent watchdog stays silent.
exit 0
