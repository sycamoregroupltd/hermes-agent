#!/usr/bin/env python3
# SHIM — approved exec wrapper. Canonical source is ~/.hermes/scripts/signal_quality.py.
# CANONICAL SOURCE — do not edit profile-local copies. See the goal-orchestrator-operating-runbook for the canonical-copy rule.
"""Signal quality analysis: measures win rates by pattern, timeframe, confidence, direction.
Runs daily via cron. Writes to Obsidian."""
# Dedupe note (t_8c18ef11, 2026-07-03): profile copies should be hardlinks to
# this root script when identical; cron sandboxes reject cross-dir symlink targets.
import subprocess, json, os, sys
from datetime import datetime, timedelta

# Secret scrubber — never persist a captured token (see redact() docstring).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from secret_redact import redact
def safe_err(label, proc):
    """Return a redacted stderr string safe to log/persist (token masked)."""
    return f"{label}: {redact((proc.stderr or '')[:2000])}"

from second_brain_writer import write_markdown_atomic

OC = "http://localhost:3001/api/openclaw"
# Credential loading: shared env file (defaults to sycode-credential.env); env vars override.
_CRED_ENV_FILE = os.environ.get("SYCODE_CREDENTIAL_ENV_FILE", "/home/frank/.hermes/secrets/sycode-credential.env")
if os.path.exists(_CRED_ENV_FILE):
    from dotenv import load_dotenv
    load_dotenv(_CRED_ENV_FILE, override=False)

TOKEN = os.environ.get("SYCODE_READ_TOKEN") or os.environ.get("OPENCLAW_READ_TOKEN")
if not TOKEN:
    print(f"[FATAL] Missing OpenClaw READ token. Set SYCODE_READ_TOKEN or OPENCLAW_READ_TOKEN\n"
          f"       in env or in {_CRED_ENV_FILE}.", flush=True)
    sys.exit(3)
# Use tuple-only, unaligned psql output so downstream parsers do not ingest headers.
DB = ["docker", "exec", "-i", "sycodetrading-supabase-db", "psql", "-U", "postgres", "-d", "postgres", "-t", "-A"]

def db(sql):
    r = subprocess.run(DB + ["-c", sql], capture_output=True, text=True, timeout=30)
    return r.stdout.strip()

def db_rows(sql):
    r = subprocess.run(DB + ["-c", sql], capture_output=True, text=True, timeout=30)
    return [l for l in r.stdout.strip().split('\n') if l]

# 1. Signal journey stats from OpenClaw
r = subprocess.run(["curl", "-s", "--connect-timeout", "5", "--max-time", "15",
    "-H", f"X-Sycode-Token:{TOKEN}", f"{OC}/signals/journey/stats"],
    capture_output=True, text=True, timeout=20)
if r.returncode != 0:
    # Failure leaks the argv repr (embedding the token) into stderr. Mask it
    # before logging so the cron job's persisted last_error stays secret-free.
    print(safe_err("OPENCLAW_STATS_FAILED", r), file=sys.stderr)
    sys.exit(1)
stats = json.loads(r.stdout) if r.stdout else {}

# 2. Paper trader performance from DB
trader_rows = db_rows("""
    SELECT payload->>'trades_opened', payload->>'trades_skipped', payload->>'fear_greed', payload->>'threshold'
    FROM n8n_market_data WHERE source='paper-trader'
    AND captured_at > NOW() - INTERVAL '24 hours'
    ORDER BY captured_at;
""")

total_opened = sum(int(r.split('|')[0]) for r in trader_rows if len(r.split('|')) > 0 and r.split('|')[0].isdigit())
total_skipped = sum(int(r.split('|')[1]) for r in trader_rows if len(r.split('|')) > 1 and r.split('|')[1].isdigit())

# Average fear/greed during trades
fg_vals = []
for r in trader_rows:
    parts = r.split('|')
    if len(parts) > 2 and parts[2].isdigit():
        fg_vals.append(int(parts[2]))
avg_fg = round(sum(fg_vals) / len(fg_vals)) if fg_vals else 0

# 3. PnL trend
pnl_rows = db_rows("""
    SELECT (payload->>'balance')::float FROM n8n_market_data WHERE source='pnl-snapshot'
    AND captured_at > NOW() - INTERVAL '24 hours'
    ORDER BY captured_at;
""")
pnl_start = float(pnl_rows[0]) if pnl_rows else 0
pnl_end = float(pnl_rows[-1]) if pnl_rows else 0
pnl_change = pnl_end - pnl_start

# 4. Fundin arb trends
arb_rows = db_rows("""
    SELECT (payload->'data'->>'spread')::float * 10000
    FROM n8n_market_data WHERE source='funding-arb'
    AND captured_at > NOW() - INTERVAL '24 hours';
""")
avg_arb = round(sum(float(r) for r in arb_rows) / len(arb_rows), 2) if arb_rows else 0

# 5. Fear/greed history
fg_rows = db_rows("""
    SELECT payload->'data'->>'value' FROM n8n_market_data WHERE source='fear-greed'
    AND captured_at > NOW() - INTERVAL '7 days' ORDER BY captured_at;
""")
fg_history = [int(r) for r in fg_rows if r.isdigit()]
fg_trend = "rising" if len(fg_history) > 2 and fg_history[-1] > fg_history[0] else \
           "falling" if len(fg_history) > 2 and fg_history[-1] < fg_history[0] else "stable"

# 6. Build report
report = f"""# Signal Quality Report
{datetime.now().strftime('%Y-%m-%d %H:%M')}

## Market Conditions (24h)
- Fear/Greed avg: {avg_fg} ({'Extreme Fear' if avg_fg < 20 else 'Fear' if avg_fg < 40 else 'Neutral' if avg_fg < 60 else 'Greed' if avg_fg < 80 else 'Extreme Greed'})
- Fear/Greed trend (7d): {fg_trend}
- Funding arb avg spread: {avg_arb}bps
- PnL change (24h): ${pnl_change:.2f}

## Paper Trader Activity (24h)
- Trades opened: {total_opened}
- Trades skipped: {total_skipped}
- OpenClaw stats: {json.dumps(stats, indent=2) if stats else 'not available'}

## OpenClaw Signal Stats
- Total journeys: {stats.get('total', '?')}
- Wins: {stats.get('wins', '?')}
- Losses: {stats.get('losses', '?')}

## Recommendations
"""

if avg_fg < 20:
    report += "- Market in Extreme Fear — conservative trading recommended\n"
elif avg_fg > 80:
    report += "- Market in Extreme Greed — consider SHORT positions\n"

if avg_arb < 1:
    report += "- Arb spreads low — arb trading unlikely to trigger\n"
elif avg_arb > 3:
    report += "- Arb spreads elevated — monitor for opportunities\n"

if pnl_change < 0:
    report += "- PnL declining — review open positions\n"

report += "- Next analysis: " + (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')

# Write to Obsidian
report_date = datetime.now().strftime('%Y-%m-%d')
report_path = f"/home/frank/obsidian/quant-team/analytics/signal-quality-{report_date}.md"
write_markdown_atomic(
    report_path,
    report,
    title=f"Signal Quality Report — {report_date}",
    type="task-evidence",
    status="active",
    created=report_date,
    updated=report_date,
    confidence="medium",
    tags=["sycode", "analytics", "signal-quality"],
    sources=["sycodetrading-supabase-db:n8n_market_data", "openclaw:/signals/journey/stats"],
    project="sycode-trading",
    owners=["quant-researcher"],
    knowledge_tier="evidence",
    generated=True,
    generator="signal_quality.py",
)

# Notification bus retired (t_056cb9dd): durable outputs are Obsidian report + DB/#quant-reports.
print(f"[SIGNAL-QUALITY] AvgFG={avg_fg} PnL=${pnl_change:.2f} Opened={total_opened}")

print(f"Report: {report_path}")
print(report[:300])
