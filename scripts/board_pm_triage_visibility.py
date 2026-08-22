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
# Statuses that make a PM TRIAGE card a live covering signal. "blocked" is
# deliberately EXCLUDED: a blocked covering card is a stuck/dead mechanism, not
# coverage (t_a4f6263c / KEP-7). A blocked card alone must never count as open
# triage, and must surface as DEAD instead.
COVERING_STATUSES = ("todo", "ready", "running", "scheduled")


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
    placeholders = ",".join("?" for _ in COVERING_STATUSES)
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
        (pm_profile, *COVERING_STATUSES),
    ).fetchall()
    return [dict(r) for r in rows]


def recent_blocked_pm_triage(con: sqlite3.Connection, pm_profile: str) -> list[dict[str, Any]]:
    """PM TRIAGE cards that exist but are BLOCKED.

    These are a stuck/dead covering signal: they are NOT open triage (see
    COVERING_STATUSES) and must not be treated as coverage. When the only
    covering card is blocked and there is no recent PM activity, the bridge
    reports DEAD instead of ALIVE or of minting a replacement card (t_a4f6263c).
    """
    rows = con.execute(
        """
        SELECT id, title, status, assignee, created_at
        FROM tasks
        WHERE assignee = ?
          AND status = 'blocked'
          AND title LIKE 'PM TRIAGE VISIBILITY:%'
        ORDER BY created_at DESC
        LIMIT 5
        """,
        (pm_profile,),
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


def liveness_line(board: str, reason: str, *, status: str = "ALIVE", **extra: Any) -> str:
    """Deterministic structured liveness marker.

    The unified-health mechanism matrix (jarvis_mechanism_liveness_collect.py)
    classifies this cron on schedule freshness (last_run_at vs max_age), NOT on
    output content. But a purely EMPTY stdout on the no-op path is indistinguishable
    from a silently-dead script to a human/agent reading the output artifact, and it
    is what produced the historical "silent (empty output)" label. Emit one stable,
    machine-parseable line per run so every execution carries an explicit liveness
    signal with a deterministic reason code (t_92444ff6).

    status is ALIVE except when the mechanism is demonstrably stuck: a covering PM
    triage card that is BLOCKED with no recent PM activity yields status=DEAD so a
    human/agent intervenes instead of treating the dead card as coverage (t_a4f6263c).
    """
    parts = [f"PM_TRIAGE_LIVENESS board={board} status={status} reason={reason}"]
    for k, v in extra.items():
        parts.append(f"{k}={v}")
    return " ".join(parts)


def run(board: str, pm_profile: str, *, dry_run: bool, source: str, recent_pm_hours: float) -> str:
    with connect_board(board) as con:
        counts = status_counts(con)
        active_total = sum(counts.get(s, 0) for s in ACTIVE_STATUSES)
        if active_total <= 0:
            return liveness_line(board, "no_active_queue", active_total=0)
        # Live covering signal: an open (todo/ready/running/scheduled) PM triage
        # card, OR a PM comment on the board in the last --recent-pm-hours.
        # Blocked cards are NOT covering (see COVERING_STATUSES).
        open_triage = recent_open_pm_triage(con, pm_profile)
        if open_triage:
            return liveness_line(board, "open_triage_covers", open_triage=str(len(open_triage)))
        recent_pm = recent_pm_activity(con, pm_profile, int(recent_pm_hours * 3600))
        if recent_pm:
            return liveness_line(
                board, "recent_pm_activity",
                recent_pm=str(len(recent_pm)), recent_pm_hours=recent_pm_hours,
            )
        # A covering card exists but is BLOCKED with no fresh PM activity: the
        # mechanism is dead. Report DEAD (fail visibly) and do NOT mint a
        # replacement card while the blocked one is unresolved.
        blocked = recent_blocked_pm_triage(con, pm_profile)
        if blocked:
            return liveness_line(
                board, "blocked_covering", status="DEAD",
                blocked=str(len(blocked)), open_triage="0",
            )
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
