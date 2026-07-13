#!/usr/bin/env python3
"""Performance wiki feed — daily P&L dashboard to Obsidian.
Runs once daily. Writes markdown to quant-team vault."""

import subprocess, json, os, sys
from datetime import datetime, timezone

sys.path.insert(0, "/home/frank/.hermes/scripts")
from second_brain_writer import write_markdown_atomic

PGPASSWORD = os.environ.get("POSTGRES_PASSWORD") or os.environ.get("PGPASSWORD") or ""
DB = ["docker", "exec", "-e", f"PGPASSWORD={PGPASSWORD}", "sycodetrading-supabase-db",
      "psql", "-U", "postgres", "-d", "postgres", "-t", "-A"]
OBSIDIAN = "/home/frank/obsidian/quant-team/performance"

def db(sql):
    r = subprocess.run(DB + ["-c", sql], capture_output=True, text=True, timeout=30)
    return r.stdout.strip()

os.makedirs(OBSIDIAN, exist_ok=True)

today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
week_start = datetime.now(timezone.utc).strftime('%Y-%m-%d')

# ── Today ──
today_data = db("""
SELECT json_build_object(
  'pnl', COALESCE(round(sum(realized_pnl)::numeric, 2), 0),
  'trades', count(*)::int,
  'wins', sum(case when realized_pnl > 0 then 1 else 0 end)::int,
  'losses', sum(case when realized_pnl < 0 then 1 else 0 end)::int
)::text FROM managed_positions
WHERE closed_at >= now() - interval '24 hours' AND realized_pnl IS NOT NULL;
""")

# ── This week ──
week_data = db("""
SELECT json_build_object(
  'pnl', COALESCE(round(sum(realized_pnl)::numeric, 2), 0),
  'trades', count(*)::int,
  'wins', sum(case when realized_pnl > 0 then 1 else 0 end)::int,
  'losses', sum(case when realized_pnl < 0 then 1 else 0 end)::int
)::text FROM managed_positions
WHERE closed_at >= now() - interval '7 days' AND realized_pnl IS NOT NULL;
""")

# ── Top strategies this week ──
strategies = db("""
SELECT string_agg(json_build_object(
  'name', strategy_name,
  'pnl', round(sum(realized_pnl)::numeric, 2),
  'trades', count(*)::int
)::text, '||' ORDER BY sum(realized_pnl) DESC)
FROM managed_positions
WHERE closed_at >= now() - interval '7 days' AND realized_pnl IS NOT NULL
GROUP BY strategy_name
ORDER BY sum(realized_pnl) DESC
LIMIT 15;
""")

# ── All-time ──
alltime = db("""
SELECT json_build_object(
  'pnl', COALESCE(round(sum(realized_pnl)::numeric, 2), 0),
  'trades', count(*)::int,
  'wins', sum(case when realized_pnl > 0 then 1 else 0 end)::int,
  'losses', sum(case when realized_pnl < 0 then 1 else 0 end)::int
)::text FROM managed_positions
WHERE realized_pnl IS NOT NULL;
""")

t = json.loads(today_data) if today_data else {}
w = json.loads(week_data) if week_data else {}
a = json.loads(alltime) if alltime else {}

today_pnl = float(t.get('pnl', 0))
week_pnl = float(w.get('pnl', 0))
all_pnl = float(a.get('pnl', 0))

# ── Build markdown ──
lines = []
lines.append(f"# Trading Performance — {today}")
lines.append("")
lines.append(f"**Auto-generated at {datetime.now(timezone.utc).strftime('%H:%M UTC')}**")
lines.append("")
lines.append("## Today")
lines.append(f"- P&L: **${today_pnl:+.2f}**")
lines.append(f"- Trades: {t.get('trades', 0)} ({t.get('wins', 0)}W / {t.get('losses', 0)}L)")
lines.append("")
lines.append("## This Week (7d)")
lines.append(f"- P&L: **${week_pnl:+.2f}**")
lines.append(f"- Trades: {w.get('trades', 0)} ({w.get('wins', 0)}W / {w.get('losses', 0)}L)")
lines.append("")
lines.append("## All-Time")
lines.append(f"- P&L: **${all_pnl:+.2f}**")
lines.append(f"- Trades: {a.get('trades', 0)} ({a.get('wins', 0)}W / {a.get('losses', 0)}L)")
lines.append("")

# Strategies
if strategies and strategies.strip():
    lines.append("## Top Strategies (7d)")
    lines.append("")
    lines.append("| Strategy | P&L | Trades |")
    lines.append("|----------|-----|--------|")
    for s in strategies.split('||'):
        s = s.strip().strip('"')
        if not s:
            continue
        try:
            obj = json.loads(s)
            lines.append(f"| {obj.get('name','?')} | ${float(obj.get('pnl',0)):+.2f} | {obj.get('trades',0)} |")
        except:
            pass

lines.append("")
lines.append("---")
lines.append(f"_Updated {datetime.now(timezone.utc).isoformat()}_")

content = '\n'.join(lines)
filename = f"{OBSIDIAN}/{today}-performance.md"
write_markdown_atomic(
    filename,
    content,
    title=f"Trading Performance — {today}",
    type="task-evidence",
    status="active",
    created=today,
    updated=today,
    confidence="high",
    tags=["sycode", "performance", "paper-trading"],
    sources=["sycodetrading-supabase-db:managed_positions"],
    project="sycode-trading",
    owners=["jarvis"],
    knowledge_tier="evidence",
    generated=True,
    generator="performance_wiki_feed.py",
)

print(f"Wrote performance dashboard → {filename}")
