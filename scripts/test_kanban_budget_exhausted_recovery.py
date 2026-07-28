from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

HERMES_AGENT = Path("/home/frank/.hermes/hermes-agent")
SCRIPT = Path("/home/frank/.hermes/scripts/kanban_budget_exhausted_recovery.py")
sys.path.insert(0, str(HERMES_AGENT))

from hermes_cli import kanban_db as kb  # type: ignore[import-not-found]  # noqa: E402

spec = importlib.util.spec_from_file_location("budget_recovery", SCRIPT)
assert spec and spec.loader
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)

BUDGET_ERROR = (
    "Iteration budget exhausted (90/90) — task could not complete within the "
    "allowed iterations"
)


def _blocked_budget_task(conn, title: str, *, cf: int, embedded: bool = False) -> str:
    tid = kb.create_task(conn, title=title, assignee="integration-builder")
    conn.execute(
        "UPDATE tasks SET status='blocked', consecutive_failures=?, "
        "last_failure_error=?, block_kind='needs_input' WHERE id=?",
        (cf, BUDGET_ERROR, tid),
    )
    if embedded:
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'spawn_failed', ?, 100)",
            (tid, '{"error": "AuthenticationError: invalid api key"}'),
        )
    conn.commit()
    return tid


def _connect_tmp_db(tmp_path: Path):
    db = tmp_path / "kanban.db"
    conn = kb.connect(db)
    conn.isolation_level = None
    return db, conn


def test_classify_single_failure_auto_recovers(tmp_path):
    _db, conn = _connect_tmp_db(tmp_path)
    try:
        tid = _blocked_budget_task(conn, "clean first cap kill", cf=1)
        buckets = recovery.classify(conn)
    finally:
        conn.close()
    assert [r["id"] for r in buckets["auto_recover"]] == [tid]
    assert buckets["escalate"] == []


def test_classify_repeat_failure_escalates(tmp_path):
    _db, conn = _connect_tmp_db(tmp_path)
    try:
        tid = _blocked_budget_task(conn, "repeat cap kill", cf=2)
        buckets = recovery.classify(conn)
    finally:
        conn.close()
    assert buckets["auto_recover"] == []
    assert [r["id"] for r in buckets["escalate"]] == [tid]


def test_classify_embedded_provider_error_escalates(tmp_path):
    _db, conn = _connect_tmp_db(tmp_path)
    try:
        tid = _blocked_budget_task(conn, "embedded auth error", cf=1, embedded=True)
        buckets = recovery.classify(conn)
    finally:
        conn.close()
    assert buckets["auto_recover"] == []
    assert [r["id"] for r in buckets["escalate"]] == [tid]
    assert buckets["escalate"][0]["embedded_error"] is True


def test_classify_reports_out_of_scope_budget_rows_as_skipped(tmp_path):
    _db, conn = _connect_tmp_db(tmp_path)
    try:
        tid = kb.create_task(conn, title="historical cap kill", assignee="jarvis")
        conn.execute(
            "UPDATE tasks SET status='archived', consecutive_failures=1, "
            "last_failure_error=? WHERE id=?",
            (BUDGET_ERROR, tid),
        )
        conn.commit()
        buckets = recovery.classify(conn)
    finally:
        conn.close()
    assert buckets["auto_recover"] == []
    assert buckets["escalate"] == []
    assert [r["id"] for r in buckets["skipped"]] == [tid]
    assert buckets["skipped"][0]["reason"] == "status_out_of_scope:archived"


def test_classify_ignores_non_cap_kill_errors(tmp_path):
    _db, conn = _connect_tmp_db(tmp_path)
    try:
        tid = kb.create_task(conn, title="ordinary timeout", assignee="jarvis")
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=1, "
            "last_failure_error='elapsed 600s > limit 300s' WHERE id=?",
            (tid,),
        )
        conn.commit()
        buckets = recovery.classify(conn)
    finally:
        conn.close()
    assert buckets == {"auto_recover": [], "escalate": [], "skipped": []}


def test_apply_clears_already_queued_stale_budget_marker(tmp_path):
    db, conn = _connect_tmp_db(tmp_path)
    try:
        tid = kb.create_task(conn, title="queued stale cap kill", assignee="jarvis")
        conn.execute(
            "UPDATE tasks SET status='todo', consecutive_failures=1, "
            "last_failure_error=? WHERE id=?",
            (BUDGET_ERROR, tid),
        )
        conn.commit()
    finally:
        conn.close()

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--db",
            str(db),
            "--board",
            "jarvis-os",
            "--apply",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    conn = kb.connect(db)
    try:
        task = kb.get_task(conn, tid)
        assert task.status == "todo"
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None
    finally:
        conn.close()


def test_dry_run_then_apply_roundtrip_on_db_copy(tmp_path):
    db, conn = _connect_tmp_db(tmp_path)
    try:
        auto_id = _blocked_budget_task(conn, "apply auto", cf=1)
        esc_id = _blocked_budget_task(conn, "apply escalate", cf=3)
    finally:
        conn.close()

    dry = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db), "--board", "jarvis-os"],
        text=True,
        capture_output=True,
        check=True,
    )
    assert "DRY-RUN" in dry.stdout
    assert "SKIPPED (out of scope)" in dry.stdout
    conn = kb.connect(db)
    try:
        assert kb.get_task(conn, auto_id).status == "blocked"
        assert kb.get_task(conn, auto_id).last_failure_error == BUDGET_ERROR
    finally:
        conn.close()

    apply = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--db",
            str(db),
            "--board",
            "jarvis-os",
            "--apply",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    assert "APPLIED: auto-recovered 1 + escalated 1" in apply.stdout
    conn = kb.connect(db)
    try:
        auto_task = kb.get_task(conn, auto_id)
        esc_task = kb.get_task(conn, esc_id)
        assert auto_task.status == "ready"
        assert auto_task.consecutive_failures == 0
        assert auto_task.last_failure_error is None
        assert esc_task.status == "blocked"
        assert esc_task.assignee == "os-reviewer"
        assert esc_task.last_failure_error is None
        comments = [row["body"] for row in conn.execute(
            "SELECT body FROM task_comments WHERE task_id=? ORDER BY id", (esc_id,)
        )]
        assert any("ESCALATED to os-reviewer" in body for body in comments)
    finally:
        conn.close()


def test_dry_run_makes_no_board_writes(tmp_path):
    db, conn = _connect_tmp_db(tmp_path)
    try:
        auto_id = _blocked_budget_task(conn, "dry auto", cf=1)
        esc_id = _blocked_budget_task(conn, "dry escalate", cf=2)
        before_tasks = [
            dict(row)
            for row in conn.execute(
                "SELECT id, assignee, status, block_kind, consecutive_failures, "
                "last_failure_error FROM tasks ORDER BY id"
            )
        ]
        before_comments = [
            dict(row)
            for row in conn.execute(
                "SELECT task_id, author, body FROM task_comments ORDER BY id"
            )
        ]
        before_events = [
            dict(row)
            for row in conn.execute(
                "SELECT task_id, kind, payload FROM task_events ORDER BY id"
            )
        ]
    finally:
        conn.close()

    subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db), "--board", "jarvis-os"],
        text=True,
        capture_output=True,
        check=True,
    )

    conn = kb.connect(db)
    try:
        after_tasks = [
            dict(row)
            for row in conn.execute(
                "SELECT id, assignee, status, block_kind, consecutive_failures, "
                "last_failure_error FROM tasks ORDER BY id"
            )
        ]
        after_comments = [
            dict(row)
            for row in conn.execute(
                "SELECT task_id, author, body FROM task_comments ORDER BY id"
            )
        ]
        after_events = [
            dict(row)
            for row in conn.execute(
                "SELECT task_id, kind, payload FROM task_events ORDER BY id"
            )
        ]
        assert {auto_id, esc_id} == {row["id"] for row in after_tasks}
        assert after_tasks == before_tasks
        assert after_comments == before_comments
        assert after_events == before_events
    finally:
        conn.close()


def test_apply_uses_sanctioned_unblock_for_blocked_auto_recovery(tmp_path, monkeypatch):
    _db, conn = _connect_tmp_db(tmp_path)
    calls = []
    try:
        tid = _blocked_budget_task(conn, "sanctioned auto", cf=1)

        def fake_unblock_task(actual_conn, actual_tid):
            calls.append((actual_conn, actual_tid))
            actual_conn.execute(
                "UPDATE tasks SET status='ready', consecutive_failures=0, "
                "last_failure_error=NULL WHERE id=?",
                (actual_tid,),
            )
            return True

        monkeypatch.setattr(kb, "unblock_task", fake_unblock_task)
        recovery.auto_recover(
            conn,
            {
                "id": tid,
                "status": "blocked",
                "consecutive_failures": 1,
            },
        )
        task = kb.get_task(conn, tid)
        assert calls == [(conn, tid)]
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        assert task.last_failure_error is None
    finally:
        conn.close()


def test_apply_uses_sanctioned_block_and_assign_for_escalation(tmp_path, monkeypatch):
    _db, conn = _connect_tmp_db(tmp_path)
    calls = []
    try:
        tid = _blocked_budget_task(conn, "sanctioned escalation", cf=3)

        def fake_block_task(actual_conn, actual_tid, *, kind, reason):
            calls.append(("block", actual_conn, actual_tid, kind, reason))
            assert kind == "needs_input"
            assert "reviewer verdict" in reason
            return True

        def fake_assign_task(actual_conn, actual_tid, reviewer):
            calls.append(("assign", actual_conn, actual_tid, reviewer))
            actual_conn.execute(
                "UPDATE tasks SET assignee=? WHERE id=?",
                (reviewer, actual_tid),
            )
            return True

        monkeypatch.setattr(kb, "block_task", fake_block_task)
        monkeypatch.setattr(kb, "assign_task", fake_assign_task)
        recovery.escalate(
            conn,
            "jarvis-os",
            {
                "id": tid,
                "assignee": "integration-builder",
                "consecutive_failures": 3,
                "embedded_error": False,
            },
        )
        task = kb.get_task(conn, tid)
        assert calls[0][0:4] == ("block", conn, tid, "needs_input")
        assert calls[1] == ("assign", conn, tid, "os-reviewer")
        assert task.assignee == "os-reviewer"
        assert task.status == "blocked"
        assert task.last_failure_error is None
    finally:
        conn.close()
