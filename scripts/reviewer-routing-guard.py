#!/usr/bin/env python3
"""Reviewer-routing guard: detect reviewer-capability routing failures.

One-shot detector for all kanban boards. Queries task_events for:
1. 'reviewer_capability' events (gate's informational record of refusing to
   route a review card to a terminal-less profile).
2. Review cards that spawned and then re-blocked with a capability reason
   (indicating the worker spawned but failed immediately and re-blocked).

The dispatch-loop capability gate (PR #3, Part B) emits reviewer_capability
events when it refuses to dispatch a review card assigned to a terminal-less
profile. These are INFORMATIONAL — the gate is working as designed.
ANY spawn-fail + re-block pattern is what we want to catch.

Acceptance check for t_a2ef2ea2 criterion (3): zero review cards that
spawn-failed and re-blocked with reviewer-capability reason.

Usage:
    python3 reviewer-routing-guard.py                    # default boards
    python3 reviewer-routing-guard.py --db <path>        # single board
    python3 reviewer-routing-guard.py --all-boards       # all known boards
    python3 reviewer-routing-guard.py --json             # machine-readable
    python3 reviewer-routing-guard.py --help
"""

import argparse
import datetime
import json
import os
import re
import sqlite3
import sys

# ── Defaults ────────────────────────────────────────────────────────────────

DEFAULT_BOARDS = {
    "jarvis-os": "/home/frank/.hermes/kanban/boards/jarvis-os/kanban.db",
    "sycode-trading": "/home/frank/.hermes/kanban/boards/sycode-trading/kanban.db",
    "upero": "/home/frank/.hermes/kanban/boards/upero/kanban.db",
    "yorkstone-supplies": "/home/frank/.hermes/kanban/boards/yorkstone-supplies/kanban.db",
    "ai-restaurant": "/home/frank/.hermes/kanban/boards/ai-restaurant/kanban.db",
    "default": "/home/frank/.hermes/kanban.db",
}

# Epoch timestamp of the fix deployment
FIX_DEPLOY_TS = 1785266989  # ~2026-07-28 19:29 UTC

LOOKBACK_HOURS = 2

# Patterns to detect capability-related block reasons
CAPABILITY_REASONS = re.compile(
    r"reviewer|capability|terminal.less|no.terminal|profile.*not.*found",
    re.IGNORECASE,
)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _find_boards(args):
    """Resolve which board DBs to scan."""
    boards = {}
    if args.db:
        boards[os.path.basename(os.path.dirname(args.db)) or "custom"] = args.db
    elif args.all_boards:
        boards = dict(DEFAULT_BOARDS)
    else:
        boards = dict(DEFAULT_BOARDS)
    return {name: path for name, path in boards.items() if os.path.exists(path)}


def _get_cutoff_ts(args, now_ts):
    if args.since:
        return args.since
    return now_ts - (LOOKBACK_HOURS * 3600)


def check_reviewer_events(conn, cutoff_ts, fix_deploy_ts):
    """Check for reviewer-capability routing events.

    Returns:
        capability_events: raw reviewer_capability events found
        spawn_fail_reblock: tasks that spawned then re-blocked with capability reason
        post_fix_reblocks: count of re-block events that are post-fix
    """
    capability_events = []
    spawn_fail_reblock = []

    # 1. Find reviewer_capability events
    rows = conn.execute(
        """
        SELECT id, task_id, created_at, payload
        FROM task_events
        WHERE kind = 'reviewer_capability'
          AND created_at >= ?
        ORDER BY created_at DESC
        """,
        (cutoff_ts,),
    ).fetchall()

    for row in rows:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except (json.JSONDecodeError, TypeError):
            payload = {}

        capability_events.append({
            "event_id": row["id"],
            "task_id": row["task_id"],
            "created_at": row["created_at"],
            "created_ts": datetime.datetime.fromtimestamp(
                row["created_at"], tz=datetime.timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "payload": payload,
        })

    # 2. Find spawned review cards that later got blocked with capability reason
    review_spawned = conn.execute(
        """
        SELECT task_id, created_at
        FROM task_events
        WHERE kind = 'spawned'
          AND created_at >= ?
        ORDER BY created_at ASC
        """,
        (cutoff_ts,),
    ).fetchall()

    spawned_tids = set(r["task_id"] for r in review_spawned)

    for tid in spawned_tids:
        # Get block events for this task after a spawn
        blocked_rows = conn.execute(
            """
            SELECT e.created_at, e.payload
            FROM task_events e
            WHERE e.task_id = ? AND e.kind = 'blocked'
              AND e.created_at >= ?
            ORDER BY e.created_at DESC
            LIMIT 3
            """,
            (tid, cutoff_ts),
        ).fetchall()

        for br in blocked_rows:
            payload_text = br["payload"] or ""
            if CAPABILITY_REASONS.search(payload_text):
                is_post_fix = br["created_at"] >= fix_deploy_ts
                try:
                    payload = json.loads(payload_text) if payload_text else {}
                except (json.JSONDecodeError, TypeError):
                    payload = {"raw": payload_text[:200]}

                spawn_fail_reblock.append({
                    "task_id": tid,
                    "blocked_at": br["created_at"],
                    "blocked_ts": datetime.datetime.fromtimestamp(
                        br["created_at"], tz=datetime.timezone.utc
                    ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "payload": payload,
                    "post_fix": is_post_fix,
                })
                break  # one entry per task

    pre_fix_reblocks = sum(1 for v in spawn_fail_reblock if not v["post_fix"])
    post_fix_reblocks = sum(1 for v in spawn_fail_reblock if v["post_fix"])

    return capability_events, spawn_fail_reblock, post_fix_reblocks


def scan_board(name, db_path, cutoff_ts, fix_deploy_ts):
    """Scan one board for reviewer-routing events."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    cap_events, reblocks, post_fix = check_reviewer_events(
        conn, cutoff_ts, fix_deploy_ts
    )
    conn.close()

    return {
        "board": name,
        "db_path": db_path,
        "capability_events": cap_events,
        "capability_event_count": len(cap_events),
        "spawn_fail_reblocks": reblocks,
        "spawn_fail_reblock_count": len(reblocks),
        "post_fix_reblocks": post_fix,
        "pre_fix_reblocks": len(reblocks) - post_fix,
        "status": "CLEAN" if len(reblocks) == 0 else "VIOLATIONS",
    }


def print_human_report(results):
    """Print human-readable report."""
    now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    now_str = datetime.datetime.fromtimestamp(
        now_ts, tz=datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    print("=" * 72)
    print(f"  REVIEWER-ROUTING GUARD  |  {now_str}")
    print("=" * 72)

    total_cap_events = sum(r["capability_event_count"] for r in results)
    total_reblocks = sum(r["spawn_fail_reblock_count"] for r in results)
    total_post_fix = sum(r["post_fix_reblocks"] for r in results)

    print(f"  Boards scanned: {len(results)}")
    print(f"  reviewer_capability events (informational): {total_cap_events}")
    print(f"  Spawn-fail + re-block (capability reason):  {total_reblocks}")
    print(f"    Post-fix: {total_post_fix}")
    print(f"    Pre-fix:  {total_reblocks - total_post_fix}")
    print(f"  Fix epoch: {FIX_DEPLOY_TS} ({datetime.datetime.fromtimestamp(FIX_DEPLOY_TS, tz=datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})")
    print("=" * 72)

    if total_cap_events > 0:
        print()
        print("  reviewer_capability events (gate working as designed):")
        for r in results:
            for ev in r["capability_events"]:
                p = ev["payload"]
                reason = p.get("reason", p.get("message", "(no reason)"))
                print(f"    [{r['board']}] Task {ev['task_id']} at {ev['created_ts']}: {reason}")
        print()

    if total_reblocks > 0:
        print()
        print("  !! SPAWN-FAIL + RE-BLOCK (capability reason) !!")
        print()
        for r in results:
            for v in r["spawn_fail_reblocks"]:
                label = "POST-FIX" if v["post_fix"] else "PRE-FIX"
                p = v["payload"]
                reason = p.get("reason", p.get("message", p.get("raw", "(no reason)")))
                print(f"  [{label}] Board:       {r['board']}")
                print(f"           Task:        {v['task_id']}")
                print(f"           Blocked at:  {v['blocked_ts']}")
                print(f"           Reason:      {reason}")
                print(f"           {'─' * 40}")
        print()
        if total_post_fix > 0:
            print("  !! POST-FIX RE-BLOCK VIOLATIONS — routing gate regression !!")
        else:
            print("  Note: All re-blocks are pre-fix (known historical state).")

    if total_cap_events == 0 and total_reblocks == 0:
        print()
        print("  >>> CLEAN: no reviewer-routing events detected. <<<")
        print()
    elif total_reblocks == 0:
        print()
        print("  >>> CLEAN: reviewer-capability gate working; no failed re-block cycles. <<<")
        print()

    print("=" * 72)
    return total_post_fix > 0


def main():
    parser = argparse.ArgumentParser(
        description="Reviewer-routing guard: detect reviewer-capability routing failures"
    )
    parser.add_argument("--db", help="Path to a single kanban DB file")
    parser.add_argument("--all-boards", action="store_true",
                        help="Scan all known kanban boards")
    parser.add_argument("--since", type=int, default=None,
                        help="Epoch timestamp to scan from (default: 2h ago)")
    parser.add_argument("--json", action="store_true", dest="output_json",
                        help="Output machine-readable JSON")
    parser.add_argument("--fix-deploy-ts", type=int, default=FIX_DEPLOY_TS,
                        help="Fix deployment epoch (default: %d)" % FIX_DEPLOY_TS)
    args = parser.parse_args()

    boards = _find_boards(args)
    if not boards:
        print("ERROR: No kanban DB files found.")
        sys.exit(2)

    now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    cutoff_ts = _get_cutoff_ts(args, now_ts)

    results = []
    for name, path in sorted(boards.items()):
        try:
            result = scan_board(name, path, cutoff_ts, args.fix_deploy_ts)
            results.append(result)
        except sqlite3.Error as e:
            results.append({
                "board": name,
                "db_path": path,
                "error": str(e),
                "status": "ERROR",
                "capability_events": [],
                "capability_event_count": 0,
                "spawn_fail_reblocks": [],
                "spawn_fail_reblock_count": 0,
                "post_fix_reblocks": 0,
                "pre_fix_reblocks": 0,
            })

    if args.output_json:
        print(json.dumps({
            "scan_ts": now_ts,
            "scan_ts_readable": datetime.datetime.fromtimestamp(
                now_ts, tz=datetime.timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fix_deploy_ts": args.fix_deploy_ts,
            "cutoff_ts": cutoff_ts,
            "boards": results,
            "total_capability_events": sum(r["capability_event_count"] for r in results),
            "total_spawn_fail_reblocks": sum(r["spawn_fail_reblock_count"] for r in results),
            "total_post_fix_reblocks": sum(r["post_fix_reblocks"] for r in results),
        }, indent=2))
        return 1 if sum(r["post_fix_reblocks"] for r in results) > 0 else 0
    else:
        has_regression = print_human_report(results)
        return 1 if has_regression else 0


if __name__ == "__main__":
    sys.exit(main())
