#!/usr/bin/env bash
# In-dir cron shim for the self-improvement harvester (t_942c4570).
# Hermes cronjob `script` resolver REJECTS symlinks and out-of-dir paths
# (see trading-devops SOUL / scheduler.py), so this REAL in-dir shim must
# exec the canonical harvester living in the sycode-trading repo. Canonical:
#   /home/frank/sycode-trading/tools/self-improvement-harvester/self-improvement-harvester.py
# Dead-pin history: the old job pointed at a deleted worktree
#   (sycode-trading/.worktrees/t_9a2cdb2d) and a missing script — fixed by
#   pointing at the canonical repo path and installing this in-dir shim.
#
# ADOPT item 5 (t_da7aea92): the monotonic watermark cursor is a SINGLE-CONSUMER
# per-job state file. It now lives in the durable cron notepad for this job
# (c097aecdef5a, trading-devops profile). The canonical harvester still reads/
# writes the loose JSON file as its working copy; this shim bridges the notepad
# <-> file so the notepad is the source of truth across runs. The loose file is
# a transient working copy, regenerated from the notepad on each run.
set -euo pipefail

JOB_ID="c097aecdef5a"
PROFILE_HOME="/home/frank/.hermes/profiles/trading-devops"
CURSOR_FILE="/home/frank/.hermes/self-improvement-harvester-cursor.json"
HERMES_BIN="${HERMES_BIN:-/home/frank/.local/bin/hermes}"

# 1) Hydrate the working cursor file from the notepad (durable source).
cursor_val="$("$HERMES_BIN" cron notepad "$JOB_ID" get harvester:cursor 2>/dev/null | grep -v '^No notepad key' || true)"
if [ -n "$cursor_val" ] && [ "$cursor_val" != "Notepad for job"* ]; then
  printf '%s' "$cursor_val" > "$CURSOR_FILE"
elif [ -f "$CURSOR_FILE" ]; then
  # First notepad-enabled run: seed the notepad from the existing file so no
  # prior watermark is lost.
  seed="$(cat "$CURSOR_FILE")"
  [ -n "$seed" ] && "$HERMES_BIN" cron notepad "$JOB_ID" set harvester:cursor "$seed" >/dev/null 2>&1 || true
fi

# 2) Run the canonical harvester against the working file.
rc=0
python3 /home/frank/sycode-trading/tools/self-improvement-harvester/self-improvement-harvester.py \
  --lookback-seconds 3600 \
  --json \
  --board sycode-trading \
  --board jarvis-os \
  "$@" || rc=$?

# 3) Persist the updated cursor back to the notepad (durable).
if [ -f "$CURSOR_FILE" ]; then
  final="$(cat "$CURSOR_FILE")"
  [ -n "$final" ] && "$HERMES_BIN" cron notepad "$JOB_ID" set harvester:cursor "$final" >/dev/null 2>&1 || true
fi

exit "$rc"
