#!/usr/bin/env python3
"""Expire consecutive_failures accrued during fleet-wide health outages.

Dispatcher exit-contract proposal item 2 (kanban jarvis-os/t_411e72b4):

  consecutive_failures accrued inside a window with a fleet-wide
  provider/credit outage (per unified-health-probe verdict history) ages
  out when health returns PASS, instead of permanently killing the card.

Deterministic, read-mostly actuator run as a no-agent LOOP. It:

  1. Reads the unified health canary JSONL
     (profiles/jarvis/cron/output/unified_health_canary.jsonl) and builds
     outage windows = maximal spans of BLOCK verdicts (adjacent BLOCK
     records <= OUTAGE_MERGE_MIN apart are one window).
  2. GATES on fleet recovery: it only ACTS when the latest verdict is
     PASS or WARN (fleet healthy again). While the latest verdict is
     BLOCK (outage ongoing) or the history is missing/stale, it does
     nothing (fail-open, exit 0, wakeAgent false).
  3. Scans every board DB for tasks whose trailing failure runs
     (crashed/gave_up/timed_out/spawn_failed/protocol_violation) ENDED
     inside an outage window. Those failures are outage-accrued.
  4. DECAYS consecutive_failures by the outage-accrued count, keeping
     failures from healthy periods untouched (healthy_failures persist).
  5. RE-QUEUES a blocked card (status -> ready/todo) ONLY when ALL hold:
       - the card is blocked (breaker-shaped: block_kind in {None,
         'capability'}, NOT needs_input/dependency/transient),
       - NOT sticky-blocked (no worker/operator kanban_block is the most
         recent blocked/unblocked event),
       - the remaining healthy-period failures < effective failure limit,
     via the sanctioned kanban_db.unblock_task API (emits 'unblocked',
     re-gates on parents, resets counter/error) so the card becomes
     re-dispatchable WITHOUT a governor hand-resurrection.
  6. NEVER deletes card history: task_runs/task_events/comments rows are
     preserved; a comment documents every expiry/requeue.

Default mode is DRY-RUN (prints the plan, mutates nothing). Pass --apply
to execute. Exit codes: 0 = clean (acted or nothing to do); 2 = usage /
board error. The final line is a wakeAgent JSON contract for cron.

This mirrors the nervous-system-engineer loop pattern
(crashstorm-watch / budget-exhausted actuator): deterministic, scoped
narrowly, fail-open on ambiguity.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

HERMES_AGENT = "/home/frank/.hermes/hermes-agent"
sys.path.insert(0, HERMES_AGENT)

from hermes_cli import kanban_db as kb  # noqa: E402

DEFAULT_HEALTH_LOG = Path(
    "/home/frank/.hermes/profiles/jarvis/cron/output/unified_health_canary.jsonl"
)
DEFAULT_BOARD_DIR = Path("/home/frank/.hermes/kanban/boards")
DEFAULT_BOARDS = ("jarvis-os", "sycode-trading", "sycode-ai", "upero")

# Adjacent BLOCK records closer than this (seconds) are one outage window.
OUTAGE_MERGE_MIN = 30 * 60
# Latest health record older than this => history stale => fail open.
STALE_HEALTH_MIN = 40 * 60
# Failure outcomes that feed the dispatcher's consecutive_failures counter.
FAILURE_OUTCOMES = (
    "crashed",
    "gave_up",
    "timed_out",
    "spawn_failed",
    "protocol_violation",
)
# Breaker-shaped block kinds that are safe to auto-requeue.
REQUEUEABLE_KINDS = (None, "capability")
# Append-only idempotency state: which failure run_ids have already been
# expired. Key: "board/task_id" -> list[int]. Prevents double-decay of the
# same outage failure across ticks while the counter is still > 0.
STATE_FILE = Path("/home/frank/.hermes/cron/state/expire-outage-failures.expired.json")


def parse_ts(value: str) -> dt.datetime | None:
    try:
        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def load_health_history(path: Path) -> list[dict]:
    """Load JSONL health canary records. Returns [] on any error (fail-open)."""
    records: list[dict] = []
    try:
        if not path.is_file():
            return []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            ts = parse_ts(str(rec.get("ts", "")))
            if ts is None:
                continue
            rec["_ts"] = ts
            records.append(rec)
    except Exception:
        return []
    records.sort(key=lambda r: r["_ts"])
    return records


def outage_windows(records: list[dict]) -> list[tuple[dt.datetime, dt.datetime]]:
    """Maximal BLOCK spans from verdict history.

    Adjacent BLOCK records whose gap <= OUTAGE_MERGE_MIN are merged into a
    single window (a lone PASS blip inside a provider storm should not split
    the outage). Returns [] when no BLOCK verdicts exist.
    """
    windows: list[tuple[dt.datetime, dt.datetime]] = []
    cur_start: dt.datetime | None = None
    cur_end: dt.datetime | None = None
    for rec in records:
        if rec.get("verdict") == "BLOCK":
            ts = rec["_ts"]
            if cur_start is None:
                cur_start = ts
                cur_end = ts
            elif (ts - cur_end).total_seconds() <= OUTAGE_MERGE_MIN:
                cur_end = ts
            else:
                if cur_start is not None and cur_end is not None:
                    windows.append((cur_start, cur_end))
                cur_start = ts
                cur_end = ts
        else:
            if cur_start is not None and cur_end is not None:
                windows.append((cur_start, cur_end))
            cur_start = None
            cur_end = None
    if cur_start is not None and cur_end is not None:
        windows.append((cur_start, cur_end))
    return windows


def health_recovered(records: list[dict], now: dt.datetime) -> bool:
    """Gate: fleet is healthy again (latest verdict PASS/WARN and fresh).

    Fail-open: missing/stale/ambiguous history => NOT recovered (no action).
    """
    if not records:
        return False
    latest = records[-1]
    if latest.get("verdict") not in ("PASS", "WARN"):
        return False
    age = (now - latest["_ts"]).total_seconds()
    return age <= STALE_HEALTH_MIN


def in_window(ts: dt.datetime, windows: list[tuple[dt.datetime, dt.datetime]]) -> bool:
    for start, end in windows:
        if start <= ts <= end:
            return True
    return False


def effective_failure_limit(conn: sqlite3.Connection, task_id: str,
                            fallback: int) -> int:
    """Mirror _record_task_failure's limit resolution (max_retries wins)."""
    row = conn.execute(
        "SELECT max_retries FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    if row and row["max_retries"] is not None:
        try:
            return int(row["max_retries"])
        except (TypeError, ValueError):
            pass
    return fallback


def failure_streak(conn: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
    """All failure runs for the task, most recent first.

    The dispatcher increments ``consecutive_failures`` on each failure run and
    resets it only on successful completion. The stable, deterministic view of
    the counter is therefore the full failure history, NOT ``LIMIT cf``: once
    a failure has been expired we must never re-derive a different trailing
    subset on a later tick (that caused healthy-period failures to be wrongly
    expired after the counter dropped). Idempotency comes from the expired
    run_id state, not from the counter value.
    """
    ph = ",".join("?" * len(FAILURE_OUTCOMES))
    runs = conn.execute(
        f"SELECT id, outcome, started_at, ended_at FROM task_runs "
        f"WHERE task_id = ? AND outcome IN ({ph}) "
        f"ORDER BY COALESCE(ended_at, started_at) DESC",
        (task_id, *FAILURE_OUTCOMES),
    ).fetchall()
    return runs


class ExpiryState:
    """Append-only idempotency state: run_ids already expired per task.

    Key: "board/task_id" -> list[int] (run ids). A run_id expired once is
    never counted again, so the same outage failure cannot be double-decayed
    across ticks while the counter is still > 0. Never removes history.
    """

    def __init__(self, path: Path = STATE_FILE):
        self.path = path
        self.data: dict[str, list[int]] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.is_file():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.data = {
                        str(k): [int(v) for v in (val if isinstance(val, list) else [])]
                        for k, val in raw.items()
                    }
        except Exception:
            self.data = {}

    def expired_ids(self, key: str) -> set[int]:
        return set(self.data.get(key, []))

    def record(self, key: str, run_ids: list[int]) -> None:
        if not run_ids:
            return
        cur = self.data.setdefault(key, [])
        for rid in run_ids:
            if rid not in cur:
                cur.append(int(rid))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            pass  # state loss is fail-open: worst case a re-expiry next tick


def is_sticky_blocked(conn: sqlite3.Connection, task_id: str) -> bool:
    row = conn.execute(
        "SELECT kind FROM task_events "
        "WHERE task_id = ? AND kind IN ('blocked', 'unblocked') "
        "ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return bool(row) and row["kind"] == "blocked"


def classify_task(conn: sqlite3.Connection, task_id: str,
                  windows: list[tuple[dt.datetime, dt.datetime]],
                  failure_limit: int, state: ExpiryState | None = None,
                  board: str = "") -> dict | None:
    """Classify one task for outage expiry. None = no NEW outage failures.

    Uses the full failure streak (deterministic, counter-independent) minus
    run_ids already recorded as expired in ``state``, so a healthy-period
    failure is never wrongly expired after the counter drops.
    """
    row = conn.execute(
        "SELECT id, title, status, block_kind, consecutive_failures, "
        "max_retries, last_failure_error FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if row is None:
        return None
    cf = int(row["consecutive_failures"] or 0)
    if cf <= 0:
        return None
    runs = failure_streak(conn, task_id)
    key = f"{board}/{task_id}" if board else task_id
    already_expired = state.expired_ids(key) if state else set()
    outage_accrued = 0
    expired_run_ids: list[int] = []
    for r in runs:
        rid = int(r["id"])
        if rid in already_expired:
            continue
        ended = r["ended_at"]
        if ended is None:
            continue
        try:
            ended_dt = dt.datetime.fromtimestamp(int(ended), tz=dt.timezone.utc)
        except (TypeError, ValueError, OSError):
            continue
        if in_window(ended_dt, windows):
            outage_accrued += 1
            expired_run_ids.append(rid)
    if outage_accrued <= 0:
        return None
    healthy_failures = max(0, cf - outage_accrued)
    limit = effective_failure_limit(conn, task_id, failure_limit)
    status = row["status"]
    block_kind = row["block_kind"]
    requeue = False
    requeue_reason = ""
    if status == "blocked":
        sticky = is_sticky_blocked(conn, task_id)
        breaker_shaped = block_kind in REQUEUEABLE_KINDS
        if healthy_failures < limit and breaker_shaped and not sticky:
            requeue = True
            requeue_reason = (
                f"healthy_failures={healthy_failures}<limit={limit} "
                f"kind={block_kind!r} sticky=False"
            )
        elif not breaker_shaped:
            requeue_reason = f"block_kind={block_kind!r} not breaker-shaped (human-owned)"
        elif sticky:
            requeue_reason = "sticky blocked (worker/operator kanban_block)"
        else:
            requeue_reason = f"healthy_failures={healthy_failures}>=limit={limit}"
    return {
        "id": task_id,
        "title": (row["title"] or "")[:70],
        "status": status,
        "block_kind": block_kind,
        "consecutive_failures": cf,
        "outage_accrued": outage_accrued,
        "healthy_failures": healthy_failures,
        "limit": limit,
        "failure_runs_seen": len(runs),
        "expired_run_ids": expired_run_ids,
        "requeue": requeue,
        "requeue_reason": requeue_reason,
    }


def apply_expiry(conn: sqlite3.Connection, rec: dict,
                 state: ExpiryState | None = None, board: str = "") -> None:
    """Decay outage-accrued failures; optionally requeue via sanctioned API.

    Preserves card history: no rows are deleted. A comment records the change.
    ``unblock_task`` opens its own write transaction, so it must be called
    OUTSIDE our own ``write_txn`` (nested BEGIN IMMEDIATE would raise).
    """
    now = int(time.time())
    if rec["requeue"]:
        ok = kb.unblock_task(conn, rec["id"])
        if not ok:
            raise RuntimeError(f"unblock_task returned False for {rec['id']}")
    with kb.write_txn(conn):
        # unblock_task resets counter to 0 + clears error. Re-apply the
        # healthy-period failures so they persist (acceptance criterion).
        conn.execute(
            "UPDATE tasks SET consecutive_failures = ? WHERE id = ?",
            (rec["healthy_failures"], rec["id"]),
        )
        action = "REQUEUED" if rec["requeue"] else "DECAYED"
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                rec["id"],
                "outage-expiry",
                f"[outage-expiry t_411e72b4] {action}: expired "
                f"{rec['outage_accrued']} outage-window failure(s) after fleet "
                f"health PASS; consecutive_failures {rec['consecutive_failures']} -> "
                f"{rec['healthy_failures']} (healthy-period failures kept). "
                f"{('Re-queued once; re-dispatchable without governor. ' if rec['requeue'] else '')}"
                f"Reason: {rec['requeue_reason'] or 'decay only'}",
                now,
            ),
        )
    if state is not None:
        key = f"{board}/{rec['id']}" if board else rec["id"]
        state.record(key, rec["expired_run_ids"])


def scan_board(db_path: Path, windows: list[tuple[dt.datetime, dt.datetime]],
               failure_limit: int, apply: bool,
               state: ExpiryState | None = None) -> dict:
    """Scan one board DB. Returns {expired: [...], skipped: [...], error}."""
    result: dict = {"expired": [], "skipped": [], "error": None}
    board = db_path.parent.name
    if not db_path.is_file():
        result["error"] = "db_missing"
        return result
    try:
        conn = kb.connect(db_path=db_path)
        conn.isolation_level = None
    except Exception as exc:
        result["error"] = f"connect_failed:{type(exc).__name__}:{exc}"
        return result
    try:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE consecutive_failures > 0 "
            "AND status IN ('blocked', 'ready', 'todo')"
        ).fetchall()
        for r in rows:
            rec = classify_task(conn, r["id"], windows, failure_limit,
                                state=state, board=board)
            if rec is None:
                continue
            if apply:
                try:
                    apply_expiry(conn, rec, state=state, board=board)
                    rec["applied"] = True
                except Exception as exc:
                    rec["applied"] = False
                    rec["error"] = f"{type(exc).__name__}:{exc}"
            else:
                rec["applied"] = False
            result["expired"].append(rec)
    except Exception as exc:
        result["error"] = f"scan_failed:{type(exc).__name__}:{exc}"
    finally:
        conn.close()
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--health-log", type=Path, default=DEFAULT_HEALTH_LOG)
    ap.add_argument("--board-dir", type=Path, default=DEFAULT_BOARD_DIR)
    ap.add_argument("--board", action="append", default=None,
                    help="restrict to a board slug (repeatable); default all")
    ap.add_argument("--apply", action="store_true",
                    help="execute expiry/requeue (default: dry-run)")
    ap.add_argument("--failure-limit", type=int, default=None,
                    help="fallback circuit-breaker limit (default: read root "
                         "config kanban.failure_limit else 2)")
    ap.add_argument("--now", default=None,
                    help="ISO timestamp for the 'current' moment (tests)")
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    if args.now:
        parsed = parse_ts(args.now)
        if parsed is None:
            print("usage: --now must be an ISO timestamp", file=sys.stderr)
            return 2
        now = parsed

    # Resolve fallback failure limit (mirror dispatcher config resolution).
    if args.failure_limit is not None:
        failure_limit = args.failure_limit
    else:
        failure_limit = 2
        try:
            import yaml  # type: ignore
            cfg = yaml.safe_load(
                Path("/home/frank/.hermes/config.yaml").read_text(encoding="utf-8")
            ) or {}
            kc = cfg.get("kanban", {}) or {}
            if isinstance(kc.get("failure_limit"), int):
                failure_limit = int(kc["failure_limit"])
        except Exception:
            pass

    records = load_health_history(args.health_log)
    windows = outage_windows(records)
    recovered = health_recovered(records, now)
    mode = "APPLY" if args.apply else "DRY-RUN"

    print(f"── outage-failure expiry [{mode}] ──")
    print(f"  health_log={args.health_log} records={len(records)} "
          f"outage_windows={len(windows)} fleet_recovered={recovered}")
    for i, (a, b) in enumerate(windows, 1):
        print(f"    window {i}: {a.isoformat()} -> {b.isoformat()}")

    if not windows or not recovered:
        # Fail-open: nothing to act on (no outage, or fleet still unhealthy,
        # or history missing/stale). Never expire during an active outage.
        reason = ("no_outage_windows" if not windows else
                  "fleet_not_recovered_or_history_stale")
        print(f"  no action ({reason}); nothing to expire.")
        # Silent gate: nothing to report -> do not deliver / wake.
        print("{\"wakeAgent\": false}")
        return 0

    boards = args.board or DEFAULT_BOARDS
    state = ExpiryState() if args.apply else None
    total_expired = 0
    total_requeued = 0
    any_error = False
    for board in boards:
        db = args.board_dir / board / "kanban.db"
        res = scan_board(db, windows, failure_limit, args.apply, state=state)
        label = f"{board}: {len(res['expired'])} candidate(s)"
        if res["error"]:
            label += f" [{res['error']}]"
            any_error = True
        print(f"  {label}")
        for rec in res["expired"]:
            applied = "APPLIED" if rec["applied"] else "DRY-RUN"
            print(f"    [{applied}] {rec['id']} cf={rec['consecutive_failures']} "
                  f"outage={rec['outage_accrued']} -> healthy={rec['healthy_failures']} "
                  f"status={rec['status']} requeue={rec['requeue']} "
                  f"({rec['requeue_reason'] or 'decay only'}) :: {rec['title']}")
            total_expired += 1
            if rec["requeue"]:
                total_requeued += 1

    if not args.apply:
        print("\n  DRY-RUN. Re-run with --apply to execute the expiry/requeue.")
    else:
        print(f"\n  APPLIED: expired {total_expired} card(s), "
              f"requeued {total_requeued} card(s).")
    # wakeAgent contract (no-agent cron gate):
    #   - error during scan   -> wakeAgent true  (mechanism degraded; alert)
    #   - applied expiries    -> wakeAgent true  (deliver the change report)
    #   - clean / nothing     -> wakeAgent false (silent tick)
    acted = args.apply and total_expired > 0
    print("{\"wakeAgent\": true}" if (any_error or acted) else "{\"wakeAgent\": false}")
    return 1 if any_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
