#!/usr/bin/env python3
"""Measure DLQ/backlog/starvation diagnostic-card activity on the jarvis-os board.

Deterministic, reproducible method used for the t_f020e1a4 / t_9cb96d37 2-week
alert-reduction follow-up (Control-Plane project, jarvis-os tracker).

Method:
  For each diagnostic term, count tasks whose LOWER(title) LIKE '%<term>%'.
  Report total title matches, by_status breakdown, open-vs-resolved, and
  (when a window is given) how many were CREATED inside the window.

Terms: dlq, backlog, starvation, pipeline_sta, queue_backlog

Usage:
  python3 dqsh_alert_reduction_measure.py --db PATH [--window-start TS] [--window-end TS] [--json]
"""
import argparse
import json
import sqlite3
import sys

TERMS = ["dlq", "backlog", "starvation", "pipeline_sta", "queue_backlog"]


def measure(db, ws, we):
    con = sqlite3.connect(db)
    cur = con.cursor()
    out = {}
    for t in TERMS:
        pat = f"%{t}%"
        cur.execute(
            "SELECT status, COUNT(*) FROM tasks WHERE lower(title) LIKE ? GROUP BY status",
            (pat,),
        )
        by_status = dict(cur.fetchall())
        cur.execute("SELECT COUNT(*) FROM tasks WHERE lower(title) LIKE ?", (pat,))
        total = cur.fetchone()[0]
        new_in_window = 0
        if ws is not None and we is not None:
            cur.execute(
                "SELECT COUNT(*) FROM tasks WHERE lower(title) LIKE ? "
                "AND created_at BETWEEN ? AND ?",
                (pat, ws, we),
            )
            new_in_window = cur.fetchone()[0]
        out[t] = {
            "total_title_matches": total,
            "by_status": by_status,
            "new_in_window": new_in_window,
            "done": by_status.get("done", 0),
            "archived": by_status.get("archived", 0),
            "open_or_other": sum(
                c for s, c in by_status.items() if s not in ("done", "archived")
            ),
        }
    con.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="path to jarvis-os kanban.db")
    ap.add_argument("--window-start", type=int, default=None, help="epoch s")
    ap.add_argument("--window-end", type=int, default=None, help="epoch s")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    res = measure(args.db, args.window_start, args.window_end)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        for t, v in res.items():
            print(
                f"{t}: total={v['total_title_matches']} "
                f"new_in_window={v['new_in_window']} "
                f"done={v['done']} archived={v['archived']} "
                f"open_or_other={v['open_or_other']}"
            )
            print(f"   by_status={v['by_status']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
