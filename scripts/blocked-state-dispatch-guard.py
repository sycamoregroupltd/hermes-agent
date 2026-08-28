#!/usr/bin/env python3
"""Blocked-state dispatch guard: detect blocked→running without unblock.

One-shot detector for all kanban boards. Queries task_events for promoted
events whose preceding control event was 'blocked' without a valid 'unblocked'
intervening. Flags violations as POST-FIX (after fix deployment epoch) or
PRE-FIX (historical, before fix).

Excludes dependency/transient block_kinds (legitimate auto-promote).
Excludes specified/decomposed events (legitimate reclassification).

Acceptance check for t_a2ef2ea2 criterion (2): zero blocked→running
transitions without explicit unblock event.

Usage:
    python3 blocked-state-dispatch-guard.py                    # default boards
    python3 blocked-state-dispatch-guard.py --db <path>        # single board
    python3 blocked-state-dispatch-guard.py --all-boards       # all known boards
    python3 blocked-state-dispatch-guard.py --json             # machine-readable
    python3 blocked-state-dispatch-guard.py --help
"""

import argparse
import datetime
import json
import os
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

# Epoch timestamp of the fix deployment (recompute_ready + sticky blocked event).
FIX_DEPLOY_TS = 1785266989  # ~2026-07-28 19:29 UTC

# Block kinds that legitimately auto-promote (no unblock needed).
AUTO_PROMOTE_KINDS = {"dependency", "transient"}

# How far back to look for promotions (covers missed cron cycles)
LOOKBACK_HOURS = 2

# An unblocked event older than this before the promotion is stale
CONTROL_WINDOW_HOURS = 1


# ── Helpers ─────────────────────────────────────────────────────────────────

def _extract_block_kind(payload_json):
    """Safely extract the 'kind' field from a blocked event payload."""
    try:
        parsed = json.loads(payload_json) if payload_json else {}
        return parsed.get("kind")
    except (json.JSONDecodeError, TypeError):
        return None


def _find_boards(args):
    """Resolve which board DBs to scan."""
    boards = {}
    if args.db:
        boards[os.path.basename(os.path.dirname(args.db)) or "custom"] = args.db
    elif args.all_boards:
        boards = dict(DEFAULT_BOARDS)
    else:
        boards = dict(DEFAULT_BOARDS)
    # Filter to existing paths only
    return {name: path for name, path in boards.items() if os.path.exists(path)}


def _get_end_ts(args):
    """Get the end timestamp for the event window."""
    now = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    if args.since:
        return now  # use the explicit --since as cutoff
    return now


def _get_cutoff_ts(args, now_ts):
    """Get the cutoff timestamp (earliest event to consider)."""
    if args.since:
        return args.since
    return now_ts - (LOOKBACK_HOURS * 3600)


def check_board_violations(conn, cutoff_ts, fix_deploy_ts):
    """Query one board's DB and return (violations, post_fix, pre_fix).

    Uses the same detection logic as blocked_to_running_soak_watchdog.py.
    """
    violations = []

    rows = conn.execute(
        """
        SELECT e.id, e.task_id, e.created_at AS promoted_at,
               t.status, t.block_kind
        FROM task_events e
        JOIN tasks t ON t.id = e.task_id
        WHERE e.kind = 'promoted'
          AND e.created_at >= ?
        ORDER BY e.created_at
        """,
        (cutoff_ts,),
    ).fetchall()

    for row in rows:
        tid = row["task_id"]
        promoted_at = row["promoted_at"]
        block_kind = row["block_kind"]
        status = row["status"]

        # Exclude auto-promote block kinds
        if block_kind in AUTO_PROMOTE_KINDS:
            continue

        # Most recent blocked event BEFORE this promotion
        blocked_row = conn.execute(
            """
            SELECT created_at, payload
            FROM task_events
            WHERE task_id = ? AND kind = 'blocked' AND created_at < ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (tid, promoted_at),
        ).fetchone()

        if not blocked_row:
            continue  # never been blocked — normal promotion

        blocked_at = blocked_row["created_at"]

        # Extract the block_kind from the event payload
        event_block_kind = _extract_block_kind(blocked_row["payload"])
        if event_block_kind in AUTO_PROMOTE_KINDS:
            continue  # this specific block was an auto-promote kind

        # Most recent unblocked event BEFORE or AT this promotion
        unblocked_row = conn.execute(
            """
            SELECT created_at
            FROM task_events
            WHERE task_id = ? AND kind = 'unblocked' AND created_at <= ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (tid, promoted_at),
        ).fetchone()

        # Check for a 'specified' event after the block
        specified_row = conn.execute(
            """
            SELECT created_at
            FROM task_events
            WHERE task_id = ? AND kind = 'specified' AND created_at > ?
              AND created_at <= ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (tid, blocked_at, promoted_at),
        ).fetchone()
        has_specified = specified_row is not None

        # Check for a 'decomposed' event between block and promotion
        decomposed_row = conn.execute(
            """
            SELECT created_at
            FROM task_events
            WHERE task_id = ? AND kind = 'decomposed' AND created_at > ?
              AND created_at < ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (tid, blocked_at, promoted_at),
        ).fetchone()
        has_decomposed = decomposed_row is not None

        has_valid_unblock = (
            not has_specified
            and not has_decomposed
            and unblocked_row is not None
            and unblocked_row["created_at"] > blocked_at
            and (promoted_at - unblocked_row["created_at"]) <= 3600
        )

        if has_specified or has_decomposed or has_valid_unblock:
            continue  # legitimate transition

        # VIOLATION
        blocked_ago_m = (promoted_at - blocked_at) / 60
        block_payload = blocked_row["payload"] or "{}"
        is_post_fix = promoted_at >= fix_deploy_ts

        violations.append({
            "task_id": tid,
            "promoted_at": promoted_at,
            "promoted_ts": datetime.datetime.fromtimestamp(
                promoted_at, tz=datetime.timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "blocked_at": blocked_at,
            "blocked_ts": datetime.datetime.fromtimestamp(
                blocked_at, tz=datetime.timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "blocked_minutes_ago": round(blocked_ago_m, 1),
            "block_reason": block_payload[:300],
            "status": status,
            "block_kind": block_kind,
            "event_block_kind": event_block_kind,
            "post_fix": is_post_fix,
        })

    post_fix = [v for v in violations if v["post_fix"]]
    pre_fix = [v for v in violations if not v["post_fix"]]
    return violations, len(post_fix), len(pre_fix)


def scan_board(name, db_path, cutoff_ts, fix_deploy_ts, output_json, end_ts=None):
    """Scan one board and print results."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    violations, post_fix, pre_fix = check_board_violations(conn, cutoff_ts, fix_deploy_ts)
    conn.close()

    result = {
        "board": name,
        "db_path": db_path,
        "total_violations": len(violations),
        "post_fix": post_fix,
        "pre_fix": pre_fix,
        "status": "CLEAN" if len(violations) == 0 else "VIOLATIONS",
        "cutoff_ts": cutoff_ts,
        "cutoff_ts_readable": datetime.datetime.fromtimestamp(
            cutoff_ts, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "violations": violations,
    }

    return result


def print_human_report(results, args):
    """Print a human-readable report."""
    now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    now_str = datetime.datetime.fromtimestamp(
        now_ts, tz=datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    print("=" * 72)
    print(f"  BLOCKED-STATE DISPATCH GUARD  |  {now_str}")
    print("=" * 72)
    total_violations = sum(r["total_violations"] for r in results)
    total_post_fix = sum(r["post_fix"] for r in results)
    total_pre_fix = sum(r["pre_fix"] for r in results)
    print(f"  Boards scanned: {len(results)}")
    print(f"  Total violations: {total_violations}")
    print(f"    Post-fix:  {total_post_fix}")
    print(f"    Pre-fix:   {total_pre_fix}")
    print(f"  Fix epoch: {FIX_DEPLOY_TS} ({datetime.datetime.fromtimestamp(FIX_DEPLOY_TS, tz=datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})")
    print("=" * 72)

    if total_violations > 0:
        # Sort: post-fix first (most important)
        all_violations = []
        for r in results:
            for v in r["violations"]:
                v["board"] = r["board"]
                all_violations.append(v)
        all_violations.sort(key=lambda v: (not v["post_fix"], v["promoted_at"]))

        for v in all_violations:
            label = "POST-FIX-REGRESSION" if v["post_fix"] else "PRE-FIX"
            print()
            print(f"  [{label}] Board:       {v['board']}")
            print(f"           Task:        {v['task_id']}")
            print(f"           Status:      {v['status']}  block_kind={v['block_kind']}")
            if v["event_block_kind"]:
                print(f"           Event kind:  {v['event_block_kind']}")
            print(f"           Promoted at: {v['promoted_ts']}")
            print(f"           Blocked at:  {v['blocked_ts']}  ({v['blocked_minutes_ago']} min before)")
            print(f"           Block msg:   {v['block_reason']}")
            print(f"           {'─' * 40}")

    if total_post_fix > 0:
        print()
        print("  !! POST-FIX VIOLATIONS DETECTED — FIX IS NOT HOLDING !!")
        print()
    elif total_pre_fix > 0:
        print()
        print("  Note: Pre-fix violations exist (known historical state).")
        print("  These are expected and do not indicate a regression.")
        print()

    if total_violations == 0:
        print()
        print("  >>> CLEAN: no blocked→running violations detected. <<<")
        print()

    print("=" * 72)
    return total_post_fix > 0  # exit 1 if post-fix violations


def main():
    parser = argparse.ArgumentParser(
        description="Blocked-state dispatch guard: detect blocked→running without unblock"
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
            result = scan_board(name, path, cutoff_ts, args.fix_deploy_ts,
                                args.output_json, now_ts)
            results.append(result)
        except sqlite3.Error as e:
            results.append({
                "board": name,
                "db_path": path,
                "error": str(e),
                "status": "ERROR",
                "total_violations": 0,
                "post_fix": 0,
                "pre_fix": 0,
                "violations": [],
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
            "total_violations": sum(r["total_violations"] for r in results),
            "total_post_fix": sum(r["post_fix"] for r in results),
            "total_pre_fix": sum(r["pre_fix"] for r in results),
        }, indent=2))
        return 1 if sum(r["post_fix"] for r in results) > 0 else 0
    else:
        has_regression = print_human_report(results, args)
        return 1 if has_regression else 0


if __name__ == "__main__":
    sys.exit(main())
