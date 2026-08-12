#!/usr/bin/env bash
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
# Dual MA paper lifecycle diagnostic monitor
# Checks every tick for fresh intents, reports on 10-intent cap, 24h expiry, or stop gates
# Auto-disables the strategy on: 24h expiry, 10-intent cap reached, stop gate firing, or insufficient activity
set -euo pipefail

STRATEGY_ID="feb9aa41-f19e-4dbc-86a0-627fd19143b6"
ENABLE_TIMESTAMP="2026-06-29 09:19:44+00"
ENABLE_EPOCH=$(date -d "$ENABLE_TIMESTAMP" +%s)
NOW_EPOCH=$(date +%s)
ELAPSED=$(( (NOW_EPOCH - ENABLE_EPOCH) / 3600 ))

# 1. Check 24h expiry
if [ "$ELAPSED" -ge 24 ]; then
  echo "[[24H_EXPIRED]] Diagnostic window expired at $(date -d @"$(( ENABLE_EPOCH + 86400 ))")"
  echo "[[ACTION]] Disabling strategy $STRATEGY_ID due to 24h expiry"
  PGPASSWORD=postgres psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -c "UPDATE strategies SET enabled = false, updated_at = NOW() WHERE id = '$STRATEGY_ID';" 2>/dev/null
  echo "[[DONE]] Strategy $STRATEGY_ID disabled"
  exit 0
fi

# 2. Query fresh intents since enable
FRESH_INTENTS=$(PGPASSWORD=postgres psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -t -A -c "
  SELECT COUNT(*) FROM trade_intents
  WHERE strategy_id = '$STRATEGY_ID'
    AND COALESCE(executed_at, created_at) >= '$ENABLE_TIMESTAMP';
" 2>/dev/null || echo "0")

FRESH_EXECUTED=$(PGPASSWORD=postgres psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -t -A -c "
  SELECT COUNT(*) FROM trade_intents
  WHERE strategy_id = '$STRATEGY_ID'
    AND status = 'executed'
    AND COALESCE(executed_at, created_at) >= '$ENABLE_TIMESTAMP';
" 2>/dev/null || echo "0")

FRESH_REJECTED=$(PGPASSWORD=postgres psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -t -A -c "
  SELECT COUNT(*) FROM trade_intents
  WHERE strategy_id = '$STRATEGY_ID'
    AND status = 'rejected'
    AND COALESCE(executed_at, created_at) >= '$ENABLE_TIMESTAMP';
" 2>/dev/null || echo "0")

FRESH_FAILED=$(PGPASSWORD=postgres psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -t -A -c "
  SELECT COUNT(*) FROM trade_intents
  WHERE strategy_id = '$STRATEGY_ID'
    AND status = 'failed'
    AND COALESCE(executed_at, created_at) >= '$ENABLE_TIMESTAMP';
" 2>/dev/null || echo "0")

# 3a. Stop gate: strategy left paper mode
MODE=$(PGPASSWORD=postgres psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -t -A -c "
  SELECT trading_mode FROM strategies WHERE id = '$STRATEGY_ID';
" 2>/dev/null || echo "paper")
if [ "$MODE" != "paper" ]; then
  echo "[[STOP_GATE]] strategy trading_mode is '$MODE', expected 'paper'"
  echo "[[ACTION]] Disabling strategy $STRATEGY_ID — left paper mode"
  PGPASSWORD=postgres psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -c "UPDATE strategies SET enabled = false, updated_at = NOW() WHERE id = '$STRATEGY_ID';" 2>/dev/null
  echo "[[DONE]] Strategy $STRATEGY_ID disabled"
fi

# 3b. Stop gate: live trading became active
LIVE_TRADING=$(PGPASSWORD=postgres psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -t -A -c "
  SELECT COUNT(*) FROM trade_intents
  WHERE strategy_id = '$STRATEGY_ID'
    AND trading_mode = 'live'
    AND COALESCE(executed_at, created_at) >= '$ENABLE_TIMESTAMP';
" 2>/dev/null || echo "0")
if [ "$LIVE_TRADING" -gt 0 ]; then
  echo "[[STOP_GATE]] $LIVE_TRADING live trade_intent(s) detected since enable"
  echo "[[ACTION]] Disabling strategy $STRATEGY_ID — live trading detected"
  PGPASSWORD=postgres psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -c "UPDATE strategies SET enabled = false, updated_at = NOW() WHERE id = '$STRATEGY_ID';" 2>/dev/null
  echo "[[DONE]] Strategy $STRATEGY_ID disabled"
fi

# 3. Check missing position refs
MISSING_POS=$(PGPASSWORD=postgres psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -t -A -c "
  SELECT COUNT(*) FROM trade_intents
  WHERE strategy_id = '$STRATEGY_ID'
    AND status = 'executed'
    AND position_id IS NULL
    AND COALESCE(executed_at, created_at) >= '$ENABLE_TIMESTAMP';
" 2>/dev/null || echo "0")

# 4. Check 10-intent cap
if [ "$FRESH_EXECUTED" -ge 10 ]; then
  echo "[[10_INTENT_CAP]] $FRESH_EXECUTED fresh executed intents reached"
  echo "[[ACTION]] Disabling strategy $STRATEGY_ID — 10-intent cap"
  PGPASSWORD=postgres psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -c "UPDATE strategies SET enabled = false, updated_at = NOW() WHERE id = '$STRATEGY_ID';" 2>/dev/null
  echo "[[DONE]] Strategy $STRATEGY_ID disabled"
fi

# 5. Stop gate: rejected + failed > executed
REJ_FAIL=$(( FRESH_REJECTED + FRESH_FAILED ))
if [ "$REJ_FAIL" -gt "$FRESH_EXECUTED" ] && [ "$FRESH_EXECUTED" -gt 0 ]; then
  echo "[[STOP_GATE]] rejected+failed ($REJ_FAIL) exceeds executed ($FRESH_EXECUTED)"
  echo "[[ACTION]] Disabling strategy $STRATEGY_ID — reject/fail ratio exceeded"
  PGPASSWORD=postgres psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -c "UPDATE strategies SET enabled = false, updated_at = NOW() WHERE id = '$STRATEGY_ID';" 2>/dev/null
  echo "[[DONE]] Strategy $STRATEGY_ID disabled"
fi

# 6. Stop gate: missing position refs > 0
if [ "$MISSING_POS" -gt 0 ]; then
  echo "[[STOP_GATE]] $MISSING_POS fresh executed intents missing position_id"
  echo "[[ACTION]] Disabling strategy $STRATEGY_ID — orphan position refs detected"
  PGPASSWORD=postgres psql -h 127.0.0.1 -p 5432 -U postgres -d postgres -c "UPDATE strategies SET enabled = false, updated_at = NOW() WHERE id = '$STRATEGY_ID';" 2>/dev/null
  echo "[[DONE]] Strategy $STRATEGY_ID disabled"
fi

# 7. Report summary
echo "[[REPORT]] timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ) elapsed_h=${ELAPSED}h fresh_total=${FRESH_INTENTS} fresh_executed=${FRESH_EXECUTED} fresh_rejected=${FRESH_REJECTED} fresh_failed=${FRESH_FAILED} missing_position_refs=${MISSING_POS}"
echo "[[STATE]] diagnostic_running=1"  # flip to 0 when stopped
