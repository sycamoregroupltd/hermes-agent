#!/bin/bash
# Daily pattern mining — discovers new indicator×regime×timeframe combos
docker exec sycodetrading-supabase-db psql -h localhost -U postgres -d postgres -t -A -F'|' <<'SQL' > /tmp/pattern_results.csv
WITH combo_scores AS (
  SELECT 
    sf.indicators->>'volumeRatio' as vol_ratio,
    sf.regime_volatility, sf.timeframe, sf.direction,
    AVG(sf.pnl_percent) as avg_pnl,
    COUNT(*) as samples
  FROM signal_fingerprints sf
  WHERE sf.created_at > NOW() - INTERVAL '7 days'
    AND sf.pnl_percent IS NOT NULL
  GROUP BY sf.indicators->>'volumeRatio', sf.regime_volatility, sf.timeframe, sf.direction
  HAVING COUNT(*) >= 20 AND AVG(sf.pnl_percent) > 0.1
  ORDER BY AVG(sf.pnl_percent) DESC
  LIMIT 10
)
SELECT direction, timeframe, regime_volatility, ROUND(avg_pnl::numeric, 2), samples
FROM combo_scores;
SQL

# If we found something new, add it
if [ -s /tmp/pattern_results.csv ]; then
  echo "Patterns found:"
  cat /tmp/pattern_results.csv
  # Could auto-INSERT here
else
  echo "No new patterns in the last 7 days"
fi
