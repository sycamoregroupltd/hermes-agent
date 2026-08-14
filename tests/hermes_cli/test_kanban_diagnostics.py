"""Tests for hermes_cli.kanban_diagnostics — rule-engine that produces
structured distress signals (diagnostics) for kanban tasks.

These tests exercise each rule in isolation using minimal in-memory
task/event/run fixtures (no DB) plus a few integration-style cases
that round-trip through the real kanban_db to make sure the rule
engine works on sqlite3.Row objects as well as dataclasses.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_diagnostics as kd


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task(**overrides):
    base = {
        "id": "t_demo00",
        "title": "demo task",
        "assignee": "demo",
        "status": "ready",
        "consecutive_failures": 0,
        "last_failure_error": None,
    }
    base.update(overrides)
    return base


def _event(kind, ts=None, **payload):
    return {
        "kind": kind,
        "created_at": int(ts if ts is not None else time.time()),
        "payload": payload or None,
    }


def _run(outcome="completed", run_id=1, error=None):
    return {
        "id": run_id,
        "outcome": outcome,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Each rule — positive + negative + clearing
# ---------------------------------------------------------------------------














def test_repeated_failures_escalates_to_critical():
    task = _task(consecutive_failures=6, last_failure_error="boom")
    diags = kd.compute_task_diagnostics(task, [], [])
    assert diags[0].severity == "critical"


def test_repeated_failures_below_threshold_silent():
    task = _task(consecutive_failures=1)
    assert kd.compute_task_diagnostics(task, [], []) == []


def test_repeated_failures_default_matches_dispatcher_failure_limit():
    """Default dispatcher auto-blocks at 2 failures, so diagnostics must
    also surface at 2 instead of waiting for the stale threshold of 3.
    """
    task = _task(status="blocked", consecutive_failures=2,
                 last_failure_error="elapsed 600s > limit 300s")
    runs = [_run(outcome="timed_out", run_id=1)]
    diags = kd.compute_task_diagnostics(task, [], runs)
    repeated = [d for d in diags if d.kind == "repeated_failures"]
    assert len(repeated) == 1
    d = repeated[0]
    assert d.data["failure_threshold"] == 2
    assert d.data["failure_limit"] == 2
    assert "default 5" not in d.detail
    assert "configured for 2" in d.detail


def test_repeated_failures_derives_threshold_from_kanban_failure_limit():
    task = _task(status="ready", consecutive_failures=2,
                 last_failure_error="Profile 'debugger' does not exist")
    runs = [_run(outcome="spawn_failed", run_id=1)]
    assert kd.compute_task_diagnostics(
        task, [], runs, config={"failure_limit": 4}
    ) == []

    task = _task(status="blocked", consecutive_failures=4,
                 last_failure_error="Profile 'debugger' does not exist")
    diags = kd.compute_task_diagnostics(
        task, [], runs, config={"failure_limit": 4}
    )
    repeated = [d for d in diags if d.kind == "repeated_failures"]
    assert len(repeated) == 1
    assert repeated[0].data["failure_threshold"] == 4
    assert repeated[0].data["failure_limit"] == 4


def test_repeated_failures_explicit_threshold_overrides_failure_limit():
    task = _task(status="ready", consecutive_failures=3,
                 last_failure_error="Profile 'debugger' does not exist")
    runs = [_run(outcome="spawn_failed", run_id=1)]
    diags = kd.compute_task_diagnostics(
        task, [], runs, config={"failure_limit": 5, "failure_threshold": 3}
    )
    repeated = [d for d in diags if d.kind == "repeated_failures"]
    assert len(repeated) == 1
    assert repeated[0].data["failure_threshold"] == 3
    assert repeated[0].data["failure_limit"] == 5


def test_config_from_kanban_config_preserves_explicit_diagnostics_threshold():
    cfg = kd.config_from_kanban_config({
        "failure_limit": 5,
        "diagnostics": {"failure_threshold": 3},
    })
    assert cfg["failure_threshold"] == 3
    assert cfg["failure_limit"] == 5


def test_missing_exit_signal_fires_for_completed_pending_review():
    task = _task(
        status="completed_pending_review",
        last_failure_error="worker exited cleanly (rc=0) without calling kanban_complete",
    )
    events = [
        _event(
            "missing_exit_signal",
            ts=200,
            protocol_violations=3,
            protocol_violation_limit=3,
            error="worker exited cleanly (rc=0) without calling kanban_complete",
        ),
    ]

    diags = kd.compute_task_diagnostics(task, events, [], now=300)
    missing = [d for d in diags if d.kind == "missing_exit_signal"]
    assert len(missing) == 1
    d = missing[0]
    assert d.severity == "error"
    assert "kanban_complete or kanban_block" in d.detail
    assert "generic crash" in d.detail
    assert d.count == 3
    assert d.data["protocol_violations"] == 3
    assert d.data["protocol_violation_limit"] == 3
    assert any(a.kind == "comment" and a.suggested for a in d.actions)


def test_missing_exit_signal_ignores_unrelated_classifier_cases():
    for status, last_error in (
        ("blocked", "Iteration budget exhausted (90/90) — task could not complete"),
        ("ready", "task t_demo00 worktree path '/tmp/nope' is not inside a git repo"),
    ):
        task = _task(status=status, last_failure_error=last_error)
        events = [_event("blocked", ts=100, reason=last_error)]
        diags = kd.compute_task_diagnostics(task, events, [], now=300)
        assert [d for d in diags if d.kind == "missing_exit_signal"] == []


def test_repeated_crashes_counts_trailing_streak_only():
    task = _task(status="ready", assignee="crashy")
    runs = [
        _run(outcome="completed", run_id=1),
        _run(outcome="crashed", run_id=2, error="OOM"),
        _run(outcome="crashed", run_id=3, error="OOM again"),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "repeated_crashes"
    # 2 consecutive crashes at the end → default threshold 2 → error severity.
    assert d.severity == "error"
    assert d.data["consecutive_crashes"] == 2


def test_repeated_crashes_breaks_on_recent_success():
    task = _task(status="ready", assignee="fixed")
    runs = [
        _run(outcome="crashed", run_id=1),
        _run(outcome="crashed", run_id=2),
        _run(outcome="completed", run_id=3),
    ]
    assert kd.compute_task_diagnostics(task, [], runs) == []


def test_repeated_crashes_escalates_on_many_crashes():
    task = _task(status="ready", assignee="x")
    runs = [_run(outcome="crashed", run_id=i) for i in range(1, 6)]  # 5 in a row
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert diags[0].severity == "critical"


def test_failure_rules_exempt_terminal_statuses():
    # A manual done (dashboard drag) ends no run, so the trailing crash
    # streak survives in run history — but done means done: neither
    # failure rule may keep flagging a terminal card.
    runs = [_run(outcome="crashed", run_id=1), _run(outcome="crashed", run_id=2)]
    for status in ("done", "archived"):
        task = _task(status=status, assignee="crashy", consecutive_failures=3)
        assert kd.compute_task_diagnostics(task, [], runs) == []


def test_failure_rules_exempt_running_retry():
    # Retrying a task (→ running) puts a fresh attempt in flight; its
    # in-flight run (no outcome) doesn't break the trailing crash scan,
    # so the past streak used to keep flagging over an active retry.
    # A running card must clear the failure/crash banner until this
    # attempt itself resolves.
    runs = [_run(outcome="crashed", run_id=1), _run(outcome="crashed", run_id=2)]
    task = _task(status="running", assignee="crashy", consecutive_failures=3)
    assert kd.compute_task_diagnostics(task, [], runs) == []


def test_stuck_in_blocked_fires_past_threshold():
    now = int(time.time())
    task = _task(status="blocked")
    events = [
        _event("blocked", ts=now - 3600 * 48, reason="needs approval"),
    ]
    diags = kd.compute_task_diagnostics(
        task, events, [], now=now,
    )
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "stuck_in_blocked"
    assert d.severity == "warning"
    assert d.data["age_hours"] >= 48






def test_repeated_crashes_truncates_huge_tracebacks():
    """Full Python tracebacks can be tens of KB. The title stays one
    line (≤160 chars); the detail caps at 500 chars + ellipsis so the
    card doesn't explode visually."""
    huge = "Traceback (most recent call last):\n" + ("  File\n" * 500)
    task = _task(status="ready")
    runs = [
        _run(outcome="crashed", run_id=1, error=huge),
        _run(outcome="crashed", run_id=2, error=huge),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    d = diags[0]
    # Title only the first line, capped.
    assert "\n" not in d.title
    assert len(d.title) < 250
    # Detail contains the snippet with ellipsis.
    assert d.detail.endswith("…") or len(d.detail) < 700


# ---------------------------------------------------------------------------
# Severity sorting
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# Integration — runs through real kanban_db so sqlite.Row fields work
# ---------------------------------------------------------------------------


def test_engine_works_on_sqlite_row_objects(kanban_home):
    """Regression: the rule functions must handle sqlite3.Row (which
    supports mapping access but not attribute access and isn't a dict)
    as well as dataclass Task / plain dict. The API layer passes Row
    objects directly.
    """
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="p", assignee="w")
        real = kb.create_task(conn, title="r", assignee="x", created_by="w")
        with pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent,
                summary="with phantom", created_cards=[real, "t_deadbeef1"],
            )
        # Pull Row objects the way the API helper does.
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (parent,),
        ).fetchone()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        runs = list(conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        diags = kd.compute_task_diagnostics(row, events, runs)
        assert len(diags) == 1
        assert diags[0].kind == "hallucinated_cards"
        assert "t_deadbeef1" in diags[0].data["phantom_ids"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Error-tolerance: a broken rule shouldn't 500 the whole compute call
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# stranded_in_ready
#
# Surfaces ready tasks that nobody has claimed within the threshold.
# Identity-agnostic by design: catches typo'd assignees, deleted profiles,
# down external worker pools, and misconfigured dispatchers in one rule.
# ---------------------------------------------------------------------------


def test_stranded_in_ready_fires_when_age_exceeds_threshold():
    """Default threshold = 30 min. A ready task promoted 45 min ago
    with no claim should fire as a warning."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    # 45 min = 2700s, threshold = 1800s.
    events = [_event("created", ts=now - 45 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].severity == "warning"
    assert stranded[0].data["age_seconds"] == 45 * 60
    assert stranded[0].data["assignee"] == "demo"




# ---------------------------------------------------------------------------
# triage_aux_unavailable rule — auto-decompose aware
# ---------------------------------------------------------------------------


def _triage_task():
    return _task(id="t_triage1", status="triage")








def test_severity_at_or_above_uses_threshold_semantics():
    assert kd.severity_at_or_above("warning", "warning") is True
    assert kd.severity_at_or_above("error", "warning") is True
    assert kd.severity_at_or_above("critical", "warning") is True
    assert kd.severity_at_or_above("critical", "error") is True
    assert kd.severity_at_or_above("warning", "error") is False
    assert kd.severity_at_or_above("error", "critical") is False
    assert kd.severity_at_or_above("mystery", "warning") is False
    assert kd.severity_at_or_above("warning", None) is True


def test_review_lane_dependency_inversion_diagnostic_from_db_context(kanban_home):
    with kb.connect() as conn:
        source = kb.create_task(conn, title="implemented thing", assignee="worker")
        kb.block_task(conn, source, reason="review-required: needs os-reviewer")
        review = kb.create_task(
            conn,
            title="REVIEW: implemented thing",
            body=f"Review source {source} and post REVIEW_VERDICT=APPROVE.",
            assignee="os-reviewer",
            parents=[source],
        )
        task = kb.get_task(conn, review)
        config = {"review_lane_parent_warnings": kb.review_lane_dependency_warnings(conn, [review])}
        diags = kd.compute_task_diagnostics(
            task,
            kb.list_events(conn, review),
            kb.list_runs(conn, review),
            now=1234,
            config=config,
        )

    kinds = [d.kind for d in diags]
    assert "review_lane_dependency_inversion" in kinds
    diag = next(d for d in diags if d.kind == "review_lane_dependency_inversion")
    assert diag.data["source_task_id"] == source
    assert "independent reviewer lane" in diag.detail
