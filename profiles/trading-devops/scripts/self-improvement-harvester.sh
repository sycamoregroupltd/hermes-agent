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
NOTEPAD=(env HERMES_HOME="$PROFILE_HOME" "$HERMES_BIN" cron notepad "$JOB_ID")

# 1) Hydrate the working cursor file from the notepad (durable source).
# Bridge failures are fatal: silently falling back to the loose file would
# violate the notepad source-of-truth contract and hide a broken cron store.
if ! cursor_val="$("${NOTEPAD[@]}" get harvester:cursor)"; then
  printf '%s\n' "ERROR: unable to read cron notepad ${JOB_ID}:harvester:cursor" >&2
  exit 1
fi
if [[ "$cursor_val" != No\ notepad\ key* ]]; then
  if [[ -n "$cursor_val" ]]; then
    printf '%s' "$cursor_val" > "$CURSOR_FILE"
  fi
elif [[ -f "$CURSOR_FILE" ]]; then
  # First notepad-enabled run: seed the notepad from the existing file so no
  # prior watermark is lost. A failed seed is fatal and visible to cron.
  seed="$(<"$CURSOR_FILE")"
  if [[ -n "$seed" ]] && ! "${NOTEPAD[@]}" set harvester:cursor "$seed" >/dev/null; then
    printf '%s\n' "ERROR: unable to seed cron notepad ${JOB_ID}:harvester:cursor" >&2
    exit 1
  fi
fi

# 2) Run the canonical harvester against the working file.
rc=0
python3 /home/frank/sycode-trading/tools/self-improvement-harvester/self-improvement-harvester.py \
  --lookback-seconds 3600 \
  --json \
  --board sycode-trading \
  --board jarvis-os \
  "$@" || rc=$?

# 3) Persist the updated cursor back to the notepad (durable). A failed write
# is a failed job even when the harvester itself returned success.
if [[ -f "$CURSOR_FILE" ]]; then
  final="$(<"$CURSOR_FILE")"
  if [[ -n "$final" ]] && ! "${NOTEPAD[@]}" set harvester:cursor "$final" >/dev/null; then
    printf '%s\n' "ERROR: unable to persist cron notepad ${JOB_ID}:harvester:cursor" >&2
    exit 1
  fi
fi

exit "$rc"
