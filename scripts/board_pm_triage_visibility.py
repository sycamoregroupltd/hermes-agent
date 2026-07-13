#!/usr/bin/env python3
"""Board PM triage visibility bridge.

No-agent cron helper: when a project board has queued PM-visible work but no
recent PM-visible triage card, create one bounded PM triage task on that same
board. Empty stdout means no action was needed.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERMES_HOME = Path("/home/frank/.hermes")
BOARDS = HERMES_HOME / "kanban" / "boards"
ACTIVE_STATUSES = ("blocked", "todo", "scheduled", "ready")
OPEN_STATUSES = ("blocked", "todo", "scheduled", "ready", "running")


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def connect_board(board: str) -> sqlite3.Connection:
    db = BOARDS / board / "kanban.db"
    if not db.exists():
        raise FileNotFoundError(f"board db not found: {db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def status_counts(con: sqlite3.Connection) -> dict[str, int]:
    rows = con.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
    return {str(r["status"]): int(r["n"] or 0) for r in rows}


def recent_open_pm_triage(con: sqlite3.Connection, pm_profile: str) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in OPEN_STATUSES)
    rows = con.execute(
        f"""
        SELECT id, title, status, assignee, created_at
        FROM tasks
        WHERE assignee = ?
          AND status IN ({placeholders})
          AND title LIKE 'PM TRIAGE VISIBILITY:%'
        ORDER BY created_at DESC
        LIMIT 5
        """,
        (pm_profile, *OPEN_STATUSES),
    ).fetchall()
    return [dict(r) for r in rows]


def recent_pm_activity(con: sqlite3.Connection, pm_profile: str, since_seconds: int) -> list[dict[str, Any]]:
    cutoff = int(datetime.now(timezone.utc).timestamp()) - since_seconds
    rows = con.execute(
        """
        SELECT task_id, author, created_at, substr(body, 1, 220) AS body
        FROM task_comments
        WHERE author = ? AND created_at >= ?
        ORDER BY created_at DESC
        LIMIT 5
        """,
        (pm_profile, cutoff),
    ).fetchall()
    return [dict(r) for r in rows]


def create_card(board: str, pm_profile: str, counts: dict[str, int], source: str, dry_run: bool) -> str:
    today = utc_today()
    title = f"PM TRIAGE VISIBILITY: {board} active queue review {today}"
    idempotency_key = f"pm-triage-visibility:{board}:{today}"
    body = "\n".join(
        [
            f"Automated PM triage visibility bridge for board `{board}`.",
            f"Source: {source} (task t_93315858).",
            f"Observed queue counts: {json.dumps(counts, sort_keys=True)}.",
            "",
            "Acceptance:",
            "1. Inspect blocked/todo/scheduled/ready queues and recent PM/reviewer comments.",
            "2. Take exactly one safe PM action: unblock with delegated evidence, create/route a narrow child, update a status/Obsidian pointer, or leave a reasoned no-op comment.",
            "3. Preserve hard gates: no credentials/secrets, live trading, production deploys, irreversible data ops, new spend, or guardrail weakening.",
            "4. Complete with metadata naming the evidence checked and action/no-op decision.",
            "",
            "Consumer rule: this task is the named consumer for the no-agent visibility cron output; if the queue is clean or recently covered, the cron stays silent.",
        ]
    )
    cmd = [
        "hermes",
        "kanban",
        "--board",
        board,
        "create",
        title,
        "--assignee",
        pm_profile,
        "--priority",
        "75",
        "--idempotency-key",
        idempotency_key,
        "--created-by",
        "board-pm-triage-visibility-cron",
        "--body",
        body,
        "--json",
    ]
    if dry_run:
        return f"DRY_RUN would create {board} {idempotency_key} title={title!r}"
    env = {**os.environ, "HERMES_HOME": str(HERMES_HOME), "HERMES_PROFILE": "jarvis"}
    cp = subprocess.run(cmd, text=True, capture_output=True, timeout=60, check=False, env=env)
    if cp.returncode != 0:
        raise RuntimeError(f"kanban create failed rc={cp.returncode}: {cp.stderr.strip() or cp.stdout.strip()}")
    try:
        payload = json.loads(cp.stdout)
        task_id = payload.get("id") or payload.get("task_id") or cp.stdout.strip()
    except Exception:
        task_id = cp.stdout.strip()[:300]
    return f"CREATED_OR_EXISTING board={board} task={task_id} assignee={pm_profile} idempotency_key={idempotency_key}"


def run(board: str, pm_profile: str, *, dry_run: bool, source: str, recent_pm_hours: float) -> str:
    with connect_board(board) as con:
        counts = status_counts(con)
        active_total = sum(counts.get(s, 0) for s in ACTIVE_STATUSES)
        if active_total <= 0:
            return ""
        open_triage = recent_open_pm_triage(con, pm_profile)
        if open_triage:
            return ""
        recent_pm = recent_pm_activity(con, pm_profile, int(recent_pm_hours * 3600))
        if recent_pm:
            return ""
        return create_card(board, pm_profile, counts, source, dry_run)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", required=True)
    ap.add_argument("--pm-profile", required=True)
    ap.add_argument("--source", default="board-pm-triage-visibility-cron")
    ap.add_argument("--recent-pm-hours", type=float, default=6.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    try:
        out = run(args.board, args.pm_profile, dry_run=args.dry_run, source=args.source, recent_pm_hours=args.recent_pm_hours)
    except Exception as exc:
        print(f"BOARD_PM_TRIAGE_VISIBILITY_ERROR board={args.board}: {exc}", file=sys.stderr)
        return 1
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
