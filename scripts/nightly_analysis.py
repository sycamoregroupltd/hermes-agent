#!/usr/bin/env python3
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Nightly historical analysis: finds patterns in n8n_market_data."""
import subprocess, json
from datetime import datetime, timedelta

from second_brain_writer import write_markdown_atomic

DB = [
    "docker", "exec", "-i", "sycodetrading-supabase-db",
    "psql", "-U", "postgres", "-d", "postgres",
    "-v", "ON_ERROR_STOP=1", "-t", "-A",
]

def db(sql):
    r = subprocess.run(DB + ["-c", sql], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout.strip()

def db_rows(sql):
    r = subprocess.run(DB + ["-c", sql], capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return [l.strip() for l in r.stdout.strip().split('\n') if l.strip()]

# 1. Fear/Greed trend
fg_rows = db_rows("""
    SELECT payload->'data'->>'classification', payload->'data'->>'value', captured_at::date
    FROM n8n_market_data WHERE source='fear-greed'
    AND captured_at > NOW() - INTERVAL '7 days'
    ORDER BY captured_at;
""")
avg_fg = 50
if fg_rows:
    vals = [int(r.split('|')[1]) for r in fg_rows if len(r.split('|')) > 1 and r.split('|')[1].isdigit()]
    avg_fg = sum(vals) / len(vals) if vals else 50

# 2. Avg arb spread
arb_rows = db_rows("""
    SELECT (payload->'data'->>'spread')::float * 10000
    FROM n8n_market_data WHERE source='funding-arb'
    AND captured_at > NOW() - INTERVAL '7 days';
""")
avg_spread = 0
max_spread = 0
if arb_rows:
    spreads = [float(r) for r in arb_rows if r]
    avg_spread = sum(spreads) / len(spreads) if spreads else 0
    max_spread = max(spreads) if spreads else 0

# 3. Signal stats
sig_rows = db_rows("""
    SELECT payload->>'trades_opened', payload->>'trades_skipped'
    FROM n8n_market_data WHERE source='paper-trader'
    AND captured_at > NOW() - INTERVAL '24 hours';
""")
total_opened = sum(int(r.split('|')[0]) for r in sig_rows if len(r.split('|')) > 0 and r.split('|')[0].isdigit())
total_skipped = sum(int(r.split('|')[1]) for r in sig_rows if len(r.split('|')) > 1 and r.split('|')[1].isdigit())

# 4. PnL trend
pnl_rows = db_rows("""
    SELECT (payload->>'balance')::float, captured_at::timestamp::time
    FROM n8n_market_data WHERE source='pnl-snapshot'
    AND captured_at > NOW() - INTERVAL '24 hours'
    ORDER BY captured_at;
""")
pnl_change = 0
if len(pnl_rows) >= 2:
    first = float(pnl_rows[0].split('|')[0]) if pnl_rows[0].split('|')[0] else 0
    last = float(pnl_rows[-1].split('|')[0]) if pnl_rows[-1].split('|')[0] else 0
    pnl_change = last - first

# 5. Generate report
report = f"""# Trading Analysis Report
{datetime.now().strftime('%Y-%m-%d %H:%M')}

## Market Context
- Avg Fear/Greed (7d): {avg_fg:.0f}/100 ({'Extreme Fear' if avg_fg < 20 else 'Fear' if avg_fg < 40 else 'Neutral' if avg_fg < 60 else 'Greed' if avg_fg < 80 else 'Extreme Greed'})
- Avg Arb Spread (7d): {avg_spread:.2f}bps
- Max Arb Spread (7d): {max_spread:.2f}bps

## Trading Activity (24h)
- Trades Opened: {total_opened}
- Trades Skipped: {total_skipped}
- PnL Change: ${pnl_change:.2f}

## DB Health
- Sources: 6 (fear-greed, funding-arb, paper-trader, pnl-snapshot, n8n-executions, +more)
- Total Rows: see n8n_market_data
"""

# Write report to Obsidian
report_date = datetime.now().strftime('%Y-%m-%d')
report_path = f"/home/frank/obsidian/quant-team/analytics/{report_date}-nightly.md"
write_markdown_atomic(
    report_path,
    report,
    title=f"Trading Analysis Report — {report_date}",
    type="task-evidence",
    status="active",
    created=report_date,
    updated=report_date,
    confidence="medium",
    tags=["sycode", "analytics", "nightly"],
    sources=["sycodetrading-supabase-db:n8n_market_data"],
    project="sycode-trading",
    owners=["quant-researcher"],
    knowledge_tier="evidence",
    generated=True,
    generator="nightly_analysis.py",
)

# Notification bus retired (t_056cb9dd): durable output is the Obsidian report.
print(f"[ANALYSIS] Avg FG={avg_fg:.0f} Avg arb={avg_spread:.2f}bps PnL=${pnl_change:.2f}")

print(f"Report written to {report_path}")
print(report)
