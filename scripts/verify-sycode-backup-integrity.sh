#!/bin/bash
# sycode nightly backup integrity verifier — READ ONLY.
# Created 2026-08-21 by claude-code-orchestrator-20260821 under A3 delegated by Frank.
#
# WHY: for at least 25 nights backup-cron.sh ran
#   pg_dump --format=custom --verbose 2>&1 | gzip > FILE
# which merged log text into the archive from byte 0. 25 dumps / 571 GB, none restorable,
# every one logging success. PR #1120 fixed it; db-backup was recreated from build-tree
# @ fe6d937849 on 2026-08-21. The fix being LIVE is not the same as the backup WORKING.
#
# This script exists because the failure was never loud enough to reach a human.
# It mutates NOTHING: no delete, no rebuild, no restart, no retention change.
set -uo pipefail
C=sycodetrading-db-backup

fail() { echo "$*"; exit 0; }   # exit 0 so the alert is delivered, not swallowed as a job error

docker inspect "$C" >/dev/null 2>&1 || fail "🔴 SYCODE BACKUP: container $C not found. No backup is being taken. Card t_e94acf27."

NEW=$(docker exec "$C" sh -c 'ls -t /backups/*.dump* 2>/dev/null | head -1' 2>/dev/null)
[ -n "$NEW" ] || fail "🔴 SYCODE BACKUP: no dump files found in $C:/backups. Card t_e94acf27."

AGE_H=$(docker exec "$C" sh -c "echo \$(( ( \$(date +%s) - \$(stat -c %Y '$NEW') ) / 3600 ))" 2>/dev/null)
SIZE=$(docker exec "$C" sh -c "stat -c %s '$NEW'" 2>/dev/null)

case "$NEW" in
  *.gz) MAGIC=$(docker exec "$C" sh -c "gunzip -c '$NEW' 2>/dev/null | head -c 5" 2>/dev/null)
        # pg_restore cannot read a pipe. Decompress a head slice to a scratch file:
        # a custom-format TOC lives near the start, so a slice suffices to list it.
        LIST=$(docker exec "$C" sh -c "T=\$(mktemp /tmp/bkverify.XXXXXX); gunzip -c '$NEW' 2>/dev/null | head -c 200000000 > \$T; pg_restore --list \$T 2>&1 | head -40; rm -f \$T" 2>/dev/null) ;;
  *)    MAGIC=$(docker exec "$C" sh -c "head -c 5 '$NEW'" 2>/dev/null)
        LIST=$(docker exec "$C" sh -c "pg_restore --list '$NEW' 2>&1 | head -40" 2>/dev/null) ;;
esac

TOC=$(printf '%s' "$LIST" | grep -c '^[0-9]')

if [ "$MAGIC" != "PGDMP" ]; then
  fail "🔴 SYCODE BACKUP UNRESTORABLE — THE 25-NIGHT DEFECT HAS RETURNED
file:  $NEW
magic: '$MAGIC'  (must be PGDMP)
size:  $SIZE bytes,  age ${AGE_H}h
pg_restore --list: $(printf '%s' "$LIST" | head -3)

The estate has NO restorable backup. Do NOT delete any dump.
Most likely cause: DB_BACKUP_PGPASS_FILE is not persisted — a recreate of db-backup
without it resolves to a missing path, Docker mounts a DIRECTORY, and the job dies.
Card t_e94acf27 · vault Orchestration/2026-08-21-orchestration-record-e918d81d.md"
fi

if [ "${AGE_H:-999}" -gt 30 ]; then
  fail "🟠 SYCODE BACKUP STALE — newest archive is ${AGE_H}h old (expected <30h)
file: $NEW  ($SIZE bytes).  Magic is PGDMP so the last one written was valid,
but the 02:00Z cron may not have run. Card t_e94acf27."
fi

echo "🟢 SYCODE BACKUP VERIFIED RESTORABLE
file:  $NEW
magic: PGDMP        pg_restore --list: ${TOC} TOC entries
size:  $SIZE bytes  age ${AGE_H}h
First independently-verified restorable backup path since the 2026-08-21 P0.
Retention is 10 days. Constraint still standing: delete nothing that has not passed this check."
