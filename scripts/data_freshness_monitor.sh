#!/usr/bin/env bash
# CANONICAL SOURCE — do not edit profile-local copies.
# data_freshness_monitor.sh — OUT-OF-BAND per-WRITER freshness monitor for the sycode data plane.
#
# WHY (2026-08-07/08): three candle timeframes (1D, 1h, 5m) stopped writing for
# ~26h and NOTHING saw it. The ingestion service logged `"errors":0` with
# container health "healthy" while ingesting only 1m; `max(candles.timestamp)`
# stayed fresh because 1m kept writing; and the coverage audit that would have
# re-subscribed the dead timeframes had never started on any boot (it sat behind
# a subscribe loop that hung past the 60s startup cap). A live consumer
# (correlationDataFeed -> correlation_snapshots) kept emitting fresh rows computed
# off 1D closes that had stopped 2.5 days earlier.
#
# The lesson is structural, not incidental: **a table-level max() cannot detect
# partial writer death.** One healthy writer masks every dead sibling. So this
# monitor asks per-WRITER questions, and — more importantly — compares SIBLINGS,
# because "one member of a cohort is stale while the others are fresh" catches
# partial death that no absolute threshold reliably will.
#
# Same doctrine as deploy_liveness_monitor.sh (which correctly caught the ship
# gap): runs from the SYSTEM crontab so it survives gateway death, OBSERVE-ONLY —
# never restarts, never deploys, never writes to the DB — and alerts through the
# same throttled failover chain.
#
# Checks:
#  1. CANDLE-TIMEFRAME: each timeframe against its own cadence-scaled threshold.
#  2. CANDLE-SIBLING-SKEW: any timeframe stale while others are fresh (the exact
#     2026-08-07 signature). Fires even when absolute thresholds are generous.
#  3. WRITER-STALE: named critical writers against per-writer thresholds.
#  4. PROBE-DEAD: if the DB probe itself fails repeatedly, say so loudly rather
#     than reporting "all clear" — a blind monitor must never look healthy.

set -u
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PATH="/home/frank/.local/bin:/home/frank/.hermes/hermes-agent/venv/bin:/usr/bin:/bin:$PATH"
export HERMES_HOME=/home/frank/.hermes

DB_CONTAINER="${DB_CONTAINER:-sycodetrading-supabase-db}"
DB_USER="${DB_USER:-postgres}"
DB_NAME="${DB_NAME:-postgres}"
MON_STATE="${MON_STATE:-/home/frank/.hermes/state/data-freshness-state.txt}"
LOG_FILE="${LOG_FILE:-/home/frank/.hermes/logs/data-freshness.log}"
REALERT_SECS="${REALERT_SECS:-21600}"   # 6h per key, matching the deploy monitor
SIBLING_SKEW_FACTOR="${SIBLING_SKEW_FACTOR:-6}"  # stale sibling = 6x the freshest
ALERT_TARGET="${DATA_MON_ALERT_TARGET:-whatsapp:Frank}"
DRY_RUN="${DRY_RUN:-0}"

mkdir -p "$(dirname "$MON_STATE")" "$(dirname "$LOG_FILE")"
touch "$MON_STATE"
now_epoch=$(date +%s)
now_iso=$(date -Is)

log() { echo "[$now_iso] $*" >> "$LOG_FILE"; }

send_alert() {
    local key="$1" subject="$2" body="$3"
    if [ "$DRY_RUN" = "1" ]; then
        echo "WOULD-ALERT key=$key subject=$subject"
        echo "  $body"
        return 0
    fi
    local last
    last=$(grep -a "^${key}=" "$MON_STATE" 2>/dev/null | tail -1 | cut -d= -f2)
    if [ -n "${last:-}" ] && [ $((now_epoch - last)) -lt "$REALERT_SECS" ]; then
        log "SUPPRESSED key=$key (re-alert window)"
        return 0
    fi
    local delivered=0
    if hermes send -q -t "$ALERT_TARGET" -s "$subject" "$body"; then
        delivered=1; log "ALERT-SENT target=$ALERT_TARGET key=$key subject=$subject"
    else
        log "ALERT-FAILED target=$ALERT_TARGET key=$key"
        local fb
        for fb in ${DATA_MON_FALLBACKS:-discord:#critical-alerts telegram:506972405}; do
            if hermes send -q -t "$fb" -s "🔁 FAILOVER: $subject" "$body"; then
                delivered=1; log "ALERT-FAILOVER-OK target=$fb key=$key"; break
            fi
            log "ALERT-FAILOVER-FAILED target=$fb key=$key"
        done
    fi
    # Arm the throttle ONLY on confirmed delivery — same fix as the deploy monitor.
    # An alert that reached nobody must not buy 6h of silence.
    if [ "$delivered" -eq 1 ]; then
        grep -av "^${key}=" "$MON_STATE" 2>/dev/null > "$MON_STATE.tmp" || true
        echo "${key}=${now_epoch}" >> "$MON_STATE.tmp"
        mv "$MON_STATE.tmp" "$MON_STATE"
    else
        log "ALERT-UNDELIVERED key=$key — all channels failed, throttle NOT armed"
    fi
}

clear_key() {
    grep -av "^${1}=" "$MON_STATE" 2>/dev/null > "$MON_STATE.tmp" || true
    mv "$MON_STATE.tmp" "$MON_STATE"
}

psql_q() {
    docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAF'|' -c "$1" 2>/dev/null
}

# ── 0. PROBE HEALTH ──────────────────────────────────────────────────────────
probe=$(psql_q "SELECT 1")
if [ "${probe:-}" != "1" ]; then
    fails=$(grep -a "^probe_fails=" "$MON_STATE" 2>/dev/null | tail -1 | cut -d= -f2)
    fails=$((${fails:-0} + 1))
    grep -av "^probe_fails=" "$MON_STATE" 2>/dev/null > "$MON_STATE.tmp" || true
    echo "probe_fails=${fails}" >> "$MON_STATE.tmp"; mv "$MON_STATE.tmp" "$MON_STATE"
    log "PROBE-FAILED container=$DB_CONTAINER consecutive=$fails"
    [ "$fails" -ge 3 ] && send_alert data_probe_dead \
        "⚠️ sycode data-freshness monitor is blind" \
        "Cannot query $DB_CONTAINER for $fails consecutive runs. The data-plane monitor itself is down — treat all-clear as meaningless until fixed."
    exit 0
fi
clear_key probe_fails

# ── 1 + 2. CANDLES: per-timeframe thresholds AND sibling skew ────────────────
# thresholds are ~3x the bar cadence, so normal lag never alerts
# 1D is special: the writer only persists CLOSED daily bars, so the newest possible
# timestamp is yesterday 00:00 — legitimately up to ~48h old by mid-afternoon. A 30h
# limit alerted on correct behaviour (caught 2026-08-09). 51h allows one missed day.
declare -A TF_MAX=( ["1m"]=900 ["5m"]=1800 ["15m"]=3600 ["1h"]=10800 ["4h"]=43200 ["1D"]=183600 )
rows=$(psql_q "SELECT timeframe, GREATEST(0, EXTRACT(epoch FROM (now()-max(timestamp)))::bigint) FROM candles GROUP BY 1")
freshest=999999999; stale_list=""; fresh_list=""
declare -A TF_AGE=()
while IFS='|' read -r tf age; do
    [ -z "${tf:-}" ] && continue
    TF_AGE["$tf"]="$age"
    [ "$age" -lt "$freshest" ] && freshest="$age"
    lim="${TF_MAX[$tf]:-}"
    if [ -n "$lim" ] && [ "$age" -gt "$lim" ]; then
        stale_list="${stale_list}${tf}=$((age/60))m(limit $((lim/60))m) "
    else
        fresh_list="${fresh_list}${tf}=$((age/60))m "
    fi
done <<< "$rows"

if [ -n "$stale_list" ]; then
    send_alert candle_tf_stale "🚨 sycode candles: timeframe(s) stopped writing" \
"Per-timeframe freshness breach. STALE: ${stale_list}
Healthy: ${fresh_list:-none}

A table-level max(candles.timestamp) will look FINE here — 1m alone keeps it fresh.
This is the 2026-08-07 failure shape (1D/1h/5m dead ~26h, service logging errors:0).
Check: the coverage audit must be running ('[CandleIngestion] coverage audit pass'
every 5 min) and 'CandleIngestion: Started' must appear at boot. If neither does,
startCandleIngestion() did not complete — see PR #1019."
else
    clear_key candle_tf_stale
    log "OK candles per-timeframe: ${fresh_list}"
fi

# Sibling skew — catches partial death even when absolute limits are generous.
# MUST be cadence-normalised: comparing raw ages flags 4h-at-4h-stale as "skewed"
# against 1m-at-1m-stale, which is simply how bars work. Compare each timeframe's
# age as a MULTIPLE OF ITS OWN CADENCE, against the median such multiple.
declare -A TF_CADENCE=( ["1m"]=60 ["5m"]=300 ["15m"]=900 ["1h"]=3600 ["4h"]=14400 ["1D"]=86400 )
ratios=""; declare -A TF_RATIO=()
for tf in "${!TF_AGE[@]}"; do
    cad="${TF_CADENCE[$tf]:-}"; [ -z "$cad" ] && continue
    r=$(( (${TF_AGE[$tf]} * 100) / cad ))   # x100 to keep integer precision
    TF_RATIO["$tf"]=$r
    ratios="${ratios}${r}\n"
done
median=$(printf "%b" "$ratios" | grep -v '^$' | sort -n | awk '{a[NR]=$1} END{print (NR%2==1)?a[(NR+1)/2]:int((a[NR/2]+a[NR/2+1])/2)}')
[ -z "${median:-}" ] || [ "$median" -lt 100 ] && median=100   # floor: 1x cadence
skew=""
for tf in "${!TF_RATIO[@]}"; do
    r="${TF_RATIO[$tf]}"
    # flag only if BOTH far worse than its peers AND absolutely stale (>1h)
    if [ "$r" -gt $((median * SIBLING_SKEW_FACTOR)) ] && [ "${TF_AGE[$tf]}" -gt 3600 ]; then
        skew="${skew}${tf}=$((TF_AGE[$tf]/60))m($((r/100))x cadence) "
    fi
done
if [ -n "$skew" ]; then
    send_alert candle_sibling_skew "⚠️ sycode candles: sibling skew (partial writer death?)" \
"These timeframes are >${SIBLING_SKEW_FACTOR}x worse than their peers, measured as age/own-cadence (peer median $((median/100))x): ${skew}
One healthy writer masks dead siblings; absolute thresholds can be generous enough
to miss this. Verify each timeframe has a live subscription."
else
    clear_key candle_sibling_skew
fi

# ── 3. NAMED CRITICAL WRITERS ────────────────────────────────────────────────
# table|timestamp column|max age seconds|note
WRITERS='
execution_events|created_at|21600|slippage/fill telemetry — went dark 7d and lost 489 trades (2026-07-30..08-05)
oi_snapshots|timestamp|7200|open-interest live feed
funding_rate_history|timestamp|7200|funding feed (semantics changed 2026-04-24: settled -> live/predicted)
signal_journeys|triggered_at|7200|decision funnel head
tick_trades|timestamp|7200|trade tape (retention unconfirmed — verify no pruning job)
correlation_snapshots|captured_at|21600|DERIVED from candles 1D: fresh rows here while 1D is stale = silently stale features
'
writer_bad=""
while IFS='|' read -r tbl col lim note; do
    [ -z "${tbl:-}" ] && continue
    age=$(psql_q "SELECT GREATEST(0, EXTRACT(epoch FROM (now()-max($col)))::bigint) FROM $tbl")
    [ -z "${age:-}" ] && { log "SKIP $tbl (no rows or query failed)"; continue; }
    if [ "$age" -gt "$lim" ]; then
        writer_bad="${writer_bad}
  ${tbl}: $((age/3600))h stale (limit $((lim/3600))h) — ${note}"
    fi
done <<< "$WRITERS"

if [ -n "$writer_bad" ]; then
    send_alert writer_stale "🚨 sycode data plane: writer(s) stale" \
"Named critical writers past their freshness budget:${writer_bad}

Each of these has a live consumer. A stale writer with a live consumer produces
confidently wrong outputs rather than an error — the dominant failure mode on this
system (cf. correlation_snapshots computing fresh rows off dead 1D candles)."
else
    clear_key writer_stale
    log "OK all named writers within budget"
fi

# ── 4. DERIVED-FRESH-ON-STALE-SOURCE ────────────────────────────────────────
# The subtlest and most dangerous shape: a derived table writing FRESH rows off a
# STALE source. It produces confidently wrong output instead of an error, so every
# freshness check above passes and every consumer is silently misled.
# Observed 2026-08-07: correlation_snapshots kept emitting fresh rows computed from
# 1D candle closes that had stopped 2.5 days earlier.
# derived | derived ts col | source table | source WHERE | max source age secs
# Array (not a heredoc string) so the SQL filter can contain single quotes.
DERIVED_ROWS=(
  # 183600 = 51h, matching TF_MAX["1D"]: the writer persists only CLOSED daily
  # bars, so ~48h staleness is correct, not a fault. Keep these two in step.
  "correlation_snapshots|captured_at|candles|timeframe IN ('1D','1d')|183600"
)
derived_bad=""
for row in "${DERIVED_ROWS[@]}"; do
    IFS='|' read -r dtbl dcol stbl sfilter slim <<< "$row"
    d_age=$(psql_q "SELECT GREATEST(0, EXTRACT(epoch FROM (now()-max($dcol)))::bigint) FROM $dtbl")
    s_age=$(psql_q "SELECT GREATEST(0, EXTRACT(epoch FROM (now()-max(timestamp)))::bigint) FROM $stbl WHERE $sfilter")
    [ -z "${d_age:-}" ] || [ -z "${s_age:-}" ] && continue
    # derived is fresh (within its own budget) BUT source is stale => silent lying
    if [ "$d_age" -lt "$slim" ] && [ "$s_age" -gt "$slim" ]; then
        derived_bad="${derived_bad}
  ${dtbl} is FRESH ($((d_age/3600))h) but its source ${stbl}[${sfilter}] is STALE ($((s_age/3600))h)"
    fi
done

if [ -n "$derived_bad" ]; then
    send_alert derived_stale_source "🚨 sycode: derived table writing fresh rows from a STALE source" \
"${derived_bad}

This is the worst shape: the derived table looks healthy to every freshness check
while its inputs are dead, so it emits CONFIDENTLY WRONG values rather than failing.
Consumers cannot tell. Fix the source writer first, then decide whether the
derived rows produced during the stale window must be invalidated."
else
    clear_key derived_stale_source
    log "OK no derived table running on a stale source"
fi

# ── 5. PER-SYMBOL DARKNESS ───────────────────────────────────────────────────
# Replaces candles_quality_monitor_cron.sh, which the crontab invoked at 24,54
# from 2026-08-06 to 2026-08-10 while the script did not exist — ~195 runs, every
# one "not found", stderr to a /tmp log nobody reads. A monitor that cannot run is
# worse than none: the crontab entry itself reads as coverage.
#
# Gap this closes: every check above aggregates with max() ACROSS symbols, so a
# single symbol going dark is invisible while its hundreds of peers keep the
# aggregate fresh. Each symbol is compared against its OWN timeframe cohort — the
# per-timeframe universes genuinely differ (1m ~228 symbols, 5m ~416), so scoring
# against one global symbol list would false-positive constantly.
dark_rows=$(psql_q "
WITH per_symbol AS (
  SELECT timeframe, symbol, EXTRACT(epoch FROM (now()-max(timestamp)))::bigint AS age
  FROM candles WHERE timestamp > now() - interval '14 days' GROUP BY 1,2
), live AS (
  -- Symbols PROVEN live: receiving on at least one timeframe within the hour.
  -- Without this join the check flags every delisted/non-USDT pair forever (74 on
  -- 15m alone) and gets ignored — exactly how the monitor it replaced died.
  SELECT symbol FROM per_symbol GROUP BY 1 HAVING min(age) <= 3600
), med AS (
  SELECT timeframe, percentile_cont(0.5) WITHIN GROUP (ORDER BY age) AS med_age,
         count(*) AS cohort FROM per_symbol GROUP BY 1
), dark AS (
  SELECT p.timeframe, p.symbol, p.age, m.med_age, m.cohort
  FROM per_symbol p
  JOIN med m USING (timeframe)
  JOIN live l USING (symbol)
  WHERE p.age > GREATEST(m.med_age * 6, 10800)
)
SELECT timeframe, count(*), max(cohort), (max(med_age)/60)::bigint,
       array_to_string((array_agg(symbol || '(' || (age/60)::bigint || 'm)' ORDER BY age DESC))[1:5], ' ')
FROM dark GROUP BY 1 ORDER BY 1")

dark_bad=""; dark_note=""
while IFS='|' read -r tf n cohort medm examples; do
    [ -z "${tf:-}" ] && continue
    if [ "${n:-0}" -ge 3 ]; then
        dark_bad="${dark_bad}
  ${tf}: ${n}/${cohort} symbols dark (cohort median ${medm}m) — e.g. ${examples}"
    else
        dark_note="${dark_note}${tf}:${n} "
    fi
done <<< "$dark_rows"

if [ -n "$dark_bad" ]; then
    send_alert candle_symbol_dark "⚠️ sycode candles: symbols dark behind a fresh aggregate" \
"Symbols dark on this timeframe while PROVEN LIVE on another (>6x cohort median age, >3h):${dark_bad}

Every other check in this monitor aggregates with max() across symbols, so these are
invisible to all of them — healthy peers keep the timeframe looking fresh. Likely
These symbols ARE receiving data on other timeframes, so this is a per-timeframe
subscription gap, not a delisting: the WS believes it is subscribed while no data
arrives (the coverage audit reports 'added' for streams that never stream). Symbols
dark on EVERY timeframe are excluded — those are inactive/non-USDT pairs. Isolated
1-2 symbol cases are logged, not alerted."
else
    clear_key candle_symbol_dark
    log "OK no symbol cohort darkness${dark_note:+ (isolated: $dark_note)}"
fi

log "RUN-COMPLETE"

