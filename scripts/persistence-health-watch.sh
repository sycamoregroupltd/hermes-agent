#!/usr/bin/env bash
# Persistence Feature-Density & Stale-Stream Health Monitor
# ==========================================================
# Run by cron (every 5-10m, no-agent) as a wakeAgent LOOP gate.
# Checks 6 persistence-health conditions per t_ec862255 / t_9e84c2a3 spec.
# Emits: {"wakeAgent": true, "signals": "<digest>", "reasons": [...]}
#        {"wakeAgent": false}   (clean or duplicate-known-condition)
# Fail-open: any error / DB-down emits false safely.
set -uo pipefail

STATE_DIR="/home/frank/.hermes/cron/state"
STATE_FILE="${STATE_DIR}/persistence-health-watch.sig"
mkdir -p "$STATE_DIR"
touch "$STATE_FILE"
signals=""
threshold_notes=""

# ── DB helpers ──────────────────────────────────────────────────────────
PSQL=(docker exec -e PGPASSWORD=postgres sycodetrading-supabase-db psql -h localhost -U postgres -d postgres -v ON_ERROR_STOP=1 -t -A -P pager=off)
PGDIRECT=(PGPASSWORD=postgres psql -h localhost -p 5432 -U postgres -d postgres -v ON_ERROR_STOP=1 -t -A -P pager=off)

db_query() {
  "${PSQL[@]}" -c "$1" 2>/dev/null || "${PGDIRECT[@]}" -c "$1" 2>/dev/null || echo ""
}

safe_int() {
  local v="${1:-0}"
  v="${v//[^0-9]/}"
  [ -n "$v" ] && echo "$v" || echo "0"
}

# ── Fail-open wrapper ──────────────────────────────────────────────────
emit_false() {
  echo '{"wakeAgent":false}'
  exit 0
}

# ── Collect signals ────────────────────────────────────────────────────
signals=""
threshold_notes=""

# ── 1. composite_confidence_trace coverage (non-empty when scorer shadow/enforce mode) ──
SHADOW_MODE=$(echo "SELECT COALESCE(NULLIF(TRIM( lower(coalesce( current_setting('app.settings.sfx_gate_mode', true), '')) ), ''), 'off');" | docker exec -i sycodetrading-supabase-db sh -c "cat > /dev/null; echo 'query_ignored'" 2>/dev/null; grep -i "SFX_GATE_MODE" /home/frank/sycode-trading/server/.env 2>/dev/null | head -1 | sed 's/.*=//' || echo "off")
SHADOW_MODE="${SHADOW_MODE:-off}"

# For shadow/enforce mode, we expect non-empty composite_confidence_trace on recent rows
if [ "$SHADOW_MODE" = "shadow" ] || [ "$SHADOW_MODE" = "enforce" ]; then
  sj_24h=$(safe_int "$(db_query "SELECT count(*) FROM signal_journeys WHERE triggered_at >= NOW() - INTERVAL '24 hours';")")
  cct_24h=$(safe_int "$(db_query "SELECT count(*) FROM signal_journeys WHERE triggered_at >= NOW() - INTERVAL '24 hours' AND composite_confidence_trace IS NOT NULL AND composite_confidence_trace != 'null'::jsonb;")") || 0
  if [ "$sj_24h" -gt 0 ] && [ "$cct_24h" -eq 0 ]; then
    signals="${signals}CCT_ZERO(sj24h=${sj_24h},trace=${cct_24h},mode=${SHADOW_MODE}) "
    threshold_notes="${threshold_notes}composite_confidence_trace is empty despite ${SHADOW_MODE} mode; "
  elif [ "$sj_24h" -gt 100 ] && [ "$cct_24h" -gt 0 ] && [ $((cct_24h * 100 / sj_24h)) -lt 10 ]; then
    signals="${signals}CCT_LOW($(echo "scale=1; ${cct_24h} * 100 / ${sj_24h}" | bc)% coverage) "
    threshold_notes="${threshold_notes}composite_confidence_trace below 10% coverage; "
  fi
fi

# ── 2. volume_ratio_at_entry / market_open_interest coverage thresholds ──
sj_24h=$(safe_int "$(db_query "SELECT count(*) FROM signal_journeys WHERE triggered_at >= NOW() - INTERVAL '24 hours';")")
if [ "$sj_24h" -gt 0 ]; then
  vratio=$(safe_int "$(db_query "SELECT count(*) FROM signal_journeys WHERE triggered_at >= NOW() - INTERVAL '24 hours' AND volume_ratio_at_entry IS NOT NULL;")") || 0
  oi=$(safe_int "$(db_query "SELECT count(*) FROM signal_journeys WHERE triggered_at >= NOW() - INTERVAL '24 hours' AND market_open_interest IS NOT NULL;")") || 0

  vpct=0; [ "$sj_24h" -gt 0 ] && vpct=$((vratio * 100 / sj_24h))
  oipct=0; [ "$sj_24h" -gt 0 ] && oipct=$((oi * 100 / sj_24h))

  # volume_ratio_at_entry: threshold 20% (low would be < 20%)
  if [ "$vpct" -lt 20 ]; then
    signals="${signals}VOL_RATIO_LOW(${vpct}% of ${sj_24h},have=${vratio}) "
    threshold_notes="${threshold_notes}volume_ratio_at_entry ${vpct}% below 20% threshold; "
  fi
  # market_open_interest: threshold 50% (low would be < 50%)
  if [ "$oipct" -lt 50 ]; then
    signals="${signals}OI_LOW(${oipct}% of ${sj_24h},have=${oi}) "
    threshold_notes="${threshold_notes}market_open_interest ${oipct}% below 50% threshold; "
  fi
fi

# ── 3. Pro-trader DB profile/position staleness ──
# True staleness: profiles that had a position change but >6h ago
TRUE_STALE_PROFILES=$(safe_int "$(db_query "SELECT count(*) FROM pro_trader_profiles WHERE is_active=true AND removed_at IS NULL AND last_position_change_at IS NOT NULL AND last_position_change_at < NOW() - INTERVAL '6 hours';")") || 0
if [ "$TRUE_STALE_PROFILES" -gt 0 ]; then
  signals="${signals}PRO_STALE(true_stale=${TRUE_STALE_PROFILES}) "
  threshold_notes="${threshold_notes}${TRUE_STALE_PROFILES} pro-trader profiles stale (>6h no position change); "
fi

# Zombie profiles: active but never had any position change or any positions
ZOMBIE_PROFILES=$(safe_int "$(db_query "SELECT count(*) FROM pro_trader_profiles p WHERE is_active=true AND removed_at IS NULL AND last_position_change_at IS NULL AND NOT EXISTS (SELECT 1 FROM pro_trader_positions pt WHERE pt.wallet_address = p.wallet_address);")") || 0
if [ "$ZOMBIE_PROFILES" -gt 0 ]; then
  signals="${signals}PRO_ZOMBIE(zombie=${ZOMBIE_PROFILES}) "
  threshold_notes="${threshold_notes}${ZOMBIE_PROFILES} pro-trader profiles active but have zero positions; "
fi

# Also check for OPEN positions that haven't been updated in 4+ hours
STALE_POSITIONS=$(safe_int "$(db_query "SELECT count(*) FROM pro_trader_positions WHERE status='OPEN' AND updated_at < NOW() - INTERVAL '4 hours';")") || 0
if [ "$STALE_POSITIONS" -gt 0 ]; then
  signals="${signals}POS_STALE(stale_open=${STALE_POSITIONS}) "
  threshold_notes="${threshold_notes}${STALE_POSITIONS} open positions stale (>4h no update); "
fi

# ── 4. Candle coverage for every signal-eligible symbol ──
# Find symbols with recent signals but without candles in last 6h (or ever)
MISSING_CANDLE_SYMS=$(db_query "WITH recent_signals AS (
  SELECT DISTINCT symbol FROM signal_journeys WHERE triggered_at >= NOW() - INTERVAL '24 hours'
), recent_candles AS (
  SELECT DISTINCT symbol FROM candles WHERE timestamp >= NOW() - INTERVAL '6 hours'
)
SELECT count(*) FROM recent_signals rs WHERE rs.symbol NOT IN (SELECT symbol FROM recent_candles);") || 0
MISSING_CANDLE_SYMS=$(safe_int "$MISSING_CANDLE_SYMS")
if [ "$MISSING_CANDLE_SYMS" -gt 0 ]; then
  # Get actual list for the signal detail (truncated to avoid overly long output)
  CANDLE_MISSING_LIST=$(db_query "WITH recent_signals AS (
    SELECT DISTINCT symbol FROM signal_journeys WHERE triggered_at >= NOW() - INTERVAL '24 hours'
  ), recent_candles AS (
    SELECT DISTINCT symbol FROM candles WHERE timestamp >= NOW() - INTERVAL '6 hours'
  )
  SELECT string_agg(rs.symbol, ', ') FROM (SELECT DISTINCT symbol FROM recent_signals rs WHERE rs.symbol NOT IN (SELECT symbol FROM recent_candles) LIMIT 15) rs;") || ""
  signals="${signals}NO_CANDLES(syms=${MISSING_CANDLE_SYMS}) "
  threshold_notes="${threshold_notes}${MISSING_CANDLE_SYMS} signal-eligible symbols missing recent candles: ${CANDLE_MISSING_LIST}; "
fi

# ── 5. Hyperliquid secSinceLastTick stale-while-connected detection ──
# Scrape /metrics for the unified_tick_seconds_since_last_tick metric
HL_TICK_AGE=$(curl -s http://localhost:3001/metrics 2>/dev/null | grep '^sycodetrading_unified_tick_seconds_since_last_tick{exchange="hyperliquid"}' | awk '{print $2}') || ""
HL_TICK_AGE="${HL_TICK_AGE:--1}"
# Check if hyperliquid is healthy (connected)
HL_HEALTHY=$(curl -s http://localhost:3001/metrics 2>/dev/null | grep '^sycodetrading_exchange_healthy{exchange="hyperliquid"}' | awk '{print $2}') || ""
HL_HEALTHY="${HL_HEALTHY:-0}"

# Also check signal_detection tick age - more accurate for current freshness
SD_TICK_AGE=$(curl -s http://localhost:3001/metrics 2>/dev/null | grep '^sycodetrading_signal_detection_last_tick_age_seconds' | awk '{print $2}') || ""
SD_TICK_AGE="${SD_TICK_AGE:--1}"

if [ "$HL_HEALTHY" = "1" ]; then
  # If connected but ticks stale for >30s, flag it
  if [ "$HL_TICK_AGE" != "-1" ] && [ "${HL_TICK_AGE%.*}" -gt 30 ] 2>/dev/null; then
    signals="${signals}HL_STALE_TICK(tickAge=${HL_TICK_AGE}s,connected=1) "
    threshold_notes="${threshold_notes}Hyperliquid WS connected but ${HL_TICK_AGE}s since last tick; "
  fi
  # Also check signal detection age as a broader indicator
  if [ "$SD_TICK_AGE" != "-1" ] && [ "${SD_TICK_AGE%.*}" -gt 60 ] 2>/dev/null; then
    signals="${signals}SD_STALE_TICK(tickAge=${SD_TICK_AGE}s) "
    threshold_notes="${threshold_notes}SignalDetection ${SD_TICK_AGE}s since last tick; "
  fi
fi

# ── 6. filter_attribution_facts insert failure detection ──
# Approach: compare recent signal_evaluations rows vs filter_attribution_facts rows.
# If evaluations exist but attributions are absent, it suggests a silent write failure.
EVAL_24H=$(safe_int "$(db_query "SELECT count(*) FROM signal_evaluations WHERE created_at >= NOW() - INTERVAL '24 hours';")") || 0
ATTR_24H=$(safe_int "$(db_query "SELECT count(*) FROM filter_attribution_facts WHERE created_at >= NOW() - INTERVAL '24 hours';")") || 0

# If evaluations exist but attribution rows are ~0, that's a failure signal
if [ "$EVAL_24H" -gt 100 ] && [ "$ATTR_24H" -eq 0 ]; then
  signals="${signals}ATTR_FAIL(eval24h=${EVAL_24H},attr24h=0) "
  threshold_notes="${threshold_notes}filter_attribution_facts has 0 rows despite ${EVAL_24H} signal_evaluations; "
elif [ "$EVAL_24H" -gt 0 ] && [ "$ATTR_24H" -eq 0 ]; then
  # Low-volume case but still zero — flag as potential concern
  signals="${signals}ATTR_ZERO(eval24h=${EVAL_24H},attr24h=0) "
  threshold_notes="${threshold_notes}filter_attribution_facts 0/24h with ${EVAL_24H} evaluations; "
fi

# ── Decision: wake or not ───────────────────────────────────────────────
if [ -z "$signals" ]; then
  # ── Clean: clear state so next fresh incident wakes ──
  : > "$STATE_FILE"
  echo 'persistence-health: all 6 checks clean'
  echo '{"wakeAgent":false}'
  exit 0
fi

# ── Deduplicate: persist signal MD5 hash so same condition doesn't re-wake ──
sig=$(echo -n "$signals" | md5sum | cut -c1-12)
if grep -qxF "$sig" "$STATE_FILE"; then
  echo "persistence-health: persists (already woke): $signals"
  echo '{"wakeAgent":false}'
  exit 0
fi

# New condition — record and wake
echo "$sig" >> "$STATE_FILE"
echo "PERSISTENCE-HEALTH — signal(s): $signals"
echo "threshold_notes: ${threshold_notes:-}"
echo '{"wakeAgent":true}'
