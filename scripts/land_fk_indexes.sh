#!/usr/bin/env bash
# land_fk_indexes.sh — wait for the nightly pg_dump to clear, then land the FK indexes.
#
# CREATE INDEX CONCURRENTLY cannot complete while an older transaction is open. The
# nightly backup (sycodetrading-db-backup, cron 0 2 * * *) holds one transaction for
# 5.5-8h, so attempts between ~02:00 and ~10:00 stall in "waiting for old snapshots".
# This waits for that window to close, then does the work. It NEVER cancels the backup.
#
# Usage: DDL_TOKEN=... setsid nohup ./land_fk_indexes.sh > /tmp/land_fk.log 2>&1 &
set -uo pipefail

TOKEN="${DDL_TOKEN:?DDL_TOKEN must be set}"
TASK="${DDL_TASK:-t_dbreview_20260821}"
REPO=/home/frank/sycode-trading
BUS=/home/frank/obsidian-fleet-vault/Orchestration/sessions/bin/session-bus.sh
SID=claude-code-mac-db-20260821
MIG_TIMEOUT=3600          # hard cap per migration; a CIC stall is NOT a lock wait and
                          # would otherwise hang forever holding SHARE UPDATE EXCLUSIVE,
                          # blocking autovacuum on a hot table with no symptom but a
                          # log that stops mid-sentence.
cd "$REPO" || { echo "FATAL: cannot cd $REPO"; exit 1; }

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

# Query helper. Fails LOUDLY: a DB outage must not be misreported as "snapshots held".
q() {
  local out rc
  out=$(docker exec sycodetrading-supabase-db psql -U postgres -d postgres -tAc "$1" 2>&1); rc=$?
  if (( rc != 0 )); then log "  QUERY FAILED (rc=$rc): ${out//$'\n'/ }"; return 1; fi
  printf '%s' "$out" | tr -d ' '
}

# Apply a migration. Success is the EXIT CODE, never a substring: migrate.sh prints
# "-- apply OK" at line 180 and then keeps working (manifest INSERT, audit select), so
# a later failure would otherwise be counted as success with no governance record.
apply() {
  local file="$1" tries="${2:-20}" extra="${3:-}" i out rc
  for ((i=1; i<=tries; i++)); do
    out=$(timeout "$MIG_TIMEOUT" tools/db/migrate.sh "$file" --task "$TASK" --token "$TOKEN" $extra 2>&1); rc=$?
    if (( rc == 0 )); then log "  OK: $(basename "$file") (attempt $i)"; return 0; fi
    if (( rc == 124 )); then
      log "  TIMEOUT after ${MIG_TIMEOUT}s: $(basename "$file") — still blocked, not retrying"
      return 2
    fi
    # Broadened: the original only matched "lock timeout" and treated genuine
    # contention (could not obtain lock / deadlock / statement timeout) as fatal.
    if grep -qiE "lock timeout|could not obtain lock|deadlock detected|statement timeout" <<<"$out"; then
      sleep 8; continue
    fi
    log "  FAILED (rc=$rc): $(basename "$file")"
    # Redact the token before it reaches a world-readable log.
    tail -3 <<<"${out//$TOKEN/<redacted>}" | sed 's/^/    /'
    return 1
  done
  log "  GAVE UP after $tries attempts (contention): $(basename "$file")"; return 1
}

log "armed; waiting for the nightly pg_dump to release its snapshot"

# ---- 1. wait for no client transaction older than 5 minutes (up to 9h) ----
# 9h, not 5h: the documented dump window is 5.5-8h from 02:00. A 5h cap could expire
# while the dump still holds its snapshot and abort having done nothing.
cleared=0
for ((i=1; i<=540; i++)); do
  old=$(q "SELECT count(*) FROM pg_stat_activity WHERE backend_type='client backend' AND xact_start IS NOT NULL AND xact_start < now()-interval '5 minutes';") || { sleep 60; continue; }
  dump=$(q "SELECT count(*) FROM pg_stat_activity WHERE application_name='pg_dump';") || dump="?"
  if [[ "$old" == "0" ]]; then log "snapshot clear after ${i}m (pg_dump sessions: $dump)"; cleared=1; break; fi
  (( i % 15 == 0 )) && log "  still waiting (${i}m): old_txns=$old pg_dump=$dump"
  sleep 60
done
(( cleared == 1 )) || { log "ABORT: snapshots still held (or DB unreachable) after 9h"; exit 1; }

# ---- clock guard: never start a build that the 02:00 dump would strand ----
hour=$(date -u +%H); min=$(date -u +%M)
if (( 10#$hour == 1 && 10#$min >= 30 )) || (( 10#$hour == 0 )) || (( 10#$hour >= 2 && 10#$hour < 10 )); then
  log "ABORT: too close to / inside the 02:00-10:00 UTC dump window (now ${hour}:${min}Z)"
  exit 1
fi

# ---- 2. clear INVALID indexes — MUST succeed before building ----
log "dropping INVALID indexes from the cancelled builds"
if ! apply server/drizzle/migrations/0131_drop_invalid_fk_indexes.sql 10; then
  log "ABORT: could not clear INVALID indexes — not proceeding to builds"
  exit 1
fi
inv=$(q "SELECT count(*) FROM pg_index WHERE NOT indisvalid;") || inv="?"
if [[ "$inv" != "0" ]]; then log "ABORT: $inv INVALID index(es) remain after the drop"; exit 1; fi
log "  invalid indexes now: 0"

# ---- 3. build the FK indexes, one at a time ----
built=0; failed=0
for f in 0132_fk_index_decision_outcomes_snapshot_id \
         0133_fk_index_decision_outcomes_position_id \
         0134_fk_index_lip_webhook_dedup_delivery_id \
         0135_fk_index_strategy_outcomes_finalized_outcome_id; do
  if [[ ! -f "server/drizzle/migrations/$f.sql" ]]; then
    log "  MISSING: $f.sql"; failed=$((failed+1)); continue   # counted, not silently skipped
  fi
  log "building $f"
  # built/failed use $(( )) not (( ))++ : the latter returns exit status 1 when
  # incrementing from 0, which becomes a silent early exit the moment anyone adds -e.
  if apply "server/drizzle/migrations/$f.sql" 5 --no-transaction; then
    built=$((built+1))
  else
    failed=$((failed+1))
  fi
done

# ---- 4. verify — indisvalid is REQUIRED here ----
# Without it an INVALID index satisfies the NOT EXISTS, so this could report
# "0 remaining" while every FK is still effectively unindexed.
remaining=$(q "SELECT count(*) FROM pg_constraint c
  JOIN pg_stat_user_tables s ON s.relid=c.conrelid
 WHERE c.contype='f' AND cardinality(c.conkey)=1
   AND NOT EXISTS (SELECT 1 FROM pg_index i
                    WHERE i.indrelid=c.conrelid AND i.indkey[0]=c.conkey[1] AND i.indisvalid)
   AND s.n_live_tup>10000;") || remaining="?"
invalid=$(q "SELECT count(*) FROM pg_index WHERE NOT indisvalid;") || invalid="?"

status="DONE"
(( failed > 0 )) && status="PARTIAL"
(( built == 0 )) && status="FAILED"
[[ "$remaining" == "?" || "$invalid" == "?" ]] && status="$status (verification query failed)"
log "$status built=$built failed=$failed unindexed_fks_remaining=$remaining invalid_indexes=$invalid"

if [[ -x "$BUS" ]]; then
  if "$BUS" event --author "$SID" \
      --text "DB-INDEX-EXPORTER-20260821 $status: FK indexes built=$built failed=$failed; unindexed FKs>10k rows remaining=$remaining; invalid indexes=$invalid" >/dev/null 2>&1; then
    log "session bus notified"
  else
    log "WARN: session bus notify FAILED — fleet not informed of this run"
  fi
else
  log "WARN: session bus not executable at $BUS — fleet not informed"
fi

(( failed == 0 && built > 0 )) && exit 0 || exit 1
