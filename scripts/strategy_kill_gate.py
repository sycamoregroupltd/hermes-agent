#!/usr/bin/env python3
"""Weekly strategy kill gate.

Flags strategies with negative trailing PnL over consecutive weekly windows and
files idempotency-keyed investigation cards for trading-risk-reviewer. This
script does not mutate strategy rows; it is paper-safety card filing only.
Silent exit if no strategies are flagged.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

DB = ["docker", "exec", "-e", "PGPASSWORD=postgres", "sycodetrading-supabase-db",
      "psql", "-U", "postgres", "-d", "postgres", "-t", "-A"]
HERMES = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")
BOARD = os.environ.get("KILL_GATE_BOARD", "sycode-trading")
ASSIGNEE = os.environ.get("KILL_GATE_ASSIGNEE", "trading-risk-reviewer")
DRY_RUN = os.environ.get("KILL_GATE_DRY_RUN", "").lower() in {"1", "true", "yes"}


def db(sql):
    r = subprocess.run(DB + ["-c", sql], capture_output=True, text=True, timeout=30)
    return r.stdout.strip()


def notify(msg):
    # Notification bus retired by t_056cb9dd; cron stdout/Discord delivery is the only alert path.
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] [KILL-GATE] {msg}", flush=True)


def week_key(now=None):
    now = now or datetime.now(timezone.utc)
    iso = now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def slugify(value):
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:80] or "unknown-strategy"


def create_investigation_card(name, latest, prev, combined, trades):
    key = f"kill-gate-{slugify(name)}-{week_key()}"
    title = f"Kill-gate investigation: {name} losing 2 consecutive weeks"
    body = "\n".join([
        "Automated weekly strategy kill-gate finding from `strategy_kill_gate.py`.",
        "",
        f"- strategy: `{name}`",
        f"- latest_week_pnl: `{latest}`",
        f"- previous_week_pnl: `{prev}`",
        f"- combined_pnl: `{combined}`",
        f"- latest_week_trades: `{trades}`",
        f"- idempotency_key: `{key}`",
        "",
        "Acceptance: independently review paper-only strategy performance, decide whether quarantine/retirement documentation is needed, and do not approve any live trading, order action, strategy enablement, DB mutation, deploy/restart, credential/provider, or spend action from this card.",
    ])
    cmd = [
        HERMES, "kanban", "--board", BOARD, "create", title,
        "--assignee", ASSIGNEE,
        "--priority", "85",
        "--idempotency-key", key,
        "--body", body,
        "--json",
    ]
    if DRY_RUN:
        print("DRY_RUN_CARD " + json.dumps({"key": key, "title": title, "assignee": ASSIGNEE}, sort_keys=True))
        return {"dry_run": True, "key": key}
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        print(f"CARD_CREATE_FAILED key={key} rc={r.returncode} stderr={r.stderr.strip()}", flush=True)
        return {"error": r.stderr.strip(), "key": key}
    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError:
        payload = {"stdout": r.stdout.strip()}
    print("CARD_FILED " + json.dumps({"key": key, "result": payload}, sort_keys=True), flush=True)
    return payload


# Fixture hook for dry-run proof. Format: name|latest|prev|combined|trades per line.
flagged = os.environ.get("KILL_GATE_FIXTURE_LINES") or db("""
WITH weekly AS (
    SELECT strategy_name,
           date_trunc('week', closed_at) as week,
           sum(realized_pnl) as week_pnl,
           count(*) as week_trades
    FROM managed_positions
    WHERE realized_pnl IS NOT NULL AND closed_at >= now() - interval '4 weeks'
    GROUP BY strategy_name, date_trunc('week', closed_at)
),
ranked AS (
    SELECT strategy_name, week, week_pnl, week_trades,
           row_number() OVER (PARTITION BY strategy_name ORDER BY week DESC) as rn
    FROM weekly
)
SELECT r1.strategy_name,
       r1.week_pnl as latest_week_pnl,
       COALESCE(r2.week_pnl, 0) as prev_week_pnl,
       (r1.week_pnl + COALESCE(r2.week_pnl, 0)) as combined_pnl,
       r1.week_trades as latest_trades
FROM ranked r1
LEFT JOIN ranked r2 ON r1.strategy_name = r2.strategy_name AND r1.rn = 1 AND r2.rn = 2
WHERE r1.rn = 1
  AND r1.week_pnl < 0
  AND (r2.week_pnl IS NOT NULL AND r2.week_pnl < 0)
  AND (r1.week_pnl + COALESCE(r2.week_pnl, 0)) < -5
ORDER BY combined_pnl;
""")

if flagged and flagged.strip():
    lines = [l for l in flagged.split('\n') if l.strip()]
    print(f"KILL-GATE: {len(lines)} strategy(s) losing 2+ weeks straight")
    notify(f"FLAGGED: {len(lines)} strategy(s) losing 2+ consecutive weeks")
    filed = []
    for l in lines:
        parts = l.split('|')
        if len(parts) >= 5:
            name, latest, prev, combined, trades = parts[:5]
            print(f"  {name}: latest_week={latest} prev_week={prev} combined={combined} trades={trades}")
            notify(f"  {name}: ${combined} over 2 weeks ({trades} trades)")
            filed.append(create_investigation_card(name, latest, prev, combined, trades))
    print(json.dumps({"cards_attempted": len(filed), "dry_run": DRY_RUN}, sort_keys=True))
else:
    sys.exit(0)
