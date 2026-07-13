#!/usr/bin/env python3
"""Real-time Trader Agent — polls signal_journeys every 1s for new signals."""

import subprocess, time, os

DB = ["docker", "exec", "sycodetrading-supabase-db", "psql",
      "-h", "localhost", "-U", "postgres", "-d", "postgres",
      "-t", "-A", "-F|"]

def sql(query):
    r = subprocess.run(DB + ["-c", query], capture_output=True, text=True, timeout=10)
    return r.stdout.strip()

last_check = time.time()
print("[Trader Agent] Running...", flush=True)

while True:
    try:
        q = f"""
        SELECT correlation_id, symbol, direction, timeframe,
               pnl_percent, bars_held
        FROM signal_journeys
        WHERE created_at > to_timestamp({last_check})
          AND pnl_percent IS NOT NULL
        ORDER BY created_at ASC;
        """
        out = sql(q)
        if out:
            for line in out.split('\n'):
                parts = line.split('|')
                if len(parts) < 6:
                    continue
                sid, sym, direc, tf, pnl, bars = parts[:6]
                print(f"[Signal] {sym} {direc} {tf} PnL={pnl} bars={bars}", flush=True)
                
                # Quick strategy match (no jsonb — just symbol/direction/tf)
                match_q = f"""
                SELECT name FROM strategy_pool WHERE status='paper' AND (
                  (name='ScalpTrader' AND '{tf}' IN ('1m','5m'))
                  OR (name='CalibratedAdvisor' AND '{pnl}'::numeric > 0)
                  OR (name='FundingCarry_DeltaNeutral' AND '{tf}' IN ('4h','1d'))
                  OR (name LIKE 'LONG_4h%' AND '{direc}'='LONG' AND '{tf}'='4h')
                ) LIMIT 1;
                """
                match = sql(match_q)
                if match:
                    print(f"  -> Matched: {match}", flush=True)
        
        last_check = time.time()
        time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
        break
    except Exception as e:
        print(f"[Err] {e}", flush=True)
        time.sleep(5)
