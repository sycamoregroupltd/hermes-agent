#!/usr/bin/env python3
"""
24h Soak Monitor: audit task_events for blocked->running transitions
without an explicit unblock event.

Run this periodically (e.g. every 15min) as a cron job.  Output:
  - SILENT                     → no violations
  - VIOLATION:<task_id>:<desc> → a blocked card was dispatched without
                                 a legitimate unblock path
  - ALERT:<task_id>:<desc>     → anything that needs a human look

Exit codes:
  0 = clean (no violations)
  1 = violation found

Legitimate unblock paths (not flagged):
  - Unblocked event (user + approval-auto-clear)
  - promoted_manual event (human override)
  - promoted event after a block (auto-clear like apply_approvals)
  - status change from blocked without any claim/spawn after the last block
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Default: check only post-fix events. The original block-gate fix was
# deployed around 2026-07-28 22:50-23:00 UTC (epoch 1785279300). This cutoff
# MUST stay at the original fix-deploy time so the soak monitor keeps
# surfacing any blocked->claimed regression since that baseline. It was
# previously bumped to 1785918991 (2026-08-05) which masked the historical
# violations and made SILENT meaningless (t_bffc6b89).
FIX_DEPLOYED_AT = 1785279300  # 2026-07-28 22:55:00Z — original block-gate deploy

DB_PATH = Path("/home/frank/.hermes/kanban/boards/jarvis-os/kanban.db")

# Event kinds that clear a block (legitimate unblock paths)
UNBLOCK_KINDS = frozenset({"unblocked", "promoted_manual", "promoted"})


def audit(db_path: Path, since: int = FIX_DEPLOYED_AT) -> list[dict[str, str]]:
    """Scan the kanban DB for blocked->running violations since ``since``.

    A violation is: a task has a 'blocked' event after ``since``, then
    a 'claimed'/'spawned' event after that block, without a legitimate
    unblock event (unblocked / promoted_manual / promoted) between them.
    """
    if not db_path.exists():
        return [{"level": "ALERT", "task_id": "N/A",
                 "desc": f"DB does not exist: {db_path}"}]

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    findings: list[dict[str, str]] = []

    try:
        # Find all tasks with a 'blocked' event since the cutoff
        blocked_tasks = conn.execute(
            "SELECT DISTINCT task_id FROM task_events "
            "WHERE kind = 'blocked' AND created_at >= ?",
            (since,),
        ).fetchall()

        if not blocked_tasks:
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            return [{"level": "INFO", "task_id": "N/A",
                     "desc": f"Clean since {since}: no blocked events found"}]

        for row in blocked_tasks:
            tid = row["task_id"]

            # Get all blocked events (timestamp) since the cutoff
            block_events = conn.execute(
                "SELECT id, created_at, payload FROM task_events "
                "WHERE task_id = ? AND kind = 'blocked' "
                "AND created_at >= ? ORDER BY id",
                (tid, since),
            ).fetchall()

            # For each blocked event, check if there's a subsequent
            # claim/spawn WITHOUT a legitimate unblock path in between
            for be in block_events:
                block_ts = be["created_at"]
                block_id = be["id"]

                # Look for legitimate unblock events AFTER this block
                unblock_events = conn.execute(
                    "SELECT id, kind, created_at FROM task_events "
                    "WHERE task_id = ? AND kind IN ('unblocked', 'promoted_manual', 'promoted') "
                    "AND created_at > ? AND id > ? ORDER BY id LIMIT 1",
                    (tid, block_ts, block_id),
                ).fetchall()

                if unblock_events:
                    # Legitimate unblock path exists — skip
                    continue

                # No unblock found. Check if any claim/spawn happened AFTER
                # this block (which would be a violation).
                claim_events = conn.execute(
                    "SELECT id, kind, created_at FROM task_events "
                    "WHERE task_id = ? AND kind IN ('claimed', 'spawned') "
                    "AND created_at > ? AND id > ? ORDER BY id LIMIT 1",
                    (tid, block_ts, block_id),
                ).fetchall()

                if claim_events:
                    ce = claim_events[0]
                    findings.append({
                        "level": "VIOLATION",
                        "task_id": tid,
                        "desc": (
                            f"Blocked→claimed without legitimate unblock: "
                            f"blocked at ts={block_ts} (event_id={block_id}), "
                            f"claimed at ts={ce['created_at']} "
                            f"(kind={ce['kind']}, event_id={ce['id']})"
                        ),
                    })

        # Check for tasks CURRENTLY in 'running' status with a pending block
        running_blocked = conn.execute(
            "SELECT t.id, e.created_at AS block_ts FROM tasks t "
            "JOIN task_events e ON e.task_id = t.id "
            "WHERE t.status = 'running' "
            "AND e.kind = 'blocked' "
            "AND e.created_at >= ? "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM task_events e2 "
            "  WHERE e2.task_id = t.id "
            "  AND e2.kind IN ('unblocked', 'promoted_manual', 'promoted') "
            "  AND e2.created_at > e.created_at"
            ") "
            "GROUP BY t.id",
            (since,),
        ).fetchall()

        for row in running_blocked:
            tid = row["id"]
            # Check if this is already in our findings
            if not any(f["task_id"] == tid for f in findings):
                findings.append({
                    "level": "VIOLATION",
                    "task_id": tid,
                    "desc": (
                        f"CURRENTLY RUNNING with unresolved block "
                        f"(blocked at ts={row['block_ts']})"
                    ),
                })

        # Summary info
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        total_blocked = conn.execute(
            "SELECT COUNT(*) AS cnt FROM tasks WHERE status = 'blocked'"
        ).fetchone()
        total_blocked_count = total_blocked["cnt"] if total_blocked else 0
        bda_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM task_events WHERE kind = 'blocked_dispatch_attempt'"
        ).fetchone()
        bda_total = bda_count["cnt"] if bda_count else 0

        findings.append({
            "level": "INFO",
            "task_id": "N/A",
            "desc": (
                f"Soak audit at {now_iso}: "
                f"{len(blocked_tasks)} tasks with blocked events since cutoff, "
                f"{total_blocked_count} currently blocked, "
                f"{bda_total} total blocked_dispatch_attempt events logged"
            ),
        })

    finally:
        conn.close()

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Blocked card soak monitor")
    parser.add_argument("--db", type=Path, default=DB_PATH,
                        help="Path to kanban DB (default: jarvis-os)")
    parser.add_argument("--since", type=int, default=FIX_DEPLOYED_AT,
                        help="Unix timestamp cutoff (default: fix-deploy time)")
    parser.add_argument("--json", action="store_true",
                        help="Output findings as JSON")
    args = parser.parse_args()

    findings = audit(args.db, since=args.since)

    violations = [f for f in findings if f["level"] == "VIOLATION"]
    alerts = [f for f in findings if f["level"] == "ALERT"]

    if args.json:
        print(json.dumps(findings, indent=2))
    else:
        for f in findings:
            if f["level"] == "VIOLATION":
                print(f"VIOLATION:{f['task_id']}:{f['desc']}")
            elif f["level"] == "ALERT":
                print(f"ALERT:{f['task_id']}:{f['desc']}")
            elif f["level"] == "INFO":
                print(f"[INFO] {f['desc']}")

    if violations:
        n = len(violations)
        print(f"\nFOUND {n} VIOLATION(S) — blocked cards dispatched without legitimate unblock path!")
        return 1
    if alerts:
        n = len(alerts)
        print(f"\nFOUND {n} ALERT(S) — needs attention.")
        return 1

    print(f"\nSILENT — {len(findings)} checks, 0 violations, 0 alerts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
