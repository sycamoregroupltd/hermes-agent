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

# Freshness alone is not proof it went OFF-BOX — and PRESENCE is not proof it went off-box INTACT.
# This check used `test -e dgx-fleet-backups/$stamp`, which an EMPTY DIRECTORY satisfies. On
# 2026-08-14 the push failed with rsync rc=10 leaving a 0-byte remote directory, and this monitor
# reported healthy. Existence checks are the cheapest probe to write and cluster exactly where the
# stakes are highest. Assert CONTENT: file count and bytes, measured against the local copy.
SSH_OPTS='ssh -4 -o BatchMode=yes -o ConnectTimeout=20'   # -4: this host drops ~33% of IPv6 packets
MIN_FILES=5             # floor used only when the local copy has already been pruned away
MIN_KB=4194304          # 4 GiB — a night is ~5G; anything smaller is a truncated payload

if $SSH_OPTS mac true 2>/dev/null; then
  # One round trip, three facts: does it exist, how many files, how many KB. Existence is probed
  # SEPARATELY from size so "absent" and "present but empty" produce different alerts — they have
  # different causes (push never started vs push died mid-transfer).
  r_raw=$($SSH_OPTS mac "test -d dgx-fleet-backups/$stamp && echo 1 || echo 0; ls -1 dgx-fleet-backups/$stamp 2>/dev/null | wc -l; du -sk dgx-fleet-backups/$stamp 2>/dev/null | cut -f1" 2>/dev/null)
  r_exists=$(printf '%s\n' "$r_raw" | sed -n 1p | tr -dc '0-9'); r_exists=${r_exists:-0}
  r_n=$(printf '%s\n' "$r_raw" | sed -n 2p | tr -dc '0-9'); r_n=${r_n:-0}
  r_kb=$(printf '%s\n' "$r_raw" | sed -n 3p | tr -dc '0-9'); r_kb=${r_kb:-0}

  # Prefer comparing to the local copy (ground truth, self-maintaining as the payload changes);
  # fall back to absolute floors once local retention has aged it out.
  local_dir="/home/frank/fleet-backups/$stamp"
  if [ -d "$local_dir" ]; then
    want_n=$(find "$local_dir" -maxdepth 1 -type f | wc -l)
    want_kb=$(( $(du -sk "$local_dir" | cut -f1) * 97 / 100 ))
  else
    want_n=$MIN_FILES; want_kb=$MIN_KB
  fi

  if [ "$r_exists" -eq 0 ]; then
    echo "BACKUP ON-BOX ONLY: local backup '$stamp' is ${age_h}h fresh, but it is NOT present at mac:dgx-fleet-backups/."
    echo "  A backup that never left the machine does not survive the machine."
  elif [ "$r_n" -lt "$want_n" ] || [ "$r_kb" -lt "$want_kb" ]; then
    echo "BACKUP OFF-BOX INCOMPLETE: '$stamp' exists at mac:dgx-fleet-backups/ but holds ${r_n} files / ${r_kb}KB (expected >=${want_n} files / >=${want_kb}KB)."
    echo "  A partial or empty remote copy is NOT a backup — it merely passes an existence check."
    echo "  Verify: ssh mac 'ls -la dgx-fleet-backups/$stamp; du -sh dgx-fleet-backups/$stamp'"
  fi
else
  echo "BACKUP OFF-BOX UNVERIFIED: mac is unreachable, so the ${age_h}h-old local backup '$stamp' cannot be confirmed off-box."
fi
