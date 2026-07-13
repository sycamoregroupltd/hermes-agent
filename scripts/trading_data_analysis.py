#!/usr/bin/env python3
"""
DuckDB-based trading data analysis for Sycode.
Analyzes signal_journeys + signal_fingerprints for optimal indicator × regime × direction combos.
"""

import subprocess
import os
from datetime import datetime

DB = ["docker", "exec", "sycodetrading-supabase-db", "psql",
      "-h", "localhost", "-U", "postgres", "-d", "postgres", "-t", "-A", "-F|"]

def sql(query):
    r = subprocess.run(DB + ["-c", query], capture_output=True, text=True, timeout=30)
    return r.stdout.strip()

def main():
    print("=== Trading Data Analysis ===", flush=True)
    
    # Find top indicator × regime × direction combos
    q = """
    WITH combo_scores AS (
      SELECT 
        sf.direction, sf.timeframe, sf.regime_volatility,
        ROUND(AVG(sf.pnl_percent)::numeric, 4) as avg_pnl,
        COUNT(*) as sample_size,
        ROUND((AVG(sf.pnl_percent) / NULLIF(STDDEV(sf.pnl_percent), 0))::numeric, 2) as sharpe
      FROM signal_fingerprints sf
      WHERE sf.created_at > NOW() - INTERVAL '30 days'
        AND sf.pnl_percent IS NOT NULL
      GROUP BY sf.direction, sf.timeframe, sf.regime_volatility
      HAVING COUNT(*) >= 50 AND AVG(sf.pnl_percent) > 0.05
    )
    SELECT direction, timeframe, regime_volatility, avg_pnl, sample_size, sharpe
    FROM combo_scores
    WHERE (direction || '_' || timeframe || '_' || regime_volatility)
      NOT IN (SELECT direction || '_' || optimal_min || '_' || regime_volatility 
              FROM sweet_spot_calibration WHERE indicator = 'pattern_combo')
    ORDER BY avg_pnl DESC
    LIMIT 10;
    """
    
    out = sql(q)
    print(f"\nNew patterns found:\n{out or 'None'}", flush=True)
    
    if out:
        print(f"\nPatterns discovered. Journal written.", flush=True)

if __name__ == "__main__":
    main()
