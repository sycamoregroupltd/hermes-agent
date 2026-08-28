#!/usr/bin/env python3
"""
gate-routing-watchdog.py — Gate-routing watchdog for sycode-trading kanban board.

Scans blocked/"needs_input" cards for gate-type patterns (gate MET, production
write approval, W2.5, etc.) and validates they are assigned to a reviewer profile
(trading-risk-reviewer, guardian, etc.) rather than an executor profile (trading-
data-oracle, trading-devops, etc.) that cannot self-approve production operations.

SMART FILTERING:
- Skips cards that already have a REVIEW_VERDICT (reviewed and awaiting external gate)
- Skips cards that are correctly assigned to their work profile (research/data tasks)
- Only flags cards that are UNASSIGNED or MISROUTED to executors for pending approval

When a misrouted gate card is detected, it:
1. Leaves a durable comment on the card with routing guidance
2. Reports to stdout for cron delivery
3. Logs to ~/.hermes/logs/gate-routing-watchdog.log

Pattern: no_agent=True script using subprocess for kanban CLI commands.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

# ── Config ──────────────────────────────────────────────────────────────────

KANBAN_DB = os.path.expanduser(
    "~/.hermes/kanban/boards/sycode-trading/kanban.db"
)

# Profiles that SHOULD own gate-type cards (reviewer/approver roles)
REVIEWER_PROFILES = frozenset({
    "trading-risk-reviewer",
    "guardian",
    "sycode-trading-pm",
    "platform-reviewer",
    "os-reviewer",
    "research-trading",
    "test-engineer",
})

# Profiles that should NOT own gate-type production-write cards (executors)
EXECUTOR_PROFILES = frozenset({
    "trading-data-oracle",
    "trading-devops",
    "platform-db-migrator",
    "trading-backtest-runner",
    "trading-strategy-dev",
    "trading-ml-ensemble",
    "trading-market-analyst",
    "jarvis",
    "jarvis-os-pm",
    "integration-builder",
    "builder",
    "self-improve-engineer",
})

# Gate-type patterns in title (case-insensitive match)
TITLE_GATE_PATTERNS = (
    "gate MET",
    "gate reached",
    "production write",
    "W2.5",
    "approve catalyst",
    "production gate",
    "service-gate",
    "runtime gate",
    "build-gate",
    "stop-gate",
)

# Gate-type patterns in body
BODY_GATE_PATTERNS = (
    "gate met",
    "gate reached",
    "production gate",
    "production write",
    "w2.5",
)

# ── Helpers ─────────────────────────────────────────────────────────────────


def utcnow_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_gate_card(title: str, body: str) -> bool:
    """Return True if a card matches production-write/approval gate patterns.

    Excludes research/investigation cards that use 'gate' in a different context
    (e.g., 'Gated: Any paid data source requires Frank approval').
    """
    tl = title.lower()
    bl = body.lower()[:2000]

    # Must match a production-write/approval gate pattern
    for p in TITLE_GATE_PATTERNS:
        if p.lower() in tl:
            return True
    for p in BODY_GATE_PATTERNS:
        if p.lower() in bl:
            return True
    return False


def has_review_verdict(conn, card_id: str) -> bool:
    """Check if the card already has a REVIEW_VERDICT comment from a reviewer."""
    cur = conn.cursor()
    cur.execute("""
        SELECT 1 FROM task_comments
        WHERE task_id = ? AND body LIKE '%REVIEW_VERDICT%'
        LIMIT 1
    """, (card_id,))
    return cur.fetchone() is not None


def has_recent_watchdog_comment(conn, card_id: str, max_age_s: int = 3600) -> bool:
    """Check if watchdog already left a comment recently (avoids spam)."""
    cur = conn.cursor()
    cur.execute("""
        SELECT created_at FROM task_comments
        WHERE task_id = ? AND body LIKE '%gate-routing-watchdog%'
        ORDER BY created_at DESC LIMIT 1
    """, (card_id,))
    row = cur.fetchone()
    if not row:
        return False
    age = datetime.now(timezone.utc).timestamp() - row[0]
    return age < max_age_s


def kanban_comment(card_id: str, body: str) -> bool:
    """Leave a durable comment on a kanban card via CLI."""
    try:
        p = subprocess.run(
            ["hermes", "kanban", "--board", "sycode-trading",
             "comment", card_id, body],
            capture_output=True, text=True, timeout=30,
        )
        if p.returncode != 0:
            print(f"[watchdog] kanban comment FAILED rc={p.returncode}: "
                  f"{p.stderr[:200]}", flush=True)
        return p.returncode == 0
    except Exception as e:
        print(f"[watchdog] kanban comment ERROR: {e}", flush=True)
        return False


def get_reviewer_for_card(title: str, body: str) -> str:
    """Determine the correct reviewer profile for a gate card."""
    t = (title + " " + body).lower()

    if "catalyst" in t and "market_news" in t:
        return "trading-risk-reviewer"
    if "production write" in t:
        return "trading-risk-reviewer"
    if "service-gate" in t:
        return "sycode-trading-pm"
    if "guardian" in t:
        return "guardian"
    if "build" in t:
        return "trading-risk-reviewer"
    return "trading-risk-reviewer"


# ── Main ────────────────────────────────────────────────────────────────────


def main():
    if not os.path.exists(KANBAN_DB):
        print(f"[gate-watchdog] KANBAN_DB not found: {KANBAN_DB}", flush=True)
        return 1

    conn = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Fetch ALL blocked cards
    c.execute("""
        SELECT
            id, title, COALESCE(body, '') AS body,
            COALESCE(assignee, '') AS assignee,
            status,
            COALESCE(block_kind, '') AS block_kind,
            COALESCE(block_recurrences, 0) AS block_recurrences
        FROM tasks
        WHERE status = 'blocked'
        ORDER BY priority DESC, created_at ASC
    """)

    cards = c.fetchall()

    if not cards:
        conn.close()
        print("[gate-watchdog] No blocked cards found — fleet healthy", flush=True)
        return 0

    reports = []
    for row in cards:
        cid = row["id"]

        # Pre-filter: must look like a production-write / approval gate card
        if not is_gate_card(row["title"], row["body"]):
            continue

        # Skip cards that already have a REVIEW_VERDICT (review completed)
        if has_review_verdict(conn, cid):
            continue

        assignee = row["assignee"].strip() if row["assignee"] else ""
        title = row["title"]
        body = row["body"]

        issues = []
        actions = []

        if not assignee:
            issues.append("UNASSIGNED — gate card has no assignee")
        elif assignee in EXECUTOR_PROFILES:
            issues.append(
                f"MISROUTED — assigned to executor profile '{assignee}' "
                f"instead of a reviewer profile"
            )
        elif assignee not in REVIEWER_PROFILES:
            issues.append(
                f"UNEXPECTED ASSIGNEE — '{assignee}' is not a known reviewer"
            )

        if row["block_kind"] == "needs_input" and (row["block_recurrences"] or 0) >= 3:
            issues.append(
                f"STUCK — blocked 'needs_input' with "
                f"{row['block_recurrences']} recurrences"
            )

        if not issues:
            continue

        # Check for recent watchdog comment
        if has_recent_watchdog_comment(conn, cid):
            actions.append("SKIPPED (recent comment exists)")
        else:
            suggested = get_reviewer_for_card(title, body)
            comment_lines = [
                "**gate-routing-watchdog: routing issue detected**",
                *[f"- {i}" for i in issues],
                "",
                f"**Suggested:** reassign to `{suggested}` (reviewer) — "
                "executor profiles cannot self-approve production operations.",
                "",
                f"🤖 `gate-routing-watchdog` @ {utcnow_iso()}",
            ]
            ok = kanban_comment(cid, "\n".join(comment_lines))
            actions.append(
                f"Left routing comment {'SUCCESS' if ok else 'FAILED'}"
            )

        reports.append({
            "card_id": cid,
            "title": title[:120],
            "assignee": assignee or "(unassigned)",
            "issues": issues,
            "actions_taken": actions,
        })

    conn.close()

    if not reports:
        print(
            f"[gate-watchdog] All blocked cards pass gate-routing check "
            f"({len(cards)} blocked total, 0 gate-type issues)",
            flush=True,
        )
        return 0

    ts = utcnow_iso()
    print(f"[gate-watchdog] ===== Gate Routing Issues @ {ts} =====", flush=True)
    print(f"[gate-watchdog] Found {len(reports)} issue(s) out of "
          f"{len(cards)} blocked cards\n", flush=True)

    for r in reports:
        print(f"  ⚠ CARD: {r['card_id']}", flush=True)
        print(f"     Title: {r['title']}", flush=True)
        print(f"     Assignee: {r['assignee']}", flush=True)
        for i in r["issues"]:
            print(f"     └─ {i}", flush=True)
        for a in r["actions_taken"]:
            print(f"     → {a}", flush=True)
        print("", flush=True)

    print("[gate-watchdog] ===== End Gate Routing Report =====", flush=True)

    # Log to file
    log_dir = os.path.expanduser("~/.hermes/logs")
    os.makedirs(log_dir, exist_ok=True)
    with open(os.path.join(log_dir, "gate-routing-watchdog.log"), "a") as f:
        f.write(f"[{ts}] {len(reports)} issue(s) from {len(cards)} blocked\n")
        for r in reports:
            f.write(f"  {r['card_id']}: {'; '.join(r['issues'])}\n")

    return 1 if reports else 0


if __name__ == "__main__":
    sys.exit(main())
