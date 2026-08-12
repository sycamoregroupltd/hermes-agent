#!/usr/bin/env python3
"""Soak monitoring: detect blocked→running transitions without proper unblock.

Runs every 5 minutes (no_agent cron). Queries the sycode-trading kanban DB's
task_events for transitions from 'blocked' to 'running' that were NOT preceded
by an 'unblocked' event within the preceding hour. Emits ALERT lines to stdout
when the bug pattern is found.

The fix deployed at ~19:29 UTC 2026-07-28 (epoch 1785266989) is supposed to
prevent tasks from being promoted from blocked status to ready without an
explicit unblock event. This monitoring proves the fix works (or alerts if it
reappears) over a 24-hour soak.

Excludes `dependency` block_kind (dependency-auto-promote is by design).
Excludes `transient` block_kind (transient-auto-promote is by design).
Excludes `triage` and `todo` initial-status promotions from noise.

Exit 0 on clean sweep (no post-fix violations), 1 if any post-fix alert fires.
"""

import argparse
import datetime
import json
import os
import sqlite3
import sys

# ── Constants ──────────────────────────────────────────────────────────────

DB_PATH = os.environ.get(
    "SOAK_KANBAN_DB",
    "/home/frank/.hermes/kanban/boards/sycode-trading/kanban.db",
)

# Look back this far for promotions to check (covers 5-min cron cycle + overlap)
PROMO_WINDOW_SECONDS = 360  # 6 minutes — overlap insurance vs 5-min cycle

# A transition from blocked→promoted is suspect if the last control event
# (blocked vs unblocked) before the promotion was "blocked" and no valid
# unblocked exists within this window before the promotion.
CONTROL_WINDOW_HOURS = 1  # unblocked must be within 1h of promotion to count

# Epoch timestamp of the fix deployment (recompute_ready + sticky blocked event).
# Violations before this are pre-existing; after this are regressions.
FIX_DEPLOY_TS = 1785266989  # ~2026-07-28 19:29 UTC

# Block kinds that legitimately auto-promote (no unblock needed).
AUTO_PROMOTE_KINDS = {"dependency", "transient"}

# State file for cumulative soak tracking (persists across cron ticks)
STATE_DIR = os.path.expanduser("~/.hermes/soak-state")
STATE_FILE = os.path.join(STATE_DIR, "blocked_to_running_soak_v2.json")

# Total ticks for 24h at 5min intervals
TOTAL_TICKS = 288

# This task id for reporting
TASK_ID = "t_10c8b42a"


# ── Helpers ────────────────────────────────────────────────────────────────

def _extract_block_kind(payload_json):
    """Safely extract the 'kind' field from a blocked event payload."""
    try:
        parsed = json.loads(payload_json) if payload_json else {}
        return parsed.get("kind")
    except (json.JSONDecodeError, TypeError):
        return None


def _load_state():
    """Load cumulative soak state from disk, or initialise fresh."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "task_id": TASK_ID,
        "started_at": int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
        "started_ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ticks_completed": 0,
        "total_ticks": TOTAL_TICKS,
        "tick_interval_minutes": 5,
        "soak_hours": 24,
        "violations_found": 0,
        "last_violation_at": None,
        "last_violation_ts": None,
        "post_fix_violations": 0,
        "pre_fix_violations": 0,
        "clean_ticks": 0,
        "status": "running",  # running | clean | violations
    }


def _save_state(state):
    """Persist cumulative soak state."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _print_report(state, ticks_remaining, now_ts):
    """Print a structured soak report to stdout."""
    elapsed_h = (now_ts - state["started_at"]) / 3600
    pct = (state["ticks_completed"] / TOTAL_TICKS) * 100 if TOTAL_TICKS else 0

    print("=" * 72)
    print(f"  BLOCKED→RUNNING SOAK MONITOR  |  Task {TASK_ID}")
    print("=" * 72)
    print(f"  Status:       {state['status'].upper()}")
    print(f"  Started:      {state['started_ts']}")
    print(f"  Elapsed:      {elapsed_h:.1f}h / 24h")
    print(f"  Ticks:        {state['ticks_completed']}/{TOTAL_TICKS}  ({pct:.0f}%)")
    print(f"  Clean ticks:  {state['clean_ticks']}")
    print(f"  Violations:   {state['violations_found']} total  "
          f"({state['post_fix_violations']} post-fix, {state['pre_fix_violations']} pre-fix)")
    print(f"  Remaining:    {ticks_remaining} ticks (~{ticks_remaining * 5}m = "
          f"{ticks_remaining * 5 / 60:.1f}h)")
    if state["last_violation_ts"]:
        print(f"  Last violation: {state['last_violation_ts']}")
    print("=" * 72)

    if state["post_fix_violations"] > 0:
        print()
        print("  !! POST-FIX VIOLATIONS DETECTED — FIX IS NOT HOLDING !!")
        print()
    elif state["pre_fix_violations"] > 0:
        print()
        print("  Note: Pre-fix violations exist (known historical state).")
        print("  These are expected and do not indicate a regression.")
        print()

    if ticks_remaining <= 0:
        print()
        if state["post_fix_violations"] == 0:
            print("  >>> SOAK COMPLETE: 24h passed with zero post-fix violations. <<<")
            print("  >>> The fix (recompute_ready + sticky blocked event) is HOLDING. <<<")
        else:
            print("  >>> SOAK COMPLETE but POST-FIX violations were detected. <<<")
            print("  >>> The fix (recompute_ready + sticky blocked event) is NOT holding. <<<")
        print()

    # Emit JSON summary line for machine parsing
    summary = {
        "task_id": TASK_ID,
        "status": state["status"],
        "elapsed_hours": round(elapsed_h, 2),
        "ticks_completed": state["ticks_completed"],
        "total_ticks": TOTAL_TICKS,
        "clean_ticks": state["clean_ticks"],
        "violations_found": state["violations_found"],
        "post_fix_violations": state["post_fix_violations"],
        "pre_fix_violations": state["pre_fix_violations"],
    }
    print(f"SOAK_SUMMARY: {json.dumps(summary)}")
    print()


def check_violations(conn, cutoff_ts):
    """Query the kanban DB and return (violations, post_fix_count, pre_fix_count)."""
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
        is_post_fix = promoted_at >= FIX_DEPLOY_TS

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


def print_violation(v):
    """Print a single violation detail."""
    label = "POST-FIX-REGRESSION" if v["post_fix"] else "PRE-FIX"
    print(f"  [{label}] Task:        {v['task_id']}")
    print(f"           Status:      {v['status']}  block_kind={v['block_kind']}")
    if v["event_block_kind"]:
        print(f"           Event kind:  {v['event_block_kind']}")
    print(f"           Promoted at: {v['promoted_ts']}")
    print(f"           Blocked at:  {v['blocked_ts']}  ({v['blocked_minutes_ago']} min before)")
    print(f"           Block msg:   {v['block_reason']}")
    print(f"           No unblocked event found (within {CONTROL_WINDOW_HOURS}h window)")
    print(f"           {'─' * 40}")


def main():
    parser = argparse.ArgumentParser(
        description="Blocked→Running transition soak monitor"
    )
    parser.add_argument("--reset", action="store_true",
                        help="Reset cumulative soak state (start fresh)")
    parser.add_argument("--status", action="store_true",
                        help="Print current soak status without running checks")
    args = parser.parse_args()

    # ── Reset mode ─────────────────────────────────────────────────────────
    if args.reset:
        state = _load_state()
        # Clear violations but keep start time
        state["violations_found"] = 0
        state["post_fix_violations"] = 0
        state["pre_fix_violations"] = 0
        state["clean_ticks"] = 0
        state["last_violation_at"] = None
        state["last_violation_ts"] = None
        state["ticks_completed"] = 0
        state["started_at"] = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        state["started_ts"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        state["status"] = "running"
        _save_state(state)
        print(f"Soak state reset. Started at {state['started_ts']}")
        return 0

    # ── Status mode ────────────────────────────────────────────────────────
    state = _load_state()
    now_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    ticks_remaining = max(0, TOTAL_TICKS - state["ticks_completed"])

    if args.status:
        _print_report(state, ticks_remaining, now_ts)
        return 0

    # ── Main check mode ────────────────────────────────────────────────────
    cutoff_ts = now_ts - PROMO_WINDOW_SECONDS

    if not os.path.exists(DB_PATH):
        print(f"SKIP: kanban DB not found at {DB_PATH}")
        print(f"SOAK_SUMMARY: {{\"status\":\"skip\",\"reason\":\"db-not-found\",\"task_id\":\"{TASK_ID}\"}}")
        return 0

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro&immutable=1", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row

    violations, post_fix_count, pre_fix_count = check_violations(conn, cutoff_ts)
    conn.close()

    # ── Update cumulative state ────────────────────────────────────────────
    state["ticks_completed"] += 1
    state["violations_found"] += len(violations)
    state["post_fix_violations"] += post_fix_count
    state["pre_fix_violations"] += pre_fix_count

    if len(violations) == 0:
        state["clean_ticks"] += 1

    if violations:
        state["status"] = "violations"
        state["last_violation_at"] = now_ts
        state["last_violation_ts"] = datetime.datetime.now(
            datetime.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

    _save_state(state)

    # ── Print violations ───────────────────────────────────────────────────
    if violations:
        print("=" * 72)
        print(f"  ALERT: blocked→running transition without unblock")
        print(f"  Detected at {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
        print(f"  {len(violations)} violation(s) | "
              f"{post_fix_count} post-fix | {pre_fix_count} pre-fix")
        print("=" * 72)

        if post_fix_count > 0:
            print()
            print("  >> POST-FIX violations (regression — fix is NOT holding) <<")
            print()
            for v in violations:
                if v["post_fix"]:
                    print_violation(v)

        if pre_fix_count > 0:
            print()
            print("  >> Pre-existing violations (stale — known state before fix) <<")
            print()
            for v in violations:
                if not v["post_fix"]:
                    print_violation(v)

        print()

    # ── Print cumulative report ───────────────────────────────────────────
    ticks_remaining = max(0, TOTAL_TICKS - state["ticks_completed"])
    _print_report(state, ticks_remaining, now_ts)

    print(f"24h soak monitoring for {TASK_ID}.")
    print(f"Post-fix violations = fix regression (needs investigation).")
    print(f"Pre-fix violations = known historical state (no unblock needed before fix).")
    print("=" * 72)

    # Return 1 only if there are post-fix violations
    return 1 if post_fix_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
