#!/usr/bin/env bash
# backup-freshness-monitor.sh — the monitor that would have caught a 44-DAY backup gap.
# Created 2026-08-03. The fleet ran from 2026-06-20 to 2026-08-03 with NO off-box backup
# while `native-hermes-backup` reported "ok" every night into deliver=local. The job ran;
# the backup did not land. Nobody could see it.
#
# LITERAL RULE THIS ENFORCES: a backup JOB reporting ok is not a backup. Only a fresh
# ARTIFACT is a backup. This checks the artifact, not the job.
# Empty stdout = healthy (no-agent watchdog pattern); any output is delivered as an alert.
set -u
export PATH="/home/frank/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
LATEST=/home/frank/fleet-backups/LATEST
MAX_AGE_H=36            # nightly job + grace

if [ ! -f "$LATEST" ]; then
  echo "BACKUP MISSING: $LATEST does not exist — no off-box backup has ever completed, or the state file was removed."
  exit 0
fi

age_h=$(( ( $(date +%s) - $(stat -c %Y "$LATEST") ) / 3600 ))
stamp=$(head -c 40 "$LATEST" 2>/dev/null)

if [ "$age_h" -gt "$MAX_AGE_H" ]; then
  echo "BACKUP STALE: ${age_h}h old (max ${MAX_AGE_H}h). LATEST stamp='${stamp}'."
  echo "  The nightly-fleet-backup cron may be reporting ok while producing nothing — check the ARTIFACT, not the job status."
  echo "  Verify: ls -lat ~/fleet-backups/ | head; ssh mac ls -lat dgx-fleet-backups/ | head"
  exit 0
fi

# Freshness alone is not proof it went OFF-BOX. Confirm the remote copy exists.
if ssh -o BatchMode=yes -o ConnectTimeout=15 mac true 2>/dev/null; then
  if ! ssh -o BatchMode=yes -o ConnectTimeout=20 mac "test -e dgx-fleet-backups/$stamp" 2>/dev/null; then
    echo "BACKUP ON-BOX ONLY: local backup '$stamp' is ${age_h}h fresh, but it is NOT present at mac:dgx-fleet-backups/."
    echo "  A backup that never left the machine does not survive the machine."
  fi
else
  echo "BACKUP OFF-BOX UNVERIFIED: mac is unreachable, so the ${age_h}h-old local backup '$stamp' cannot be confirmed off-box."
fi
