#!/usr/bin/env bash
# PIT Context-Join Validation — context join correctness for PIT analysis
# no_agent=True watchdog — silent when clean, alerts on violations.
# Companion to pit-monitor.sh (event-chain integrity). This script checks
# the feature-level context join integrity:
#   CJ1: Stale context ratio — signal_journeys without realized exit
#   CJ2: Post-hoc feature contamination — leaked/backfilled columns populated after signal time
#   CJ3: Correlation_id chain health — can context be joined to outcomes?
#   CJ4: Signal-time feature PIT freshness — context columns within PIT window
#
# All queries are SELECT-only. Exit code always 0 (alerts via stdout content).
# Performance: uses sequential scans on ~2.3M signal_journeys; designed for
# daily off-peak run (06:00 UTC). Batches all checks in one psql connection.

set -uo pipefail

PSQL="docker exec sycodetrading-supabase-db psql -U postgres -d postgres -t -A"
TMPDIR=$(mktemp -d /tmp/pit-context-join-XXXXXX)
trap 'rm -rf "$TMPDIR"' EXIT
RESULTS="$TMPDIR/results.txt"
VIOLATIONS=0

# Batch 1: Fast aggregate checks — CJ1, CJ3 (scans indexed columns)
B1_SQL=$(cat <<'EOSQL'
-- CJ1: Stale context ratio
SELECT 'CJ1-stale-context-pct' AS c,
       count(*)::text,
       count(*) FILTER (WHERE exit_price IS NULL)::text,
       round(100.0 * count(*) FILTER (WHERE exit_price IS NULL) / count(*), 1)::text,
       count(*) FILTER (WHERE exit_price IS NOT NULL)::text,
       count(*) FILTER (WHERE exit_price IS NULL AND triggered_at >= now() - interval '24 hours')::text
FROM signal_journeys
HAVING count(*) > 0

UNION ALL

-- CJ3a: Signal-to-close join rate
SELECT 'CJ3-signal-to-close-join-rate',
       count(*)::text,
       count(DISTINCT tce.correlation_id)::text,
       round(100.0 * count(DISTINCT tce.correlation_id) / NULLIF(count(*), 0), 1)::text,
       count(DISTINCT tce.correlation_id) FILTER (WHERE sj.exit_price IS NOT NULL)::text,
       NULL::text
FROM signal_journeys sj
LEFT JOIN trade_close_events tce ON tce.correlation_id = sj.correlation_id

UNION ALL

-- CJ3b: Close correlation distinctness
SELECT 'CJ3-close-correlation-distinct',
       count(*)::text,
       count(DISTINCT correlation_id)::text,
       round(100.0 * count(DISTINCT correlation_id) / NULLIF(count(*), 0), 1)::text,
       count(*) FILTER (WHERE correlation_id IS NULL)::text,
       NULL::text
FROM trade_close_events

UNION ALL

-- CJ3c: Execution events correlation joinability
SELECT 'CJ3-exec-correlation-joinable',
       count(*)::text,
       count(DISTINCT correlation_id)::text,
       count(*) FILTER (WHERE correlation_id IS NULL)::text,
       count(*) FILTER (WHERE trade_intent_id IS NOT NULL)::text,
       NULL::text
FROM execution_events

UNION ALL

-- CJ3d: Trade_outcomes joinable via position_id
SELECT 'CJ3-outcome-position-join',
       count(*)::text,
       count(*) FILTER (WHERE t.position_id IS NOT NULL)::text,
       count(DISTINCT t.position_id)::text,
       count(*) FILTER (WHERE t.signal_id IS NOT NULL)::text,
       count(*) FILTER (WHERE t.position_id IS NOT NULL AND mp.id IS NOT NULL)::text
FROM trade_outcomes t
LEFT JOIN managed_positions mp ON mp.id = t.position_id

UNION ALL

-- CJ3e: Context column availability for executed signals
SELECT 'CJ3-context-column-availability',
       count(*)::text,
       count(*) FILTER (WHERE indicators IS NOT NULL)::text,
       count(*) FILTER (WHERE fast_validation IS NOT NULL)::text,
       count(*) FILTER (WHERE deep_validation IS NOT NULL)::text,
       count(*) FILTER (WHERE confluence_log IS NOT NULL)::text
FROM signal_journeys
WHERE executed_at IS NOT NULL

UNION ALL

-- CJ4e: Mutable-update window for signal_journeys
SELECT 'CJ4-mutable-update-window',
       count(*)::text,
       count(*) FILTER (WHERE delta_hours > 1 AND delta_hours <= 24)::text,
       count(*) FILTER (WHERE delta_hours > 24 AND delta_hours <= 168)::text,
       count(*) FILTER (WHERE delta_hours > 168)::text,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY delta_hours)::numeric, 1)::text
FROM (
    SELECT extract(epoch FROM (updated_at - triggered_at))/3600.0 AS delta_hours
    FROM signal_journeys
    WHERE updated_at IS NOT NULL
      AND triggered_at IS NOT NULL
      AND updated_at > triggered_at
) sub
;
EOSQL
)

# Batch 2: Post-hoc contamination checks — CJ2 (scan with updated_at filter)
B2_SQL=$(cat <<'EOSQL'
-- CJ2a: Post-hoc PnL/current_price on stale (no exit)
SELECT 'CJ2a-pnl-column-populated',
       count(*)::text,
       '-', NULL::text, NULL::text, NULL::text
FROM signal_journeys s1
WHERE s1.updated_at IS NOT NULL
  AND s1.triggered_at IS NOT NULL
  AND s1.updated_at > s1.triggered_at + interval '1 hour'
  AND s1.current_price IS NOT NULL
  AND s1.exit_price IS NULL
HAVING count(*) > 0

UNION ALL

-- CJ2b: Trajectory labels captured post-trigger
SELECT 'CJ2b-trajectory-label-leaked',
       count(*)::text,
       '-', NULL::text, NULL::text, NULL::text
FROM signal_journeys s1
WHERE s1.trajectory_captured_at IS NOT NULL
  AND s1.triggered_at IS NOT NULL
  AND s1.trajectory_label IS NOT NULL
  AND s1.trajectory_captured_at > s1.triggered_at + interval '1 minute'
HAVING count(*) > 0

UNION ALL

-- CJ2c: Market/regime features post-trigger
SELECT 'CJ2c-market-features-backfilled',
       count(*)::text,
       '-', NULL::text, NULL::text, NULL::text
FROM signal_journeys s1
WHERE s1.updated_at IS NOT NULL
  AND s1.triggered_at IS NOT NULL
  AND s1.updated_at > s1.triggered_at + interval '1 hour'
  AND (s1.market_fear_greed IS NOT NULL
    OR s1.market_funding_rate IS NOT NULL
    OR s1.market_open_interest_usd IS NOT NULL
    OR s1.regime_volatility IS NOT NULL)
HAVING count(*) > 0

UNION ALL

-- CJ2d: Historical win rate shadow populated post-trigger
SELECT 'CJ2d-historical-winrate-shadow',
       count(*)::text,
       '-', NULL::text, NULL::text, NULL::text
FROM signal_journeys s1
WHERE s1.historical_win_rate_shadow IS NOT NULL
  AND s1.triggered_at IS NOT NULL
  AND s1.created_at IS NOT NULL
  AND s1.updated_at IS NOT NULL
  AND s1.updated_at > s1.created_at + interval '1 hour'
HAVING count(*) > 0
;
EOSQL
)

# Batch 3: PIT freshness checks — CJ4 (jsonb extraction, slower)
B3_SQL=$(cat <<'EOSQL'
-- CJ4a: Indicator capturedAt gap (last 7 days for speed)
-- capturedAt may be epoch ms (13-digit) or ISO timestamp
SELECT 'CJ4-indicators-captured-gap' AS c,
       count(*)::text,
       count(*) FILTER (WHERE gap_sec > 60)::text,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY gap_sec)::numeric, 1)::text,
       round(avg(gap_sec)::numeric, 1)::text,
       max(gap_sec)::text
FROM (
    SELECT abs(extract(epoch FROM (
        CASE WHEN indicator_raw ~ '^\d{13}$'
             THEN to_timestamp(indicator_raw::numeric / 1000.0)
             ELSE indicator_raw::timestamp with time zone
        END - triggered_at
    ))) AS gap_sec
    FROM (
        SELECT triggered_at,
               indicators->>'capturedAt' AS indicator_raw
        FROM signal_journeys
        WHERE indicators IS NOT NULL
          AND indicators->>'capturedAt' IS NOT NULL
          AND triggered_at IS NOT NULL
          AND triggered_at >= now() - interval '7 days'
    ) sub1
) sub2
HAVING count(*) > 0

UNION ALL

-- CJ4b: Deep_validation capturedAt gap (last 7 days)
SELECT 'CJ4-deep-validation-captured-gap',
       count(*)::text,
       count(*) FILTER (WHERE gap_sec > 60)::text,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY gap_sec)::numeric, 1)::text,
       round(avg(gap_sec)::numeric, 1)::text,
       max(gap_sec)::text
FROM (
    SELECT abs(extract(epoch FROM (
        CASE WHEN raw ~ '^\d{13}$'
             THEN to_timestamp(raw::numeric / 1000.0)
             ELSE raw::timestamp with time zone
        END - triggered_at
    ))) AS gap_sec
    FROM (
        SELECT triggered_at,
               deep_validation->>'capturedAt' AS raw
        FROM signal_journeys
        WHERE deep_validation IS NOT NULL
          AND deep_validation->>'capturedAt' IS NOT NULL
          AND triggered_at IS NOT NULL
          AND triggered_at >= now() - interval '7 days'
    ) sub1
) sub2
HAVING count(*) > 0

UNION ALL

-- CJ4c: Confluence capturedAt gap (last 7 days)
SELECT 'CJ4-confluence-captured-gap',
       count(*)::text,
       count(*) FILTER (WHERE gap_sec > 60)::text,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY gap_sec)::numeric, 1)::text,
       round(avg(gap_sec)::numeric, 1)::text,
       max(gap_sec)::text
FROM (
    SELECT abs(extract(epoch FROM (
        CASE WHEN raw ~ '^\d{13}$'
             THEN to_timestamp(raw::numeric / 1000.0)
             ELSE raw::timestamp with time zone
        END - triggered_at
    ))) AS gap_sec
    FROM (
        SELECT triggered_at,
               confluence_log->>'capturedAt' AS raw
        FROM signal_journeys
        WHERE confluence_log IS NOT NULL
          AND confluence_log->>'capturedAt' IS NOT NULL
          AND triggered_at IS NOT NULL
          AND triggered_at >= now() - interval '7 days'
    ) sub1
) sub2
HAVING count(*) > 0

UNION ALL

-- CJ4d: Orderbook capturedAt gap (last 7 days via capture_metadata)
SELECT 'CJ4-orderbook-captured-gap',
       count(*)::text,
       round(percentile_cont(0.5) WITHIN GROUP (ORDER BY gap_sec)::numeric, 1)::text,
       max(gap_sec)::text,
       NULL::text, NULL::text
FROM (
    SELECT abs(extract(epoch FROM (
        CASE WHEN raw ~ '^\d{13}$'
             THEN to_timestamp(raw::numeric / 1000.0)
             ELSE raw::timestamp with time zone
        END - triggered_at
    ))) AS gap_sec
    FROM (
        SELECT triggered_at,
               capture_metadata->>'capturedAt' AS raw
        FROM signal_journeys
        WHERE capture_metadata IS NOT NULL
          AND capture_metadata->>'capturedAt' IS NOT NULL
          AND triggered_at IS NOT NULL
          AND triggered_at >= now() - interval '7 days'
    ) sub1
) sub2
HAVING count(*) > 0
;
EOSQL
)

# === Collect results ===

# Results header
printf '=== sycode-trading PIT Context-Join Monitor === %s ===\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$RESULTS"
echo "Checks: CJ1(stale-context) CJ2(contamination) CJ3(lineage-chain) CJ4(PIT-freshness)" >> "$RESULTS"
echo "" >> "$RESULTS"

# Mapping from check prefix to violation label
declare -A CHECK_LABELS=(
  [CJ1]="STALE-CONTEXT: signal_journeys without realized exit (baseline: 1.8M)"
  [CJ2]="POST-HOC-CONTAMINATION: leaked/backfilled columns populated after signal time"
  [CJ3]="LINEAGE-CHAIN: correlation_id joinability to outcomes"
  [CJ4]="PIT-FRESHNESS: context columns captured outside 60s PIT window"
)

run_batch() {
  local label="$1" sql="$2"
  local out="$TMPDIR/${label}.txt"

  $PSQL -c "$sql" > "$out" 2>&1
  local rc=$?
  if [[ "$rc" -ne 0 ]]; then
    echo "BATCH-ERROR [${label}]: psql exit $rc" >> "$RESULTS"
    cat "$out" >> "$RESULTS"
    return 1
  fi

  while IFS='|' read -r check col2 col3 col4 col5 col6; do
    [[ -z "$check" ]] && continue
    short="${check%%-*}"

    if [[ -z "${CHECK_SEEN[$short]:-}" ]]; then
      CHECK_SEEN[$short]=1
      VIOLATIONS=$((VIOLATIONS + 1))
      printf 'PIT-CONTEXT-JOIN-VIOLATION: %s\n' "${CHECK_LABELS[$short]:-$short}" >> "$RESULTS"
    fi

    detail="$check"
    for val in "$col2" "$col3" "$col4" "$col5" "$col6"; do
      if [[ -n "$val" && "$val" != "-" ]]; then
        detail="$detail | $val"
      fi
    done
    echo "    $detail" >> "$RESULTS"
  done < "$out"
}

declare -A CHECK_SEEN

run_batch "B1-fast" "$B1_SQL"
run_batch "B2-contamination" "$B2_SQL"
run_batch "B3-freshness" "$B3_SQL"

echo "" >> "$RESULTS"

# Watchdog pattern — output only on violations
if [[ "$VIOLATIONS" -gt 0 ]]; then
  printf 'PIT-CONTEXT-JOIN-ALERT: %s check group(s) have findings — see context report\n' "$VIOLATIONS"
  cat "$RESULTS"
fi

exit 0
