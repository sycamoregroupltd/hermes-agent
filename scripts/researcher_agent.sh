#!/bin/bash
# Write researcher discovery results to Obsidian vault
DATE=$(date +%Y-%m-%d)
TIME=$(date +%H:%M)
OUTPUT="/home/frank/obsidian/quant-team/research/patterns/${DATE}-discovery.md"

docker exec -e PGPASSWORD=*** sycodetrading-supabase-db psql -h localhost -U postgres -d postgres -t -A -F',' <<'SQL' > /tmp/research_results.csv
SELECT direction, timeframe, regime_volatility, 
  ROUND(AVG(pnl_percent)::numeric, 4),
  COUNT(*),
  ROUND((AVG(pnl_percent) / NULLIF(STDDEV(pnl_percent), 0))::numeric, 2)
FROM signal_fingerprints
WHERE created_at > NOW() - INTERVAL '7 days'
  AND pnl_percent IS NOT NULL
GROUP BY direction, timeframe, regime_volatility
HAVING COUNT(*) >= 20 AND AVG(pnl_percent) > 0.05
ORDER BY AVG(pnl_percent) DESC
LIMIT 10;
SQL

cat > "$OUTPUT" << EOF
# Pattern Discovery — ${DATE} ${TIME}

| Direction | Timeframe | Regime | Avg PnL | Samples | Sharpe |
|---|---|---|---|---|---|
EOF

while IFS=',' read -r dir tf vol pnl samples sharpe; do
  echo "| $dir | $tf | $vol | $pnl | $samples | $sharpe |" >> "$OUTPUT"
done < /tmp/research_results.csv

echo "" >> "$OUTPUT"
echo "Auto-discovered by Researcher Agent — $(date)" >> "$OUTPUT"
echo "Written to: $OUTPUT"
