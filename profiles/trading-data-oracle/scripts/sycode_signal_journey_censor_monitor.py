#!/usr/bin/env python3
"""Standing monitor: COMPLETED_* signal_journeys missing realized_pnl_percent.

Read-only defect watchdog for the Sycode signal-journey pipeline.

A closed trade (COMPLETED_WIN / COMPLETED_LOSS / COMPLETED_BREAKEVEN) with a null
``realized_pnl_percent`` is a genuine persistence defect -- the trade was closed
and should have realized net PnL recorded. The historical baseline (per
t_f379ecf5) is ~0.19% over 14d; steady-state expected is 0/day.

Silence contract (no_agent=True, watchdog pattern):
  * 0 censored journeys in the last 24h  -> SILENT (empty stdout + rc=0)
  * >0 censored journeys  -> stdout ALERT block + rc=0 (delivery is the alert)

This script performs NO data mutation. It never inserts trade_intents, never
enables live trading, never alters credentials, and never touches the trading
engine. PAPER MODE ONLY by policy.

Query (expected result: 0):
  SELECT count(*) FROM signal_journeys
  WHERE final_status LIKE 'COMPLETED%'
    AND realized_pnl_percent IS NULL
    AND updated_at > now() - interval '1 day';
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone

# --- Connection knobs (env-overridable, no secrets in code) -----------------
PSQL_HOST = os.environ.get("PGHOST", "localhost")
PSQL_PORT = os.environ.get("PGPORT", "5432")
PSQL_USER = os.environ.get("PGUSER", "postgres")
PSQL_DB = os.environ.get("PGDB", "postgres")
# Password is injected from the environment only; never logged.
PSQL_PASSWORD = os.environ.get("PGPASSWORD", os.environ.get("PGPASS", "postgres"))

PSQL_CMD = [
    "psql",
    "-h", PSQL_HOST,
    "-p", PSQL_PORT,
    "-U", PSQL_USER,
    "-d", PSQL_DB,
    "-X", "-A", "-t",  # -A: unaligned, -t: tuples only, -X: no psqlrc
    "--pset", "footer=off",
    "--set", "ON_ERROR_STOP=1",
    "-c",
    "SELECT count(*) FROM signal_journeys "
    "WHERE final_status LIKE 'COMPLETED%' "
    "AND realized_pnl_percent IS NULL "
    "AND updated_at > now() - interval '1 day';",
]

WINDOW = "last 24 hours"


def run_query() -> tuple[int | None, str | None]:
    """Return (count, error). Count is None on failure."""
    env = os.environ.copy()
    env["PGPASSWORD"] = PSQL_PASSWORD
    try:
        res = subprocess.run(
            PSQL_CMD,
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return None, "psql timed out after 30s"
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()
        return None, f"psql exited {res.returncode}: {err}"
    raw = res.stdout.strip()
    if raw == "" or raw is None:
        return None, "psql returned empty output (no rows)"
    try:
        return int(raw), None
    except ValueError:
        return None, f"psql returned non-integer: {raw!r}"


def build_alert(count: int) -> list[str]:
    """Build the alert block emitted when count > 0."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "⛔ SYCODE SIGNAL-JOURNEY CENSOR DEFECT DETECTED ⛔",
        f"Time (UTC): {ts}",
        f"Window: {WINDOW}",
        f"COMPLETED_* journeys missing realized_pnl_percent: {count}",
        "",
        "A closed trade (COMPLETED_WIN/LOSS/BREAKEVEN) with null realized_pnl_percent",
        "is a genuine persistence defect, not structural noise. Expected steady-state: 0/day.",
        "Baseline over 14d was ~0.19% (t_f379ecf5). See PR #886 (t_36b88de6) for warn-channel gating.",
        "",
        "Triage query (run read-only):",
        "  SELECT id, symbol, timeframe, direction, final_status, updated_at,",
        "         realized_pnl_percent, pnl_percent, entry_price",
        "  FROM signal_journeys",
        "  WHERE final_status LIKE 'COMPLETED%'",
        "    AND realized_pnl_percent IS NULL",
        "    AND updated_at > now() - interval '1 day'",
        "  ORDER BY updated_at DESC LIMIT 50;",
        "",
        "Ownership: SignalJourneyPersistenceService.mlOutcomes.ts",
        "Obsidian runbook: [[operations/runbooks/signal-journey-censor-monitor.md]]",
    ]
    return lines


def main() -> int:
    count, err = run_query()
    if err is not None:
        # A monitor failure must be LOUD, never silently silent. Emit an error
        # block and exit non-zero so the cron delivery surfaces the failure.
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        print("⚠ SYCODE SIGNAL-JOURNEY CENSOR MONITOR — ERROR ⚠", file=sys.stderr)
        print(f"Time (UTC): {ts}", file=sys.stderr)
        print(f"Error: {err}", file=sys.stderr)
        print("This is a monitor failure, not a clean zero. Inspect immediately.",
              file=sys.stderr)
        return 1
    # After the error guard, count is a real int (query succeeded).
    assert count is not None
    if count == 0:
        # SILENCE CONTRACT: 0 defects -> no output, rc=0.
        return 0
    # Defect detected: emit alert block to stdout (delivered to origin/subscribers).
    for line in build_alert(count):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
