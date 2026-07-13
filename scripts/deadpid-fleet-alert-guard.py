#!/usr/bin/env python3
"""deadpid-fleet-alert-guard.py — durable fleet alert for silent dead-PID accumulation.

WHY THIS EXISTS (t_1290f179 / CEO follow-up to t_ccaa946a)
  The dispatcher's ``detect_crashed_workers`` is supposed to fire a
  ``kanban_failure_alert`` lifecycle hook at ``consecutive_failures >= 3`` on a
  dead-PID fingerprint (``_maybe_fire_deadpid_fleet_alert`` in kanban_db.py).
  That hook is served by the ``deadpid-fleet-alert`` plugin, which forwards ONE
  deduped summary to ``#fleet-reports``. That plugin is a standalone plugin and
  was never enabled in ``plugins.enabled`` for any dispatcher, so the hook had
  ZERO listeners and the alert silently never fired — a dead-PID storm could
  accumulate to cf=4..10 with no warning. That is why acceptance #3 of
  t_ccaa946a was never satisfied (t_ee20a992 only stopped mislabeling; it did
  not alert).

  Fix has two layers:
    1. (Config) enabled ``deadpid-fleet-alert`` in every dispatcher's
       ``plugins.enabled`` (the gateway will arm it on next restart).
    2. (This script) an independent, restart-free guard that runs in a FRESH
       process every tick and scans the board DBs for the exact stuck
       condition, delivering a single deduped fleet alert. This closes the
       delivery-latency gap (a running gateway caches plugin discovery at
       startup and does not re-arm until restart, which is Frank-gated).

WHAT IT ALERTS ON (the silent-accumulation signature)
  Any task on any board where:
    status IN ('blocked','gave_up')   # genuinely-stuck, active accumulation
       (resolved 'done'/'archived' and pre-active 'scheduled'/'triage' are
        excluded as noise even if they carry a stale dead-PID error string)
    AND last_failure_error LIKE 'pid % not alive'
    AND consecutive_failures >= 3
  i.e. a dead-PID worker-kill that has built up retries WITHOUT the operator
  being notified.

DELIVERY: self-delivers via ``hermes send`` to #fleet-reports. The cron-layer
``deliver: discord`` path is currently broken fleet-wide ("platform 'discord'
not configured/enabled"), so this script does NOT rely on the scheduler to
forward stdout — it sends directly. ``hermes send`` to #fleet-reports was
verified working (exit 0).

DEDUP: only re-alerts when the SET of offending task ids changes (added/removed)
or every ALERT_MIN_INTERVAL_SECONDS. A stable storm = one message, not spam.

OUTPUT: prints the alert to stdout too (harmless; the cron deliver=local
captures it). Silent when green.

This script NEVER mutates any board. Read-only by contract.
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import subprocess
import sys
import time

KANBAN_HOME = os.environ.get("HERMES_KANBAN_HOME", "/home/frank/.hermes/kanban")
DEADPID_RE_SQL = "last_failure_error LIKE 'pid % not alive'"
ALERT_CF_THRESHOLD = int(os.environ.get("DEADPID_ALERT_CF", "3"))
STATE_PATH = os.environ.get(
    "DEADPID_ALERT_STATE",
    "/home/frank/.hermes/cron/state/deadpid-fleet-alert-guard.seen",
)
ALERT_MIN_INTERVAL_SECONDS = int(
    os.environ.get("DEADPID_ALERT_MIN_INTERVAL", "3600")
)
HERMES_BIN = os.environ.get("HERMES_BIN", "/home/frank/.local/bin/hermes")
TARGET = os.environ.get("DEADPID_FLEET_ALERT_TARGET", "discord:#fleet-reports")
HERMES_PROFILE = os.environ.get("DEADPID_FLEET_ALERT_PROFILE", "jarvis")


def _board_db_paths() -> list[str]:
    out: list[str] = []
    for db in glob.glob(os.path.join(KANBAN_HOME, "boards", "*", "kanban.db")):
        out.append(db)
    for extra in glob.glob(os.path.join(KANBAN_HOME, "*.db")):
        base = os.path.basename(extra)
        if base in ("db", "db.db", "store.duckdb", "dispatch.db", "default.db"):
            continue
        out.append(extra)
    return sorted(out)


def _scan() -> list[dict]:
    hits: list[dict] = []
    for db in _board_db_paths():
        slug = os.path.basename(os.path.dirname(db))
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            cur = con.cursor()
            for r in cur.execute(
                "SELECT id, status, block_kind, consecutive_failures, "
                "last_failure_error, assignee FROM tasks"
            ):
                err = r["last_failure_error"] or ""
                status = r["status"] or ""
                bk = r["block_kind"] or ""
                if "pid " not in err or "not alive" not in err:
                    continue
                # Only genuinely-stuck states count as active accumulation:
                # 'blocked'/'gave_up'. Resolved (done/archived) and pre-active
                # (scheduled/triage) states are noise, even if they carry a
                # stale dead-PID error string + recovery block_kind.
                if status not in ("blocked", "gave_up"):
                    continue
                cf = int(r["consecutive_failures"] or 0)
                if cf < ALERT_CF_THRESHOLD:
                    continue
                hits.append(
                    {
                        "board": slug,
                        "id": r["id"],
                        "status": status,
                        "block_kind": bk,
                        "cf": cf,
                        "assignee": r["assignee"],
                        "err": err[:160],
                    }
                )
            con.close()
        except Exception as ex:  # pragma: no cover
            print(f"  ! {db}: {ex}", file=sys.stderr)
    return hits


def _load_seen() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_seen(data: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, STATE_PATH)


def _collect_alert_lines(hits: list[dict], by_board: dict) -> list[str]:
    lines = [
        "DEAD-PID FLEET ALERT (silent-accumulation guard)",
        f"{len(hits)} task(s) stuck on 'pid N not alive' with "
        f"consecutive_failures >= {ALERT_CF_THRESHOLD}",
        "A dead-PID worker-kill storm is accumulating WITHOUT a prior alert.",
        "Root cause: see t_ccaa946a RCA. This guard is t_1290f179.",
        "---",
    ]
    for h in hits:
        lines.append(
            f"  [{h['board']}] {h['id']} status={h['status']} "
            f"cf={h['cf']} block={h['block_kind'] or '-'} "
            f"assignee={h['assignee'] or '?'} :: {h['err']}"
        )
    lines.append("---")
    lines.append("By board: " + ", ".join(f"{b}={c}" for b, c in sorted(by_board.items())))
    return lines


def _deliver(body: str) -> None:
    """Deliver the alert to #fleet-reports via `hermes send` (verified path).

    Failure to deliver is logged loudly to stderr but does NOT crash the
    watchdog (a delivery outage must not also suppress the local signal).
    """
    import shutil

    if not shutil.which(HERMES_BIN) and not os.path.exists(HERMES_BIN):
        print("deadpid-fleet-alert-guard: HERMES_BIN not found, cannot deliver", file=sys.stderr)
        return
    try:
        proc = subprocess.run(
            [HERMES_BIN, "-p", HERMES_PROFILE, "send", "-q", "-t",
             TARGET, "-s", "[deadpid-fleet-alert-guard]", body],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "")[:300]
            print(
                f"deadpid-fleet-alert-guard: hermes send FAILED rc={proc.returncode}: {detail}",
                file=sys.stderr,
            )
    except Exception as ex:  # pragma: no cover
        print(f"deadpid-fleet-alert-guard: hermes send exception: {ex}", file=sys.stderr)


def main() -> int:
    hits = _scan()
    if not hits:
        # Green: do not touch state so a future storm always alerts.
        return 0

    seen = _load_seen()
    now = time.time()

    current_ids = sorted(f"{h['board']}/{h['id']}" for h in hits)
    last_ids = seen.get("ids", [])
    last_alert_at = float(seen.get("last_alert_at", 0.0))

    # Re-alert only if the offending set changed OR the min interval elapsed.
    changed = set(current_ids) != set(last_ids)
    interval_ok = (now - last_alert_at) >= ALERT_MIN_INTERVAL_SECONDS
    if not (changed or interval_ok):
        # Still in the quiet window with a stable set — stay silent.
        _save_seen({"ids": current_ids, "last_alert_at": last_alert_at})
        return 0

    by_board: dict[str, int] = {}
    for h in hits:
        by_board[h["board"]] = by_board.get(h["board"], 0) + 1

    body_lines = _collect_alert_lines(hits, by_board)
    for line in body_lines:
        print(line)

    _save_seen({"ids": current_ids, "last_alert_at": now})

    # Self-deliver (cron-layer discord delivery is broken fleet-wide).
    _deliver("\n".join(body_lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
