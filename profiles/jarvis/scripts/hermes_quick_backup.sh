#!/usr/bin/env bash
# native-hermes-backup (no_agent cron wrapper)
# Idempotent, non-destructive critical-state snapshot.
# `hermes backup --quick --label weekly` writes to a NEW timestamped dir under
# profiles/jarvis/state-snapshots/ each run — it never overwrites an existing
# backup, so re-running is safe and cannot corrupt prior backups.
set -uo pipefail

HERMES=/home/frank/.local/bin/hermes
TS=$(date -u +%Y-%m-%dT%H:%MZ)

# Guard: don't stack backups if the binary is missing.
if [ ! -x "$HERMES" ]; then
  echo "[SILENT] hermes binary missing at $HERMES; backup skipped $TS"
  exit 0
fi

# Run the actual backup. The 300s in-band cap keeps the cron slot bounded and
# ensures a slow/failed copy FAILS LOUDLY (rc=124) instead of relying on the
# scheduler's hard _DEFAULT_SCRIPT_TIMEOUT=3600s SIGKILL, which previously
# produced silent "Script timed out after 3600s" errors (RCA t_2b90eaa8).
# Without this, `hermes backup --quick` had no internal bound and could run
# past an hour while copying the ~8.6 GB jarvis state.db under live gateway
# WAL write-load. 300s is ~20-30x the normal copy time (~10-15s), so it only
# trips on genuine stalls.
TIMEOUT_CAP_S=300
OUT=$(timeout "${TIMEOUT_CAP_S}" "$HERMES" backup --quick --label weekly 2>&1)
RC=$?
if [ "$RC" -eq 124 ]; then
  echo "BACKUP TIMED OUT (>${TIMEOUT_CAP_S}s) at $TS — state.db copy stalled (likely live gateway WAL contention)."
  echo "$OUT" | tail -20
  exit 124
fi

if [ $RC -ne 0 ]; then
  # Surface the failure (delivered) instead of silently succeeding.
  echo "BACKUP FAILED (rc=$RC) at $TS"
  echo "$OUT" | tail -20
  exit 1
fi

# Report the produced snapshot id + disk size so the run is verifiable.
# `hermes backup` prints: "State snapshot created: <ID>" and stores under the
# active profile's state-snapshots/ dir (resolved at cron-fire time).
SNAP_ID=$(echo "$OUT" | grep -oE "State snapshot created: [A-Za-z0-9._-]+" | awk '{print $4}')
SNAP_DIR=$( [ -n "$SNAP_ID" ] && find /home/frank/.hermes/profiles -maxdepth 3 -type d -name "$SNAP_ID" 2>/dev/null | head -1 )
SIZE=$( [ -n "$SNAP_DIR" ] && du -sh "$SNAP_DIR" 2>/dev/null | cut -f1 )
echo "Hermes weekly quick backup OK at $TS"
echo "Snapshot: ${SNAP_ID:-unknown}"
echo "Path: ${SNAP_DIR:-unknown}"
echo "Size: ${SIZE:-unknown}"
echo "Restore: hermes snapshot restore ${SNAP_ID:-<id>}"
