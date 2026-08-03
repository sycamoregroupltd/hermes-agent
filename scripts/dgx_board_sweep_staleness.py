#!/usr/bin/env python3
"""
dgx_board_sweep_staleness.py — per-board READY-queue staleness probe.

Companion to dgx_board_sweep.py. Deterministic, NO-AGENT, READ-ONLY.
Computes per-board ready-queue staleness metrics for the 5 key boards and
emits a clearly labeled `DISPATCH GAP` alert line when the threshold is breached.

This is OBSERVABILITY ONLY. It opens each kanban.db read-only, runs SELECT
counts/aggregates, and never issues INSERT/UPDATE/DELETE. Opened with
`sqlite3.connect(..., uri=True)` + `mode=ro` so it cannot mutate even by accident.

Metrics per board:
  ready_count          — tasks in `ready`
  running_count        — tasks in `running`
  oldest_ready_age_h   — age (hours) of the oldest ready task (from created_at;
                         board stores unix epoch seconds)
  ready_older_48h      — count of ready tasks older than 48h

DISPATCH GAP rule (initial — tunable; Card 2 / t_aaa43bb6 will refine after soak):
  Fire when: ready_count > 0 AND (
      running_count == 0
      OR oldest_ready_age_h > 48
      OR (ready_count - running_count) >= 10
  )
  The third term catches a deep backlogged queue with a few runners (e.g. the
  historical 90-ready / 2-running sycode-trading case) that the original
  (running==0 OR oldest>48h) rule would miss.

Output: compact JSON + markdown summary to /tmp/dgx_board_staleness_{date}.txt
AND stdout. Exit 0 on success; non-zero ONLY on a DB access error.

Canonical location: /home/frank/.hermes/scripts/dgx_board_sweep_staleness.py
(Do not edit profile-local copies if any exist; edit this canonical file.)

See ADR: /home/frank/obsidian-fleet-vault/Decisions/2026-07-13-board-sweep-ready-queue-staleness-t_05773565.md
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

# Canonical board roots. Each board is a directory under the kanban boards home
# containing a kanban.db. Board name == directory name == the board the kernel
# resolves from HERMES_KANBAN_BOARD.
# Board list is DATA (t_911a916c): single source is the fleet boards manifest
# ~/.hermes/kanban/boards-manifest.json, read via fleet_boards.py. The sweep
# flag includes dormant boards (they still get read-only staleness reporting)
# but never the denied orchestrator-sync bus.
sys.path.insert(0, "/home/frank/.hermes/scripts")
try:
    from fleet_boards import boards_for as _boards_for  # type: ignore

    BOARDS = _boards_for("sweep")
except Exception:
    BOARDS = ["jarvis-os", "upero", "sycode-trading", "sycode-ai", "yorkstone-supplies"]

# Where the board DBs live. Override via HERMES_KANBAN_HOME if needed.
KANBAN_HOME = os.environ.get(
    "HERMES_KANBAN_HOME", "/home/frank/.hermes/kanban/boards"
)

# DISPATCH GAP threshold (see module docstring). Tunable via env for soak testing.
STALE_HOURS_THRESHOLD = float(os.environ.get("DISPATCH_GAP_STALE_HOURS", "48"))
BACKLOG_GAP_THRESHOLD = int(os.environ.get("DISPATCH_GAP_BACKLOG", "10"))

DB_ACCESS_ERROR = False  # set True if any board read fails


def board_db_path(board: str) -> str:
    return os.path.join(KANBAN_HOME, board, "kanban.db")


def probe_board(board: str, now: int) -> dict:
    """Read-only probe of one board. Returns a metric dict. Never mutates."""
    path = board_db_path(board)
    entry = {
        "board": board,
        "db_path": path,
        "present": os.path.exists(path),
        "ready_count": 0,
        "running_count": 0,
        "blocked_count": 0,
        "oldest_ready_age_h": 0.0,
        "ready_older_48h": 0,
        "dispatch_gap": False,
        "gap_reasons": [],
        "error": None,
    }
    if not entry["present"]:
        entry["error"] = "db_missing"
        return entry

    # Open READ-ONLY via URI mode=ro. This makes accidental writes impossible:
    # sqlite raises OperationalError on any write against a ro connection.
    uri = "file:%s?mode=ro" % path
    try:
        # timeout=0 + immutable=1: never blocks on a lock, never waits, pure read.
        conn = sqlite3.connect(uri, uri=True, timeout=0)
        conn.execute("PRAGMA query_only = ON;")
    except sqlite3.Error as exc:
        entry["error"] = "open_failed: %s" % exc
        return entry

    try:
        cur = conn.cursor()
        cur.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")
        counts = {row[0]: row[1] for row in cur.fetchall()}
        entry["ready_count"] = int(counts.get("ready", 0))
        entry["running_count"] = int(counts.get("running", 0))
        entry["blocked_count"] = int(counts.get("blocked", 0))

        # Oldest ready task age (hours). created_at is unix epoch seconds.
        cur.execute(
            "SELECT MIN(created_at) FROM tasks WHERE status='ready'"
        )
        row = cur.fetchone()
        oldest_ts = row[0] if row is not None else None
        if oldest_ts:
            entry["oldest_ready_age_h"] = round((now - int(oldest_ts)) / 3600.0, 3)

        # Ready tasks older than 48h.
        older_cutoff = now - int(STALE_HOURS_THRESHOLD * 3600)
        cur.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='ready' AND created_at < ?",
            (older_cutoff,),
        )
        entry["ready_older_48h"] = int(cur.fetchone()[0])
    except sqlite3.Error as exc:
        entry["error"] = "query_failed: %s" % exc
        return entry
    finally:
        conn.close()

    # Evaluate DISPATCH GAP rule.
    if entry["ready_count"] > 0:
        if entry["running_count"] == 0:
            entry["gap_reasons"].append("running==0 with ready>0")
        if entry["oldest_ready_age_h"] > STALE_HOURS_THRESHOLD:
            entry["gap_reasons"].append(
                "oldest_ready_age_h=%.2f > %.0f"
                % (entry["oldest_ready_age_h"], STALE_HOURS_THRESHOLD)
            )
        backlog = entry["ready_count"] - entry["running_count"]
        if backlog >= BACKLOG_GAP_THRESHOLD:
            entry["gap_reasons"].append(
                "backlog(ready-running)=%d >= %d"
                % (backlog, BACKLOG_GAP_THRESHOLD)
            )
        entry["dispatch_gap"] = len(entry["gap_reasons"]) > 0

    return entry


def build_report(results: list, now: int, date: str) -> tuple:
    """Return (json_str, markdown_str)."""
    any_gap = any(r["dispatch_gap"] for r in results)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "now_epoch": now,
        "boards": results,
        "any_dispatch_gap": any_gap,
        "dispatch_gap_boards": [r["board"] for r in results if r["dispatch_gap"]],
    }
    json_str = json.dumps(summary, indent=2)

    # Markdown summary.
    lines = []
    lines.append("# DGX Board Staleness Probe — %s" % date)
    lines.append("")
    lines.append("Generated: %s" % summary["generated_at"])
    lines.append("")
    lines.append(
        "| Board | Ready | Running | Blocked | OldestReady(h) | Ready>48h | DISPATCH GAP |"
    )
    lines.append(
        "|-------|------:|--------:|--------:|--------------:|---------:|:-----------:|"
    )
    for r in results:
        gap = "**YES**" if r["dispatch_gap"] else "no"
        if r["error"]:
            gap = "ERR:%s" % r["error"]
        lines.append(
            "| %s | %d | %d | %d | %.2f | %d | %s |"
            % (
                r["board"],
                r["ready_count"],
                r["running_count"],
                r["blocked_count"],
                r["oldest_ready_age_h"],
                r["ready_older_48h"],
                gap,
            )
        )
    lines.append("")

    # DISPATCH GAP alert lines (the durable signal for #fleet-reports).
    if any_gap:
        lines.append("## DISPATCH GAP ALERT")
        for r in results:
            if r["dispatch_gap"]:
                lines.append(
                    "DISPATCH GAP @ %s — ready=%d running=%d oldest_ready=%.2fh "
                    "(%s)"
                    % (
                        r["board"],
                        r["ready_count"],
                        r["running_count"],
                        r["oldest_ready_age_h"],
                        "; ".join(r["gap_reasons"]),
                    )
                )
    else:
        lines.append("## DISPATCH GAP ALERT")
        lines.append("No dispatch gaps detected across the 5 key boards.")
    lines.append("")

    # Per-board reasons for transparency.
    detail = [l for r in results if r["gap_reasons"] for l in (
        ["- %s: %s" % (r["board"], reason) for reason in r["gap_reasons"]]
    )]
    if detail:
        lines.append("### Gap reasons")
        lines.extend(detail)
        lines.append("")

    md_str = "\n".join(lines)
    return json_str, md_str


def main() -> int:
    global DB_ACCESS_ERROR
    now = int(datetime.now(timezone.utc).timestamp())
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    results = []
    for board in BOARDS:
        r = probe_board(board, now)
        if r["error"] and r["error"].startswith(
            ("open_failed", "query_failed", "db_missing")
        ):
            # DB access error -> signal via exit code, but still report best-effort.
            DB_ACCESS_ERROR = True
        results.append(r)

    json_str, md_str = build_report(results, now, date)

    out_path = "/tmp/dgx_board_staleness_%s.txt" % date
    try:
        with open(out_path, "w") as fh:
            fh.write(json_str)
            fh.write("\n\n")
            fh.write(md_str)
    except OSError as exc:
        sys.stderr.write("WARN: could not write %s: %s\n" % (out_path, exc))

    # stdout: the markdown summary + a compact dispatch-gap header.
    any_gap = any(r["dispatch_gap"] for r in results)
    if any_gap:
        sys.stdout.write("DISPATCH GAP DETECTED on: %s\n" % ", ".join(
            r["board"] for r in results if r["dispatch_gap"]
        ))
    else:
        sys.stdout.write("No dispatch gaps detected.\n")
    sys.stdout.write(md_str)
    sys.stdout.write("\n# JSON:\n")
    sys.stdout.write(json_str)
    sys.stdout.write("\n")

    # Exit non-zero ONLY on DB access error (per acceptance A2).
    return 2 if DB_ACCESS_ERROR else 0


if __name__ == "__main__":
    sys.exit(main())
