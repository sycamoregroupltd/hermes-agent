#!/bin/bash
# Weekend Short 1m (strategy 29) post-weekend verification
# Runs after weekend concludes (Monday 00:00+ UTC)
# Checks if strategy 29 produced any 1m SHORT signals during the weekend

set -euo pipefail

STRATEGY_UUID="c773d6aa-31de-4c59-98e1-344cb9bbbc3d"
WEEKEND_START="2026-07-04 00:00:00+00"
WEEKEND_END="2026-07-06 00:00:00+00"

echo "============================================"
echo "Weekend Short 1m (strategy 29) Verification"
echo "Period: $WEEKEND_START to $WEEKEND_END"
echo "============================================"
echo ""

# 1. Check signal_intent_decisions for this strategy
echo "--- Signal Intent Decisions for Strategy 29 ---"
docker exec sycodetrading-supabase-db psql -U postgres -d postgres -c "
SELECT id, terminal_state, correlation_id, created_at
FROM signal_intent_decisions
WHERE strategy_id='$STRATEGY_UUID'
  AND created_at >= '$WEEKEND_START'
ORDER BY created_at DESC
LIMIT 20;
"

echo ""
echo "--- Count by terminal_state ---"
docker exec sycodetrading-supabase-db psql -U postgres -d postgres -c "
SELECT terminal_state, COUNT(*)
FROM signal_intent_decisions
WHERE strategy_id='$STRATEGY_UUID'
  AND created_at >= '$WEEKEND_START'
GROUP BY terminal_state;
"

# 2. Check signal_journeys for strategy 29 symbols
SYMBOLS="'AVAXUSDT','APTUSDT','DOTUSDT','DOGEUSDT','NEARUSDT','SUIUSDT','SOLUSDT'"
echo ""
echo "--- Signal Journeys for Strategy 29 Symbols (SHORT, 1m, weekend) ---"
docker exec sycodetrading-supabase-db psql -U postgres -d postgres -c "
SELECT symbol, direction, trigger_score, triggered_at, current_stage, is_weekend
FROM signal_journeys
WHERE symbol IN ($SYMBOLS)
  AND direction='SHORT'
  AND timeframe='1m'
  AND triggered_at >= '$WEEKEND_START'
  AND triggered_at < '$WEEKEND_END'
ORDER BY triggered_at DESC
LIMIT 20;
"

echo ""
echo "--- Count by Symbol (SHORT, 1m, trigger_score >= 95) ---"
docker exec sycodetrading-supabase-db psql -U postgres -d postgres -c "
SELECT symbol, COUNT(*), ROUND(AVG(trigger_score)::numeric,1) AS avg_score, MAX(trigger_score) AS max_score
FROM signal_journeys
WHERE symbol IN ($SYMBOLS)
  AND direction='SHORT'
  AND timeframe='1m'
  AND trigger_score >= 95
  AND triggered_at >= '$WEEKEND_START'
  AND triggered_at < '$WEEKEND_END'
GROUP BY symbol
ORDER BY COUNT(*) DESC;
"

echo ""
echo "--- All 1m SHORT Signals (all symbols) during weekend with score >= 95 ---"
docker exec sycodetrading-supabase-db psql -U postgres -d postgres -c "
SELECT symbol, direction, trigger_score, triggered_at, current_stage
FROM signal_journeys
WHERE timeframe='1m'
  AND direction='SHORT'
  AND trigger_score >= 95
  AND triggered_at >= '$WEEKEND_START'
  AND triggered_at < '$WEEKEND_END'
ORDER BY triggered_at DESC;
"

echo ""
echo "--- Weekend 1m Totals ---"
docker exec sycodetrading-supabase-db psql -U postgres -d postgres -c "
SELECT direction, COUNT(*), ROUND(AVG(trigger_score)::numeric,1) AS avg_score, MAX(trigger_score), MIN(trigger_score)
FROM signal_journeys
WHERE timeframe='1m'
  AND triggered_at >= '$WEEKEND_START'
  AND triggered_at < '$WEEKEND_END'
GROUP BY direction;
"

echo ""
echo "--- VERDICT ---"
SID_COUNT=$(docker exec sycodetrading-supabase-db psql -U postgres -d postgres -t -c "
SELECT COUNT(*) FROM signal_intent_decisions
WHERE strategy_id='$STRATEGY_UUID'
  AND created_at >= '$WEEKEND_START';" | tr -d ' ')

SJ_COUNT=$(docker exec sycodetrading-supabase-db psql -U postgres -d postgres -t -c "
SELECT COUNT(*) FROM signal_journeys
WHERE symbol IN ($SYMBOLS)
  AND direction='SHORT'
  AND timeframe='1m'
  AND trigger_score >= 95
  AND triggered_at >= '$WEEKEND_START'
  AND triggered_at < '$WEEKEND_END';" | tr -d ' ')

echo "Signal Intent Decisions for strategy 29: $SID_COUNT"
echo "SHORT 1m signals (score>=95) on strategy symbols: $SJ_COUNT"

if [ "$SID_COUNT" -gt 0 ] || [ "$SJ_COUNT" -gt 0 ]; then
    echo "RESULT: PASS - Strategy 29 received matching signals during the weekend"
else
    echo "RESULT: WARNING - Zero matching signals for strategy 29 this weekend"
    echo "Possible causes:"
    echo "  1. Natural: market produced no SHORT signals on strategy's altcoin symbols at trigger_score>=95"
    echo "  2. Pipeline: additional filters beyond the 1m block may be gating strategy 29"
    echo "  3. Timing: strategy was registered and fix deployed mid-weekend (Sun ~15:18 UTC)"
fi
