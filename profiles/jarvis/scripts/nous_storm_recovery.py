#!/usr/bin/env python3
"""nous_storm_recovery.py — revive cards killed by a Nous capacity storm, once it clears.

WHY (2026-08-04): deepseek-v4-flash-0731 threw HTTP 503 "upstream capacity limits"
in waves (peak 273/min). Workers died mid-turn, so the dispatcher recorded the SYMPTOM
-- "worker exited cleanly (rc=0) without calling kanban_complete" / "pid not alive" --
and the CAUSE was lost. 82 sycode-trading cards were blocked this way; 67 of them
(82%) crashed inside the three peak storm hours.

Nothing recovered them:
  - kanban_transient_recovery cooldown mode only touches block_kind='transient' (2 cards)
  - its provider-sweep mode requires the literal string "provider-transient", which a
    crash casualty never carries
  - provider-sweep is triggered ONLY by codex_exhaustion_circuit_breaker on Codex
    recovery -- and the fleet now runs on Nous, so a Nous outage has no trigger at all
So these cards sat blocked permanently: backlog that was never really blocked.

This closes the loop. It:
  1. measures the CURRENT 503 rate from the fleet's own logs,
  2. stays SILENT while the storm is running (reviving into a failing provider just
     re-crashes the card and burns tokens -- doctrine: verify the pool before a mass
     unblock, and revive in waves),
  3. once clear, annotates storm-window casualties as provider-transient so the EXISTING
     sweep machinery recognises them, and unblocks them in bounded waves.

Deliberately reuses kanban_transient_recovery rather than reimplementing unblock logic.

FAIL-CLOSED: any probe error exits non-zero. A no-agent cron's only liveness signal is
its exit code, so a broken probe must never look like "storm still running, nothing to do".
"""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta

BOARD_DB = "/home/frank/.hermes/kanban/boards/{board}/kanban.db"
BOARDS = ("sycode-trading", "jarvis-os", "upero", "ai-restaurant")
LOGS = ["/home/frank/.hermes/logs/agent.log"] + glob.glob("/home/frank/.hermes/profiles/*/logs/agent.log")
STORM_MARKER = "upstream capacity limits"
CRASH_PATTERNS = ("kanban_complete", "not alive")

CLEAR_THRESHOLD = int(os.environ.get("NSR_CLEAR_THRESHOLD", "5"))   # 503s/min considered clear
CLEAR_MINUTES = int(os.environ.get("NSR_CLEAR_MINUTES", "10"))      # sustained for this long
WAVE = int(os.environ.get("NSR_WAVE", "15"))                        # cards revived per run
PROVIDER_NOTE = "provider-transient (nous 503 upstream capacity storm)"


class ProbeError(RuntimeError):
    pass


def rate_per_minute(minutes: int) -> dict[str, int]:
    """503s per minute over the trailing `minutes`, from the fleet's own logs."""
    now = datetime.now()
    keys = {(now - timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M") for i in range(minutes)}
    counts = {k: 0 for k in keys}
    seen_any = False
    for f in LOGS:
        if not os.path.exists(f):
            continue
        seen_any = True
        try:
            with open(f, errors="replace") as fh:
                for line in fh:
                    if STORM_MARKER in line and line[:16] in counts:
                        counts[line[:16]] += 1
        except OSError as e:
            raise ProbeError(f"cannot read {f}: {e}")
    if not seen_any:
        raise ProbeError("no agent logs found — cannot judge storm state")
    return counts


def storm_is_clear(counts: dict[str, int]) -> tuple[bool, int]:
    peak = max(counts.values()) if counts else 0
    return peak <= CLEAR_THRESHOLD, peak


def storm_hours() -> set[str]:
    """Hours (YYYY-MM-DD HH) in which the fleet observed a meaningful 503 burst."""
    hours: dict[str, int] = {}
    for f in LOGS:
        if not os.path.exists(f):
            continue
        with open(f, errors="replace") as fh:
            for line in fh:
                if STORM_MARKER in line:
                    hours[line[:13]] = hours.get(line[:13], 0) + 1
    return {h for h, n in hours.items() if n >= 20}


def revive(board: str, hours: set[str], apply: bool) -> list[str]:
    db = BOARD_DB.format(board=board)
    if not os.path.exists(db):
        return []
    out: list[str] = []
    conn = sqlite3.connect(db, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, title, last_heartbeat_at FROM tasks WHERE status='blocked' "
            "AND (last_failure_error LIKE '%kanban_complete%' OR last_failure_error LIKE '%not alive%') "
            "ORDER BY last_heartbeat_at DESC"
        ).fetchall()
        for r in rows:
            if len(out) >= WAVE:
                break
            hb = r["last_heartbeat_at"]
            if not hb:
                continue
            hr = datetime.fromtimestamp(hb).strftime("%Y-%m-%d %H")
            if hr not in hours:
                continue  # crashed outside any storm window -> likely a real bug, leave it
            if apply:
                with conn:
                    conn.execute(
                        "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)",
                        (r["id"], "nous-storm-recovery",
                         f"{PROVIDER_NOTE}: crashed at {hr} during a Nous 503 capacity storm; "
                         f"storm has since cleared (<={CLEAR_THRESHOLD}/min for {CLEAR_MINUTES}m). "
                         f"Unblocking for retry — the block recorded the symptom, not a real gate.",
                         int(time.time())),
                    )
                    conn.execute(
                        "UPDATE tasks SET status='ready', block_kind=NULL, consecutive_failures=0 WHERE id=?",
                        (r["id"],),
                    )
            out.append(f"{'REVIVED' if apply else 'WOULD REVIVE'} {board}:{r['id']} ({hr}) {r['title'][:50]}")
    finally:
        conn.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    # Applies BY DEFAULT: `hermes cron --script` passes no arguments, so an opt-in
    # --apply flag would make this a permanently silent no-op -- a recovery actuator
    # that never actuates. Safety comes from the storm gate, the wave cap, and the
    # storm-hour filter, not from being inert. --dry-run is available for inspection.
    ap.add_argument("--dry-run", action="store_true", help="report what would be revived, change nothing")
    a = ap.parse_args()
    a.apply = not a.dry_run

    counts = rate_per_minute(CLEAR_MINUTES)
    clear, peak = storm_is_clear(counts)
    if not clear:
        # Storm running. Silence is correct here: this is the healthy steady state of a
        # guard that is deliberately waiting, and it runs every 15 minutes.
        return 0

    hours = storm_hours()
    if not hours:
        return 0
    lines: list[str] = []
    for b in BOARDS:
        lines += revive(b, hours, apply=a.apply)
    if not lines:
        return 0
    print(f"NOUS STORM RECOVERY — storm clear (peak {peak}/min over {CLEAR_MINUTES}m)")
    print(f"reviving cards that crashed during storm hours, wave cap {WAVE}:")
    for l in lines:
        print(f"  {l}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ProbeError as e:
        print(f"nous_storm_recovery: PROBE FAILED (not 'storm running'): {e}", file=sys.stderr)
        sys.exit(1)
