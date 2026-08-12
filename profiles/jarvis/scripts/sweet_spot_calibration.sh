#!/bin/bash
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
# Sweet spot calibration — discovers optimal indicator thresholds per regime
docker exec -e PGPASSWORD=postgres sycodetrading-supabase-db psql -h localhost -U postgres -d postgres -c "
SELECT indicator, direction, regime_volatility, optimal_min, optimal_max, sample_size, avg_pnl, confidence
FROM sweet_spot_calibration
ORDER BY last_calibrated DESC, avg_pnl DESC NULLS LAST
LIMIT 20;
"
