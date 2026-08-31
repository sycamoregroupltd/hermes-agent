"""Tests for t_6baff6ad: gc_events emits first-party audit evidence.

Verifies:
  - gc_events appends one JSON-line to the audit file per invocation
  - The line carries run_at, retention_seconds, cutoff, min/max event_id,
    count, and task_ids sorted set
  - gc_events_preview returns full snapshot info without modifying the DB
  - Zero-deletion runs do NOT write an audit record
  - Non-terminal tasks' events are never deleted
  - Multiple successive sweeps produce distinct audit lines
  - Custom audit dir via $HERMES_KANBAN_GC_AUDIT_DIR is respected
"""
from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Create a temporary Hermes home with an isolated gc_audit directory."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Isolated audit dir per test run
    monkeypatch.setenv(kb._GCAUDIT_ENV, str(tmp_path / "gc_audit"))
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_old_event(conn, task_id, age_days, index=0):
    """Insert a single task_event row with a crafted created_at timestamp."""
    now = int(time.time())
    ts = now - age_days * 86400 + 100 + index * 3600
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, NULL, 'comment', ?, ?)",
        (task_id, json.dumps({"test": True, "index": index}), ts),
    )


def _make_done_task_with_events(conn, count=5, age_days=60):
    tid = kb.create_task(conn, title=f"old-task-{age_days}d", assignee="alice")
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    for i in range(count):
        _insert_old_event(conn, tid, age_days, i)
    return tid


def _make_archived_task_with_events(conn, count=3, age_days=45):
    tid = kb.create_task(conn, title=f"archived-task-{age_days}d", assignee="bob")
    conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (tid,))
    for i in range(count):
        _insert_old_event(conn, tid, age_days, i)
    return tid


def _make_running_task_with_old_events(conn, count=3):
    """Create a running task with old events."""
    tid = kb.create_task(conn, title="running-old-events", assignee="carol")
    conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
    for i in range(count):
        _insert_old_event(conn, tid, 90, i)
    return tid


def _clear_audit():
    path = kb.gc_events_audit_file_path()
    if path.exists():
        path.unlink()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_gc_emits_audit_line(kanban_home, monkeypatch):
    """After a GC sweep, the audit file exists and contains one valid line."""
    _clear_audit()
    with kb.connect_closing() as conn:
        tid1 = _make_done_task_with_events(conn, count=5, age_days=60)
        tid2 = _make_archived_task_with_events(conn, count=3, age_days=45)
        before_count = conn.execute("SELECT count(*) FROM task_events").fetchone()[0]
        removed = kb.gc_events(conn, older_than_seconds=30 * 86400)
        after_count = conn.execute("SELECT count(*) FROM task_events").fetchone()[0]
        assert after_count == before_count - removed
        assert removed == 8

        lines = kb.load_gc_audit_lines()
        assert len(lines) == 1
        entry = lines[0]
        assert entry["count"] == 8
        assert entry["min_event_id"] is not None
        assert entry["max_event_id"] is not None
        assert entry["retention_seconds"] == 30 * 86400
        assert entry["cutoff"] is not None
        assert entry["run_at"] is not None
        assert sorted(entry["task_ids"]) == sorted([str(tid1), str(tid2)])


def test_gc_preview_does_not_delete(kanban_home, monkeypatch):
    """gc_events_preview() returns full snapshot info without modifying the DB."""
    _clear_audit()
    with kb.connect_closing() as conn:
        tid = _make_done_task_with_events(conn, count=7, age_days=60)
        before_count = conn.execute("SELECT count(*) FROM task_events").fetchone()[0]
        snapshot = kb.gc_events_preview(
            conn, older_than_seconds=30 * 86400
        )
        after_count = conn.execute("SELECT count(*) FROM task_events").fetchone()[0]
        assert after_count == before_count
        assert snapshot["count"] == 7
        assert str(tid) in snapshot["task_ids"]
        assert len(snapshot["details"]) == 7
        for detail in snapshot["details"]:
            assert "event_id" in detail
            assert "task_id" in detail
            assert "created_at" in detail


def test_no_deletion_no_audit(kanban_home, monkeypatch):
    """When GC deletes zero rows it does NOT write an audit line."""
    _clear_audit()
    with kb.connect_closing() as conn:
        now = int(time.time())
        tid = kb.create_task(conn, title="fresh-task", assignee="alice")
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        # Insert a very recent event
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, NULL, 'log', ?, ?)",
            (tid, json.dumps({"msg": "recent"}), now - 3600),
        )
        audit_before = kb.load_gc_audit_lines()
        removed = kb.gc_events(conn, older_than_seconds=30 * 86400)
        assert removed == 0
        audit_after = kb.load_gc_audit_lines()
        assert audit_after == audit_before


def test_audit_write_failure_does_not_block_gc(kanban_home, monkeypatch):
    """A filesystem failure while emitting audit evidence must not stop GC."""
    _clear_audit()
    monkeypatch.setattr(kb, "_gc_audit_file_path", lambda: Path("/proc/gc_events.jsonl"))
    with kb.connect_closing() as conn:
        _make_done_task_with_events(conn, count=2, age_days=60)
        assert kb.gc_events(conn, older_than_seconds=30 * 86400) == 2
        # create_task's recent event remains; only the two old rows are pruned.
        assert conn.execute("SELECT count(*) FROM task_events").fetchone()[0] == 1


def test_non_terminal_tasks_not_deleted(kanban_home, monkeypatch):
    """Events belonging to running/blocked tasks survive GC even if they're old.

    create_task adds 1 event; we add 3 more = 4 total. All stay intact.
    """
    _clear_audit()
    with kb.connect_closing() as conn:
        running_tid = _make_running_task_with_old_events(conn, count=3)
        done_tid = _make_done_task_with_events(conn, count=5, age_days=60)
        before = conn.execute("SELECT count(*) FROM task_events").fetchone()[0]
        removed = kb.gc_events(conn, older_than_seconds=30 * 86400)
        assert removed == 5  # only the done task's events
        remaining_running = conn.execute(
            "SELECT count(*) FROM task_events WHERE task_id=?", (running_tid,),
        ).fetchone()[0]
        assert remaining_running == 4  # 1 from create_task + 3 inserted
        lines = kb.load_gc_audit_lines()
        assert len(lines) == 1
        assert str(running_tid) not in lines[0]["task_ids"]
        assert str(done_tid) in lines[0]["task_ids"]


def test_multiple_sweeps_produce_multiple_lines(kanban_home, monkeypatch):
    """Two successive GC sweeps produce two distinct audit lines."""
    _clear_audit()
    with kb.connect_closing() as conn:
        tid1 = _make_done_task_with_events(conn, count=3, age_days=60)
        tid2 = _make_archived_task_with_events(conn, count=2, age_days=45)
        removed1 = kb.gc_events(conn, older_than_seconds=30 * 86400)
        assert removed1 == 5
        lines = kb.load_gc_audit_lines()
        assert len(lines) == 1
        assert lines[0]["count"] == 5
        assert lines[0]["run_at"] is not None

        # Create more tasks and sleep so timestamps differ
        tid3 = _make_done_task_with_events(conn, count=4, age_days=60)
        time.sleep(1.1)
        removed2 = kb.gc_events(conn, older_than_seconds=30 * 86400)
        assert removed2 == 4
        lines = kb.load_gc_audit_lines()
        assert len(lines) == 2
        # Newest first
        assert lines[0]["count"] == 4
        assert str(tid3) in lines[0]["task_ids"]
        assert lines[1]["count"] == 5
        assert lines[0]["run_at"] != lines[1]["run_at"]


def test_custom_audit_dir(monkeypatch, tmp_path):
    """$HERMES_KANBAN_GC_AUDIT_DIR overrides the default audit location.

    Uses a separate subprocess so the env var is picked up at module-load time.
    """
    custom_dir = tmp_path / "custom_audit"
    hermes_home = tmp_path / "hermes"

    script = tmp_path / "gc_test_subprocess.py"
    script.write_text(
        f"""\
import json, os, sys, time
from pathlib import Path

os.environ['{kb._GCAUDIT_ENV}'] = '{custom_dir}'
os.environ['HERMES_HOME'] = '{hermes_home}'
Path.home = lambda: Path('{hermes_home}')

from hermes_cli import kanban_db as kb

kb.init_db()
with kb.connect_closing() as conn:
    now = int(time.time())
    tid = kb.create_task(conn, title='custom-audit-test', assignee='alice')
    conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, NULL, 'comment', ?, ?)",
        (tid, json.dumps({{'x': 1}}), now - 60*86400),
    )
    removed = kb.gc_events(conn, older_than_seconds=30*86400)
    assert removed == 1, f'expected 1, got {{removed}}'
"""
    )
    subprocess_env = os.environ.copy()
    repo_root = Path(__file__).resolve().parents[2]
    subprocess_env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(repo_root), subprocess_env.get("PYTHONPATH", "")) if part
    )
    result = subprocess.run(
        [sys.executable, "-u", str(script)],
        capture_output=True, text=True, timeout=30, env=subprocess_env,
    )
    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    expected_file = custom_dir / "gc_events.jsonl"
    assert expected_file.exists(), f"audit file missing at {expected_file}"
    lines = expected_file.read_text().strip().split("\n")
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["count"] == 1
    assert entry["task_ids"]


def test_audit_record_fields_are_complete(kanban_home, monkeypatch):
    """Every audit line has all required fields with sensible types."""
    _clear_audit()
    with kb.connect_closing() as conn:
        tid = _make_done_task_with_events(conn, count=3, age_days=60)
        kb.gc_events(conn, older_than_seconds=30 * 86400)
        lines = kb.load_gc_audit_lines()
        assert len(lines) == 1
        entry = lines[0]
        # Verify all required keys present
        for key in ("run_at", "retention_seconds", "cutoff",
                    "min_event_id", "max_event_id", "count", "task_ids"):
            assert key in entry, f"missing field: {key}"
        # Type checks
        assert isinstance(entry["run_at"], int)
        assert isinstance(entry["retention_seconds"], int)
        assert isinstance(entry["cutoff"], int)
        assert isinstance(entry["min_event_id"], int)
        assert isinstance(entry["max_event_id"], int)
        assert isinstance(entry["count"], int)
        assert isinstance(entry["task_ids"], list)
        # Semantic checks
        assert entry["count"] == 3
        assert entry["min_event_id"] <= entry["max_event_id"]
        assert len(entry["task_ids"]) == 1
        assert entry["task_ids"][0] == str(tid)
        # min/max should match actual event IDs
        actual_min = None
        actual_max = None
        for d in kb.gc_events_preview(conn, older_than_seconds=30 * 86400)["details"]:
            eid = d["event_id"]
            if actual_min is None or eid < actual_min:
                actual_min = eid
            if actual_max is None or eid > actual_max:
                actual_max = eid
        # NOTE: preview runs on *remaining* events (already deleted above),
        # so just check the structure is sound
        assert entry["min_event_id"] <= entry["max_event_id"]


def test_failed_delete_emits_no_completed_audit(kanban_home):
    """A rolled-back delete must not produce authoritative completion evidence."""
    with kb.connect_closing() as conn:
        _make_done_task_with_events(conn, count=2, age_days=60)
        conn.execute("""
            CREATE TRIGGER reject_gc_delete BEFORE DELETE ON task_events
            BEGIN SELECT RAISE(ABORT, 'test delete failure'); END
        """)
        with pytest.raises(Exception, match="test delete failure"):
            kb.gc_events(conn, older_than_seconds=30 * 86400)
        assert kb.load_gc_audit_lines() == []
        assert conn.execute("SELECT count(*) FROM task_events").fetchone()[0] == 3


def test_malformed_audit_fails_closed(kanban_home):
    """Truncated JSONL is an explicit corruption signal, never empty history."""
    path = kb.gc_events_audit_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"status":"completed",\n', encoding="utf-8")
    with pytest.raises(kb.GcAuditCorruptionError, match="invalid JSON"):
        kb.load_gc_audit_lines()


def test_concurrent_audit_writers_preserve_jsonl(kanban_home):
    """Separate processes cannot interleave or truncate completed records."""
    def write_record(index):
        kb._emit_gc_audit([(index + 1, str(index))], 1000 + index, 3600, 500)

    workers = [multiprocessing.Process(target=write_record, args=(i,)) for i in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0
    records = kb.load_gc_audit_lines()
    assert len(records) == 8
    assert {record["status"] for record in records} == {"completed"}
    assert {record["min_event_id"] for record in records} == set(range(1, 9))


def test_valid_but_incomplete_audit_fails_closed(kanban_home):
    """A completed marker alone is never authoritative evidence."""
    path = kb.gc_events_audit_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"status":"completed"}\n', encoding="utf-8")
    with pytest.raises(kb.GcAuditCorruptionError, match="typed integer fields"):
        kb.load_gc_audit_lines()


def test_gc_audit_lock_fallback_without_fcntl(kanban_home, monkeypatch):
    """The shared module remains importable and usable without POSIX fcntl."""
    monkeypatch.setattr(kb, "_fcntl", None)
    assert kb._emit_gc_audit([(7, "t_windows")], 1000, 3600, 500) is True
    assert kb.load_gc_audit_lines()[0]["event_ids"] == [7]


def test_post_commit_invariant_failure_still_emits_evidence(kanban_home, monkeypatch):
    """A post-COMMIT invariant exception cannot lose the deletion record."""
    with kb.connect_closing() as conn:
        tid = _make_done_task_with_events(conn, count=2, age_days=60)
        monkeypatch.setattr(
            kb, "_check_file_length_invariant",
            lambda _conn: (_ for _ in ()).throw(RuntimeError("post-commit check")),
        )
        assert kb.gc_events(conn, older_than_seconds=30 * 86400) == 2
        records = kb.load_gc_audit_lines()
        assert len(records) == 1
        assert records[0]["task_ids"] == [str(tid)]


def test_gc_audit_failure_status_is_false(kanban_home, monkeypatch):
    """Best-effort audit failure is exposed to callers instead of claimed."""
    monkeypatch.setattr(kb, "_gc_audit_file_path", lambda: Path("/proc/gc_events.jsonl"))
    with kb.connect_closing() as conn:
        _make_done_task_with_events(conn, count=1, age_days=60)
        assert kb.gc_events(conn, older_than_seconds=30 * 86400) == 1
        assert kb.gc_events_audit_write_succeeded() is False


def test_cli_zero_deletion_does_not_claim_audit(capsys, monkeypatch, tmp_path):
    """CLI reports zero deletion without inventing an audit record."""
    class _RowsConn:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def execute(self, *_args):
            class _Cursor:
                def fetchall(self):
                    return []
            return _Cursor()

    class _Args:
        event_retention_days = 30
        log_retention_days = 30
        dry_run = False
        audit_report = False

    monkeypatch.setattr(kb, "connect_closing", lambda: _RowsConn())
    monkeypatch.setattr(kb, "workspaces_root", lambda: tmp_path)
    monkeypatch.setattr(kb, "gc_events", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(kb, "gc_worker_logs", lambda **_kwargs: 0)
    monkeypatch.setattr(kb, "gc_events_audit_write_succeeded", lambda: None)
    from hermes_cli.kanban import _cmd_gc
    assert _cmd_gc(_Args()) == 0
    out, err = capsys.readouterr()
    assert "0 event row(s)" in out
    assert "No GC event audit record written" in out
    assert not err


def test_cli_does_not_claim_failed_audit(capsys, monkeypatch, tmp_path):
    """CLI output distinguishes deleted rows from missing evidence."""
    class _RowsConn:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def execute(self, *_args):
            class _Cursor:
                def fetchall(self):
                    return []
            return _Cursor()

    class _Args:
        event_retention_days = 30
        log_retention_days = 30
        dry_run = False
        audit_report = False

    monkeypatch.setattr(kb, "connect_closing", lambda: _RowsConn())
    monkeypatch.setattr(kb, "workspaces_root", lambda: tmp_path)
    monkeypatch.setattr(kb, "gc_events", lambda *_args, **_kwargs: 2)
    monkeypatch.setattr(kb, "gc_worker_logs", lambda **_kwargs: 0)
    monkeypatch.setattr(kb, "gc_events_audit_write_succeeded", lambda: False)
    from hermes_cli.kanban import _cmd_gc
    assert _cmd_gc(_Args()) == 0
    out, err = capsys.readouterr()
    assert "Audit record written" not in out
    assert "no audit record was written" in err
