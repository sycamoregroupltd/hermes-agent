#!/usr/bin/env python3
"""Soak-test monitor for blocked-card dispatch fix (t_f4984287).

Periodic checker that:
  1. Scans task_events for blocked→claimed/spawned transitions without unblock.
  2. Monitors tasks table for status='blocked' that appear in claimed events.
  3. Injects test blocked cards and verifies they stay unclaimed.
  4. Outputs structured report with any violations (or clean bill of health).

Usage:
  python3 soak_monitor.py [--inject-test] [--report] [--db PATH]

--inject-test : Create one test blocked card per run (idempotent).
--report      : Produce final summary report covering the entire soak window.
--db PATH     : Path to kanban DB (default ~/.hermes/kanban.db).
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from hermes_cli import kanban_db as kb

KANBAN_DB = Path.home() / ".hermes" / "kanban.db"
SOAK_MARKER_PREFIX = "soak-test-blocked-"
SOAK_EPOCH_FILE = Path.home() / ".hermes" / "soak_epoch.txt"
SOAK_REPORT_FILE = Path.home() / ".hermes" / "soak_report.jsonl"
SOAK_VIOLATIONS_FILE = Path.home() / ".hermes" / "soak_violations.jsonl"
SOAK_INJECTED_FILE = Path.home() / ".hermes" / "soak_injected.txt"

now = lambda: int(time.time())


def get_conn(db_path: Path) -> sqlite3.Connection:
    """Open connection via kanban_db.connect() — handles WAL, row_factory, init."""
    return kb.connect(db_path=db_path)


def find_violations(conn: sqlite3.Connection, since_ts: int) -> list[dict]:
    """Find blocked→running transitions without prior unblock event."""
    violations = []

    # Pattern 1: 'claimed' or 'spawned' events on tasks whose last previous
    # 'blocked'/'unblocked' event was 'blocked' (no intervening unblock).
    rows = conn.execute("""
        SELECT e.id, e.task_id, e.kind, e.created_at,
               t.status AS current_status
        FROM task_events e
        JOIN tasks t ON t.id = e.task_id
        WHERE e.kind IN ('claimed', 'spawned')
          AND e.created_at >= ?
          AND e.id > (
              SELECT COALESCE(MAX(e2.id), 0)
              FROM task_events e2
              WHERE e2.task_id = e.task_id
                AND e2.kind IN ('blocked', 'unblocked')
                AND e2.id < e.id
                AND e2.kind = 'blocked'
          )
          AND NOT EXISTS (
              SELECT 1 FROM task_events e3
              WHERE e3.task_id = e.task_id
                AND e3.kind = 'unblocked'
                AND e3.id < e.id
                AND e3.id > (
                    SELECT COALESCE(MAX(e4.id), 0)
                    FROM task_events e4
                    WHERE e4.task_id = e.task_id
                      AND e4.kind = 'blocked'
                      AND e4.id < e.id
                )
          )
        ORDER BY e.id
    """, (since_ts,)).fetchall()

    for r in rows:
        violations.append({
            "type": "blocked_to_claimed_no_unblock",
            "event_id": r["id"],
            "task_id": r["task_id"],
            "event_kind": r["kind"],
            "event_ts": r["created_at"],
            "current_status": r["current_status"],
            "description": f"Task {r['task_id']} had {r['kind']} event "
                           f"while most recent block event was 'blocked' (no unblock)",
        })

    # Pattern 2: tasks whose status changed from 'blocked' to 'running' 
    # without an unblocked event (raw status change, no claim event).
    # This catches direct DB edits or code-path bugs.
    rows2 = conn.execute("""
        SELECT t.id, t.status, t.started_at, t.created_at
        FROM tasks t
        WHERE t.status = 'running'
          AND t.started_at >= ?
          AND EXISTS (
              SELECT 1 FROM task_events e
              WHERE e.task_id = t.id
                AND e.kind = 'blocked'
                AND e.id > COALESCE((
                    SELECT MAX(e2.id) FROM task_events e2
                    WHERE e2.task_id = t.id AND e2.kind = 'unblocked'
                ), 0)
          )
    """, (since_ts,)).fetchall()

    for r in rows2:
        violations.append({
            "type": "blocked_status_to_running_no_unblock",
            "task_id": r["id"],
            "current_status": r["status"],
            "started_at": r["started_at"],
            "description": f"Task {r['id']} status is 'running' but last "
                           f"block/unblock event was 'blocked' (no unblock)",
        })

    return violations


def inject_test_blocked_card(conn: sqlite3.Connection) -> str | None:
    """Create one persistent test blocked card if one doesn't already exist.
    Returns the task id, or None if already exists."""
    existing = conn.execute(
        "SELECT id FROM tasks WHERE id LIKE ? AND status = 'blocked'",
        (f"{SOAK_MARKER_PREFIX}%",),
    ).fetchone()
    if existing:
        return existing["id"]

    from uuid import uuid4
    task_id = f"{SOAK_MARKER_PREFIX}{uuid4().hex[:8]}"
    ts = now()
    conn.execute(
        "INSERT INTO tasks (id, title, body, assignee, status, block_kind, "
        "created_at, workspace_kind) "
        "VALUES (?, ?, ?, ?, 'blocked', ?, ?, 'scratch')",
        (task_id,
         f"[SOAK TEST] Blocked card for dispatch gate verification",
         "This task is a soak-test artifact. If it ever gets claimed/dispatched, "
         "the block-gate fix has regressed.",
         "builder",  # assignee that exists
         "needs_input",
         ts),
    )
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES (?, 'created', ?, ?)",
        (task_id, json.dumps({"via": "soak_monitor.py"}), ts),
    )
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES (?, 'blocked', ?, ?)",
        (task_id, json.dumps({"block_kind": "needs_input",
                              "reason": "soak-test blocked card",
                              "origin": "soak_monitor.py"}), ts),
    )
    conn.commit()
    return task_id


def verify_test_cards(conn: sqlite3.Connection) -> list[dict]:
    """Check all injected test cards are still blocked and unclaimed."""
    results = []
    cards = conn.execute(
        "SELECT id, status, started_at, claim_lock FROM tasks "
        "WHERE id LIKE ?",
        (f"{SOAK_MARKER_PREFIX}%",),
    ).fetchall()
    for c in cards:
        ok = True
        issues = []
        if c["status"] != "blocked":
            ok = False
            issues.append(f"status={c['status']} (expected 'blocked')")
        if c["started_at"] is not None:
            ok = False
            issues.append(f"started_at={c['started_at']} (was claimed/dispatched)")
        if c["claim_lock"] is not None:
            ok = False
            issues.append(f"claim_lock={c['claim_lock']} (was locked)")
        results.append({
            "task_id": c["id"],
            "ok": ok,
            "status": c["status"],
            "issues": issues,
        })
    return results


def write_report_entry(entry: dict):
    """Append one JSONL line to the permanent report file."""
    with open(SOAK_REPORT_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def write_violation(v: dict):
    """Append one violation to the violations file."""
    v["detected_at"] = now()
    with open(SOAK_VIOLATIONS_FILE, "a") as f:
        f.write(json.dumps(v) + "\n")


def run_check(args: argparse.Namespace):
    """Run one periodic check."""
    db_path = Path(args.db) if args.db else KANBAN_DB
    conn = get_conn(db_path)

    # Determine epoch start
    if SOAK_EPOCH_FILE.exists():
        epoch_start = int(SOAK_EPOCH_FILE.read_text().strip())
    else:
        epoch_start = now()
        SOAK_EPOCH_FILE.write_text(str(epoch_start))

    # Run checks
    elapsed_h = (now() - epoch_start) / 3600.0

    # Scan ALL events since epoch start
    violations = find_violations(conn, epoch_start)

    # Inject test card if requested
    injected = None
    if args.inject_test:
        injected = inject_test_blocked_card(conn)

    # Verify existing test cards
    test_card_results = verify_test_cards(conn)

    # If we injected this run, note it in the injected file
    if injected:
        with open(SOAK_INJECTED_FILE, "a") as f:
            f.write(f"{injected} {now()}\n")

    # Write report entry
    entry = {
        "check_ts": now(),
        "check_iso": datetime.now(timezone.utc).isoformat(),
        "elapsed_h": round(elapsed_h, 2),
        "new_violations": len(violations),
        "test_cards_injected": 1 if injected else 0,
        "test_cards_total": len(test_card_results),
        "test_cards_ok": sum(1 for c in test_card_results if c["ok"]),
    }
    write_report_entry(entry)

    # Write any violations
    for v in violations:
        write_violation(v)

    # Print summary to stdout
    print(f"=== Soak Monitor Check @ {entry['check_iso']} ===")
    print(f"  Elapsed: {elapsed_h:.1f}h / 24.0h")
    print(f"  Violations detected: {len(violations)}")
    for v in violations:
        print(f"    ⚠ {v['description']}")
    print(f"  Test blocked cards: {entry['test_cards_ok']}/{entry['test_cards_total']} ok")
    for c in test_card_results:
        status_icon = "✓" if c["ok"] else "⚠"
        print(f"    {status_icon} {c['task_id']}: status={c['status']}"
              f"{' - issues: ' + '; '.join(c['issues']) if c.get('issues') else ''}")
    print(f"  Evidence: {SOAK_REPORT_FILE}")
    print(f"========================================")

    return len(violations) == 0 and all(c["ok"] for c in test_card_results)


def produce_final_report(args: argparse.Namespace):
    """Produce the 24-hour final report."""
    db_path = Path(args.db) if args.db else KANBAN_DB
    conn = get_conn(db_path)

    if SOAK_EPOCH_FILE.exists():
        epoch_start = int(SOAK_EPOCH_FILE.read_text().strip())
    else:
        epoch_start = now()
        SOAK_EPOCH_FILE.write_text(str(epoch_start))

    elapsed_h = (now() - epoch_start) / 3600.0

    # Read all report entries
    entries = []
    if SOAK_REPORT_FILE.exists():
        with open(SOAK_REPORT_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))

    # Read all violations
    violations = []
    if SOAK_VIOLATIONS_FILE.exists():
        with open(SOAK_VIOLATIONS_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    violations.append(json.loads(line))

    # Read injected test cards
    injected_ids = set()
    if SOAK_INJECTED_FILE.exists():
        for line in SOAK_INJECTED_FILE.read_text().strip().splitlines():
            parts = line.strip().split()
            if parts:
                injected_ids.add(parts[0])

    # Current state of test cards
    test_cards = verify_test_cards(conn)

    # Print final report
    print()
    print("=" * 70)
    print("  SOAK TEST FINAL REPORT — Blocked-card dispatch gate verification")
    print("=" * 70)
    print()
    print(f"  Soak period: {epoch_start} → {now()} ({elapsed_h:.1f}h)")
    print(f"  Checks performed: {len(entries)}")
    print(f"  Test cards injected: {len(injected_ids)}")
    print(f"  Test cards still blocked+unclaimed: "
          f"{sum(1 for c in test_cards if c['ok'])}/{len(test_cards)}")
    print()
    if test_cards:
        for c in test_cards:
            ok = c["ok"]
            print(f"  {'✓' if ok else '⚠'} Test card {c['task_id']}: "
                  f"{'HELD' if ok else 'BREACHED — ' + '; '.join(c['issues'])}")

    print()
    print(f"  Total violations found: {len(violations)}")
    if violations:
        print()
        print("  VIOLATIONS:")
        for v in violations:
            print(f"    ⚠ [{v.get('type', '?')}] {v['description']}")
            print(f"       task_id={v['task_id']} event_id={v.get('event_id', 'N/A')}")
        print()
        verdict = "FAIL — Violations detected during soak"
    else:
        verdict = "PASS — Zero violations across entire soak window"

    # Also do a final scan for any violations we might have missed
    final_violations = find_violations(conn, epoch_start)
    if final_violations and not violations:
        print()
        print(f"  ⚠ LATE VIOLATIONS (found in final scan):")
        for v in final_violations:
            print(f"    ⚠ {v['description']}")
            write_violation(v)
            violations.append(v)
        verdict = "FAIL — Violations detected in final scan"

    print()
    print(f"  VERDICT: {verdict}")
    print()
    print("  EVIDENCE FILES:")
    print(f"    Report:    {SOAK_REPORT_FILE}")
    print(f"    Violations:{SOAK_VIOLATIONS_FILE}")
    print(f"    Injected:  {SOAK_INJECTED_FILE}")
    print("=" * 70)

    return violations, verdict


def main():
    parser = argparse.ArgumentParser(
        description="Soak-test monitor for blocked-card dispatch fix")
    parser.add_argument("--db", default=None,
                        help=f"Kanban DB path (default {KANBAN_DB})")
    parser.add_argument("--inject-test", action="store_true",
                        help="Inject a test blocked card")
    parser.add_argument("--report", action="store_true",
                        help="Produce 24h final report")
    parser.add_argument("--snapshot", action="store_true",
                        help="Just dump current state, no monitoring")
    args = parser.parse_args()

    if args.report:
        produce_final_report(args)
        return

    ok = run_check(args)
    sys.exit(0 if ok else 3)


if __name__ == "__main__":
    main()
