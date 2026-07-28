#!/usr/bin/env python3
"""Fleet-wide worker-agent liveness churn measurement.

Reads Hermes kanban SQLite boards (source of truth for session churn) and
computes per-board and fleet aggregate liveness churn / death-rate metrics.

No prod systems, no credentials touched. Local SQLite only.

Outputs JSON to stdout. Designed for both one-shot reporting and to be the
engine behind a live alert (high churn) and a weekly death-rate report.
"""
import argparse
import json
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta

BOARDS = {
    "upero": "/home/frank/.hermes/kanban/boards/upero/kanban.db",
    "sycode-trading": "/home/frank/.hermes/kanban/boards/sycode-trading/kanban.db",
    "jarvis-os": "/home/frank/.hermes/kanban/boards/jarvis-os/kanban.db",
    "sycode-ai": "/home/frank/.hermes/kanban/boards/sycode-ai/kanban.db",
    "yorkstone-supplies": "/home/frank/.hermes/kanban/boards/yorkstone-supplies/kanban.db",
}

WINDOW_DAYS = 14  # measurement window for churn/death rates


def q(db, sql, args=()):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(sql, args).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def board_metrics(board, db, window_days=WINDOW_DAYS):
    out = {
        "board": board,
        "db_exists": os.path.exists(db),
        "task_counts": {},
        "run_outcomes": {},
        "window": {},
        "blocked_needs_input": [],
    }
    if not out["db_exists"]:
        return out

    now = int(time.time())
    window_start = now - window_days * 86400

    # task status census
    tc = q(db, "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
    out["task_counts"] = {r["status"]: r["n"] for r in tc}

    # run outcomes census (all time)
    ro = q(db, "SELECT outcome, COUNT(*) AS n FROM task_runs GROUP BY outcome")
    out["run_outcomes"] = {r["outcome"]: r["n"] for r in ro}

    # window-scoped runs: started within WINDOW_DAYS
    w = q(
        db,
        """
        SELECT status, outcome, COUNT(*) AS n,
               MIN(started_at) AS first_start,
               MAX(started_at) AS last_start
        FROM task_runs
        WHERE started_at >= ?
        GROUP BY status, outcome
        """,
        (window_start,),
    )
    started_total = 0
    deaths = {"crashed": 0, "timed_out": 0, "failed": 0, "reclaimed": 0, "spawn_failed": 0}
    reclaim = 0
    for r in w:
        n = r["n"]
        started_total += n
        oc = r["outcome"]
        if oc in deaths:
            deaths[oc] += n
        if oc == "reclaimed":
            reclaim += n
    death_total = sum(deaths.values())
    out["window"] = {
        "window_days": WINDOW_DAYS,
        "started_total": started_total,
        "death_total": death_total,
        "deaths_by_cause": deaths,
        "reclaimed": reclaim,
        "churn_rate_per_day": round(started_total / WINDOW_DAYS, 2),
        "death_rate_per_day": round(death_total / WINDOW_DAYS, 2),
        "death_rate_pct": round(100.0 * death_total / started_total, 2) if started_total else None,
    }

    # blocked needs_input cards (the stale dead-PID evidence the CEO flagged)
    bi = q(
        db,
        """
        SELECT id, title, status, block_kind, created_at, completed_at
        FROM tasks
        WHERE status='blocked' AND block_kind='needs_input'
        ORDER BY created_at DESC
        """,
    )
    for r in bi:
        age_h = round((now - r["created_at"]) / 3600.0, 1) if r["created_at"] else None
        out["blocked_needs_input"].append({
            "id": r["id"],
            "title": r["title"],
            "block_kind": r["block_kind"],
            "age_hours": age_h,
            "created_at": r["created_at"],
            "completed_at": r["completed_at"],
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boards", nargs="*", default=list(BOARDS.keys()),
                    help="subset of: " + " ".join(BOARDS.keys()))
    ap.add_argument("--window-days", type=int, default=WINDOW_DAYS)
    args = ap.parse_args()

    results = {}
    for b in args.boards:
        if b not in BOARDS:
            continue
        results[b] = board_metrics(b, BOARDS[b], args.window_days)

    # fleet aggregate
    agg = {
        "boards": len(results),
        "task_total_by_status": {},
        "run_total_by_outcome": {},
        "window_started_total": 0,
        "window_death_total": 0,
        "blocked_needs_input_total": 0,
        "blocked_needs_input_by_board": {},
        "death_rate_per_day_fleet": None,
        "death_rate_pct_fleet": None,
        "churn_rate_per_day_fleet": None,
    }
    for b, m in results.items():
        for s, n in m["task_counts"].items():
            agg["task_total_by_status"][s] = agg["task_total_by_status"].get(s, 0) + n
        for o, n in m["run_outcomes"].items():
            agg["run_total_by_outcome"][o] = agg["run_total_by_outcome"].get(o, 0) + n
        agg["window_started_total"] += m["window"].get("started_total", 0)
        agg["window_death_total"] += m["window"].get("death_total", 0)
        nb = len(m["blocked_needs_input"])
        agg["blocked_needs_input_total"] += nb
        if nb:
            agg["blocked_needs_input_by_board"][b] = nb

    st = agg["window_started_total"]
    dt = agg["window_death_total"]
    agg["churn_rate_per_day_fleet"] = round(st / WINDOW_DAYS, 2)
    agg["death_rate_per_day_fleet"] = round(dt / WINDOW_DAYS, 2)
    agg["death_rate_pct_fleet"] = round(100.0 * dt / st, 2) if st else None
    agg["generated_at"] = datetime.now(timezone.utc).isoformat()

    print(json.dumps({"per_board": results, "fleet_aggregate": agg}, indent=2))


if __name__ == "__main__":
    main()
