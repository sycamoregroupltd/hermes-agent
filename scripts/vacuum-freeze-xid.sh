#!/usr/bin/env bash
# Pre-emptive anti-wraparound FREEZE — sycodetrading supabase-db
# Author: claude-code-orchestrator-20260821  Created: 2026-08-21  Approved by: Frank
#
# WHY: 21 public tables sit above 180M XID age; autovacuum_freeze_max_age=200M.
# When the database age crosses 200M, autovacuum begins AGGRESSIVE anti-wraparound
# vacuums across all of them, at a moment it picks, on a box that also runs CI and
# a latency-sensitive trading server. Anti-wraparound vacuums do not yield: cancel
# one and it returns immediately. Freezing ahead of that lets US choose the window.
#
# NOT AN EMERGENCY: ~1.95 BILLION XIDs remain to the 2^31 write-refusal ceiling
# (~4 years at the measured 14 XID/s). 200M is where the SAFETY MECHANISM ENGAGES,
# not where danger begins. Zero replication slots, zero prepared xacts, no long
# transactions -- so freezing can actually complete. This is scheduling, not rescue.
#
# SECOND BENEFIT: builds the visibility map. Never-vacuumed means the VM is empty,
# and Index Only Scans (migrations 0136/0137) depend on it entirely -- without this
# those indexes would still do heap fetches.
#
# SAFE: VACUUM FREEZE takes ShareUpdateExclusiveLock. It does NOT block reads or
# writes. It does not rewrite the table. It is interruptible.
set -uo pipefail

LOG=/home/frank/logs/vacuum-freeze-xid.log
CT=sycodetrading-supabase-db
MAX_RUNTIME_S=${MAX_RUNTIME_S:-14400}   # 4h ceiling for the whole pass
START=$(date +%s)

log(){ echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >> "$LOG"; }

q(){ docker exec $CT psql -U postgres -X -q -tAc "$1" 2>&1; }

log '=== BEGIN pre-emptive freeze pass ==='

# Refuse to run alongside a pg_dump -- that combination is what stopped signal
# generation for hours on 2026-08-21.
DUMPS=$(q "SELECT count(*) FROM pg_stat_activity WHERE application_name ILIKE '%pg_dump%';")
if [ "$DUMPS" != "0" ]; then
  log "ABORT: $DUMPS pg_dump backend(s) active. Refusing to add load. Will retry next scheduled run."
  exit 0
fi

log "db age before: $(q 'SELECT max(age(datfrozenxid)) FROM pg_database;')"

# Smallest first: quick wins land even if the window is cut short.
TABLES=$(q "SELECT quote_ident(n.nspname)||'.'||quote_ident(c.relname)
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE c.relkind IN ('r','m') AND n.nspname <> 'pg_toast'
              AND age(c.relfrozenxid) > 180000000
            UNION
            SELECT quote_ident(pn.nspname)||'.'||quote_ident(p.relname)
            FROM pg_class t JOIN pg_class p ON p.reltoastrelid = t.oid
            JOIN pg_namespace pn ON pn.oid = p.relnamespace
            WHERE t.relnamespace = 'pg_toast'::regnamespace
              AND age(t.relfrozenxid) > 180000000;")

COUNT=0; FAILED=0
for t in $TABLES; do
  ELAPSED=$(( $(date +%s) - START ))
  if [ $ELAPSED -gt $MAX_RUNTIME_S ]; then
    log "STOP: runtime ceiling ${MAX_RUNTIME_S}s reached after $COUNT tables. Remainder deferred to next run."
    break
  fi
  AGE_BEFORE=$(q "SELECT age(relfrozenxid) FROM pg_class WHERE oid='$t'::regclass;")
  T0=$(date +%s)
  OUT=$(docker exec -e PGOPTIONS="-c statement_timeout=0" $CT psql -U postgres -X -q -c "VACUUM (FREEZE, ANALYZE) $t;" 2>&1)
  RC=$?
  T1=$(( $(date +%s) - T0 ))
  AGE_AFTER=$(q "SELECT age(relfrozenxid) FROM pg_class WHERE oid='$t'::regclass;")
  if [ $RC -ne 0 ]; then
    FAILED=$((FAILED+1))
    log "FAIL  $t  rc=$RC  ${T1}s  :: $(echo "$OUT" | head -1)"
  else
    COUNT=$((COUNT+1))
    log "ok    $t  ${T1}s  age $AGE_BEFORE -> $AGE_AFTER"
  fi
done

log "db age after: $(q 'SELECT max(age(datfrozenxid)) FROM pg_database;')"
log "=== END: $COUNT frozen, $FAILED failed, $(( $(date +%s) - START ))s total ==="

# Exit non-zero on failure so the cron log and any watchdog can see it.
# (Explicitly NOT exit 0 on error -- that defect was found on this estate today.)
[ $FAILED -eq 0 ] || exit 1
exit 0
