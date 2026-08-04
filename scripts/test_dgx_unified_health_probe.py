#!/usr/bin/env python3
"""Regression tests for dgx_unified_health_probe.py.

Covers:
  * Repeat-BLOCK hard-alert escalation (t_7a97ba51 #3 / t_cafc1119 C3):
    - critical_alert_due() returns False below the threshold and True at/above
      CRITICAL_ALERT_MIN_COUNT (>=3) within CRITICAL_ALERT_WINDOW_H.
    - events older than the window do NOT count (rolling-window correctness).
    - record_block_event() is append-only and idempotent per call.
  * check_kanban_crashes() precision:
    - crashed/gave_up run on a still-active (running) task counts as ACTIVE.
    - same run on a resolved (done) task counts as STALE (not active).
    - de-dupes multiple runs of one task to a single active hit.
  * Ready-backlog observability (t_7c589e9c jarvis, t_bf11a0ce per-board):
    - per-board scan reports counts + oldest ready task id + age.
    - oldest_ready > READY_BACKLOG_WARN_DAYS (7d) flags warn=True but NEVER
      drives a BLOCK verdict (degrades PASS -> WARN only).
  * Cron forced-release observability (t_615aa245):
    - reader counts releases inside CRON_FORCED_RELEASE_WINDOW_H and rolls
      older releases off the window.
    - absent file fails open (available False, count 0, never a BLOCK cause).
    - corrupt/no-`at` rows are skipped without failing the probe.
    - one recent release degrades PASS -> WARN; >= CRON_FORCED_RELEASE_BLOCK_MIN
      (3) recent releases drive BLOCK with a countable probe field.

No live board is touched; everything uses temp dirs. The canary module is
imported with its path constant monkeypatched to the temp layout.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent
MODULE_PATH = REPO / "dgx_unified_health_probe.py"

spec = importlib.util.spec_from_file_location("uhealth", MODULE_PATH)
assert spec is not None and spec.loader is not None
uhealth: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uhealth)


def _snapshot_uhealth() -> dict[str, Any]:
    """Copy the module attribute table so tests can restore it verbatim."""
    return dict(uhealth.__dict__)


def _restore_uhealth(snapshot: dict[str, Any]) -> None:
    """Restore the module attribute table, undoing any monkeypatches a test
    made (check-function stubs, path constants, utc_now/run overrides)."""
    uhealth.__dict__.clear()
    uhealth.__dict__.update(snapshot)


@pytest.fixture(autouse=True)
def _hermetic_uhealth():
    """Each test starts from a pristine module (import-time state) and leaves
    the module pristine for the next test.

    Several tests monkeypatch uhealth.check_* / uhealth.utc_now /
    uhealth.run in place (e.g. _stub_checks_pass) and never restore them.
    Without this fixture, those stubs leak into later tests that need the
    REAL function (e.g. check_kanban_crashes), producing order-dependent
    failures (count=0 when a stub returns (0, [], 0)).
    """
    snapshot = _snapshot_uhealth()
    yield
    _restore_uhealth(snapshot)


def _now_iso(offset_min: float = 0.0) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=offset_min)


def test_repeat_escalation_below_threshold():
    tmp = Path(tempfile.mkdtemp())
    state = tmp / "blocks.jsonl"
    uhealth.CRITICAL_ALERT_STATE = state
    # Seed 2 events within window -> below threshold (>=3)
    with state.open("w", encoding="utf-8") as fh:
        for i in range(2):
            fh.write(json.dumps({"ts": _now_iso(-i).isoformat()}) + "\n")
    # A fresh BLOCK recorded now = 3rd event total -> still need >=3 counts
    # AFTER recording. critical_alert_due records the current event then counts.
    due = uhealth.critical_alert_due(_now_iso())
    # 2 seeded + 1 current = 3 >= 3 => due True. Use 1 seed to test below.
    assert due is True, "3 events in window should be due"

    # Now prove strictly-below: clear and seed 1
    state.write_text(json.dumps({"ts": _now_iso(0).isoformat()}) + "\n")
    # critical_alert_due will add a 2nd => total 2 < 3 => not due
    due2 = uhealth.critical_alert_due(_now_iso())
    assert due2 is False, "2 events in window should NOT be due"


def test_window_rolloff_excludes_old_events():
    tmp = Path(tempfile.mkdtemp())
    state = tmp / "blocks.jsonl"
    uhealth.CRITICAL_ALERT_STATE = state
    # 5 events, all older than the window (window default 24h, push 25h back)
    old = _now_iso(-(uhealth.CRITICAL_ALERT_WINDOW_H * 60 + 30))
    with state.open("w", encoding="utf-8") as fh:
        for _ in range(5):
            fh.write(json.dumps({"ts": old.isoformat()}) + "\n")
    # current event + 5 ancient = only 1 in window -> not due
    due = uhealth.critical_alert_due(_now_iso())
    assert due is False, "ancient events must roll off the window"


def test_record_block_event_append_only_and_count():
    tmp = Path(tempfile.mkdtemp())
    state = tmp / "blocks.jsonl"
    uhealth.CRITICAL_ALERT_STATE = state
    assert not state.exists()
    n1 = uhealth.record_block_event(_now_iso())
    assert state.exists()
    lines = state.read_text().splitlines()
    assert len(lines) == 1
    assert n1 == 1
    # second call appends (no overwrite), count increments
    n2 = uhealth.record_block_event(_now_iso())
    assert len(state.read_text().splitlines()) == 2
    assert n2 == 2


def _make_board(board_dir: Path, task_id: str, task_status: str,
                outcome: str, ended_offset_min: float,
                parent_status: str | None = None) -> None:
    board_dir.mkdir(parents=True, exist_ok=True)
    db = board_dir / "kanban.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, status TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS task_links (parent_id TEXT, child_id TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS task_runs "
                "(id INTEGER PRIMARY KEY, task_id TEXT, outcome TEXT, ended_at INTEGER)")
    con.execute("INSERT OR REPLACE INTO tasks (id, status) VALUES (?, ?)",
                (task_id, task_status))
    if parent_status is not None:
        parent_id = f"{task_id}_parent"
        con.execute("INSERT OR REPLACE INTO tasks (id, status) VALUES (?, ?)",
                    (parent_id, parent_status))
        con.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                    (parent_id, task_id))
    ended = int(_now_iso(ended_offset_min).timestamp())
    con.execute("INSERT INTO task_runs (task_id, outcome, ended_at) VALUES (?, ?, ?)",
                (task_id, outcome, ended))
    con.commit()
    con.close()


def _make_ready_backlog_db(db: Path, now: datetime) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE tasks ("
        "id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT, created_at INTEGER)"
    )
    rows = [
        ("t_old_devops", "old devops", "devops", "ready", int((now - timedelta(days=10)).timestamp())),
        ("t_mid_builder", "builder", "builder", "ready", int((now - timedelta(days=6)).timestamp())),
        ("t_new_devops", "new devops", "devops", "ready", int((now - timedelta(days=2)).timestamp())),
        ("t_running_devops", "running", "devops", "running", int((now - timedelta(days=20)).timestamp())),
        ("t_blocked_old", "blocked", "devops", "blocked", int((now - timedelta(days=30)).timestamp())),
    ]
    con.executemany(
        "INSERT INTO tasks (id, title, assignee, status, created_at) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()


def _make_sycode_ready_backlog_db(db: Path, now: datetime) -> None:
    """Sycode-trading-style ready board: includes a long-stalled old ready task
    (mimics the 24.4d approved-review awaiting PR merge, t_bf11a0ce)."""
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE tasks ("
        "id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT, created_at INTEGER)"
    )
    rows = [
        ("t_30c13209", "approved review awaiting merge", "trading-devops",
         "ready", int((now - timedelta(days=24, hours=10)).timestamp())),
        ("t_recent1", "recent card A", "researcher-a", "ready",
         int((now - timedelta(days=3)).timestamp())),
        ("t_recent2", "recent card B", "researcher-b", "ready",
         int((now - timedelta(days=1)).timestamp())),
        ("t_running", "running", "trading-devops", "running",
         int((now - timedelta(days=40)).timestamp())),
    ]
    con.executemany(
        "INSERT INTO tasks (id, title, assignee, status, created_at) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()


def _absent_sycode(tmp: Path) -> Path:
    """Return a path that does NOT exist, so a test body never reads the live
    sycode-trading board."""
    return tmp / "sycode-trading" / "kanban.db"


def _seed_forced_releases(path: Path, count: int, now: datetime | None = None,
                          base_offset_min: float = 0.0,
                          gap_min: float = 2.0) -> None:
    """Write `count` forced-release rows shaped exactly like scheduler.py's
    _record_forced_release mirror (job_id/name/age_seconds/allowance_seconds/
    at) so the probe reader is exercised against the real record contract."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ref = now or _now_iso()
    with path.open("a", encoding="utf-8") as fh:
        for i in range(count):
            fh.write(json.dumps({
                "job_id": f"job_{i}",
                "name": f"wedged-job-{i}",
                "age_seconds": 7200.0 + i,
                "allowance_seconds": 1800.0,
                "at": (ref - timedelta(minutes=base_offset_min + i * gap_min)).isoformat(),
            }) + "\n")


def _stub_checks_pass() -> None:
    """Stub every other probe check to PASS so verdict tests isolate the
    forced-release signal (t_615aa245)."""
    uhealth.check_hermes_cli = lambda: (True, "ok", False)
    uhealth.check_gateway_unit = lambda: (True, "ok", False)
    uhealth.check_gateway_runtime = lambda: (True, True, "ok")
    uhealth.check_cron_ticker = lambda: (True, "ok", False)
    uhealth.check_canary_freshness = lambda: (True, "ok")
    uhealth.check_docker = lambda: (True, "ok", False)
    uhealth.check_disk = lambda: (True, "ok", False)
    uhealth.check_mechanism_matrix = lambda: {
        "available": True,
        "overall": "GREEN",
        "dead": 0,
        "detail": "ok",
        "fork_resource_pressure": False,
    }
    uhealth.check_kanban_crashes = lambda: (0, [], 0)


def _make_executions_db(db: Path, now: datetime, *,
                        gaps_min: list[float] | None = None,
                        claimed_ages_min: list[float] | None = None,
                        claimed_not_started: int = 0,
                        claimed_stale_min: float = 2.0,
                        running_zombies: int = 0) -> None:
    """Seed a cron executions.db shaped exactly like the jarvis scheduler
    ledger (schema: id/job_id/source/process_id/pid/process_started_at/
    status/claimed_at/started_at/finished_at/error).

    - gaps_min: N completed rows, each claimed at base+i min, started
      base+i+gap min (claim->start gap in minutes).
    - claimed_ages_min: N claimed-but-not-started rows, each claimed that many
      minutes before `now` (overrides claimed_not_started/claimed_stale_min).
    - claimed_not_started + claimed_stale_min: N claimed rows all claimed
      claimed_stale_min minutes before `now`.
    - running_zombies: N status=running rows whose started_at is 150 min old.
    """
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE executions ("
        "id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL, "
        "process_id TEXT NOT NULL, pid INTEGER NOT NULL, "
        "process_started_at INTEGER, "
        "status TEXT NOT NULL, claimed_at TEXT NOT NULL, "
        "started_at TEXT, finished_at TEXT, error TEXT)"
    )
    base = now - timedelta(minutes=90)
    for i, gap in enumerate(gaps_min or []):
        c = base + timedelta(minutes=i)
        s = c + timedelta(minutes=gap)
        con.execute(
            "INSERT INTO executions (id, job_id, source, process_id, pid, "
            "process_started_at, status, claimed_at, started_at, finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"g{i}", f"job{i}", "builtin", f"p{i}", 1000 + i, None,
             "completed", c.isoformat(), s.isoformat(),
             (s + timedelta(seconds=30)).isoformat()),
        )
    if claimed_ages_min is not None:
        claimed_rows = [(now - timedelta(minutes=age)) for age in claimed_ages_min]
    else:
        claimed_rows = [now - timedelta(minutes=claimed_stale_min)
                        for _ in range(claimed_not_started)]
    for i, c in enumerate(claimed_rows):
        con.execute(
            "INSERT INTO executions (id, job_id, source, process_id, pid, "
            "process_started_at, status, claimed_at, started_at, finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"c{i}", f"jobc{i}", "builtin", f"pc{i}", 2000 + i, None,
             "claimed", c.isoformat(), None, None),
        )
    for i in range(running_zombies):
        s = now - timedelta(minutes=150)  # > QUEUE_BACKLOG_ZOMBIE_STARTED_MIN (2h)
        con.execute(
            "INSERT INTO executions (id, job_id, source, process_id, pid, "
            "process_started_at, status, claimed_at, started_at, finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"z{i}", f"jobz{i}", "builtin", f"pz{i}", 3000 + i, None,
             "running", (s - timedelta(minutes=1)).isoformat(), s.isoformat(), None),
        )
    con.commit()
    con.close()


def _hermetic_main_paths(tmp: Path) -> None:
    """Point every probe DB/log path at the temp layout so main() tests never
    read the live jarvis executions.db or board DBs (hermetic, deterministic)."""
    uhealth.EXECUTIONS_DB = tmp / "executions.db"  # absent
    uhealth.JARVIS_OS_KANBAN_DB = tmp / "jarvis-os" / "kanban.db"  # absent
    uhealth.SYCODE_TRADING_KANBAN_DB = tmp / "sycode-trading" / "kanban.db"  # absent
    uhealth.CRON_OUTPUT = tmp / "cron"
    uhealth.UNIFIED_LOG = uhealth.CRON_OUTPUT / "unified_health_canary.jsonl"
    uhealth.CRON_FORCED_RELEASES_LOG = tmp / "inflight_forced_releases.jsonl"  # absent


def test_cron_forced_releases_absent_file_fails_open():
    tmp = Path(tempfile.mkdtemp())
    uhealth.CRON_FORCED_RELEASES_LOG = tmp / "inflight_forced_releases.jsonl"  # absent
    rep = uhealth.check_cron_forced_releases()
    assert rep["available"] is False
    assert rep["count"] == 0
    assert rep["block"] is False
    assert rep["warn"] is False


def test_cron_forced_releases_counts_recent_and_rolls_off_window():
    tmp = Path(tempfile.mkdtemp())
    log = tmp / "inflight_forced_releases.jsonl"
    now = _now_iso()
    _seed_forced_releases(log, 2, now=now, base_offset_min=0.0)  # recent
    _seed_forced_releases(log, 1, now=now,
                          base_offset_min=uhealth.CRON_FORCED_RELEASE_WINDOW_H * 60 + 30)
    uhealth.CRON_FORCED_RELEASES_LOG = log
    rep = uhealth.check_cron_forced_releases(now)
    assert rep["available"] is True
    assert rep["count"] == 2, f"old releases must roll off, got {rep['count']}"
    assert rep["block"] is False
    assert rep["warn"] is True
    assert len(rep["recent"]) == 2
    assert rep["recent"][0]["job_id"] == "job_0"  # newest first


def test_cron_forced_releases_corrupt_row_skipped():
    tmp = Path(tempfile.mkdtemp())
    log = tmp / "inflight_forced_releases.jsonl"
    _seed_forced_releases(log, 1, base_offset_min=1.0)
    with log.open("a", encoding="utf-8") as fh:
        fh.write("not-json{{{}\n")
        fh.write(json.dumps({"job_id": "x", "name": "no-at"}) + "\n")
    uhealth.CRON_FORCED_RELEASES_LOG = log
    rep = uhealth.check_cron_forced_releases()
    assert rep["count"] == 1, f"corrupt/no-at rows must be skipped, got {rep['count']}"
    assert rep["warn"] is True
    assert rep["block"] is False


def test_single_forced_release_warns_not_blocks():
    tmp = Path(tempfile.mkdtemp())
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    log = tmp / "inflight_forced_releases.jsonl"
    _seed_forced_releases(log, 1, now=now, base_offset_min=5.0)
    uhealth.EXECUTIONS_DB = tmp / "executions.db"  # absent
    uhealth.CRON_FORCED_RELEASES_LOG = log
    uhealth.JARVIS_OS_KANBAN_DB = tmp / "jarvis-os" / "kanban.db"  # absent
    uhealth.SYCODE_TRADING_KANBAN_DB = tmp / "sycode-trading" / "kanban.db"  # absent
    uhealth.CRON_OUTPUT = tmp / "cron"
    uhealth.UNIFIED_LOG = uhealth.CRON_OUTPUT / "unified_health_canary.jsonl"
    _stub_checks_pass()
    uhealth.utc_now = lambda: now

    rc = uhealth.main()
    assert rc == 0
    record = json.loads(uhealth.UNIFIED_LOG.read_text(encoding="utf-8").splitlines()[-1])
    assert record["verdict"] == "WARN", \
        f"single release must WARN, not BLOCK: {record['verdict']}"
    assert record["cron_forced_release_count"] == 1
    assert record["cron_forced_releases"]["count"] == 1
    assert record["cron_forced_releases"]["block"] is False
    assert record["cron_forced_releases"]["warn"] is True
    assert "cron_forced_releases" not in record["infra_failed"], \
        "a single release is not an infra failure"


def test_repeated_forced_releases_block():
    tmp = Path(tempfile.mkdtemp())
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    log = tmp / "inflight_forced_releases.jsonl"
    _seed_forced_releases(log, uhealth.CRON_FORCED_RELEASE_BLOCK_MIN,
                          now=now, base_offset_min=2.0, gap_min=1.0)
    uhealth.EXECUTIONS_DB = tmp / "executions.db"  # absent
    uhealth.CRON_FORCED_RELEASES_LOG = log
    uhealth.JARVIS_OS_KANBAN_DB = tmp / "jarvis-os" / "kanban.db"  # absent
    uhealth.SYCODE_TRADING_KANBAN_DB = tmp / "sycode-trading" / "kanban.db"  # absent
    uhealth.CRON_OUTPUT = tmp / "cron"
    uhealth.UNIFIED_LOG = uhealth.CRON_OUTPUT / "unified_health_canary.jsonl"
    uhealth.CRITICAL_ALERT_STATE = tmp / "unified_health_block_history.jsonl"
    # Stub the escalation path so the BLOCK body never reaches hermes send.
    uhealth.run = lambda argv, timeout=25: {
        "rc": 0, "out": "ok", "err": "", "timeout": False,
        "fork_resource_pressure": False,
    }
    _stub_checks_pass()
    uhealth.utc_now = lambda: now

    rc = uhealth.main()
    assert rc == 0  # BLOCK verdict delivered OK; probe itself is healthy
    record = json.loads(uhealth.UNIFIED_LOG.read_text(encoding="utf-8").splitlines()[-1])
    assert record["verdict"] == "BLOCK", \
        f"repeated wedges must BLOCK: {record['verdict']}"
    assert record["cron_forced_release_count"] == uhealth.CRON_FORCED_RELEASE_BLOCK_MIN
    assert record["cron_forced_releases"]["block"] is True
    assert "cron_forced_releases" in record["infra_failed"], \
        "repeated wedges must surface as an infra failure"
    alert_file = uhealth.CRON_OUTPUT / "unified_health_alert.last"
    assert alert_file.exists()
    body = alert_file.read_text(encoding="utf-8")
    assert "## Cron forced releases" in body, "BLOCK body must name the wedge class"


def test_forced_releases_absent_main_stays_pass():
    tmp = Path(tempfile.mkdtemp())
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    uhealth.EXECUTIONS_DB = tmp / "executions.db"  # absent
    uhealth.CRON_FORCED_RELEASES_LOG = tmp / "inflight_forced_releases.jsonl"  # absent
    uhealth.JARVIS_OS_KANBAN_DB = tmp / "jarvis-os" / "kanban.db"  # absent
    uhealth.SYCODE_TRADING_KANBAN_DB = tmp / "sycode-trading" / "kanban.db"  # absent
    uhealth.CRON_OUTPUT = tmp / "cron"
    uhealth.UNIFIED_LOG = uhealth.CRON_OUTPUT / "unified_health_canary.jsonl"
    _stub_checks_pass()
    uhealth.utc_now = lambda: now

    rc = uhealth.main()
    assert rc == 0
    record = json.loads(uhealth.UNIFIED_LOG.read_text(encoding="utf-8").splitlines()[-1])
    assert record["verdict"] == "PASS", f"no releases must stay PASS: {record['verdict']}"
    assert record["cron_forced_release_count"] == 0
    assert record["cron_forced_releases"]["count"] == 0


def test_check_kanban_crashes_active_vs_stale():
    tmp = Path(tempfile.mkdtemp())
    boards = tmp / "boards"
    # Active crash: running task with crashed run in window
    _make_board(boards / "jarvis-os", "t_active", "running", "crashed", -5)
    # Stale crash: done task with crashed run in window
    _make_board(boards / "sycode-trading", "t_done", "done", "gave_up", -5)
    uhealth.BOARDS_DIR = boards
    count, hits, stale = uhealth.check_kanban_crashes()
    assert count == 1, f"expected 1 active crash, got {count}: {hits}"
    assert any("t_active" in h for h in hits), f"active hit missing: {hits}"
    assert stale == 1, f"expected 1 stale, got {stale}"
    assert not any("t_done" in h for h in hits), f"stale leaked into active: {hits}"


def test_jarvis_ready_backlog_counts_and_ages():
    tmp = Path(tempfile.mkdtemp())
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    db = tmp / "jarvis-os" / "kanban.db"
    _make_ready_backlog_db(db, now)
    uhealth.JARVIS_OS_KANBAN_DB = db
    uhealth.SYCODE_TRADING_KANBAN_DB = _absent_sycode(tmp)

    rep = uhealth.check_jarvis_ready_backlog(now)
    assert rep["available"] is True
    assert rep["ready_total"] == 3
    assert rep["oldest_ready_age_days"] == 10.0
    assert rep["devops_ready_count"] == 2
    assert rep["oldest_devops_ready_age_days"] == 10.0
    assert rep["top_ready_ids"] == ["t_old_devops", "t_mid_builder", "t_new_devops"]
    assert rep["top_devops_ready_ids"] == ["t_old_devops", "t_new_devops"]


def test_jarvis_ready_backlog_observability_does_not_block_main():
    tmp = Path(tempfile.mkdtemp())
    now = datetime(2026, 7, 28, tzinfo=timezone.utc)
    db = tmp / "jarvis-os" / "kanban.db"
    _make_ready_backlog_db(db, now)

    uhealth.JARVIS_OS_KANBAN_DB = db
    uhealth.SYCODE_TRADING_KANBAN_DB = _absent_sycode(tmp)
    uhealth.EXECUTIONS_DB = tmp / "executions.db"  # absent
    uhealth.CRON_OUTPUT = tmp / "cron"
    uhealth.UNIFIED_LOG = uhealth.CRON_OUTPUT / "unified_health_canary.jsonl"
    uhealth.CRON_FORCED_RELEASES_LOG = tmp / "inflight_forced_releases.jsonl"  # absent
    uhealth.check_hermes_cli = lambda: (True, "ok", False)
    uhealth.check_gateway_unit = lambda: (True, "ok", False)
    uhealth.check_gateway_runtime = lambda: (True, True, "ok")
    uhealth.check_cron_ticker = lambda: (True, "ok", False)
    uhealth.check_canary_freshness = lambda: (True, "ok")
    uhealth.check_docker = lambda: (True, "ok", False)
    uhealth.check_disk = lambda: (True, "ok", False)
    uhealth.check_mechanism_matrix = lambda: {
        "available": True,
        "overall": "GREEN",
        "dead": 0,
        "detail": "ok",
        "fork_resource_pressure": False,
    }
    uhealth.check_kanban_crashes = lambda: (0, [], 0)
    uhealth.utc_now = lambda: now

    rc = uhealth.main()
    assert rc == 0
    record = json.loads(uhealth.UNIFIED_LOG.read_text(encoding="utf-8").splitlines()[-1])
    # Observability backlog age must NEVER drive a BLOCK verdict (the original
    # contract of this test). With a 10d-old ready task present it now degrades
    # to WARN, which is also acceptable here; it must not be BLOCK.
    assert record["verdict"] != "BLOCK"
    assert record["jarvis_ready_backlog"]["oldest_ready_age_days"] == 10.0


def test_legacy_substrate_bridge_stamps_fresh_health_canary_record():
    # t_bd9d284e: the unified probe must re-stamp a fresh substrate record
    # into the legacy health_canary.jsonl every cycle so legacy consumers
    # (dgx_report_anomaly_detector.py, t_5311fb77 channel, spine-audit) never
    # read the frozen ~24h-stale gateway_running left by the paused canary.
    tmp = Path(tempfile.mkdtemp())
    uhealth.EXECUTIONS_DB = tmp / "executions.db"  # absent
    uhealth.CRON_OUTPUT = tmp / "cron"
    uhealth.UNIFIED_LOG = uhealth.CRON_OUTPUT / "unified_health_canary.jsonl"
    uhealth.CRON_FORCED_RELEASES_LOG = tmp / "inflight_forced_releases.jsonl"  # absent
    uhealth.JARVIS_OS_KANBAN_DB = tmp / "jarvis-os" / "kanban.db"  # absent
    uhealth.SYCODE_TRADING_KANBAN_DB = tmp / "sycode-trading" / "kanban.db"  # absent
    uhealth.check_hermes_cli = lambda: (True, "ok", False)
    uhealth.check_gateway_unit = lambda: (True, "ok", False)
    uhealth.check_gateway_runtime = lambda: (True, True, "ok")
    uhealth.check_cron_ticker = lambda: (True, "ok", False)
    uhealth.check_canary_freshness = lambda: (True, "ok")
    uhealth.check_docker = lambda: (True, "ok", False)
    uhealth.check_disk = lambda: (True, "ok", False)
    uhealth.check_mechanism_matrix = lambda: {
        "available": True,
        "overall": "GREEN",
        "dead": 0,
        "detail": "ok",
        "fork_resource_pressure": False,
    }
    uhealth.check_kanban_crashes = lambda: (0, [], 0)
    uhealth.utc_now = lambda: datetime(2026, 7, 28, 17, 0, tzinfo=timezone.utc)

    rc = uhealth.main()
    assert rc == 0
    legacy = uhealth.CRON_OUTPUT / "health_canary.jsonl"
    assert legacy.exists(), "bridge must write legacy health_canary.jsonl"
    lines = legacy.read_text(encoding="utf-8").splitlines()
    assert lines, "bridge wrote at least one record"
    rec = json.loads(lines[-1])
    assert rec.get("substrate_source") == "unified-health-probe"
    assert rec.get("hermes_cli") is True
    assert rec.get("gateway_running") is True
    assert rec.get("verdict") == "PASS"


def test_sycode_ready_backlog_reports_oldest_and_counts():
    tmp = Path(tempfile.mkdtemp())
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    db = tmp / "sycode-trading" / "kanban.db"
    _make_sycode_ready_backlog_db(db, now)

    rep = uhealth._scan_board_ready_backlog(db, "sycode-trading", now)
    assert rep["available"] is True
    assert rep["board"] == "sycode-trading"
    assert rep["ready_total"] == 3
    # oldest ready = t_30c13209 at 24d10h -> ~24.417 days
    assert rep["oldest_ready_task_id"] == "t_30c13209"
    assert abs(rep["oldest_ready_age_days"] - 24.417) < 0.01
    assert rep["warn"] is True
    assert rep["top_ready_ids"][0] == "t_30c13209"


def test_ready_backlog_does_not_warn_below_threshold():
    tmp = Path(tempfile.mkdtemp())
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    db = tmp / "sycode-trading" / "kanban.db"
    # all ready tasks < 7d old
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE tasks ("
        "id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT, created_at INTEGER)"
    )
    con.execute(
        "INSERT INTO tasks VALUES ('t_new', 'new', 'r', 'ready', ?)",
        (int((now - timedelta(days=2)).timestamp()),),
    )
    con.commit()
    con.close()
    rep = uhealth._scan_board_ready_backlog(db, "sycode-trading", now)
    assert rep["warn"] is False
    assert rep["oldest_ready_age_days"] == 2.0


def test_ready_backlog_warn_does_not_block_main_verdict():
    tmp = Path(tempfile.mkdtemp())
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    db = tmp / "jarvis-os" / "kanban.db"
    sdb = tmp / "sycode-trading" / "kanban.db"
    _make_ready_backlog_db(db, now)
    _make_sycode_ready_backlog_db(sdb, now)

    uhealth.JARVIS_OS_KANBAN_DB = db
    uhealth.SYCODE_TRADING_KANBAN_DB = sdb
    uhealth.EXECUTIONS_DB = tmp / "executions.db"  # absent
    uhealth.CRON_OUTPUT = tmp / "cron"
    uhealth.UNIFIED_LOG = uhealth.CRON_OUTPUT / "unified_health_canary.jsonl"
    uhealth.CRON_FORCED_RELEASES_LOG = tmp / "inflight_forced_releases.jsonl"  # absent
    uhealth.check_hermes_cli = lambda: (True, "ok", False)
    uhealth.check_gateway_unit = lambda: (True, "ok", False)
    uhealth.check_gateway_runtime = lambda: (True, True, "ok")
    uhealth.check_cron_ticker = lambda: (True, "ok", False)
    uhealth.check_canary_freshness = lambda: (True, "ok")
    uhealth.check_docker = lambda: (True, "ok", False)
    uhealth.check_disk = lambda: (True, "ok", False)
    uhealth.check_mechanism_matrix = lambda: {
        "available": True,
        "overall": "GREEN",
        "dead": 0,
        "detail": "ok",
        "fork_resource_pressure": False,
    }
    uhealth.check_kanban_crashes = lambda: (0, [], 0)
    uhealth.utc_now = lambda: now

    rc = uhealth.main()
    assert rc == 0
    record = json.loads(uhealth.UNIFIED_LOG.read_text(encoding="utf-8").splitlines()[-1])
    # Observability backlog age degrades PASS -> WARN, NEVER BLOCK.
    assert record["verdict"] == "WARN"
    assert record["sycode_trading_ready_backlog"]["oldest_ready_task_id"] == "t_30c13209"
    assert record["sycode_trading_ready_backlog"]["oldest_ready_age_days"] > 7
    assert any(
        w["board"] == "sycode-trading" and w["oldest_ready_task_id"] == "t_30c13209"
        for w in record["ready_backlog_warn_boards"]
    )


def test_check_kanban_crashes_treats_done_parent_active_child_as_stale():
    tmp = Path(tempfile.mkdtemp())
    boards = tmp / "boards"
    _make_board(
        boards / "jarvis-os",
        "t_done_parent_child",
        "running",
        "gave_up",
        -5,
        parent_status="done",
    )
    _make_board(boards / "sycode-trading", "t_unparented", "running", "crashed", -5)
    uhealth.BOARDS_DIR = boards
    count, hits, stale = uhealth.check_kanban_crashes()
    assert count == 1, f"expected only unparented active crash, got {count}: {hits}"
    assert any("t_unparented" in h for h in hits), f"active hit missing: {hits}"
    assert not any("t_done_parent_child" in h for h in hits), \
        f"done-parent child leaked into active hits: {hits}"
    assert stale == 1, f"expected done-parent child stale count, got {stale}"


def test_check_kanban_crashes_dedupes_same_task():
    tmp = Path(tempfile.mkdtemp())
    boards = tmp / "boards"
    bd = boards / "jarvis-os"
    bd.mkdir(parents=True, exist_ok=True)
    db = bd / "kanban.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT)")
    con.execute("CREATE TABLE task_links (parent_id TEXT, child_id TEXT)")
    con.execute("CREATE TABLE task_runs "
                "(id INTEGER PRIMARY KEY, task_id TEXT, outcome TEXT, ended_at INTEGER)")
    con.execute("INSERT INTO tasks VALUES ('t_x', 'ready')")
    ended = int(_now_iso(-3).timestamp())
    for i in range(4):  # 4 crashed runs for the SAME task
        con.execute("INSERT INTO task_runs (task_id, outcome, ended_at) "
                    "VALUES ('t_x', 'crashed', ?)", (ended,))
    con.commit()
    con.close()
    uhealth.BOARDS_DIR = boards
    count, hits, stale = uhealth.check_kanban_crashes()
    assert count == 1, f"same-task multi-run should dedup to 1, got {count}: {hits}"


def test_queue_backlog_absent_db_fails_open():
    """C5 telemetry must fail open when executions.db is absent — never a
    BLOCK cause (t_f6f61faa)."""
    tmp = Path(tempfile.mkdtemp())
    uhealth.EXECUTIONS_DB = tmp / "executions.db"  # absent
    rep = uhealth.check_cron_queue_backlog()
    assert rep["available"] is False
    assert rep["claim_start_samples"] == 0
    assert rep["claim_start_median_min"] is None
    assert rep["claimed_not_started"] == 0
    assert rep["claimed_stale_gt_10min"] == 0
    assert rep["zombies_running_gt_2h"] == 0
    assert rep["warn"] is False


def test_queue_backlog_reports_gaps_depth_zombies():
    """C5 telemetry computes median+max claim->start gap, claimed-but-not-
    started depth with stale (>10m) count, and zombie (running >2h) count."""
    tmp = Path(tempfile.mkdtemp())
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    db = tmp / "executions.db"
    _make_executions_db(
        db, now,
        gaps_min=[1, 2, 3, 4, 5],           # completed rows with 1-5m gaps
        claimed_ages_min=[2, 12, 40],       # 1 fresh, 2 stale (>10m)
        running_zombies=2,                  # running started 2.5h ago (claimed 1m before)
    )
    uhealth.EXECUTIONS_DB = db
    rep = uhealth.check_cron_queue_backlog(now)
    assert rep["available"] is True
    # Zombie rows also carry claim+start timestamps, so they legitimately join
    # the gap window: sample set [1,2,3,4,5,1,1] -> median 2.0, max 5.0, n=7.
    assert rep["claim_start_samples"] == 7
    assert rep["claim_start_median_min"] == 2.0
    assert rep["claim_start_max_min"] == 5.0
    assert rep["claimed_not_started"] == 3
    assert rep["claimed_stale_gt_10min"] == 2
    assert rep["zombies_running_gt_2h"] == 2
    assert rep["warn"] is False
    assert "claim_start_median=2.0m" in rep["detail"]
    assert "claimed_not_started=3" in rep["detail"]
    assert "zombies_running_gt_2h=2" in rep["detail"]


def test_queue_backlog_warn_degrades_pass_to_warn_not_block():
    """Median claim->start > 15m degrades PASS -> WARN but NEVER BLOCKs."""
    tmp = Path(tempfile.mkdtemp())
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    db = tmp / "executions.db"
    _make_executions_db(db, now, gaps_min=[16, 16, 16])  # median 16 > 15
    uhealth.EXECUTIONS_DB = db
    uhealth.JARVIS_OS_KANBAN_DB = tmp / "jarvis-os" / "kanban.db"  # absent
    uhealth.SYCODE_TRADING_KANBAN_DB = tmp / "sycode-trading" / "kanban.db"  # absent
    uhealth.CRON_OUTPUT = tmp / "cron"
    uhealth.UNIFIED_LOG = uhealth.CRON_OUTPUT / "unified_health_canary.jsonl"
    uhealth.CRON_FORCED_RELEASES_LOG = tmp / "inflight_forced_releases.jsonl"  # absent
    _stub_checks_pass()
    uhealth.utc_now = lambda: now

    rc = uhealth.main()
    assert rc == 0
    record = json.loads(uhealth.UNIFIED_LOG.read_text(encoding="utf-8").splitlines()[-1])
    assert record["verdict"] == "WARN", \
        f"claim->start median > 15m must WARN, not {record['verdict']}"
    assert record["cron_queue_backlog"]["warn"] is True
    assert record["queue_backlog_warn"] is True
    assert "cron_queue_backlog" not in record["infra_failed"], \
        "queue-backlog telemetry must never be an infra failure"


def test_queue_backlog_depth_never_blocks_main():
    """A deep claimed-but-not-started backlog + zombies with a LOW median
    must NOT BLOCK the fleet verdict (informational telemetry only)."""
    tmp = Path(tempfile.mkdtemp())
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    db = tmp / "executions.db"
    _make_executions_db(
        db, now,
        gaps_min=[0.5, 1, 1.5, 2, 3],       # median 1.5 -> no warn
        claimed_ages_min=[11, 12, 40],      # 3 stale claimed
        running_zombies=2,
    )
    uhealth.EXECUTIONS_DB = db
    uhealth.JARVIS_OS_KANBAN_DB = tmp / "jarvis-os" / "kanban.db"  # absent
    uhealth.SYCODE_TRADING_KANBAN_DB = tmp / "sycode-trading" / "kanban.db"  # absent
    uhealth.CRON_OUTPUT = tmp / "cron"
    uhealth.UNIFIED_LOG = uhealth.CRON_OUTPUT / "unified_health_canary.jsonl"
    uhealth.CRON_FORCED_RELEASES_LOG = tmp / "inflight_forced_releases.jsonl"  # absent
    _stub_checks_pass()
    uhealth.utc_now = lambda: now

    rc = uhealth.main()
    assert rc == 0
    record = json.loads(uhealth.UNIFIED_LOG.read_text(encoding="utf-8").splitlines()[-1])
    assert record["verdict"] != "BLOCK", \
        f"queue backlog must never BLOCK: {record['verdict']}"
    assert record["cron_queue_backlog"]["claimed_not_started"] == 3
    assert record["cron_queue_backlog"]["claimed_stale_gt_10min"] == 3
    assert record["cron_queue_backlog"]["zombies_running_gt_2h"] == 2
    assert record["cron_queue_backlog"]["warn"] is False


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa
            failures += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
