#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""
Pro Trader Tournament Pipeline
Living system: discover, score, tier, regime-track, archive.
Runs every 6h via cron.
"""
import subprocess
import json
from datetime import datetime, timedelta
from collections import defaultdict

DB_CONTAINER = "sycodetrading-supabase-db"
DB_NAME = "postgres"
DB_USER = "postgres"

def run_psql(sql):
    cmd = [
        "docker", "exec", "-e", "PGPASSWORD=***",
        DB_CONTAINER, "psql", "-h", "localhost", "-U", DB_USER, "-d", DB_NAME,
        "-t", "-A", "-c", sql
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"PSQL ERROR: {result.stderr}")
        return []
    return [line.strip() for line in result.stdout.strip().split('\n') if line.strip()]

def discover_new_traders():
    # Placeholder: in real impl would fetch Hyperliquid leaderboard
    # For now just count current UNRATED
    rows = run_psql("SELECT COUNT(*) FROM pro_trader_profiles WHERE tier='UNRATED' AND removed_at IS NULL;")
    new_count = int(rows[0]) if rows else 0
    print(f"Discovery: {new_count} UNRATED traders (would fetch new from leaderboard)")
    return new_count

def score_and_tier():
    # Compute simple 30d metrics and update tiers
    sql = """
    WITH metrics AS (
      SELECT 
        wallet_address,
        COUNT(*) FILTER (WHERE closed_at > NOW() - INTERVAL '30 days') as trades_30d,
        COUNT(*) FILTER (WHERE closed_at > NOW() - INTERVAL '30 days' AND pnl > 0) as wins_30d,
        SUM(pnl) FILTER (WHERE closed_at > NOW() - INTERVAL '30 days') as total_pnl_30d
      FROM pro_trader_positions 
      WHERE status='CLOSED' 
      GROUP BY wallet_address
    )
    UPDATE pro_trader_profiles p
    SET 
      tier = CASE 
        WHEN m.trades_30d >= 30 AND (m.wins_30d::float / NULLIF(m.trades_30d,0)) > 0.55 THEN 'S-TIER'
        WHEN m.trades_30d >= 15 AND (m.wins_30d::float / NULLIF(m.trades_30d,0)) > 0.52 THEN 'A-TIER'
        WHEN m.trades_30d >= 10 AND (m.wins_30d::float / NULLIF(m.trades_30d,0)) > 0.50 THEN 'B-TIER'
        WHEN m.trades_30d = 0 THEN 'PROBATION'
        ELSE 'DROPPED'
      END,
      updated_at = NOW()
    FROM metrics m
    WHERE p.wallet_address = m.wallet_address AND p.removed_at IS NULL;
    """
    run_psql(sql)
    print("Scoring & tiering complete")

def update_regime_performance():
    regimes = ['volatile_bull', 'stable_bull', 'volatile_bear', 'stable_bear', 'sideways']
    for regime in regimes:
        sql = f"""
        INSERT INTO pro_trader_regime_performance (wallet_address, regime, total_trades, wins, losses, total_pnl, avg_pnl, win_rate)
        SELECT 
          wallet_address,
          '{regime}',
          COUNT(*),
          COUNT(*) FILTER (WHERE pnl > 0),
          COUNT(*) FILTER (WHERE pnl <= 0),
          SUM(pnl),
          AVG(pnl),
          (COUNT(*) FILTER (WHERE pnl > 0))::float / NULLIF(COUNT(*), 0)
        FROM pro_trader_positions
        WHERE status='CLOSED' AND closed_at > NOW() - INTERVAL '30 days'
        GROUP BY wallet_address
        ON CONFLICT (wallet_address, regime) DO UPDATE SET
          total_trades = EXCLUDED.total_trades,
          wins = EXCLUDED.wins,
          losses = EXCLUDED.losses,
          total_pnl = EXCLUDED.total_pnl,
          avg_pnl = EXCLUDED.avg_pnl,
          win_rate = EXCLUDED.win_rate,
          last_updated = NOW();
        """
        run_psql(sql)
    print("Regime performance updated")

def archive_old_positions():
    sql = """
    DELETE FROM pro_trader_positions 
    WHERE status='CLOSED' AND closed_at < NOW() - INTERVAL '30 days';
    """
    rows = run_psql(sql)
    print(f"Archived old closed positions")

def main():
    print(f"=== Pro Trader Pipeline Run: {datetime.now().isoformat()} ===")
    new = discover_new_traders()
    score_and_tier()
    update_regime_performance()
    archive_old_positions()
    print("Pipeline complete.")

if __name__ == "__main__":
    main()
