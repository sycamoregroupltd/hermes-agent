#!/usr/bin/env bash
# CANONICAL SOURCE — do not edit profile-local copies.
# supabase-migration-drift-monitor.sh — merged-but-never-applied detector for supabase/migrations.
#
# WHY (2026-07-29, fable seat): the deploy pipeline's scripts/migrate.sh preflight applies
# DRIZZLE migrations only; supabase/migrations/ (views, measurement DDL) has NO automated
# applier. The canonical_outcomes_v2 view sat 16 days stale (v3_2026-07-03 double-counting
# slippage 15-18.6 bps/trade vs the merged 07-13 ruling) and nobody noticed — every
# expectancy stat, bandit reward and kill decision was computed on phantom costs.
# supabase_migrations.schema_migrations tracking died 2026-05-13. Per the silent-failure
# doctrine the fix (governed re-apply, done 07-29) ships WITH this monitor.
#
# Observe-only. Applies nothing. Alerts via hermes send with failover chain.
#
# Checks:
#  1. VIEW-VERSION: canonical_outcomes_v2 view_version == EXPECTED_VIEW_VERSION.
#  2. BASELINE-VIEW: baseline_outcomes_v2 exists.
#  3. UNAPPLIED: any supabase/migrations/*.sql on origin/main with version > CUTOFF
#     whose 14-digit version prefix appears neither in public.schema_migration_manifest
#     (tools/db/migrate.sh records there) nor supabase_migrations.schema_migrations.
#     NOTE for operators: apply via tools/db/migrate.sh from a REPO-RELATIVE path so the
#     manifest row carries the canonical path/version (a scratch path breaks matching).
#
# CUTOFF 20260729000000: everything <= 20260718 was verified applied (out-of-band era);
# this monitor guards the future, not the archaeology.

set -u
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PATH="/home/frank/.local/bin:/home/frank/.hermes/hermes-agent/venv/bin:/usr/bin:/bin:$PATH"
export HERMES_HOME=/home/frank/.hermes

REPO="${REPO:-/home/frank/sycode-trading}"
DB_CONTAINER="${DB_CONTAINER:-sycodetrading-supabase-db}"
EXPECTED_VIEW_VERSION="${EXPECTED_VIEW_VERSION:-v3_2026-07-13_funding_source_aware_no_fabricated_zero}"
CUTOFF="${CUTOFF:-20260729000000}"
MON_STATE="${MON_STATE:-/home/frank/.hermes/state/migration-drift-state.txt}"
LOG_FILE="${LOG_FILE:-/home/frank/.hermes/logs/migration-drift.log}"
REALERT_SECS="${REALERT_SECS:-21600}"
ALERT_TARGET="${MIGRATION_MON_ALERT_TARGET:-whatsapp:Frank}"

mkdir -p "$(dirname "$MON_STATE")" "$(dirname "$LOG_FILE")"
now_epoch=$(date +%s)
now_iso=$(date -Is)

log() { echo "[$now_iso] $*" >> "$LOG_FILE"; }

psqlq() { docker exec "$DB_CONTAINER" psql -U postgres -d postgres -tAc "$1" 2>/dev/null; }

send_alert() {
    local key="$1" subject="$2" body="$3"
    local last
    last=$(grep -a "^${key}=" "$MON_STATE" 2>/dev/null | tail -1 | cut -d= -f2)
    if [ -n "${last:-}" ] && [ $((now_epoch - last)) -lt "$REALERT_SECS" ]; then
        log "SUPPRESSED key=$key"
        return 0
    fi
    local delivered=0
    if hermes send -q -t "$ALERT_TARGET" -s "$subject" "$body"; then
        delivered=1
        log "ALERT-SENT target=$ALERT_TARGET key=$key"
    else
        local fb
        for fb in ${MIGRATION_MON_FALLBACKS:-discord:#critical-alerts telegram:506972405}; do
            if hermes send -q -t "$fb" -s "🔁 FAILOVER: $subject" "$body"; then
                delivered=1
                log "ALERT-FAILOVER-OK target=$fb key=$key"
                break
            fi
            log "ALERT-FAILOVER-FAILED target=$fb rc=$? key=$key"
        done
    fi
    # Arm the throttle ONLY on confirmed delivery (2026-07-28 lesson: a FAILED send
    # must not silence its own retries).
    if [ "$delivered" = 1 ]; then
        grep -av "^${key}=" "$MON_STATE" > "$MON_STATE.tmp" 2>/dev/null || true
        echo "${key}=${now_epoch}" >> "$MON_STATE.tmp"
        mv "$MON_STATE.tmp" "$MON_STATE"
    fi
}

# --- 1+2: view checks ---
actual_version=$(psqlq "select distinct view_version from canonical_outcomes_v2 limit 1")
if [ -z "$actual_version" ]; then
    send_alert "view-unreadable" "⚠️ canonical_outcomes_v2 unreadable" "Monitor could not read view_version from canonical_outcomes_v2 (db container down or view dropped). Host: $(hostname)."
elif [ "$actual_version" != "$EXPECTED_VIEW_VERSION" ]; then
    send_alert "view-drift" "🚨 canonical_outcomes_v2 VERSION DRIFT" "Deployed view_version='$actual_version' expected='$EXPECTED_VIEW_VERSION'. All expectancy/bandit/kill stats are suspect until reconciled. Apply the repo-canonical migration via tools/db/migrate.sh. (If a NEWER version was intentionally applied, update EXPECTED_VIEW_VERSION in $0.)"
else
    log "OK view_version=$actual_version"
fi
baseline_ok=$(psqlq "select count(*) from pg_views where viewname='baseline_outcomes_v2'")
if [ "${baseline_ok:-0}" != "1" ]; then
    send_alert "baseline-view-missing" "🚨 baseline_outcomes_v2 missing" "Control-arm comparison view baseline_outcomes_v2 not found — random-baseline promotion checks will silently fail."
fi

# --- 3: merged-but-unapplied ---
if git -C "$REPO" fetch origin main -q 2>/dev/null; then
    applied=$( { psqlq "select path from public.schema_migration_manifest"; psqlq "select version from supabase_migrations.schema_migrations"; } | grep -oE '[0-9]{14}' | sort -u)
    unapplied=""
    while IFS= read -r f; do
        v=$(basename "$f" | grep -oE '^[0-9]{14}') || continue
        [ -n "$v" ] || continue
        [ "$v" \> "$CUTOFF" ] || continue
        echo "$applied" | grep -q "^$v$" || unapplied="$unapplied $f"
    done < <(git -C "$REPO" ls-tree -r --name-only origin/main supabase/migrations/ | grep '\.sql$')
    if [ -n "$unapplied" ]; then
        send_alert "unapplied-migrations" "🚨 merged supabase migrations NOT applied" "On origin/main but absent from schema_migration_manifest + supabase_migrations:$unapplied — apply via tools/db/migrate.sh (repo-relative path) or record why not."
    else
        log "OK no unapplied supabase migrations (cutoff $CUTOFF)"
    fi
else
    log "WARN git fetch failed — skipping unapplied check (not alerting: transient network)"
fi
