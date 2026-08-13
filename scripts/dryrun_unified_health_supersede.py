#!/usr/bin/env python3
"""Deterministic end-to-end dry-run for t_047d91e7 (unified-health false BLOCK).

Drives the REAL dgx_unified_health_probe.main() against a temp boards layout
that mirrors the live t_0e1a9416 evidence (older gave_up within the lookback
plus a NEWER running run with a recent heartbeat and an alive pid), with every
other probe check stubbed to PASS so the kanban crash signal is the ONLY thing
that can move the verdict.

Assertions:
  * superseded crash => verdict is WARN (observability), never BLOCK.
  * a no-newer-live-run crash => verdict stays BLOCK (true crash preserved).
Everything runs on temp dirs; the live kanban DB is never touched.
"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path("/home/frank/.hermes/scripts")
MOD = Path("/home/frank/.hermes/scripts/dgx_unified_health_probe.py")
if len(sys.argv) > 1 and sys.argv[1] == "live":
    MOD = Path("/home/frank/.hermes/profiles/jarvis/scripts/dgx_unified_health_probe.py")

spec = importlib.util.spec_from_file_location("uhealth", MOD)
assert spec is not None and spec.loader is not None
uhealth: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uhealth)


def make_board(board_dir: Path, task_id: str, task_status: str, runs: list,
               now: int) -> None:
    board_dir.mkdir(parents=True, exist_ok=True)
    db = board_dir / "kanban.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)")
    con.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")
    con.execute(
        "CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, "
        "status TEXT, outcome TEXT, worker_pid INTEGER, last_heartbeat_at "
        "INTEGER, started_at INTEGER, ended_at INTEGER)"
    )
    con.execute("INSERT INTO tasks (id, status) VALUES (?, ?)", (task_id, task_status))
    for i, (outcome, st, en, status, pid, hb) in enumerate(runs):
        con.execute(
            "INSERT INTO task_runs (id, task_id, status, outcome, worker_pid, "
            "last_heartbeat_at, started_at, ended_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (i + 1, task_id, status, outcome, pid,
             None if hb is None else int(hb),
             int(st), None if en is None else int(en)),
        )
    con.commit()
    con.close()


def stub_pass(boards_dir: Path, tmp: Path, now: datetime) -> None:
    """Stub every probe check except check_kanban_crashes to PASS, and point
    all live paths at temp/absent dirs so nothing reads the real fleet."""
    uhealth.BOARDS_DIR = boards_dir
    uhealth.JARVIS_OS_KANBAN_DB = tmp / "jarvis-os" / "kanban.db"
    uhealth.SYCODE_TRADING_KANBAN_DB = tmp / "sycode-trading" / "kanban.db"
    uhealth.CRON_OUTPUT = tmp / "cron"
    uhealth.UNIFIED_LOG = uhealth.CRON_OUTPUT / "unified_health_canary.jsonl"
    uhealth.CRON_FORCED_RELEASES_LOG = tmp / "inflight_forced_releases.jsonl"
    uhealth.CRITICAL_ALERT_STATE = tmp / "unified_health_block_history.jsonl"
    uhealth.check_hermes_cli = lambda: (True, "ok", False)
    uhealth.check_gateway_unit = lambda: (True, "ok", False)
    uhealth.check_gateway_runtime = lambda: (True, True, "ok")
    uhealth.check_cron_ticker = lambda: (True, "ok", False)
    uhealth.check_canary_freshness = lambda: (True, "ok")
    uhealth.check_docker = lambda: (True, "ok", False)
    uhealth.check_disk = lambda: (True, "ok", False)
    uhealth.check_mechanism_matrix = lambda: {
        "available": True, "overall": "GREEN", "dead": 0,
        "detail": "ok", "fork_resource_pressure": False,
    }
    uhealth.run = lambda argv, timeout=25: {
        "rc": 0, "out": "ok", "err": "", "timeout": False,
        "fork_resource_pressure": False,
    }
    uhealth.utc_now = lambda: now


def run_verdict(board_tasks: list[tuple], now: datetime) -> str:
    tmp = Path(tempfile.mkdtemp())
    boards = tmp / "boards"
    epoch = int(now.timestamp())
    for (board, task_id, status, runs) in board_tasks:
        make_board(boards / board, task_id, status, runs, epoch)
    stub_pass(boards, tmp, now)
    rc = uhealth.main()
    assert rc == 0
    rec = json.loads(uhealth.UNIFIED_LOG.read_text().splitlines()[-1])
    return rec["verdict"]


def main() -> int:
    now = datetime(2026, 8, 13, 9, 0, tzinfo=timezone.utc)
    epoch = int(now.timestamp())
    alive = os.getpid()

    # CASE 1 (regression): the exact t_0e1a9416 shape — old gave_up within the
    # lookback, NEWER run running + recent heartbeat + alive pid.
    superseded = [
        ("sycode-trading", "t_0e1a9416", "running", [
            ("gave_up", epoch - 45 * 60, epoch - 30 * 60, "crashed", None, epoch - 31 * 60),
            (None, epoch - 20 * 60, None, "running", alive, epoch - 10),
        ]),
    ]
    verdict1 = run_verdict(superseded, now)
    print(f"CASE 1 superseded (newer live run): verdict={verdict1}")
    assert verdict1 in ("WARN", "PASS"), f"must not BLOCK, got {verdict1}"
    assert verdict1 != "BLOCK"

    # CASE 2 (preserve): crash run with NO newer run => genuine BLOCK.
    no_newer = [
        ("jarvis-os", "t_no_newer", "running", [
            ("gave_up", epoch - 45 * 60, epoch - 30 * 60, "crashed", None, epoch - 31 * 60),
        ]),
    ]
    verdict2 = run_verdict(no_newer, now)
    print(f"CASE 2 no newer run:            verdict={verdict2}")
    assert verdict2 == "BLOCK"

    # CASE 3 (preserve): newer run but DEAD pid => genuine BLOCK.
    dead_pid = [
        ("sycode-trading", "t_dead_pid", "running", [
            ("gave_up", epoch - 45 * 60, epoch - 30 * 60, "crashed", None, epoch - 31 * 60),
            (None, epoch - 20 * 60, None, "running", 99999999, epoch - 10),
        ]),
    ]
    verdict3 = run_verdict(dead_pid, now)
    print(f"CASE 3 dead-pid newer run:     verdict={verdict3}")
    assert verdict3 == "BLOCK"

    print("\nALL DRY-RUN ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
