#!/bin/bash
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
# Hourly trading report — new signals, PnL, holding periods
docker exec -e PGPASSWORD=*** sycodetrading-supabase-db psql -h localhost -U postgres -d postgres <<'SQL'
SELECT '=== NEW SIGNALS (last hour) ===' as report;

SELECT LEFT(correlation_id, 8) as id, symbol, direction, timeframe,
  ROUND(pnl_percent::numeric, 2) as pnl_pct,
  bars_held,
  CASE 
    WHEN timeframe = '1m' THEN bars_held || ' min'
    WHEN timeframe = '5m' THEN (bars_held * 5) || ' min'
    WHEN timeframe = '15m' THEN (bars_held * 15) || ' min'
    WHEN timeframe = '1h' THEN bars_held || ' hours'
    WHEN timeframe = '4h' THEN (bars_held * 4) || ' hours'
    ELSE bars_held || ' bars'
  END as duration,
  trajectory_label,
  created_at::timestamp::time as time
FROM signal_journeys
WHERE created_at > NOW() - INTERVAL '1 hour'
  AND pnl_percent IS NOT NULL
ORDER BY created_at DESC
LIMIT 15;

SELECT '=== BEST VS WORST ===' as section;

SELECT 
  ROUND(AVG(pnl_percent)::numeric, 2) as avg_pnl,
  ROUND(AVG(bars_held)) as avg_bars_held,
  COUNT(*) as total_signals,
  ROUND(SUM(CASE WHEN pnl_percent > 0 THEN 1 ELSE 0 END)::numeric / COUNT(*) * 100, 1) as win_rate_pct
FROM signal_journeys
WHERE created_at > NOW() - INTERVAL '1 hour'
  AND pnl_percent IS NOT NULL;

SELECT '=== TOP SIGNALS (best 3) ===' as section;

SELECT LEFT(correlation_id, 8) as id, symbol, direction, timeframe,
  ROUND(pnl_percent::numeric, 2) as pnl_pct, bars_held, trajectory_label
FROM signal_journeys
WHERE created_at > NOW() - INTERVAL '1 hour'
  AND pnl_percent IS NOT NULL
ORDER BY pnl_percent DESC
LIMIT 3;
SQL
